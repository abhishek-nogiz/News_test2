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
# Maps display name → {
#     topic_category:         slug passed to main.py --topic-category
#     category_id:            PeopleNewsTime REST API category ID (MongoDB ObjectId)
#     wordpress_category_id:  WordPress category ID (small integer) — passed to
#                             main.py --wordpress-category-id so the WP GraphQL
#                             createPost mutation assigns the right category.
#                             If omitted, WordPress uses its default category.
# }
#
# IMPORTANT — IDs below are taken directly from the client spec.
#   PeopleNewsTime IDs and WordPress IDs are SEPARATE systems:
#     - PeopleNewsTime ID looks like "6a22e0741f748391e98c6bab" (MongoDB ObjectId)
#     - WordPress ID looks like "3" (small integer)
#   Do NOT mix them up — the original bug had Politics and Business IDs
#   swapped, which caused Politics articles to appear under /us/business/<slug>
#   on the public site.

CATEGORY_MAP = {
    "Politics": {
        "topic_category": "politics",
        "category_id": "6a22e0741f748391e98c6bab",   # PeopleNewsTime: politics
        "wordpress_category_id": 3,                   # WordPress: Politics
    },
    "Business": {
        "topic_category": "business",
        "category_id": "6a22e0741f748391e98c6bad",   # PeopleNewsTime: business
        "wordpress_category_id": 4,                   # WordPress: Business
    },
    "Sports": {
        "topic_category": "sports",
        "category_id": "6a22e0741f748391e98c6baf",   # PeopleNewsTime: Sports
        "wordpress_category_id": 9,                   # WordPress: Sports
    },
    "Tech": {
        "topic_category": "technology",
        "category_id": "6a22e0751f748391e98c6bb1",   # PeopleNewsTime: Tech
        "wordpress_category_id": 5,                   # WordPress: Tech
    },
    "Travel": {
        "topic_category": "travel",
        "category_id": "6a22e0751f748391e98c6bb5",   # PeopleNewsTime: Travel
        "wordpress_category_id": 10,                  # WordPress: Travel
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