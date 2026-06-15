import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING

from app.scheduler_config import SCHEDULE_EVERY_HOURS


scheduler = BackgroundScheduler()
STATUS_PATH = Path("app/scheduler_status.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_status() -> dict[str, object]:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_status(**updates: object) -> None:
    payload = _read_status()
    payload.update(updates)
    STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_scheduler_status() -> dict[str, object]:
    payload = _read_status()
    payload.setdefault("last_triggered_at", None)
    payload.setdefault("last_main_status", "never")
    payload.setdefault("last_main_at", None)
    payload.setdefault("last_main_run_id", None)
    payload.setdefault("last_api_status", "never")
    payload.setdefault("last_api_at", None)
    payload.setdefault("last_api_html", None)
    payload.setdefault("last_error", None)
    return payload


def _extract_run_id(stdout: str) -> str | None:
    match = re.search(r'"run_id"\s*:\s*"([^"]+)"', stdout or "")
    if match is None:
        return None
    return match.group(1).strip()


def _find_generated_html(run_id: str) -> Path | None:
    blogs_dir = Path("storage/blogs")
    if not blogs_dir.exists():
        return None

    candidates = sorted(
        blogs_dir.glob(f"*-{run_id}.html"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _publish_generated_html(cfg: dict[str, object], html_path: Path) -> bool:
    category_id = str(cfg.get("category_id") or "").strip()
    if not category_id:
        print("Skipping API publish: category_id missing in app/config.json")
        _write_status(
            last_api_status="skipped",
            last_api_at=_now_iso(),
            last_error="category_id missing in app/config.json",
        )
        return False

    publish_cmd = [
        sys.executable,
        "-m",
        "app.publish_local_html",
        "--file",
        str(html_path),
        "--category-id",
        category_id,
        "--status",
        "draft",
    ]

    print("\n" + "=" * 60)
    print("Running:", " ".join(publish_cmd))
    print("=" * 60)

    publish_result = subprocess.run(
        publish_cmd,
        capture_output=True,
        text=True,
    )

    if publish_result.stdout:
        print("API STDOUT:")
        print(publish_result.stdout)

    if publish_result.stderr:
        print("API STDERR:")
        print(publish_result.stderr)

    if publish_result.returncode != 0:
        print(f"API publish exited with code {publish_result.returncode}")
        _write_status(
            last_api_status="failed",
            last_api_at=_now_iso(),
            last_api_html=str(html_path),
            last_error=f"API publish exit code {publish_result.returncode}",
        )
        return False

    _write_status(
        last_api_status="success",
        last_api_at=_now_iso(),
        last_api_html=str(html_path),
        last_error=None,
    )
    return True


def run_job():
    try:
        _write_status(
            last_triggered_at=_now_iso(),
            last_main_status="running",
            last_api_status="pending",
            last_error=None,
        )

        with open("app/config.json", "r") as f:
            cfg = json.load(f)

        cmd = [
            sys.executable,
            "main.py",
            "--country",
            str(cfg["country"]),
            "--topic-category",
            str(cfg["topic_category"]),
            "--wordpress-sync",
            "--wordpress-status",
            "draft",
        ]

        print("\n" + "=" * 60)
        print("Running:", " ".join(cmd))
        print("=" * 60)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.stdout:
            print("STDOUT:")
            print(result.stdout)

        if result.stderr:
            print("STDERR:")
            print(result.stderr)

        if result.returncode != 0:
            print(f"Process exited with code {result.returncode}")
            _write_status(
                last_main_status="failed",
                last_main_at=_now_iso(),
                last_error=f"main.py exit code {result.returncode}",
                last_api_status="skipped",
            )
            return

        run_id = _extract_run_id(result.stdout)
        if not run_id:
            print("Skipping API publish: could not determine run_id from main.py output")
            _write_status(
                last_main_status="success",
                last_main_at=_now_iso(),
                last_error="main.py output missing run_id",
                last_api_status="skipped",
            )
            return

        _write_status(
            last_main_status="success",
            last_main_at=_now_iso(),
            last_main_run_id=run_id,
            last_error=None,
        )

        html_path = _find_generated_html(run_id)
        if html_path is None:
            print(f"Skipping API publish: no generated HTML found for run_id={run_id}")
            _write_status(
                last_api_status="skipped",
                last_api_at=_now_iso(),
                last_error=f"No generated HTML for run_id={run_id}",
            )
            return

        _publish_generated_html(cfg, html_path)

    except Exception as e:
        print(f"Scheduler job failed: {e}")
        _write_status(
            last_main_status="failed",
            last_main_at=_now_iso(),
            last_api_status="failed",
            last_api_at=_now_iso(),
            last_error=str(e),
        )


def update_scheduler():
    try:
        with open("app/config.json", "r") as f:
            json.load(f)

        scheduler.remove_all_jobs()

        # Run every 4 hours, starting immediately.
        scheduler.add_job(
            run_job,
            trigger="interval",
            hours=SCHEDULE_EVERY_HOURS,
            next_run_time=datetime.now(),
            id="trend_agent",
            replace_existing=True,
        )

        if scheduler.state != STATE_RUNNING:
            scheduler.start()

        _write_status(
            scheduler_status="running",
            scheduler_updated_at=_now_iso(),
            scheduler_interval_hours=SCHEDULE_EVERY_HOURS,
            last_error=None,
        )
        print(f"Scheduler configured: runs every {SCHEDULE_EVERY_HOURS} hours")

    except Exception as e:
        print(f"Failed to update scheduler: {e}")
        _write_status(scheduler_status="failed", last_error=str(e))


def start_scheduler():
    update_scheduler()