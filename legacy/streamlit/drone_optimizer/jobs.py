from __future__ import annotations
from pathlib import Path
import os, subprocess, sys, time

def run_command(args, cwd: Path, timeout=None):
    started = time.time()
    env = os.environ.copy()
    # Ensure extracted optimizer package + private data root resolve correctly
    # when Streamlit shells out with cwd=legacy/streamlit.
    repo_root = Path(__file__).resolve().parents[3]
    env["DRONE_OPTIMIZER_DATA_ROOT"] = str(Path(cwd).resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(repo_root) + (os.pathsep + existing if existing else "")
    )
    proc = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env=env
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_s": round(time.time() - started, 1),
        "command": " ".join(str(x) for x in args),
    }

def refresh_database(project_root: Path, builder_path: Path, refresh: bool = False):
    args = [sys.executable, str(builder_path)]
    if refresh:
        args.append("--refresh")
    return run_command(args, cwd=project_root)

def run_detailed_optimizer(
    project_root: Path,
    optimizer_path: Path,
    *,
    force_parametric: bool = False,
    force_physics: bool = False,
    motor_mode: str = "optimize",
    motor_id: str | None = None,
    prop_mode: str = "optimize",
    prop_id: str | None = None,
    battery_mode: str = "optimize",
    battery_id: str | None = None,
    esc_mode: str = "optimize",
    esc_id: str | None = None,
    fixed_mass: float | None = None,
    max_auw: float = 10.0,
    min_twr: float = 1.8,
    pack_overhead: float = 0.10,
    esc_margin: float = 1.15,
    rpm_tolerance: float = 0.10,
    search_depth: str = "thorough",
    budget_mode: str = "off",
    max_budget: float | None = None,
    fixed_nonpropulsion_cost: float = 0.0,
    frame_cost_per_kg: float = 0.0,
    frame_center_mass: float = 0.160,
    frame_arm_density: float = 0.110,
    frame_load_coeff: float = 0.020,
    frame_battery_mount_fraction: float = 0.040,
    frame_prop_clearance_mm: float = 50.0,
    frame_structural_margin: float = 1.25,
    weight_cost: float = 35.0,
    weight_endurance: float = 25.0,
    weight_aircraft_mass: float = 17.0,
    weight_quality: float = 13.0,
    weight_complexity: float = 10.0,
):
    args = [
        sys.executable, str(optimizer_path),
        "--motor-mode", str(motor_mode),
        "--prop-mode", str(prop_mode),
        "--battery-mode", str(battery_mode),
        "--esc-mode", str(esc_mode),
        "--max-auw", str(max_auw),
        "--min-twr", str(min_twr),
        "--pack-overhead", str(pack_overhead),
        "--esc-margin", str(esc_margin),
        "--rpm-tolerance", str(rpm_tolerance),
        "--search-depth", str(search_depth),
        "--budget-mode", str(budget_mode),
        "--fixed-nonpropulsion-cost", str(fixed_nonpropulsion_cost),
        "--frame-cost-per-kg", str(frame_cost_per_kg),
        "--frame-prop-clearance-mm", str(frame_prop_clearance_mm),
        "--frame-structural-margin", str(frame_structural_margin),
        "--weight-cost", str(weight_cost),
        "--weight-endurance", str(weight_endurance),
        "--weight-aircraft-mass", str(weight_aircraft_mass),
        "--weight-quality", str(weight_quality),
        "--weight-complexity", str(weight_complexity),
    ]
    if motor_id:
        args.extend(["--motor-id", str(motor_id)])
    if prop_id:
        args.extend(["--prop-id", str(prop_id)])
    if battery_id:
        args.extend(["--battery-id", str(battery_id)])
    if esc_id:
        args.extend(["--esc-id", str(esc_id)])
    if max_budget is not None:
        args.extend(["--max-budget", str(max_budget)])
    if force_parametric:
        args.append("--force-parametric")
    if force_physics:
        args.append("--force-physics")
    if fixed_mass is not None:
        args.extend(["--fixed-mass", str(fixed_mass)])
    return run_command(args, cwd=project_root)
