"""Background optimization job worker.

Runs the v0.7 integrated CLI in a subprocess with per-job result isolation.
Shared propulsion curve cache remains under the data root.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from backend.app.services import job_store

# Map stdout markers → (stage, progress fraction)
_STAGE_PATTERNS: list[tuple[re.Pattern[str], str, float, Optional[str]]] = [
    (re.compile(r"REUSING VALIDATED PHYSICAL DESIGN POOL", re.I), "decision_ranking", 0.85, "Reusing exact-validated pool"),
    (re.compile(r"FAST EXPLORE \+ EXACT VALIDATE", re.I), "cached_explore", 0.35, "Cached Explore"),
    (re.compile(r"Guide finalists loaded", re.I), "guide_search", 0.25, "Propulsion guide search"),
    (re.compile(r"Exact validation\s+(\d+)\s*/\s*(\d+)", re.I), "exact_validation", None, None),
    (re.compile(r"Validating design\s+(\d+)\s+of\s+(\d+)", re.I), "exact_validation", None, None),
    (re.compile(r"Exact PyThrust finalist validations:\s*(\d+)", re.I), "decision_ranking", 0.90, "Decision ranking"),
    (re.compile(r"INTEGRATED RUN COMPLETE", re.I), "complete", 1.0, "Complete"),
]

_EXACT_LINE = re.compile(
    r"(?:Exact validation|Validating design)\s+(\d+)\s*(?:/|of)\s*(\d+)",
    re.I,
)


def _build_cli_flags(request: dict[str, Any]) -> list[str]:
    args = [
        "--motor-mode",
        str(request.get("motor_mode", "optimize")),
        "--prop-mode",
        str(request.get("prop_mode", "optimize")),
        "--battery-mode",
        str(request.get("battery_mode", "optimize")),
        "--esc-mode",
        str(request.get("esc_mode", "optimize")),
        "--max-auw",
        str(request.get("max_auw_kg", 10.0)),
        "--min-twr",
        str(request.get("min_twr", 1.8)),
        "--pack-overhead",
        str(request.get("pack_overhead_fraction", 0.10)),
        "--esc-margin",
        str(request.get("esc_current_margin", 1.15)),
        "--rpm-tolerance",
        str(request.get("rpm_data_tolerance", 0.10)),
        "--search-depth",
        str(request.get("search_depth", "standard")),
        "--budget-mode",
        str(request.get("budget_mode", "off")),
        "--frame-prop-clearance-mm",
        str(request.get("frame_prop_clearance_mm", 50.0)),
        "--frame-structural-margin",
        str(request.get("frame_structural_margin", 1.25)),
        "--weight-cost",
        str(request.get("weight_cost", 35.0)),
        "--weight-endurance",
        str(request.get("weight_endurance", 25.0)),
        "--weight-aircraft-mass",
        str(request.get("weight_aircraft_mass", 17.0)),
        "--weight-quality",
        str(request.get("weight_quality", 13.0)),
        "--weight-complexity",
        str(request.get("weight_complexity", 10.0)),
        "--fixed-nonpropulsion-cost",
        str(request.get("fixed_nonpropulsion_cost", 700.0)),
    ]
    if request.get("motor_id"):
        args.extend(["--motor-id", str(request["motor_id"])])
    if request.get("prop_id"):
        args.extend(["--prop-id", str(request["prop_id"])])
    if request.get("battery_id"):
        args.extend(["--battery-id", str(request["battery_id"])])
    if request.get("esc_id"):
        args.extend(["--esc-id", str(request["esc_id"])])
    if request.get("fixed_mass_kg") is not None:
        args.extend(["--fixed-mass", str(request["fixed_mass_kg"])])
    if request.get("force_physics"):
        args.append("--force-physics")
    if request.get("force_parametric"):
        args.append("--force-parametric")
    return args


def _parse_progress(line: str) -> Optional[tuple[str, float, str]]:
    m = _EXACT_LINE.search(line)
    if m:
        done, total = int(m.group(1)), max(int(m.group(2)), 1)
        frac = 0.40 + 0.50 * (done / total)
        return (
            "exact_validation",
            min(frac, 0.95),
            f"Validating design {done} of {total}",
        )
    for pat, stage, progress, message in _STAGE_PATTERNS:
        if pat.search(line):
            if progress is None:
                continue
            return stage, float(progress), message or stage
    return None


def run_optimization_job(
    job_id: str,
    *,
    runner: Optional[Callable[[str, dict[str, Any], Path], int]] = None,
) -> None:
    job = job_store.get_job(job_id)
    if job is None:
        return
    request = job["request"]
    jdir = job_store.job_dir(job_id)
    results_dir = jdir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    log_path = jdir / "worker.log"

    started = time.time()
    job_store.update_job(
        job_id,
        status="running",
        stage="loading_component_data",
        progress=0.02,
        message="Loading component data",
        started_at=started,
        error=None,
    )

    if runner is not None:
        try:
            code = runner(job_id, request, results_dir)
            if code != 0:
                job_store.update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    progress=1.0,
                    message="Worker failed",
                    finished_at=time.time(),
                    error=f"runner_exit_{code}",
                )
                return
            job_store.update_job(
                job_id,
                status="complete",
                stage="complete",
                progress=1.0,
                message="Complete",
                finished_at=time.time(),
            )
        except Exception as exc:  # noqa: BLE001
            job_store.update_job(
                job_id,
                status="failed",
                stage="failed",
                progress=1.0,
                message="Worker exception",
                finished_at=time.time(),
                error=str(exc),
            )
        return

    from optimizer.paths import DATA_ROOT

    env = os.environ.copy()
    env["DRONE_OPTIMIZER_DATA_ROOT"] = str(DATA_ROOT)
    env["DRONE_OPTIMIZER_JOB_RESULTS"] = str(results_dir)
    env["PYTHONUNBUFFERED"] = "1"
    repo_root = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

    args = _build_cli_flags(request)
    wrapper = jdir / "_run_job.py"
    wrapper.write_text(
        "import sys\n"
        "from optimizer.propulsion.integrated import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    cli = [sys.executable, str(wrapper), *args]

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cli,
            cwd=str(DATA_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        job_store.update_job(job_id, pid=proc.pid)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            parsed = _parse_progress(line)
            if parsed:
                stage, progress, message = parsed
                job_store.update_job(
                    job_id,
                    stage=stage,
                    progress=progress,
                    message=message,
                )
        code = proc.wait()

    if code != 0:
        job_store.update_job(
            job_id,
            status="failed",
            stage="failed",
            progress=1.0,
            message="Optimization process failed",
            finished_at=time.time(),
            error=f"exit_code_{code}",
        )
        return

    job_store.update_job(
        job_id,
        status="complete",
        stage="complete",
        progress=1.0,
        message="Complete",
        finished_at=time.time(),
    )


def start_job_thread(job_id: str) -> None:
    thread = threading.Thread(
        target=run_optimization_job,
        args=(job_id,),
        name=f"opt-job-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
