"""
MongoDB database backend for the Trend Agent Scheduler.

Uses ``MongoEngine`` from ``app.mongo_engine`` for all connection
management, auto-creation, and CRUD operations.  This file only
contains the *scheduler-specific* logic (field defaults, recalc,
status transitions) and delegates storage to the engine.

Mirrors the exact same interface as SQLiteBackend so the factory
can swap transparently.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.mongo_engine import MongoEngine
from app.scheduler_db_base import SchedulerDBBase
from app.scheduler_config import (
    DEFAULT_INTERVAL_HOURS,
    JOB_TYPE_INTERVAL,
    JOB_TYPE_ALARM,
    JOB_STATUS_PENDING,
)


class MongoBackend(SchedulerDBBase):
    """MongoDB implementation of the scheduler database."""

    def __init__(
        self,
        mongo_uri: str | None = None,
        db_name: str = "trend_scheduler",
        jobs_collection: str = "scheduler_jobs",
        history_collection: str = "execution_history",
    ) -> None:
        self._engine = MongoEngine.instance(mongo_uri=mongo_uri, db_name=db_name)
        self._jobs_col = jobs_collection
        self._history_col = history_collection

    # ── Schema / Init ─────────────────────────────────────────────────

    def init_db(self) -> None:
        """Create collections and indexes if they do not exist."""
        self._engine.ensure_collections()

    # ── Jobs CRUD ─────────────────────────────────────────────────────

    def create_job(self, data: dict) -> dict:
        now = datetime.now().isoformat()
        job_type = data.get("job_type", JOB_TYPE_INTERVAL)

        next_run_at: Optional[str] = None
        if job_type == JOB_TYPE_INTERVAL:
            hours = int(data.get("interval_hours", DEFAULT_INTERVAL_HOURS))
            next_run_at = (datetime.now() + timedelta(hours=hours)).isoformat()
        elif job_type == JOB_TYPE_ALARM:
            run_at = data.get("run_at")
            if run_at:
                next_run_at = run_at

        doc = {
            "id": self._engine.next_id(self._jobs_col),
            "name": data.get("name", "Untitled Job"),
            "job_type": job_type,
            "run_at": data.get("run_at"),
            "interval_hours": data.get("interval_hours", DEFAULT_INTERVAL_HOURS),
            "country": data.get("country", "US"),
            "category_name": data.get("category_name", "Sports"),
            "topic_category": data.get("topic_category", "sports"),
            "category_id": data.get("category_id", ""),
            "wordpress_status": data.get("wordpress_status", "draft"),
            "status": data.get("status", JOB_STATUS_PENDING),
            "created_at": now,
            "updated_at": now,
            "last_run_at": None,
            "next_run_at": next_run_at,
            "error_message": None,
        }

        return self._engine.insert(self._jobs_col, doc)

    def get_all_jobs(self) -> list[dict]:
        return self._engine.find_many(
            self._jobs_col,
            sort=[("created_at", -1)],
        )

    def get_job(self, job_id: int) -> Optional[dict]:
        return self._engine.find_one(self._jobs_col, {"id": job_id})

    def update_job(self, job_id: int, data: dict) -> Optional[dict]:
        now = datetime.now().isoformat()

        allowed = {
            "name", "job_type", "run_at", "interval_hours", "country",
            "category_name", "topic_category", "category_id", "wordpress_status",
            "status", "last_run_at", "next_run_at", "error_message",
        }

        updates: dict = {"updated_at": now}
        for key in allowed:
            if key in data:
                updates[key] = data[key]

        # Recalculate next_run_at when schedule-relevant fields change
        need_recalc = {"job_type", "run_at", "interval_hours", "status"}
        if need_recalc & set(data.keys()):
            job = self.get_job(job_id)
            if job:
                jt = data.get("job_type", job["job_type"])
                st = data.get("status", job["status"])

                if st == JOB_STATUS_PENDING:
                    if jt == JOB_TYPE_INTERVAL:
                        hrs = int(data.get("interval_hours", job.get("interval_hours", 4)))
                        updates["next_run_at"] = (
                            datetime.now() + timedelta(hours=hrs)
                        ).isoformat()
                    elif jt == JOB_TYPE_ALARM:
                        run_at = data.get("run_at", job.get("run_at"))
                        if run_at:
                            updates["next_run_at"] = run_at

        return self._engine.update_one(self._jobs_col, {"id": job_id}, updates)

    def soft_delete_job(self, job_id: int) -> bool:
        now = datetime.now().isoformat()
        self._engine.update_one(
            self._jobs_col,
            {"id": job_id},
            {"status": "cancelled", "updated_at": now},
        )
        return True

    def hard_delete_job(self, job_id: int) -> bool:
        self._engine.delete_many(self._history_col, {"job_id": job_id})
        self._engine.delete_one(self._jobs_col, {"id": job_id})
        return True

    def get_pending_due_jobs(self) -> list[dict]:
        now = datetime.now().isoformat()
        return self._engine.find_many(
            self._jobs_col,
            {
                "status": JOB_STATUS_PENDING,
                "next_run_at": {"$lte": now},
            },
        )

    # ── Execution History ─────────────────────────────────────────────

    def create_history_entry(self, data: dict) -> dict:
        doc = {
            "id": self._engine.next_id(self._history_col),
            "job_id": data["job_id"],
            "started_at": data["started_at"],
            "finished_at": None,
            "status": data.get("status", "running"),
            "trigger_type": data.get("trigger_type", "scheduler"),
            "run_id": None,
            "html_path": None,
            "publish_status": "pending",
            "error_message": None,
        }

        return self._engine.insert(self._history_col, doc)

    def update_history_entry(self, history_id: int, data: dict) -> Optional[dict]:
        allowed = {
            "finished_at", "status", "run_id", "html_path",
            "publish_status", "error_message",
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return None

        return self._engine.update_one(
            self._history_col, {"id": history_id}, updates
        )

    def get_history(self, job_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        query: dict = {}
        if job_id:
            query["job_id"] = job_id

        return self._engine.find_many(
            self._history_col,
            query,
            sort=[("started_at", -1)],
            limit=limit,
        )