"""
Database factory for the Trend Agent Scheduler.

Reads ``app/db_config.json`` to decide which backend to use
(sqlite or mongo) and re-exports every function from that backend
so that existing code like::

    from app.scheduler_db import create_job, get_all_jobs, …

continues to work with zero changes.

Switch backends by editing db_config.json::

    { "db_backend": "sqlite" }   ← default
    { "db_backend": "mongo" }    ← uses MONGO_URI from .env
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# ── Load config ──────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).resolve().parent / "db_config.json"


def _load_db_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    # Default to sqlite if config file is missing
    return {"db_backend": "sqlite"}


_db_config = _load_db_config()
_BACKEND_NAME = _db_config.get("db_backend", "sqlite").lower()


# ── Instantiate backend ──────────────────────────────────────────────────

def _create_backend():
    """Create and return the appropriate backend instance."""
    if _BACKEND_NAME == "mongo":
        from app.scheduler_db_mongo import MongoBackend
        mongo_cfg = _db_config.get("mongo", {})
        return MongoBackend(
            db_name=mongo_cfg.get("db_name", "trend_scheduler"),
            jobs_collection=mongo_cfg.get("jobs_collection", "scheduler_jobs"),
            history_collection=mongo_cfg.get("history_collection", "execution_history"),
        )
    else:
        from app.scheduler_db_sqlite import SQLiteBackend
        sqlite_cfg = _db_config.get("sqlite", {})
        db_path = sqlite_cfg.get("db_path")
        return SQLiteBackend(db_path=db_path)


_backend = _create_backend()


# ── Re-export all functions ──────────────────────────────────────────────
# Every consumer does:  from app.scheduler_db import create_job, ...
# This keeps that working regardless of backend.

def init_db() -> None:
    """Create tables/collections if they do not exist."""
    _backend.init_db()


def create_job(data: dict) -> dict:
    """Insert a new job and return the full row/document."""
    return _backend.create_job(data)


def get_all_jobs() -> list[dict]:
    """Return all jobs, newest first."""
    return _backend.get_all_jobs()


def get_job(job_id: int) -> Optional[dict]:
    """Return one job by ID."""
    return _backend.get_job(job_id)


def update_job(job_id: int, data: dict) -> Optional[dict]:
    """Update selected fields of a job."""
    return _backend.update_job(job_id, data)


def soft_delete_job(job_id: int) -> bool:
    """Soft-delete a job by setting status to cancelled."""
    return _backend.soft_delete_job(job_id)


def hard_delete_job(job_id: int) -> bool:
    """Permanently remove a job and its execution history."""
    return _backend.hard_delete_job(job_id)


def get_pending_due_jobs() -> list[dict]:
    """Return pending jobs whose next_run_at has passed."""
    return _backend.get_pending_due_jobs()


def create_history_entry(data: dict) -> dict:
    """Insert a new execution_history row/document."""
    return _backend.create_history_entry(data)


def update_history_entry(history_id: int, data: dict) -> Optional[dict]:
    """Update selected fields of an execution_history row/document."""
    return _backend.update_history_entry(history_id, data)


def get_history(job_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Return execution history rows, optionally filtered by job_id."""
    return _backend.get_history(job_id=job_id, limit=limit)


# ── Convenience ──────────────────────────────────────────────────────────

def get_backend_name() -> str:
    """Return the active backend name ('sqlite' or 'mongo')."""
    return _BACKEND_NAME
