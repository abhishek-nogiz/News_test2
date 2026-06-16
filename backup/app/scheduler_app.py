from flask import Flask, render_template, request, redirect
import json
import os

from app.scheduler_runner import load_scheduler_status, start_scheduler, update_scheduler
from app.scheduler_config import CATEGORY_MAP, COUNTRIES

app = Flask(__name__)


@app.route("/")
def home():

    with open("app/config.json", "r") as f:
        config = json.load(f)
    status = load_scheduler_status()

    return render_template(
        "index.html",
        config=config,
        status=status,
        categories=CATEGORY_MAP.keys(),
        countries=COUNTRIES
    )


@app.route("/save", methods=["POST"])
def save():

    category_name = request.form["category"]
    category_details = CATEGORY_MAP[category_name]

    data = {
        "country": request.form["country"],
        "category_name": category_name,
        "category_id": category_details["category_id"],
        "topic_category": category_details["topic_category"],
        "schedule_every_hours": 4,
    }

    with open("app/config.json", "w") as f:
        json.dump(data, f, indent=4)

    # Reload scheduler with recurring 4-hour interval.
    update_scheduler()

    return redirect("/")


if __name__ == "__main__":

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()

    app.run(
        host="0.0.0.0",
        port=8000,
        debug=True
    )