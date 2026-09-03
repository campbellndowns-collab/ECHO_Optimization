"""Optimization job + rerank API routes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException

from backend.app.jobs.worker import start_job_thread
from backend.app.models.jobs import (
    OptimizationJobCreated,
    OptimizationJobRequest,
    OptimizationJobResults,
    OptimizationJobStatus,
    RerankRequest,
    RerankResponse,
)
from backend.app.services import job_store
from optimizer.engine import rerank_designs

router = APIRouter(tags=["optimization-jobs"])


def _elapsed(job: dict[str, Any]) -> Optional[float]:
    started = job.get("started_at")
    if not started:
        return None
    end = job.get("finished_at") or time.time()
    return round(float(end) - float(started), 1)


def _eta(job: dict[str, Any]) -> Optional[float]:
    if job.get("status") != "running":
        return None
    progress = float(job.get("progress") or 0.0)
    elapsed = _elapsed(job)
    if not elapsed or progress <= 0.05:
        return None
    remaining = elapsed * (1.0 - progress) / progress
    return round(max(remaining, 0.0), 1)


@router.post("/optimization-jobs", response_model=OptimizationJobCreated)
def create_optimization_job(body: OptimizationJobRequest):
    job_id = job_store.create_job(body.model_dump())
    start_job_thread(job_id)
    return OptimizationJobCreated(job_id=job_id)


@router.get("/optimization-jobs/{job_id}", response_model=OptimizationJobStatus)
def get_optimization_job(job_id: str):
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return OptimizationJobStatus(
        job_id=job_id,
        status=job["status"],
        stage=job.get("stage"),
        progress=float(job.get("progress") or 0.0),
        message=job.get("message"),
        elapsed_seconds=_elapsed(job),
        estimated_remaining_seconds=_eta(job),
        error=job.get("error"),
    )


@router.get(
    "/optimization-jobs/{job_id}/results",
    response_model=OptimizationJobResults,
)
def get_optimization_job_results(job_id: str):
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    if job["status"] not in {"complete", "failed"}:
        raise HTTPException(status_code=409, detail="job_not_finished")

    results_dir = job_store.job_dir(job_id) / "results"
    top_path = results_dir / "decision_matrix_top25.csv"
    pool_path = results_dir / "physical_design_pool.csv"
    settings_path = results_dir / "run_settings.json"

    top: list[dict[str, Any]] = []
    pool: list[dict[str, Any]] = []
    settings = None
    if top_path.exists():
        top = pd.read_csv(top_path).to_dict("records")
    if pool_path.exists():
        pool = pd.read_csv(pool_path).to_dict("records")
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

    timing = (settings or {}).get("timing_s")
    cache_stats = (settings or {}).get("propulsion_cache")

    return OptimizationJobResults(
        job_id=job_id,
        status=job["status"],
        top_designs=top,
        pool=pool,
        run_settings=settings,
        timing=timing,
        cache_statistics=cache_stats,
    )


@router.post("/rank", response_model=RerankResponse)
def post_rank(body: RerankRequest):
    """Instant weight-only rerank of an exact-validated design pool."""
    ranked = rerank_designs(
        body.designs,
        weights={
            "cost": body.weight_cost,
            "endurance": body.weight_endurance,
            "weight": body.weight_aircraft_mass,
            "quality": body.weight_quality,
            "complexity": body.weight_complexity,
        },
    )
    return RerankResponse(designs=ranked)
