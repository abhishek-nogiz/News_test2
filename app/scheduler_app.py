"""
Flask web UI for the Trend Agent Scheduler.

Standalone layer — reads/writes the SQLite DB via scheduler_db and
controls execution via scheduler_service.  Never imports from news_agent.

Three job types:
  - alarm:   Run once at a specific time
  - interval: Run every N hours (article generation)
  - index:    Crawl sitemap → embed → store (background indexing)

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

import json
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify

from app.scheduler_config import (
    CATEGORY_MAP,
    ALL_CATEGORIES_LABEL,
    COUNTRIES,
    WORDPRESS_STATUSES,
    VALID_JOB_TYPES,
    JOB_TYPE_ALARM,
    JOB_TYPE_INDEX,
    JOB_STATUS_PAUSED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
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

from dotenv import load_dotenv
load_dotenv(override=True) 

def _log_payload(label: str, payload) -> None:
    """Print exact payload content for debugging request/DB flow."""
    print("\n" + "=" * 100)
    print(f"[Scheduler Payload] {label}")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("=" * 100 + "\n")


def _log_auth_header(label: str) -> None:
    auth = request.headers.get("Authorization", "")
    if auth:
        _log_payload(f"{label} Authorization header", auth)


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
    _log_payload("POST /api/jobs raw body", request.get_data(as_text=True) or "")
    _log_payload("POST /api/jobs parsed JSON", data)
    _log_auth_header("POST /api/jobs")

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Job name is required"}), 400

    job_type = data.get("job_type", "interval")
    if job_type not in VALID_JOB_TYPES:
        return jsonify({"error": f"Invalid job type: {job_type}. Must be one of: {', '.join(VALID_JOB_TYPES)}"}), 400

    # ═══════════════════════════════════════════════════════════════════
    # INDEX JOB — minimal validation (no category/country needed)
    # ═══════════════════════════════════════════════════════════════════
    if job_type == JOB_TYPE_INDEX:
        interval_hours = data.get("interval_hours", 24)  # Default 24h for indexing

        job_payload = {
            "name": name,
            "job_type": job_type,
            "interval_hours": interval_hours,
            # Index jobs don't need these, but DB requires them
            "country": "US",
            "category_name": "Indexing",
            "topic_category": "",
            "category_id": "",
            "wordpress_status": "draft",
        }
        _log_payload("create_job payload (index)", job_payload)
        job = create_job(job_payload)
        return jsonify(job), 201

    # ═══════════════════════════════════════════════════════════════════
    # ALARM / INTERVAL JOBS — full validation
    # ═══════════════════════════════════════════════════════════════════

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
    category_name = data.get("category_name", ALL_CATEGORIES_LABEL)

    if category_name == ALL_CATEGORIES_LABEL:
        # All Categories mode — runs all 5 categories sequentially
        topic_category = "all"
        category_id = ""
    else:
        cat = CATEGORY_MAP.get(category_name, {})
        topic_category = cat.get("topic_category", data.get("topic_category", "sports"))
        category_id = cat.get("category_id", data.get("category_id", ""))

    job_payload = {
        "name": name,
        "job_type": job_type,
        "run_at": data.get("run_at"),
        "interval_hours": data.get("interval_hours", 4),
        "country": data.get("country", "US"),
        "category_name": category_name,
        "topic_category": topic_category,
        "category_id": category_id,
        "wordpress_status": data.get("wordpress_status", "draft"),
    }
    _log_payload("create_job payload (alarm/interval)", job_payload)
    job = create_job(job_payload)
    return jsonify(job), 201


@app.route("/api/jobs/<int:job_id>", methods=["PUT"])
def api_update_job(job_id):
    data = request.get_json(silent=True) or {}
    _log_payload(f"PUT /api/jobs/{job_id} raw body", request.get_data(as_text=True) or "")
    _log_payload(f"PUT /api/jobs/{job_id} parsed JSON", data)
    _log_auth_header(f"PUT /api/jobs/{job_id}")

    existing = get_job(job_id)
    if not existing:
        return jsonify({"error": "Job not found"}), 404

    # Map category if changed (only for non-index jobs)
    if existing.get("job_type") != JOB_TYPE_INDEX:
        cat_name = data.get("category_name")
        if cat_name == ALL_CATEGORIES_LABEL:
            data["topic_category"] = "all"
            data["category_id"] = ""
        elif cat_name and cat_name in CATEGORY_MAP:
            cat = CATEGORY_MAP[cat_name]
            data["topic_category"] = cat["topic_category"]
            data["category_id"] = cat["category_id"]

    job = update_job(job_id, data)
    _log_payload(f"update_job payload for job_id={job_id}", data)
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

    # Allow re-running completed, failed, cancelled, and pending jobs
    # (previously blocked completed/cancelled, but that's annoying for testing)
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
    _log_payload("GET /api/history query params", {"job_id": job_id, "limit": limit})
    _log_auth_header("GET /api/history")
    return jsonify(get_history(job_id=job_id, limit=limit))


# ── Main ─────────────────────────────────────────────────────────────────
import os
if __name__ == "__main__":
    start_scheduler_service()
    app.run(host="0.0.0.0", port=int(os.getenv("APP_PORT", 8000)), debug=False)
