"""Optimization job API lifecycle tests (uses a fast stub worker)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
POOL = (
    REPO
    / "legacy"
    / "streamlit"
    / "optimizer_results"
    / "drone_optimizer_integrated"
    / "physical_design_pool.csv"
)
TOP = (
    REPO
    / "legacy"
    / "streamlit"
    / "optimizer_results"
    / "drone_optimizer_integrated"
    / "decision_matrix_top25.csv"
)
SETTINGS = (
    REPO
    / "legacy"
    / "streamlit"
    / "optimizer_results"
    / "drone_optimizer_integrated"
    / "run_settings.json"
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ["DRONE_OPTIMIZER_DATA_ROOT"] = str(
        REPO / "legacy" / "streamlit"
    )

    def fake_start(job_id: str):
        from backend.app.jobs.worker import run_optimization_job
        from backend.app.services import job_store
        import threading

        def runner(_job_id, _request, results_dir: Path) -> int:
            results_dir.mkdir(parents=True, exist_ok=True)
            # Copy reference artifacts into the job results dir.
            if POOL.exists():
                pd.read_csv(POOL).head(10).to_csv(
                    results_dir / "physical_design_pool.csv", index=False
                )
            if TOP.exists():
                pd.read_csv(TOP).head(5).to_csv(
                    results_dir / "decision_matrix_top25.csv", index=False
                )
            settings = {
                "timing_s": {"guide": 1.0, "cached_explore": 0.2, "exact_validation": 0.5},
                "propulsion_cache": {"hits": 10, "misses": 0, "new_curves_built": 0},
            }
            if SETTINGS.exists():
                settings = json.loads(SETTINGS.read_text())
            (results_dir / "run_settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )
            job_store.update_job(
                _job_id,
                stage="exact_validation",
                progress=0.7,
                message="Validating design 3 of 5",
            )
            time.sleep(0.05)
            return 0

        threading.Thread(
            target=run_optimization_job,
            kwargs={"job_id": job_id, "runner": runner},
            daemon=True,
        ).start()

    monkeypatch.setattr(
        "backend.app.api.jobs.start_job_thread", fake_start
    )

    from backend.app.main import app

    return TestClient(app)


def test_optimization_job_lifecycle(client):
    r = client.post(
        "/optimization-jobs",
        json={
            "motor_mode": "lock",
            "motor_id": "NeuMotors_4610MC-207",
            "search_depth": "standard",
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert job_id

    # Poll until complete
    status = None
    for _ in range(50):
        s = client.get(f"/optimization-jobs/{job_id}")
        assert s.status_code == 200
        status = s.json()
        if status["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    assert status is not None
    assert status["status"] == "complete"
    assert status["progress"] == 1.0

    results = client.get(f"/optimization-jobs/{job_id}/results")
    assert results.status_code == 200, results.text
    body = results.json()
    assert body["status"] == "complete"
    assert len(body["top_designs"]) >= 1
    assert len(body["pool"]) >= 1
    assert body["cache_statistics"] is not None


def test_rank_endpoint_instant(client):
    if not POOL.exists():
        pytest.skip("pool missing")
    df = pd.read_csv(POOL).head(8)
    rows = json.loads(df.to_json(orient="records"))
    r = client.post(
        "/rank",
        json={
            "designs": rows,
            "weight_cost": 10,
            "weight_endurance": 50,
            "weight_aircraft_mass": 10,
            "weight_quality": 20,
            "weight_complexity": 10,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["designs"]) == len(rows)
    assert "decision_matrix_score" in body["designs"][0]
