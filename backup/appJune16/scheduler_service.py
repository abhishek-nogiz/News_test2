"""
Background scheduler service for the Trend Agent.

Runs as a daemon thread, continuously polling the database for due jobs.
Executes main.py and publish_local_html.py via subprocess — never imports
from news_agent.

Three job types:
  - alarm:   Run once at a specific time
  - interval: Run every N hours (article generation)
  - index:    Crawl sitemap → embed → store (background indexing)

When a job's category is "All Categories", it runs all 5 categories
sequentially (Politics, Sports, Technology, Business & Finance, Travel),
each with its own main.py call and publish step with the correct category_id.

Index jobs run: python main.py --index

Scheduler loop (every 30 s):
    get_pending_due_jobs() → execute each → update status / next_run_at
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.scheduler_config import (
    CATEGORY_MAP,
    DEFAULT_INTERVAL_HOURS,
    ALL_CATEGORIES_LABEL,
    JOB_TYPE_ALARM,
    JOB_TYPE_INTERVAL,
    JOB_TYPE_INDEX,
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
PUBLISH_SCRIPT = PROJECT_ROOT / "app" / "publish_local_html.py"

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


# ── Indexing ────────────────────────────────────────────────────────────

def _run_indexing(job: dict, trigger_type: str) -> bool:
    """
    Run the indexing service to crawl sitemap and populate the vector store.

    This runs: python main.py --index

    The sitemap URL and tenant ID come from environment variables
    (NEWS_AGENT_SITEMAP_URL, NEWS_AGENT_TENANT_ID) which are loaded
    from .env by the config module.

    Returns True if indexing succeeded.
    """
    job_id = job["id"]
    now = _now_iso()

    # ── Create execution history row ─────────────────────────────────
    hist = create_history_entry({
        "job_id": job_id,
        "started_at": now,
        "status": "running",
        "trigger_type": trigger_type,
    })
    hist_id = hist.get("id")

    try:
        cmd = [
            sys.executable,
            str(MAIN_PY),
            "--index",
        ]

        # Pass sitemap URL if the job has one stored (for multi-tenant)
        # Otherwise it comes from .env / config
        sitemap_url = job.get("sitemap_url") or os.getenv("NEWS_AGENT_SITEMAP_URL", "")
        if sitemap_url:
            cmd.extend(["--sitemap-url", sitemap_url])

        tenant_id = job.get("tenant_id") or os.getenv("NEWS_AGENT_TENANT_ID", "")
        if tenant_id:
            cmd.extend(["--tenant-id", tenant_id])

        vector_store = os.getenv("NEWS_AGENT_VECTOR_STORE_TYPE", "json")
        if vector_store:
            cmd.extend(["--vector-store", vector_store])

        print(f"\n{'='*60}")
        print(f"Job #{job_id} '{job['name']}' — INDEXING")
        print(f"  {' '.join(cmd)}")
        print(f"  cwd={PROJECT_ROOT}")
        print(f"{'='*60}")

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        if result.stdout:
            print("INDEX STDOUT:", result.stdout)
        if result.stderr:
            print("INDEX STDERR:", result.stderr)

        if result.returncode != 0:
            err = f"Indexing exit code {result.returncode}"
            print(f"[INDEX] {err}")
            update_history_entry(hist_id, {
                "finished_at": _now_iso(),
                "status": "failed",
                "error_message": err,
            })
            return False

        # ── Parse indexing result ────────────────────────────────────
        indexed_count = None
        try:
            # main.py --index prints JSON with documents_indexed
            data = json.loads(result.stdout.strip().splitlines()[-1])
            indexed_count = data.get("documents_indexed", "?")
        except Exception:
            pass

        msg = f"Indexing complete ({indexed_count} documents)" if indexed_count else "Indexing complete"
        print(f"[INDEX] {msg}")

        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "success",
            "error_message": None,
        })

        return True

    except Exception as exc:
        print(f"[INDEX] execution error: {exc}")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "failed",
            "error_message": str(exc),
        })
        return False


# ── Publish ─────────────────────────────────────────────────────────────

def _publish_html(
    category_id: str,
    wordpress_status: str,
    html_path: Path,
    history_id: int,
) -> bool:
    """
    Publish an HTML file via publish_local_html.py.

    Args:
        category_id:       The PeopleNewsTime category ID for the POST request.
        wordpress_status:  Draft or publish.
        html_path:         Path to the generated HTML file.
        history_id:        Execution history row to update.
    """
    if not category_id:
        update_history_entry(history_id, {
            "publish_status": "skipped",
            "error_message": "category_id not configured",
        })
        return False

    cmd = [
        sys.executable,
        str(PUBLISH_SCRIPT),
        "--file", str(html_path),
        "--category-id", category_id,
        "--status", wordpress_status,
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


# ── Run one category ────────────────────────────────────────────────────

def _run_single_category(
    job: dict,
    cat_name: str,
    cat_info: dict,
    trigger_type: str,
) -> bool:
    """
    Run main.py for a single category and publish the result.

    Args:
        job:          The job row from the database.
        cat_name:     Display name (e.g. "Sports").
        cat_info:     Dict with topic_category and category_id.
        trigger_type: 'scheduler' or 'manual'.

    Returns:
        True if main.py succeeded (even if publish was skipped).
    """
    country = str(job.get("country", "US"))
    topic_cat = cat_info["topic_category"]
    category_id = cat_info["category_id"]
    wp_status = str(job.get("wordpress_status", "draft"))
    job_id = job["id"]

    now = _now_iso()

    # ── Create execution history row for this category ────────────────
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
            "--country", country,
            "--topic-category", topic_cat,
            "--wordpress-sync",
            "--wordpress-status", wp_status,
            "--trigger", trigger_type,
        ]

        print(f"\n{'='*60}")
        print(f"Job #{job_id} '{job['name']}' — category: {cat_name}")
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
            print(f"[{cat_name}] {err}")
            update_history_entry(hist_id, {
                "finished_at": _now_iso(),
                "status": "failed",
                "error_message": err,
            })
            return False

        # ── main.py succeeded ─────────────────────────────────────────
        run_id = _extract_run_id(result.stdout or "")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "success",
            "run_id": run_id,
        })

        # Try to publish with this category's ID
        if run_id:
            html_path = _find_generated_html(run_id)
            if html_path:
                _publish_html(category_id, wp_status, html_path, hist_id)
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

        return True

    except Exception as exc:
        print(f"[{cat_name}] execution error: {exc}")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "failed",
            "error_message": str(exc),
        })
        return False


# ── Execute one job ──────────────────────────────────────────────────────

def execute_job(job_id: int, trigger_type: str = "scheduler") -> None:
    """
    Run a job based on its type:
      - 'index':    Run sitemap indexing (crawl → embed → store)
      - 'alarm':    Run once at a specific time
      - 'interval': Run every N hours

    For non-index jobs, if category is "All Categories", runs all 5
    categories sequentially. Otherwise runs a single category.

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

    try:
        job_type = job.get("job_type", JOB_TYPE_INTERVAL)

        # ═══════════════════════════════════════════════════════════════
        # INDEX JOB — crawl sitemap → embed → store
        # ═══════════════════════════════════════════════════════════════
        if job_type == JOB_TYPE_INDEX:
            ok = _run_indexing(job, trigger_type)
            _finish_job(
                job_id, job, trigger_type, original_status,
                failed=not ok,
                error=None if ok else "Indexing failed",
            )

        # ═══════════════════════════════════════════════════════════════
        # ARTICLE GENERATION JOBS (alarm / interval)
        # ═══════════════════════════════════════════════════════════════
        else:
            category_name = job.get("category_name", "Sports")

            # ── All Categories mode: run each category one by one ─────
            if category_name == ALL_CATEGORIES_LABEL:
                results: dict[str, bool] = {}
                for cat_name, cat_info in CATEGORY_MAP.items():
                    print(f"\n>>> [{cat_name}] starting…")
                    ok = _run_single_category(job, cat_name, cat_info, trigger_type)
                    results[cat_name] = ok
                    status_word = "OK" if ok else "FAILED"
                    print(f">>> [{cat_name}] {status_word}")

                all_ok = all(results.values())
                if all_ok:
                    _finish_job(job_id, job, trigger_type, original_status, failed=False)
                else:
                    failed_cats = [k for k, v in results.items() if not v]
                    _finish_job(
                        job_id, job, trigger_type, original_status,
                        failed=True,
                        error=f"Failed categories: {', '.join(failed_cats)}",
                    )

            # ── Single category mode ──────────────────────────────────
            else:
                cat_info = CATEGORY_MAP.get(category_name, {
                    "topic_category": job.get("topic_category", "business"),
                    "category_id": job.get("category_id", ""),
                })
                ok = _run_single_category(job, category_name, cat_info, trigger_type)
                _finish_job(
                    job_id, job, trigger_type, original_status,
                    failed=not ok,
                    error=None if ok else f"main.py failed for {category_name}",
                )

    except Exception as exc:
        print(f"Job #{job_id} execution error: {exc}")
        _finish_job(job_id, job, trigger_type, original_status,
                    failed=True, error=str(exc))

    finally:
        with _running_lock:
            _running_jobs.discard(job_id)


