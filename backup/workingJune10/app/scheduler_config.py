"""Constants for the Trend Agent Scheduler."""

CATEGORY_MAP = {
    "Politics": {"topic_category": "politics", "category_id": "6a22e0741f748391e98c6bae"},
    "Sports": {"topic_category": "sports", "category_id": "6a22e0741f748391e98c6baf"},
    "Business & Finance": {"topic_category": "business", "category_id": "6a22e0741f748391e98c6bad"},
    "Travel": {"topic_category": "travel", "category_id": "6a22e0751f748391e98c6bb5"},
    "Technology": {"topic_category": "tech", "category_id": "6a22e0751f748391e98c6bb1"},
}

COUNTRIES = ["US", "IN", "UK"]
WORDPRESS_STATUSES = ["draft", "publish"]

# Job types
JOB_TYPE_ALARM = "alarm"
JOB_TYPE_INTERVAL = "interval"
VALID_JOB_TYPES = {JOB_TYPE_ALARM, JOB_TYPE_INTERVAL}

# Job statuses
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
VALID_JOB_STATUSES = {
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_PAUSED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
}

DEFAULT_INTERVAL_HOURS = 4
