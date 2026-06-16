"""
Flask web UI for the Trend Agent Scheduler.

This is a standalone layer that reads/writes config.json and controls
the APScheduler. It does NOT import from news_agent.
"""
from flask import Flask, render_template, request, redirect, jsonify
import json
import threading
from pathlib import Path

from app.scheduler_runner import load_scheduler_status, start_scheduler, update_scheduler, run_job
from app.scheduler_config import (
    CATEGORY_MAP,
    COUNTRIES,
    WORDPRESS_STATUSES,
    VALID_SCHEDULER_TYPES,
    SCHEDULER_TYPE_INTERVAL,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

app = Flask(__name__)


@app.route("/")
def home():
    with open("app/config.json", "r") as f:
        config = json.load(f)

    status = load_scheduler_status()

    # Backward-compatible defaults
    config.setdefault("country", "US")
    config.setdefault("category_name", "Sports")
    config.setdefault("topic_category", "sports")
    config.setdefault("category_id", "")
    config.setdefault("scheduler_type", SCHEDULER_TYPE_INTERVAL)
    config.setdefault("interval_hours", 4)
    config.setdefault("alarm_datetime", "")
    config.setdefault("wordpress_status", "draft")
    config.setdefault("scheduler_enabled", True)

    return render_template(
        "index.html",
        config=config,
        status=status,
        categories=CATEGORY_MAP.keys(),
        countries=COUNTRIES,
        wordpress_statuses=WORDPRESS_STATUSES,
    )


@app.route("/save", methods=["POST"])
def save():
    category_name = request.form.get("category", "Sports")
    category_details = CATEGORY_MAP.get(category_name)

    if not category_details:
        with open("app/config.json", "r") as f:
            existing = json.load(f)
        category_details = {
            "category_id": existing.get("category_id", ""),
            "topic_category": existing.get("topic_category", ""),
        }

    scheduler_type = request.form.get("scheduler_type", SCHEDULER_TYPE_INTERVAL)
    if scheduler_type not in VALID_SCHEDULER_TYPES:
        scheduler_type = SCHEDULER_TYPE_INTERVAL

    interval_hours = int(request.form.get("interval_hours", 4))
    if interval_hours < 1:
        interval_hours = 1
    if interval_hours > 168:
        interval_hours = 168

    alarm_datetime = request.form.get("alarm_datetime", "").strip()

    wordpress_status = request.form.get("wordpress_status", "draft")
    if wordpress_status not in WORDPRESS_STATUSES:
        wordpress_status = "draft"

    scheduler_enabled = request.form.get("scheduler_enabled", "true") == "true"

    data = {
        "country": request.form.get("country", "US"),
        "category_name": category_name,
        "category_id": category_details["category_id"],
        "topic_category": category_details["topic_category"],
        "scheduler_type": scheduler_type,
        "interval_hours": interval_hours,
        "alarm_datetime": alarm_datetime,
        "wordpress_status": wordpress_status,
        "scheduler_enabled": scheduler_enabled,
    }

    with open("app/config.json", "w") as f:
        json.dump(data, f, indent=4)

    # Reconfigure the scheduler with new settings
    update_scheduler()

    return redirect("/")


@app.route("/api/status")
def api_status():
    """Return current config and status as JSON for auto-refresh."""
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    # Backward-compatible defaults
    config.setdefault("country", "US")
    config.setdefault("category_name", "Sports")
    config.setdefault("topic_category", "sports")
    config.setdefault("category_id", "")
    config.setdefault("scheduler_type", SCHEDULER_TYPE_INTERVAL)
    config.setdefault("interval_hours", 4)
    config.setdefault("alarm_datetime", "")
    config.setdefault("wordpress_status", "draft")
    config.setdefault("scheduler_enabled", True)

    status = load_scheduler_status()

    return jsonify({
        "config": config,
        "status": status,
    })


@app.route("/run-now", methods=["POST"])
def run_now():
    """Trigger an immediate manual run of the pipeline."""
    try:
        thread = threading.Thread(target=run_job, daemon=True)
        thread.start()
        return jsonify({"message": "Job triggered — refresh to see status", "status": "ok"})
    except Exception as e:
        return jsonify({"message": f"Failed: {e}", "status": "error"}), 500


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=8000, debug=False)
