"""
Abstract base class for the scheduler database backends.

Every backend (SQLite, MongoDB, etc.) must implement this interface.
The factory in scheduler_db.py will instantiate the correct one based
on db_config.json and re-export all methods so callers never change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class SchedulerDBBase(ABC):
    """Contract for scheduler database operations."""

    # ── Schema / Init ─────────────────────────────────────────────────

    @abstractmethod
    def init_db(self) -> None:
        """Create tables/collections if they do not exist."""
        ...

    # ── Jobs CRUD ─────────────────────────────────────────────────────

    @abstractmethod
    def create_job(self, data: dict) -> dict:
        """Insert a new job and return the full row/document."""
        ...

    @abstractmethod
    def get_all_jobs(self) -> list[dict]:
        """Return all jobs, newest first."""
        ...

    @abstractmethod
    def get_job(self, job_id: int) -> Optional[dict]:
        """Return one job by ID."""
        ...

    @abstractmethod
    def update_job(self, job_id: int, data: dict) -> Optional[dict]:
        """Update selected fields of a job.  Recalculates next_run_at
        when schedule-relevant fields change."""
        ...

    @abstractmethod
    def soft_delete_job(self, job_id: int) -> bool:
        """Soft-delete a job by setting status to cancelled."""
        ...

    @abstractmethod
    def hard_delete_job(self, job_id: int) -> bool:
        """Permanently remove a job and its execution history."""
        ...

    @abstractmethod
    def get_pending_due_jobs(self) -> list[dict]:
        """Return pending jobs whose next_run_at has passed."""
        ...

    # ── Execution History ─────────────────────────────────────────────

    @abstractmethod
    def create_history_entry(self, data: dict) -> dict:
        """Insert a new execution_history row/document."""
        ...

    @abstractmethod
    def update_history_entry(self, history_id: int, data: dict) -> Optional[dict]:
        """Update selected fields of an execution_history row/document."""
        ...

    @abstractmethod
    def get_history(self, job_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        """Return execution history rows, optionally filtered by job_id."""
        ...
