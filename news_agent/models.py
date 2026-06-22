from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RunLog:
    time: str
    message: str


# ─────────────────────────────────────────────────────────────────────────
# NEW: SerpAPI usage tracking (per-stage counters on each PipelineRun)
# ─────────────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class SerpApiStats:
    calls: int = 0
    errors: int = 0
    by_engine: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TrendTopic:
    keyword: str
    traffic: int | None
    source: str
    cluster_key: str = ""
    trend_score: float = 0.0
    freshness_score: float = 0.0
    competition_score: float = 0.0
    monetization_score: float = 0.0
    final_score: float = 0.0


@dataclass(slots=True)
class ResearchSource:
    title: str
    url: str
    snippet: str
    publisher: str = ""
    published_at: str = ""
    content: str = ""
    source_tier: str = "secondary"
    image_url: str = ""
    image_caption: str = ""
    image_credit: str = ""


@dataclass(slots=True)
class ResearchClaim:
    claim: str
    source_title: str
    source_url: str
    source_tier: str = "secondary"
    section: str = "present"


@dataclass(slots=True)
class ContextReference:
    entity: str
    title: str
    url: str
    snippet: str = ""
    source: str = "background"
    summary: str = ""


@dataclass(slots=True)
class ResearchPacket:
    topic: str
    sources: list[ResearchSource] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    context: str = ""
    lead: str = ""
    present: list[str] = field(default_factory=list)
    past: list[str] = field(default_factory=list)
    future: list[str] = field(default_factory=list)
    claims: list[ResearchClaim] = field(default_factory=list)
    context_references: list[ContextReference] = field(default_factory=list)


@dataclass(slots=True)
class ContentPlan:
    audience: str
    tone: str
    primary_keyword: str
    article_type: str = "news_analysis"
    secondary_keywords: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    brief: str = ""
    memory_notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GeneratedArticle:
    catchy_title: str
    seo_keywords: list[str]
    meta_description: str
    blog_outline: list[str]
    article_markdown: str
    article_html: str
    image_prompts: list[str]


@dataclass(slots=True)
class ImageAsset:
    prompt: str
    alt_text: str
    status: str = "planned"
    provider: str = "prompt"
    image_path: str | None = None
    mime_type: str | None = None
    error: str | None = None


@dataclass(slots=True)
class EditorialMemoryEntry:
    run_id: str
    keyword: str
    cluster_key: str
    title: str
    audience: str
    sections: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    quality_score: int = 0
    seo_score: int = 0
    grounding_score: int = 0
    publish: bool = False
    captured_at: str = ""


@dataclass(slots=True)
class EditorialMemoryPacket:
    entries: list[EditorialMemoryEntry] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WordPressPostMetadata:
    title: str
    slug: str
    excerpt: str
    meta_description: str
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    post_status: str = "draft"
    featured_image_path: str | None = None
    featured_image_alt: str | None = None
    featured_image_provider: str | None = None


@dataclass(slots=True)
class WordPressAuthResult:
    endpoint: str | None = None
    authenticated: bool = False
    viewer_database_id: int | None = None
    viewer_username: str | None = None
    viewer_roles: list[str] = field(default_factory=list)
    viewer_capabilities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WordPressSyncResult:
    synced: bool = False
    endpoint: str | None = None
    requested_status: str | None = None
    remote_status: str | None = None
    post_id: str | None = None
    database_id: int | None = None
    slug: str | None = None
    categories: list[str] = field(default_factory=list)
    response_path: str | None = None
    auth: WordPressAuthResult | None = None
    seo_synced: bool = False
    seo_update_method: str | None = None
    seo_error: str | None = None


@dataclass(slots=True)
class PublishArtifact:
    markdown_path: str
    metadata_path: str
    html_path: str | None = None
    wordpress: WordPressPostMetadata | None = None
    wordpress_sync: WordPressSyncResult | None = None


@dataclass(slots=True)
class ValidationResult:
    quality_score: int
    seo_score: int
    grounding_score: int
    issues: list[str] = field(default_factory=list)
    publish: bool = False


@dataclass(slots=True)
class PipelineRun:
    run_id: str
    started_at: str
    trigger_source: str
    country: str
    max_topics: int
    status: str = "started"
    trends: list[TrendTopic] = field(default_factory=list)
    selected_topic: TrendTopic | None = None
    research: ResearchPacket | None = None
    memory: EditorialMemoryPacket | None = None
    plan: ContentPlan | None = None
    blog: GeneratedArticle | None = None
    internal_links: list[dict[str, object]] = field(default_factory=list)
    validation: ValidationResult | None = None
    images: list[ImageAsset] = field(default_factory=list)
    published: PublishArtifact | None = None
    logs: list[RunLog] = field(default_factory=list)

    # ───────────────────────────────────────────────────────────────────
    # NEW: per-stage SerpAPI usage for this run.
    # Keyed by stage_name (e.g. "trends", "research"). Populated automatically
    # by InstrumentedGoogleSearch via the contextvars binding in pipeline.py.
    # ───────────────────────────────────────────────────────────────────
    serpapi_by_stage: dict[str, SerpApiStats] = field(default_factory=dict)

    @classmethod
    def create(cls, trigger_source: str, country: str, max_topics: int) -> "PipelineRun":
        return cls(
            run_id=str(uuid.uuid4()),
            started_at=utc_now_iso(),
            trigger_source=trigger_source,
            country=country,
            max_topics=max_topics,
        )

    def log(self, message: str) -> None:
        self.logs.append(RunLog(time=utc_now_iso(), message=message))

    def transition(self, state: str) -> None:
        self.status = state
        self.log(f"State -> {state}")

    def fail(self, message: str) -> None:
        self.status = "failed"
        self.log(message)