# ── Finish job ──────────────────────────────────────────────────────────

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
      - Scheduled interval: ALWAYS goes back to pending with next_run_at,
        even if some categories failed. The error is logged but the
        recurring schedule never stops.
      - Scheduled alarm + failure: status → failed (one-time job, won't
        retry automatically).
      - Index jobs: always interval-based, always reschedule.
    """
    # ── Manual run: keep the original schedule intact ─────────────────
    if trigger_type == "manual":
        restore = original_status if original_status != JOB_STATUS_RUNNING else JOB_STATUS_PENDING
        update_job(job_id, {
            "status": restore,
            "error_message": error,
        })
        return

    jt = job.get("job_type", JOB_TYPE_INTERVAL)

    # ── Recurring (interval + index) jobs: ALWAYS continue the schedule
    # Failures are logged in execution_history and in error_message,
    # but the recurring timer never stops.
    if jt in (JOB_TYPE_INTERVAL, JOB_TYPE_INDEX):
        hrs = int(job.get("interval_hours", DEFAULT_INTERVAL_HOURS))
        # Default index interval is 24h if not specified
        if jt == JOB_TYPE_INDEX and hrs == DEFAULT_INTERVAL_HOURS:
            hrs = 24
        next_run = (datetime.now() + timedelta(hours=hrs)).isoformat()
        update_job(job_id, {
            "status": JOB_STATUS_PENDING,
            "next_run_at": next_run,
            "error_message": error,
        })
        return

    # ── Alarm (one-time) jobs ─────────────────────────────────────────
    if failed:
        update_job(job_id, {
            "status": JOB_STATUS_FAILED,
            "error_message": error,
        })
    else:
        update_job(job_id, {
            "status": JOB_STATUS_COMPLETED,
            "error_message": None,
        })


# ── Background loop ──────────────────────────────────────────────────────

def _scheduler_loop() -> None:
    """Poll every 30 seconds for due jobs and execute them."""
    while True:
        try:
            due = get_pending_due_jobs()
            for job in due:
                job_type = job.get("job_type", "interval")
                print(f"Scheduler: job #{job['id']} '{job['name']}' ({job_type}) is due — executing")
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