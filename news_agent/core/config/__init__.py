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


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Parse a comma-separated env var into a list of strings."""
    value = os.getenv(name)
    if value is None:
        return default if default is not None else []
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_json(name: str, default: dict | None = None) -> dict:
    """Parse a JSON env var into a dict."""
    import json
    value = os.getenv(name)
    if value is None:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


@dataclass(slots=True)
class AppConfig:
    # ── Existing fields (unchanged) ──
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

    # ── NEW: Tenant isolation ──
    tenant_id: str = ""
    # Unique identifier for the current tenant/client.
    # All vector store operations are scoped to this ID.
    # Example: "peoplenewstime", "client-abc123"

    # ── NEW: Vector store configuration ──
    vector_store_type: str = "json"
    # "json"     → JSONVectorStore (dev / single-tenant / < 10k articles)
    # "pgvector" → PgvectorStore (production / multi-tenant / 10k+ articles)

    vector_store_database_url: str = ""
    # PostgreSQL connection string for PgvectorStore.
    # Only used when vector_store_type == "pgvector".

    # ── NEW: Sitemap discovery settings ──
    sitemap_url: str = ""
    # URL to the client's sitemap.xml
    # Can be a sitemap index — the provider handles recursion.

    sitemap_crawl_delay: float = 0.5
    # Seconds to wait between HTTP requests when crawling.

    sitemap_max_urls: int = 500
    # Maximum number of URLs to crawl per indexing run.

    sitemap_crawl_timeout: int = 15
    # HTTP request timeout in seconds for each page fetch.

    sitemap_user_agent: str = "InternalLinkBot/1.0"
    # User-Agent header sent during crawling.

    sitemap_include_patterns: list[str] = None  # type: ignore[assignment]
    # Regex patterns — if specified, a URL must match at least one.
    # Leave empty (None) to include everything after excludes.

    sitemap_exclude_patterns: list[str] = None  # type: ignore[assignment]
    # Regex patterns — URLs matching any of these are skipped.

    sitemap_category_map: dict[str, str] = None  # type: ignore[assignment]
    # Maps URL path segments to category names.
    # Per-tenant configuration — NOT hardcoded inference.
    # If None, categories come purely from HTML extraction.

    # ── NEW: Indexing schedule ──
    sitemap_cron_enabled: bool = False
    sitemap_cron_interval_hours: int = 24
    indexing_webhook_enabled: bool = False

    def __post_init__(self) -> None:
        """Set mutable defaults after dataclass init (slots=True requires this)."""
        if self.sitemap_include_patterns is None:
            self.sitemap_include_patterns = []
        if self.sitemap_exclude_patterns is None:
            self.sitemap_exclude_patterns = [
                r"/pages/",
                r"/advertise",
                r"/contact",
                r"/cookies",
                r"/help-faq",
                r"/privacy-policy",
                r"/subscription-terms",
                r"/terms-of-use",
            ]
        if self.sitemap_category_map is None:
            self.sitemap_category_map = {
                "politics": "politics",
                "business": "business",
                "sports": "sports",
                "tech": "technology",
                "stock-market": "stock-market",
                "travel": "travel",
            }

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
            # ── NEW: internal linking env vars ──
            tenant_id=os.getenv("NEWS_AGENT_TENANT_ID", ""),
            vector_store_type=os.getenv("NEWS_AGENT_VECTOR_STORE_TYPE", "json"),
            vector_store_database_url=os.getenv("NEWS_AGENT_VECTOR_STORE_DATABASE_URL", ""),
            sitemap_url=os.getenv("NEWS_AGENT_SITEMAP_URL", ""),
            sitemap_crawl_delay=float(os.getenv("NEWS_AGENT_SITEMAP_CRAWL_DELAY", "0.5")),
            sitemap_max_urls=int(os.getenv("NEWS_AGENT_SITEMAP_MAX_URLS", "500")),
            sitemap_crawl_timeout=int(os.getenv("NEWS_AGENT_SITEMAP_CRAWL_TIMEOUT", "15")),
            sitemap_user_agent=os.getenv("NEWS_AGENT_SITEMAP_USER_AGENT", "InternalLinkBot/1.0"),
            sitemap_include_patterns=_env_list("NEWS_AGENT_SITEMAP_INCLUDE_PATTERNS"),
            sitemap_exclude_patterns=None,  # uses default from __post_init__
            sitemap_category_map=None,  # uses default from __post_init__
            sitemap_cron_enabled=_env_flag("NEWS_AGENT_SITEMAP_CRON", default=False),
            sitemap_cron_interval_hours=int(os.getenv("NEWS_AGENT_SITEMAP_CRON_INTERVAL", "24")),
            indexing_webhook_enabled=_env_flag("NEWS_AGENT_INDEXING_WEBHOOK", default=False),
        )
