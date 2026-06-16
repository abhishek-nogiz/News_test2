"""
Flask web UI for the Trend Agent Scheduler.

Standalone layer — reads/writes the SQLite DB via scheduler_db and
controls execution via scheduler_service.  Never imports from news_agent.

API routes:
    GET  /                  Render dashboard
    GET  /api/jobs          List all jobs
    POST /api/jobs          Create job
    PUT  /api/jobs/<id>     Update job
    DELETE /api/jobs/<id>   Soft-delete (cancel) job
    POST /api/jobs/<id>/pause   Pause a job
    POST /api/jobs/<id>/resume  Resume a job
    POST /api/jobs/<id>/run     Run a job now (manual trigger)
    GET  /api/history       List execution history
"""

from __future__ import annotations

import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify

from app.scheduler_config import (
    CATEGORY_MAP,
    COUNTRIES,
    WORDPRESS_STATUSES,
    VALID_JOB_TYPES,
    JOB_TYPE_ALARM,
    JOB_STATUS_PAUSED,
    JOB_STATUS_PENDING,
)
from app.scheduler_db import (
    create_job,
    get_all_jobs,
    get_job,
    update_job,
    soft_delete_job,
    hard_delete_job,
    get_history,
)
from app.scheduler_service import start_scheduler_service, execute_job

app = Flask(__name__)


# ── Page ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template(
        "index.html",
        categories=CATEGORY_MAP,
        countries=COUNTRIES,
        wordpress_statuses=WORDPRESS_STATUSES,
    )


# ── Jobs API ─────────────────────────────────────────────────────────────

@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    return jsonify(get_all_jobs())


@app.route("/api/jobs", methods=["POST"])
def api_create_job():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Job name is required"}), 400

    job_type = data.get("job_type", "interval")
    if job_type not in VALID_JOB_TYPES:
        return jsonify({"error": f"Invalid job type: {job_type}"}), 400

    # Alarm must have a future datetime
    if job_type == JOB_TYPE_ALARM:
        run_at = (data.get("run_at") or "").strip()
        if not run_at:
            return jsonify({"error": "Alarm jobs require a date and time"}), 400
        try:
            run_time = datetime.fromisoformat(run_at)
            if run_time <= datetime.now():
                return jsonify({"error": "Alarm time must be in the future"}), 400
        except ValueError:
            return jsonify({"error": f"Invalid datetime: {run_at}"}), 400

    # Resolve category
    category_name = data.get("category_name", "Sports")
    cat = CATEGORY_MAP.get(category_name, {})

    job = create_job({
        "name": name,
        "job_type": job_type,
        "run_at": data.get("run_at"),
        "interval_hours": data.get("interval_hours", 4),
        "country": data.get("country", "US"),
        "category_name": category_name,
        "topic_category": cat.get("topic_category", data.get("topic_category", "sports")),
        "category_id": cat.get("category_id", data.get("category_id", "")),
        "wordpress_status": data.get("wordpress_status", "draft"),
    })
    return jsonify(job), 201


@app.route("/api/jobs/<int:job_id>", methods=["PUT"])
def api_update_job(job_id):
    data = request.get_json(silent=True) or {}
    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404

    # Map category if changed
    cat_name = data.get("category_name")
    if cat_name and cat_name in CATEGORY_MAP:
        cat = CATEGORY_MAP[cat_name]
        data["topic_category"] = cat["topic_category"]
        data["category_id"] = cat["category_id"]

    job = update_job(job_id, data)
    return jsonify(job)


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def api_delete_job(job_id):
    perm = request.args.get("permanent", "").lower() == "true"
    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404

    if perm:
        hard_delete_job(job_id)
        return jsonify({"message": f"Job {job_id} permanently deleted"})
    else:
        soft_delete_job(job_id)
        return jsonify({"message": f"Job {job_id} cancelled"})


@app.route("/api/jobs/<int:job_id>/pause", methods=["POST"])
def api_pause_job(job_id):
    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404
    if existing["status"] not in ("pending", "failed"):
        return jsonify({"error": f"Cannot pause a job in '{existing['status']}' state"}), 400
    return jsonify(update_job(job_id, {"status": JOB_STATUS_PAUSED}))


@app.route("/api/jobs/<int:job_id>/resume", methods=["POST"])
def api_resume_job(job_id):
    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404
    if existing["status"] != JOB_STATUS_PAUSED:
        return jsonify({"error": f"Cannot resume a job in '{existing['status']}' state"}), 400
    return jsonify(update_job(job_id, {"status": JOB_STATUS_PENDING}))


@app.route("/api/jobs/<int:job_id>/run", methods=["POST"])
def api_run_job(job_id):
    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404
    if existing["status"] == JOB_STATUS_RUNNING:
        return jsonify({"error": "Job is already running"}), 400
    if existing["status"] in ("completed", "cancelled"):
        return jsonify({"error": f"Cannot run a '{existing['status']}' job"}), 400

    thread = threading.Thread(
        target=execute_job,
        args=(job_id,),
        kwargs={"trigger_type": "manual"},
        daemon=True,
    )
    thread.start()
    return jsonify({
        "message": f"Job '{existing['name']}' triggered",
        "status": "ok",
    })


# ── History API ──────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def api_history():
    job_id = request.args.get("job_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(get_history(job_id=job_id, limit=limit))


# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler_service()
    app.run(host="0.0.0.0", port=8000, debug=False)
