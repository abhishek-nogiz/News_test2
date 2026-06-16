"""
SQLite database layer for the Trend Agent Scheduler.

Two tables:
  - scheduler_jobs: stores job definitions and current status
  - execution_history: logs every execution with timing and results

Supports three job types:
  - alarm:   Run once at a specific time
  - interval: Run every N hours
  - index:    Crawl sitemap → embed → store (background indexing)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.scheduler_config import (
    DEFAULT_INTERVAL_HOURS,
    JOB_TYPE_INTERVAL,
    JOB_TYPE_ALARM,
    JOB_TYPE_INDEX,
    JOB_STATUS_PENDING,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "app" / "scheduler.db"


# ── Connection ────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Return a new database connection with Row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 2  # v2 adds 'index' job type


def init_db() -> None:
    """Create tables if they do not exist, and apply migrations."""
    conn = get_connection()

    # ── Ensure schema version tracking table exists ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    current_version = 0
    row = conn.execute(
        "SELECT value FROM _schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row:
        try:
            current_version = int(row[0])
        except (ValueError, TypeError):
            current_version = 0

    # ── Fresh install: create everything from scratch ──
    if current_version < 1:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                job_type        TEXT    NOT NULL CHECK(job_type IN ('alarm','interval','index')),
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
        current_version = 2  # Fresh install gets latest schema

    # ── Migration v1 → v2: add 'index' to job_type CHECK ──
    elif current_version < 2:
        _migrate_v1_to_v2(conn)
        current_version = 2

    # Save current version
    conn.execute(
        "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES ('schema_version', ?)",
        ("schema_version", str(current_version)),
    )

    conn.commit()
    conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Migrate from v1 (alarm + interval only) to v2 (adds 'index' job type).

    SQLite doesn't support ALTER CHECK constraints, so we:
      1. Create new table with updated CHECK
      2. Copy data over
      3. Drop old table
      4. Rename new table
    """
    conn.executescript("""
        -- Step 1: Create new table with updated CHECK
        CREATE TABLE IF NOT EXISTS scheduler_jobs_v2 (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            job_type        TEXT    NOT NULL CHECK(job_type IN ('alarm','interval','index')),
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

        -- Step 2: Copy data
        INSERT INTO scheduler_jobs_v2
            SELECT * FROM scheduler_jobs;

        -- Step 3: Drop old table
        DROP TABLE scheduler_jobs;

        -- Step 4: Rename
        ALTER TABLE scheduler_jobs_v2 RENAME TO scheduler_jobs;
    """)


# ── Jobs CRUD ─────────────────────────────────────────────────────────────

def create_job(data: dict) -> dict:
    """Insert a new job and return the full row."""
    now = datetime.now().isoformat()
    job_type = data.get("job_type", JOB_TYPE_INTERVAL)

    # Calculate first next_run_at
    next_run_at: Optional[str] = None
    if job_type == JOB_TYPE_INTERVAL:
        hours = int(data.get("interval_hours", DEFAULT_INTERVAL_HOURS))
        next_run_at = (datetime.now() + timedelta(hours=hours)).isoformat()
    elif job_type == JOB_TYPE_ALARM:
        run_at = data.get("run_at")
        if run_at:
            next_run_at = run_at
    elif job_type == JOB_TYPE_INDEX:
        hours = int(data.get("interval_hours", DEFAULT_INTERVAL_HOURS))
        next_run_at = (datetime.now() + timedelta(hours=hours)).isoformat()

    # For index jobs, use default/empty values for category fields
    if job_type == JOB_TYPE_INDEX:
        data.setdefault("category_name", "Indexing")
        data.setdefault("topic_category", "")
        data.setdefault("category_id", "")
        data.setdefault("wordpress_status", "draft")
        data.setdefault("country", "US")

    conn = get_connection()
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


def get_all_jobs() -> list[dict]:
    """Return all jobs, newest first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM scheduler_jobs ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_job(job_id: int) -> Optional[dict]:
    """Return one job by ID."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM scheduler_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job(job_id: int, data: dict) -> Optional[dict]:
    """
    Update selected fields of a job.

    If job_type, run_at, or interval_hours changed, next_run_at is
    automatically recalculated.
    """
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
        job = get_job(job_id)
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
                elif jt == JOB_TYPE_INDEX:
                    hrs = int(data.get("interval_hours", job.get("interval_hours", 24)))
                    updates["next_run_at"] = (
                        datetime.now() + timedelta(hours=hrs)
                    ).isoformat()

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]

    conn = get_connection()
    conn.execute(f"UPDATE scheduler_jobs SET {set_clause} WHERE id=?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM scheduler_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def soft_delete_job(job_id: int) -> bool:
    """Soft-delete a job by setting status to cancelled."""
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        "UPDATE scheduler_jobs SET status='cancelled', updated_at=? WHERE id=?",
        (now, job_id),
    )
    conn.commit()
    conn.close()
    return True


def hard_delete_job(job_id: int) -> bool:
    """Permanently remove a job and its execution history."""
    conn = get_connection()
    conn.execute("DELETE FROM execution_history WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM scheduler_jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return True


def get_pending_due_jobs() -> list[dict]:
    """Return pending jobs whose next_run_at has passed."""
    now = datetime.now().isoformat()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM scheduler_jobs WHERE status='pending' AND next_run_at<=?",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Execution History ────────────────────────────────────────────────────

def create_history_entry(data: dict) -> dict:
    """Insert a new execution_history row."""
    conn = get_connection()
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


def update_history_entry(history_id: int, data: dict) -> Optional[dict]:
    """Update selected fields of an execution_history row."""
    allowed = {
        "finished_at", "status", "run_id", "html_path",
        "publish_status", "error_message",
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return None

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [history_id]

    conn = get_connection()
    conn.execute(f"UPDATE execution_history SET {set_clause} WHERE id=?", values)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM execution_history WHERE id=?", (history_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_history(job_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Return execution history rows, optionally filtered by job_id."""
    conn = get_connection()
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