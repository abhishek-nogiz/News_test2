from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_orchestrator(value: str | None, default: str = "queue") -> str:
    if value is None:
        return default

    normalized = value.strip().lower()
    aliases = {
        "queue": "queue",
        "legacy": "queue",
        "langgraph": "langgraph",
        "graph": "langgraph",
    }
    return aliases.get(normalized, default)


TREND_WINDOW_HOURS = {
    "4h": 4,
    "24h": 24,
    "48h": 48,
    "7d": 168,
}


def _normalize_trend_window(value: str | None, default: str = "24h") -> str:
    if value is None:
        return default

    normalized = value.strip().lower()
    aliases = {
        "4": "4h",
        "4h": "4h",
        "24": "24h",
        "24h": "24h",
        "48": "48h",
        "48h": "48h",
        "168": "7d",
        "168h": "7d",
        "7d": "7d",
        "7day": "7d",
        "7days": "7d",
    }
    return aliases.get(normalized, default)


def _normalize_wordpress_status(value: str | None, default: str = "draft") -> str:
    if value is None:
        return default

    normalized = value.strip().lower()
    aliases = {
        "draft": "draft",
        "publish": "publish",
        "published": "publish",
        "auto": "auto",
    }
    return aliases.get(normalized, default)


@dataclass(slots=True)
class AppConfig:
    groq_api_key: str | None = None
    groq_fallback_api_key: str | None = None
    gemini_api_key: str | None = None
    serpapi_key: str | None = None
    firecrawl_api_key: str | None = None
    country: str = "IN"
    trend_window: str = "24h"
    orchestrator: str = "queue"
    max_topics: int = 10
    research_results: int = 5
    editorial_memory_limit: int = 3
    storage_root: Path = Path("storage")
    mock_mode: bool = False
    groq_model: str = "llama-3.3-70b-versatile"
    groq_fallback_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    gemini_image_model: str = "gemini-2.0-flash-preview-image-generation"
    min_article_words: int = 700
    duplicate_lookback_hours: int = 72
    topic_category: str | None = None
    wordpress_sync_enabled: bool = False
    wordpress_status: str = "draft"
    wordpress_graphql_url: str | None = None
    wordpress_graphql_user: str | None = None
    wordpress_graphql_password: str | None = None
    internal_link_embeddings_enabled: bool = False

    @property
    def trend_window_hours(self) -> int:
        return TREND_WINDOW_HOURS[self.trend_window]

    @classmethod
    def from_env(cls) -> "AppConfig":
        storage_root = Path(os.getenv("NEWS_AGENT_STORAGE_ROOT", "storage"))
        primary_groq_api_key = os.getenv("GROQ_API") or os.getenv("GROQ_API1") or os.getenv("GROQ_API2")
        fallback_groq_api_key = os.getenv("GROQ_API2") or os.getenv("GROQ_API_FALLBACK")
        if fallback_groq_api_key == primary_groq_api_key:
            fallback_groq_api_key = None

        primary_groq_model = (
            os.getenv("NEWS_AGENT_GROQ_MODEL")
            or os.getenv("NEWS_AGENT_GROQ_PRIMARY_MODEL")
            or "meta-llama/llama-4-scout-17b-16e-instruct"
        )
        fallback_groq_model = os.getenv("NEWS_AGENT_GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile")
        return cls(
            groq_api_key=primary_groq_api_key,
            groq_fallback_api_key=fallback_groq_api_key,
            gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
            serpapi_key=os.getenv("SERPAPI"),
            firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY") or None,
            country=os.getenv("NEWS_AGENT_COUNTRY", "IN"),
            trend_window=_normalize_trend_window(os.getenv("NEWS_AGENT_TREND_WINDOW", "24h")),
            orchestrator=_normalize_orchestrator(os.getenv("NEWS_AGENT_ORCHESTRATOR", "queue")),
            max_topics=int(os.getenv("NEWS_AGENT_MAX_TOPICS", "10")),
            research_results=int(os.getenv("NEWS_AGENT_RESEARCH_RESULTS", "5")),
            editorial_memory_limit=int(os.getenv("NEWS_AGENT_EDITORIAL_MEMORY_LIMIT", "3")),
            storage_root=storage_root,
            mock_mode=_env_flag("NEWS_AGENT_MOCK_MODE", default=False),
            groq_model=primary_groq_model,
            groq_fallback_model=fallback_groq_model,
            gemini_image_model=os.getenv(
                "NEWS_AGENT_GEMINI_IMAGE_MODEL",
                "gemini-2.0-flash-preview-image-generation",
            ),
            min_article_words=int(os.getenv("NEWS_AGENT_MIN_ARTICLE_WORDS", "700")),
            duplicate_lookback_hours=int(os.getenv("NEWS_AGENT_DUPLICATE_LOOKBACK_HOURS", "72")),
            topic_category=os.getenv("NEWS_AGENT_TOPIC_CATEGORY") or None,
            wordpress_sync_enabled=_env_flag("NEWS_AGENT_WORDPRESS_SYNC", default=False),
            wordpress_status=_normalize_wordpress_status(os.getenv("NEWS_AGENT_WORDPRESS_STATUS", "draft")),
            wordpress_graphql_url=os.getenv("WP_GRAPHQL_URL") or None,
            wordpress_graphql_user=os.getenv("WP_GRAPHQL_USER") or None,
            wordpress_graphql_password=os.getenv("WP_GRAPHQL_PASSWORD") or None,
            internal_link_embeddings_enabled=_env_flag("NEWS_AGENT_INTERNAL_LINK_EMBEDDINGS", default=False),
        )
