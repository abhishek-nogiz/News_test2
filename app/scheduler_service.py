"""
Background scheduler service for the Trend Agent.

Runs as a daemon thread, continuously polling the database for due jobs.
Executes main.py and publish_local_html.py via subprocess — never imports
from news_agent.

CHANGES FROM ORIGINAL:
  - After each pipeline run, calls CloudSync.sync_run() to upload
    artifacts to Backblaze B2 and save metadata to MongoDB.
  - Uses CloudSync.get_html_content() instead of _find_generated_html()
    to locate HTML files (local-first, B2 fallback).
  - Uses CloudSync for scheduler status instead of scheduler_status.json.
  - Marks blog_metadata as published after successful publish.

When a job's category is "All Categories", it runs all 5 categories
sequentially (Politics, Sports, Technology, Business & Finance, Travel),
each with its own main.py call and publish step with the correct category_id.

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
    CATEGORY_MAP,
    DEFAULT_INTERVAL_HOURS,
    ALL_CATEGORIES_LABEL,
    JOB_TYPE_INTERVAL,
    JOB_TYPE_ALARM,
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
SCHEDULER_LOG_DIR = PROJECT_ROOT / "storage" / "logs"

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


def _summarize_main_failure(stdout: str, stderr: str, default: str) -> str:
    """Return a compact, human-readable failure reason for scheduler history."""
    for stream in (stderr, stdout):
        if not stream:
            continue
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        if not lines:
            continue
        # Keep the last meaningful line (usually the actual exception/error message).
        detail = lines[-1]
        if len(detail) > 180:
            detail = detail[:177] + "..."
        return f"{default} ({detail})"
    return default


def _tail_lines(text: str, max_lines: int = 12, max_chars: int = 300) -> list[str]:
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = lines[-max_lines:]
    clipped: list[str] = []
    for line in tail:
        if len(line) > max_chars:
            clipped.append(line[: max_chars - 3] + "...")
        else:
            clipped.append(line)
    return clipped


def _write_main_failure_log(
    history_id: int,
    cmd: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    """Persist full command/stdout/stderr for failed main.py runs."""
    SCHEDULER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = SCHEDULER_LOG_DIR / f"main-failure-h{history_id}-{ts}.log"

    payload = [
        f"time: {_now_iso()}",
        f"history_id: {history_id}",
        f"return_code: {returncode}",
        f"command: {' '.join(cmd)}",
        "",
        "===== STDOUT =====",
        stdout or "",
        "",
        "===== STDERR =====",
        stderr or "",
        "",
    ]
    log_path.write_text("\n".join(payload), encoding="utf-8")
    return str(log_path)


def _stream_pipe_output(pipe, prefix: str, collector: list[str]) -> None:
    """Read a subprocess pipe line-by-line, print it, and collect raw text."""
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            if line == "":
                break
            collector.append(line)
            stripped = line.rstrip("\n")
            if stripped:
                print(f"{prefix}{stripped}")
    finally:
        pipe.close()


def _run_streamed_process(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a command and stream stdout/stderr in real time."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    out_thread = threading.Thread(
        target=_stream_pipe_output,
        args=(proc.stdout, "MAIN STDOUT: ", stdout_lines),
        daemon=True,
    )
    err_thread = threading.Thread(
        target=_stream_pipe_output,
        args=(proc.stderr, "MAIN STDERR: ", stderr_lines),
        daemon=True,
    )
    out_thread.start()
    err_thread.start()

    started = time.time()
    last_heartbeat = -1
    while proc.poll() is None:
        elapsed = int(time.time() - started)
        if elapsed >= 20 and elapsed % 20 == 0 and elapsed != last_heartbeat:
            print(f"[Scheduler] main.py still running... {elapsed}s")
            last_heartbeat = elapsed
        time.sleep(1)

    out_thread.join(timeout=2)
    err_thread.join(timeout=2)

    return proc.returncode, "".join(stdout_lines), "".join(stderr_lines)


# ── CloudSync lazy loader ────────────────────────────────────────────────
# We don't import CloudSync at module level because it depends on
# B2/MongoDB engines which may not be configured yet.  Instead we
# load it lazily and gracefully degrade if cloud is not available.

_sync = None


def _get_sync():
    """Lazily get the CloudSync singleton."""
    global _sync
    if _sync is None:
        try:
            from app.cloud_sync import CloudSync
            _sync = CloudSync.instance()
        except Exception as exc:
            print(f"CloudSync not available (cloud features disabled): {exc}")
            _sync = False  # Sentinel: tried and failed
    return _sync if _sync is not False else None


def _find_generated_html(run_id: str) -> Path | None:
    """
    Find generated HTML for a run_id.

    Tries local filesystem first (same container, fast), then falls
    back to CloudSync which checks MongoDB metadata + Backblaze B2.
    """
    # ── Local filesystem check ───────────────────────────────────────
    blogs_dir = PROJECT_ROOT / "storage" / "blogs"
    if blogs_dir.exists():
        candidates = sorted(
            blogs_dir.glob(f"*-{run_id}.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    # ── CloudSync fallback ───────────────────────────────────────────
    sync = _get_sync()
    if sync:
        html_content = sync.get_html_content(run_id)
        if html_content:
            # Write to a temp file so publish_local_html.py can use it
            temp_dir = PROJECT_ROOT / "storage" / "blogs"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"restored-{run_id}.html"
            temp_path.write_text(html_content, encoding="utf-8")
            return temp_path

    return None


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
    Run main.py for a single category, sync to cloud, and publish.

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

        print(
            f"[Scheduler] starting job_id={job_id} cat={cat_name} "
            f"topic_category={topic_cat} wp_status={wp_status} trigger={trigger_type}"
        )
        print(f"\n{'='*60}")
        print(f"Job #{job_id} '{job['name']}' — category: {cat_name}")
        print(f"  {' '.join(cmd)}")
        print(f"  cwd={PROJECT_ROOT}")
        print(f"{'='*60}")

        returncode, stdout_text, stderr_text = _run_streamed_process(
            cmd,
            cwd=str(PROJECT_ROOT),
        )

        # ── main.py failed ────────────────────────────────────────────
        if returncode != 0:
            failure_log = _write_main_failure_log(
                hist_id,
                cmd,
                returncode,
                stdout_text,
                stderr_text,
            )
            err = _summarize_main_failure(
                stdout_text,
                stderr_text,
                f"main.py exit code {returncode}",
            )
            tail = _tail_lines(stderr_text or stdout_text)
            if tail:
                print("[Scheduler] main.py failure tail:")
                for line in tail:
                    print(f"  {line}")
            print(f"[{cat_name}] {err} | debug_log={failure_log}")
            update_history_entry(hist_id, {
                "finished_at": _now_iso(),
                "status": "failed",
                "error_message": f"{err} | log: {failure_log}",
            })
            return False

        # ── main.py succeeded ─────────────────────────────────────────
        run_id = _extract_run_id(stdout_text or "")
        update_history_entry(hist_id, {
            "finished_at": _now_iso(),
            "status": "success",
            "run_id": run_id,
        })

        # ── Sync artifacts to cloud (B2 + MongoDB) ─────────────────────
        if run_id:
            sync = _get_sync()
            if sync:
                try:
                    sync_result = sync.sync_run(
                        run_id=run_id,
                        topic=None,  # Not easily available here; metadata handles it
                        job_id=job_id,
                        category=topic_cat,
                    )
                    print(f"[CloudSync] Run {run_id} synced: "
                          f"html={bool(sync_result.get('html'))}, "
                          f"md={bool(sync_result.get('md'))}, "
                          f"images={len(sync_result.get('images', []))}, "
                          f"cloud={sync_result.get('cloud_enabled')}")
                except Exception as exc:
                    print(f"[CloudSync] Sync failed for run {run_id}: {exc}")
                    # Non-fatal — local files still work

        # ── Try to publish ────────────────────────────────────────────
        if run_id:
            html_path = _find_generated_html(run_id)
            if html_path:
                publish_ok = _publish_html(category_id, wp_status, html_path, hist_id)

                # Mark as published in blog_metadata
                if publish_ok:
                    sync = _get_sync()
                    if sync:
                        try:
                            sync.mark_published(run_id, publish_status="success")
                        except Exception:
                            pass  # Non-fatal
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
    Run a job. If category is "All Categories", runs all 5 categories
    sequentially. Otherwise runs a single category.

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
        category_name = job.get("category_name", "Sports")

        # ── All Categories mode: run each category one by one ─────────
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

        # ── Single category mode ──────────────────────────────────────
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

    # ── Recurring (interval) jobs: ALWAYS continue the schedule ───────
    if jt == JOB_TYPE_INTERVAL:
        hrs = int(job.get("interval_hours", DEFAULT_INTERVAL_HOURS))
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
            print(f"[Scheduler] poll at {_now_iso()} due_jobs={len(due)}")
            for job in due:
                print(f"Scheduler: job #{job['id']} '{job['name']}' is due — executing")
                execute_job(job["id"], trigger_type="scheduler")
        except Exception as exc:
            print(f"Scheduler loop error: {exc}")
        time.sleep(30)


def start_scheduler_service() -> None:
    """Initialise the DB, cloud sync, and start the background scheduler thread."""
    init_db()

    # ── Initialize cloud sync (MongoDB collections + B2 buckets) ───────
    try:
        sync = _get_sync()
        if sync:
            # cloud_enabled check is lazy — this first access triggers
            # the B2 connection test and prints diagnostic warnings
            cloud_ok = sync.cloud_enabled
            result = sync.initialize()
            print(f"CloudSync initialized: {json.dumps(result, default=str)}")
            if not cloud_ok:
                print("=" * 60)
                print("[CloudSync] B2 cloud storage is NOT active.")
                print("  Files will be stored locally ONLY — lost on CI/CD redeploy.")
                print("  To fix, set these in your .env:")
                print("    B2_ENDPOINT_URL=https://s3.<region>.backblazeb2.com")
                print("    B2_ACCESS_KEY_ID=<your-key-id>")
                print("    B2_SECRET_ACCESS_KEY=<your-application-key>")
                print("  Example:")
                print("    B2_ENDPOINT_URL=https://s3.us-west-002.backblazeb2.com")
                print("    B2_ACCESS_KEY_ID=0051...your-key-id...")
                print("    B2_SECRET_ACCESS_KEY=K005...your-application-key...")
                print("=" * 60)
    except Exception as exc:
        print(f"CloudSync initialization skipped: {exc}")

    # ── Start scheduler thread ────────────────────────────────────────
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        print("Scheduler service started (polling every 30 s)")
