"""
Configuration constants for the Trend Agent Scheduler.

Edit this file to change available categories, countries, etc.
"""

from __future__ import annotations

# ── Job Types ─────────────────────────────────────────────────────────────

JOB_TYPE_ALARM = "alarm"        # Run once at a specific time
JOB_TYPE_INTERVAL = "interval"  # Run every N hours
JOB_TYPE_INDEX = "index"        # Crawl sitemap → embed → store (background indexing)

VALID_JOB_TYPES = {JOB_TYPE_ALARM, JOB_TYPE_INTERVAL, JOB_TYPE_INDEX}

# ── Job Statuses ──────────────────────────────────────────────────────────

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

# ── Default Settings ──────────────────────────────────────────────────────

DEFAULT_INTERVAL_HOURS = 4
SCHEDULE_EVERY_HOURS = 4

# ── Categories ────────────────────────────────────────────────────────────
# Maps display name → {topic_category (for main.py), category_id (for API)}

CATEGORY_MAP = {
    "Politics": {
        "topic_category": "politics",
        "category_id": "6a22e0741f748391e98c6bad",
    },
    "Sports": {
        "topic_category": "sports",
        "category_id": "6a22e0741f748391e98c6baf",
    },
    "Technology": {
        "topic_category": "technology",
        "category_id": "6a22e0741f748391e98c6bae",
    },
    "Business & Finance": {
        "topic_category": "business",
        "category_id": "6a22e0741f748391e98c6bab",
    },
    "Travel": {
        "topic_category": "travel",
        "category_id": "6a22e0741f748391e98c6bac",
    },
}

ALL_CATEGORIES_LABEL = "All Categories"

# ── Countries ─────────────────────────────────────────────────────────────

COUNTRIES = {
    "US": "United States",
    "IN": "India",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
}

# ── WordPress Statuses ────────────────────────────────────────────────────

WORDPRESS_STATUSES = ["draft", "publish"]