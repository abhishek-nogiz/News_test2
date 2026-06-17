"""
SQLite database backend for the Trend Agent Scheduler.

Extracted from the original scheduler_db.py — same logic, now wrapped
in the SchedulerDBBase interface so it can be swapped via the factory.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.scheduler_db_base import SchedulerDBBase
from app.scheduler_config import (
    DEFAULT_INTERVAL_HOURS,
    JOB_TYPE_INTERVAL,
    JOB_TYPE_ALARM,
    JOB_STATUS_PENDING,
)


class SQLiteBackend(SchedulerDBBase):
    """SQLite implementation of the scheduler database."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent
            db_path = project_root / "app" / "scheduler.db"
        self.db_path = Path(db_path)

    # ── Connection ────────────────────────────────────────────────────

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Schema ────────────────────────────────────────────────────────

    def init_db(self) -> None:
        conn = self._get_connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                job_type        TEXT    NOT NULL CHECK(job_type IN ('alarm','interval')),
                run_at          TEXT,
                interval_hours  INTEGER DEFAULT 4,
                country         TEXT    NOT NULL DEFAULT 'US',
                category_name   TEXT    NOT NULL DEFAULT 'Sports',
                topic_category  TEXT    NOT NULL DEFAULT 'sports',
                category_id     TEXT    DEFAULT '',
                wordpress_status TEXT   NOT NULL DEFAULT 'draft',
                status          TEXT    NOT NULL DEFAULT 'pending'
                                  CHECK(status IN ('pending','running','paused','completed','failed','cancelled')),
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL,
                last_run_at     TEXT,
                next_run_at     TEXT,
                error_message   TEXT
            );

            CREATE TABLE IF NOT EXISTS execution_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL,
                started_at      TEXT    NOT NULL,
                finished_at     TEXT,
                status          TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('running','success','failed')),
                trigger_type    TEXT    NOT NULL DEFAULT 'scheduler'
                                  CHECK(trigger_type IN ('scheduler','manual')),
                run_id          TEXT,
                html_path       TEXT,
                publish_status  TEXT    DEFAULT 'pending'
                                  CHECK(publish_status IN ('pending','success','failed','skipped')),
                error_message   TEXT,
                FOREIGN KEY (job_id) REFERENCES scheduler_jobs(id)
            );
        """)
        conn.commit()
        conn.close()

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

        conn = self._get_connection()
        cur = conn.execute("""
            INSERT INTO scheduler_jobs
                (name, job_type, run_at, interval_hours, country,
                 category_name, topic_category, category_id, wordpress_status,
                 status, created_at, updated_at, next_run_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("name", "Untitled Job"),
            job_type,
            data.get("run_at"),
            data.get("interval_hours", DEFAULT_INTERVAL_HOURS),
            data.get("country", "US"),
            data.get("category_name", "Sports"),
            data.get("topic_category", "sports"),
            data.get("category_id", ""),
            data.get("wordpress_status", "draft"),
            data.get("status", JOB_STATUS_PENDING),
            now,
            now,
            next_run_at,
        ))
        job_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM scheduler_jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else {}

    def get_all_jobs(self) -> list[dict]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM scheduler_jobs ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_job(self, job_id: int) -> Optional[dict]:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM scheduler_jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

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

        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [job_id]

        conn = self._get_connection()
        conn.execute(f"UPDATE scheduler_jobs SET {set_clause} WHERE id=?", values)
        conn.commit()
        row = conn.execute("SELECT * FROM scheduler_jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def soft_delete_job(self, job_id: int) -> bool:
        now = datetime.now().isoformat()
        conn = self._get_connection()
        conn.execute(
            "UPDATE scheduler_jobs SET status='cancelled', updated_at=? WHERE id=?",
            (now, job_id),
        )
        conn.commit()
        conn.close()
        return True

    def hard_delete_job(self, job_id: int) -> bool:
        conn = self._get_connection()
        conn.execute("DELETE FROM execution_history WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM scheduler_jobs WHERE id=?", (job_id,))
        conn.commit()
        conn.close()
        return True

    def get_pending_due_jobs(self) -> list[dict]:
        now = datetime.now().isoformat()
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM scheduler_jobs WHERE status='pending' AND next_run_at<=?",
            (now,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Execution History ─────────────────────────────────────────────

    def create_history_entry(self, data: dict) -> dict:
        conn = self._get_connection()
        cur = conn.execute("""
            INSERT INTO execution_history
                (job_id, started_at, status, trigger_type)
            VALUES (?,?,?,?)
        """, (
            data["job_id"],
            data["started_at"],
            data.get("status", "running"),
            data.get("trigger_type", "scheduler"),
        ))
        hid = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM execution_history WHERE id=?", (hid,)).fetchone()
        conn.close()
        return dict(row) if row else {}

    def update_history_entry(self, history_id: int, data: dict) -> Optional[dict]:
        allowed = {
            "finished_at", "status", "run_id", "html_path",
            "publish_status", "error_message",
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return None

        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [history_id]

        conn = self._get_connection()
        conn.execute(f"UPDATE execution_history SET {set_clause} WHERE id=?", values)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM execution_history WHERE id=?", (history_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_history(self, job_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        conn = self._get_connection()
        if job_id:
            rows = conn.execute(
                "SELECT * FROM execution_history WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM execution_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
