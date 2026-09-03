"""SQLite-backed optimization job store (multi-user safe by job_id)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from optimizer.paths import get_data_root

_LOCK = threading.Lock()


def jobs_root() -> Path:
    root = get_data_root() / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _db_path() -> Path:
    return jobs_root() / "jobs.sqlite"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _LOCK:
        con = _connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT,
                    progress REAL,
                    message TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    request_json TEXT NOT NULL,
                    error TEXT,
                    pid INTEGER
                )
                """
            )
            con.commit()
        finally:
            con.close()


def create_job(request: dict[str, Any]) -> str:
    init_db()
    job_id = uuid.uuid4().hex
    now = time.time()
    job_dir = jobs_root() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "results").mkdir(exist_ok=True)
    with _LOCK:
        con = _connect()
        try:
            con.execute(
                """
                INSERT INTO jobs (
                    job_id, status, stage, progress, message,
                    created_at, updated_at, request_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "queued",
                    "queued",
                    0.0,
                    "Queued",
                    now,
                    now,
                    json.dumps(request),
                ),
            )
            con.commit()
        finally:
            con.close()
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _LOCK:
        con = _connect()
        try:
            con.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", values)
            con.commit()
        finally:
            con.close()


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    init_db()
    with _LOCK:
        con = _connect()
        try:
            row = con.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        finally:
            con.close()
    if row is None:
        return None
    data = dict(row)
    data["request"] = json.loads(data.pop("request_json"))
    return data


def job_dir(job_id: str) -> Path:
    return jobs_root() / job_id
