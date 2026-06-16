from flask import Flask, render_template, request, redirect, jsonify
import json
import os
import threading

from testing.test2.app.scheduler_config import CATEGORY_MAP, COUNTRIES, VALID_SCHEDULER_TYPES, VALID_PUBLISH_STATUSES

app = Flask(__name__)


def load_status():
    try:
        with open("app/scheduler_status.json", "r") as f:
            return json.load(f)
    except Exception:
        return {
            "last_triggered_at": None,
            "last_main_status": "never",
            "last_main_at": None,
            "last_main_run_id": None,
            "last_api_status": "never",
            "last_api_at": None,
            "last_api_html": None,
            "last_error": None,
            "scheduler_type": "interval",
            "scheduler_enabled": True,
        }


@app.route("/")
def home():
    with open("app/config.json", "r") as f:
        config = json.load(f)
    status = load_status()

    config.setdefault("scheduler_type", "interval")
    config.setdefault("schedule_every_hours", 4)
    config.setdefault("alarm_times", ["09:00", "13:00", "17:00", "21:00"])
    config.setdefault("publish_status", "draft")
    config.setdefault("scheduler_enabled", True)

    return render_template(
        "index.html",
        config=config,
        status=status,
        categories=CATEGORY_MAP.keys(),
        countries=COUNTRIES,
    )


@app.route("/save", methods=["POST"])
def save():
    category_name = request.form["category"]
    category_details = CATEGORY_MAP.get(category_name)

    if not category_details:
        with open("app/config.json", "r") as f:
            existing = json.load(f)
        category_details = {
            "category_id": existing.get("category_id", ""),
            "topic_category": existing.get("topic_category", ""),
        }

    scheduler_type = request.form.get("scheduler_type", "interval")
    if scheduler_type not in VALID_SCHEDULER_TYPES:
        scheduler_type = "interval"

    schedule_every_hours = int(request.form.get("schedule_every_hours", 4))
    if schedule_every_hours < 1:
        schedule_every_hours = 1
    if schedule_every_hours > 168:
        schedule_every_hours = 168

    alarm_times_raw = request.form.getlist("alarm_times")
    alarm_times = [t.strip() for t in alarm_times_raw if t.strip()]

    publish_status = request.form.get("publish_status", "draft")
    if publish_status not in VALID_PUBLISH_STATUSES:
        publish_status = "draft"

    scheduler_enabled = request.form.get("scheduler_enabled", "true") == "true"

    data = {
        "country": request.form["country"],
        "category_name": category_name,
        "category_id": category_details["category_id"],
        "topic_category": category_details["topic_category"],
        "scheduler_type": scheduler_type,
        "schedule_every_hours": schedule_every_hours,
        "alarm_times": alarm_times,
        "publish_status": publish_status,
        "scheduler_enabled": scheduler_enabled,
    }

    with open("app/config.json", "w") as f:
        json.dump(data, f, indent=4)

    return redirect("/")


@app.route("/run-now", methods=["POST"])
def run_now():
    return jsonify({"message": "Job triggered (preview mode)", "status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
