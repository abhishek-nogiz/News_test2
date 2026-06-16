"""
Constants for the Trend Agent Scheduler.
"""

# ── Category mapping ──────────────────────────────────────────────────────
# Display name → { cli_value (for --topic-category), category_id (for local_publish_html.py) }
CATEGORY_MAP = {
    "Politics": {
        "topic_category": "politics",
        "category_id": "6a22e0741f748391e98c6bae",
    },
    "Sports": {
        "topic_category": "sports",
        "category_id": "6a22e0741f748391e98c6baf",
    },
    "Business & Finance": {
        "topic_category": "business",
        "category_id": "6a22e0741f748391e98c6bad",
    },
    "Travel": {
        "topic_category": "travel",
        "category_id": "6a22e0751f748391e98c6bb5",
    },
    "Technology": {
        "topic_category": "tech",
        "category_id": "6a22e0751f748391e98c6bb1",
    },
}

COUNTRIES = ["US", "IN", "UK"]

WORDPRESS_STATUSES = ["draft", "publish"]

# ── Scheduler types ───────────────────────────────────────────────────────
SCHEDULER_TYPE_INTERVAL = "interval"
SCHEDULER_TYPE_ALARM = "alarm"
VALID_SCHEDULER_TYPES = [SCHEDULER_TYPE_INTERVAL, SCHEDULER_TYPE_ALARM]

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_INTERVAL_HOURS = 4
