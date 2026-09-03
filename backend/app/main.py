"""FastAPI application for Drone Optimizer."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.catalog import router as catalog_router
from backend.app.api.evaluate import router as evaluate_router
from backend.app.api.jobs import router as jobs_router

app = FastAPI(
    title="Drone Optimizer API",
    version="0.7.0",
    description=(
        "Lock what you know. Optimize what you don't. "
        "Fixed-configuration evaluation and mixed/full optimization jobs."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:43128",
        "http://localhost:43128",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(evaluate_router)
app.include_router(jobs_router)
app.include_router(catalog_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.7.0"}
