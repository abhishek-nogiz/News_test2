"""
Background scheduler service for the Trend Agent.

Runs as a daemon thread, continuously polling the database for due jobs.
Executes main.py and local_publish_html.py via subprocess — never imports
from news_agent.

Scheduler loop (every 30 s):
    get_pending_due_jobs() → execute each → update status / next_run_at
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.scheduler_config import (
    DEFAULT_INTERVAL_HOURS,
    JOB_TYPE_ALARM,
    JOB_TYPE_INTERVAL,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED,
)
from app.scheduler_db import (
    init_db,
    get_pending_due_jobs,
    get_job,
    update_job,
    create_history_entry,
    update_history_entry,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"
PUBLISH_SCRIPT = PROJECT_ROOT / "app" / "local_publish_html.py"

# ── Concurrency guard ────────────────────────────────────────────────────
_running_jobs: set[int] = set()
_running_lock = threading.Lock()

_scheduler_thread: threading.Thread | None = None


# ── Helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat()


def _extract_run_id(stdout: str) -> str | None:
    if not stdout:
        return None
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
        return data.get("run_id")
    except (json.JSONDecodeError, IndexError):
        pass
    m = re.search(r'"run_id"\s*:\s*"([^"]+)"', stdout)
    return m.group(1) if m else None


def _find_generated_html(run_id: str) -> Path | None:
    blogs_dir = PROJECT_ROOT / "storage" / "blogs"
    if not blogs_dir.exists():
        return None
    candidates = sorted(
        blogs_dir.glob(f"*-{run_id}.html"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ── Publish ─────────────────────────────────────────────────────────────

def _publish_html(job: dict, html_path: Path, history_id: int) -> bool:
    category_id = str(job.get("category_id") or "").strip()
    if not category_id:
        update_history_entry(history_id, {
            "publish_status": "skipped",
            "error_message": "category_id not configured",
        })
        return False

    wp_status = str(job.get("wordpress_status") or "draft").strip()
    cmd = [
        sys.executable,
        str(PUBLISH_SCRIPT),
        "--file", str(html_path),
        "--category-id", category_id,
        "--status", wp_status,
    ]

    print(f"\n{'='*60}\nPublishing: {' '.join(cmd)}\n{'='*60}")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    if result.stdout:
        print("PUBLISH STDOUT:", result.stdout)
    if result.stderr:
        print("PUBLISH STDERR:", result.stderr)

    if result.returncode != 0:
        update_history_entry(history_id, {
            "publish_status": "failed",
            "html_path": str(html_path),
            "error_message": f"publish script exit code {result.returncode}",
        })
        return False

    update_history_entry(history_id, {
        "publish_status": "success",
        "html_path": str(html_path),
    })
    return True


# ── Execute one job ──────────────────────────────────────────────────────

def execute_job(job_id: int, trigger_type: str = "scheduler") -> None:
    """
    Run main.py for a job, then publish the generated HTML.

    Args:
        job_id:       The database row ID.
        trigger_type: 'scheduler' for auto-runs, 'manual' for Run Now.
    """
    # Prevent double-execution of the same job
    with _running_lock:
        if job_id in _running_jobs:
            print(f"Job #{job_id} is already running — skipping")
            return
        _running_jobs.add(job_id)

    job = get_job(job_id)
    if not job:
        with _running_lock:
            _running_jobs.discard(job_id)
        print(f"Job #{job_id} not found")
        return

    original_status = job.get("status", JOB_STATUS_PENDING)
    now = _now_iso()

    # ── Mark job as running ───────────────────────────────────────────
    update_job(job_id, {
        "status": JOB_STATUS_RUNNING,
        "last_run_at": now,
        "error_message": None,
    })

    # ── Create execution history row ──────────────────────────────────
    hist = create_history_entry({
        "job_id": job_id,
        "started_at": now,
        "status": "running",
        "trigger_type": trigger_type,
    })
    hist_id = hist.get("id")

    try:
        # ── Run main.py ───────────────────────────────────────────────
        cmd = [
            sys.executable,
            str(MAIN_PY),
            "--country", str(job.get("country", "US")),
            "--topic-category", str(job.get("topic_category", "business")),
            "--wordpress-sync",
            "--wordpress-status", str(job.get("wordpress_status", "draft")),
            "--trigger", trigger_type,
        ]

        print(f"\n{'='*60}")
        print(f"Job #{job_id} '{job['name']}' — running:")
        print(f"  {' '.join(cmd)}")
        print(f"  cwd={PROJECT_ROOT}")
        print(f"{'='*60}")

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        if result.stdout:
            print("MAIN STDOUT:", result.stdout)
        if result.stderr:
            print("MAIN STDERR:", result.stderr)

        # ── main.py failed ────────────────────────────────────────────
        if result.returncode != 0:
            err = f"main.py exit code {result.returncode}"
            print(err)
            update_history_entry(hist_id, {
                "finished_at": _now_iso(),
                "status": "failed",
                "error_message": err,
            })
            _finish_job(job_id, job, trigger_type, original_status,
                        failed=True, error=err)
            return

        # ── main.py succeeded ─────────────────────────────────────────
        run_id = _extract_run_id(result.stdout or "")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "success",
            "run_id": run_id,
        })

        # Try to publish
        if run_id:
            html_path = _find_generated_html(run_id)
            if html_path:
                _publish_html(job, html_path, hist_id)
            else:
                update_history_entry(hist_id, {
                    "publish_status": "skipped",
                    "error_message": f"No HTML file for run_id={run_id}",
                })
        else:
            update_history_entry(hist_id, {
                "publish_status": "skipped",
                "error_message": "Could not extract run_id from main.py output",
            })

        _finish_job(job_id, job, trigger_type, original_status, failed=False)

    except Exception as exc:
        print(f"Job #{job_id} execution error: {exc}")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "failed",
            "error_message": str(exc),
        })
        _finish_job(job_id, job, trigger_type, original_status,
                    failed=True, error=str(exc))

    finally:
        with _running_lock:
            _running_jobs.discard(job_id)


def _finish_job(
    job_id: int,
    job: dict,
    trigger_type: str,
    original_status: str,
    *,
    failed: bool = False,
    error: str | None = None,
) -> None:
    """
    Update job status after execution completes.

    Rules:
      - Manual run: restore the pre-run status (don't alter schedule).
      - Scheduled alarm + success: status → completed.
      - Scheduled interval + success: status → pending, next_run_at = now + interval.
      - Any failure: status → failed.
    """
    now = _now_iso()

    # ── Manual run: keep the original schedule intact ─────────────────
    if trigger_type == "manual":
        restore = original_status if original_status != JOB_STATUS_RUNNING else JOB_STATUS_PENDING
        update_job(job_id, {
            "status": restore,
            "error_message": error,
        })
        return

    # ── Scheduled run ─────────────────────────────────────────────────
    if failed:
        update_job(job_id, {
            "status": JOB_STATUS_FAILED,
            "error_message": error,
        })
        return

    jt = job.get("job_type", JOB_TYPE_INTERVAL)
    if jt == JOB_TYPE_ALARM:
        update_job(job_id, {
            "status": JOB_STATUS_COMPLETED,
            "error_message": None,
        })
    else:
        hrs = int(job.get("interval_hours", DEFAULT_INTERVAL_HOURS))
        next_run = (datetime.now() + timedelta(hours=hrs)).isoformat()
        update_job(job_id, {
            "status": JOB_STATUS_PENDING,
            "next_run_at": next_run,
            "error_message": None,
        })


# ── Background loop ──────────────────────────────────────────────────────

def _scheduler_loop() -> None:
    """Poll every 30 seconds for due jobs and execute them."""
    while True:
        try:
            due = get_pending_due_jobs()
            for job in due:
                print(f"Scheduler: job #{job['id']} '{job['name']}' is due — executing")
                execute_job(job["id"], trigger_type="scheduler")
        except Exception as exc:
            print(f"Scheduler loop error: {exc}")
        time.sleep(30)


def start_scheduler_service() -> None:
    """Initialise the DB and start the background scheduler thread."""
    init_db()
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        print("Scheduler service started (polling every 30 s)")
