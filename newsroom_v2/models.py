from __future__ import annotations

from dataclasses import dataclass, field

from news_agent.models import ResearchPacket, TrendTopic


@dataclass(slots=True)
class EditorialDecision:
    topic: str
    article_mode: str = "skip"
    primary_angle: str = ""
    reasoning: str = ""
    confidence: float = 0.0
    should_write: bool = False
    evidence_strength: str = "low"
    missing_elements: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FactSpine:
    topic: str
    primary_angle: str
    article_mode: str
    core_event: str
    why_it_matters: str
    timeline: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    official_points: list[str] = field(default_factory=list)
    consequence: str = ""
    open_questions: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class EvidenceLedgerEntry:
    claim: str
    section: str
    source_url: str
    source_title: str
    source_tier: str
    published_at: str = ""
    supporting_snippet: str = ""
    source_domain: str = ""


@dataclass(slots=True)
class CandidateDossier:
    topic: TrendTopic
    research: ResearchPacket
    decision: EditorialDecision
    fact_spine: FactSpine
    tavily_enriched: bool = False
    enriched_source_count: int = 0
    research_source_count: int = 0
    filtered_source_count: int = 0
    source_filter_notes: list[str] = field(default_factory=list)
    evidence_ledger: list[EvidenceLedgerEntry] = field(default_factory=list)
    selection_rank: int = 1
    skipped_recent_topics: int = 0
    duplicate_filter_exhausted: bool = False
    topic_discovery_engine: str = ""
    research_engine: str = ""


@dataclass(slots=True)
class NewsroomPlan:
    headline: str
    angle: str
    article_mode: str
    lead_focus: str
    nut_graf: str
    section_heads: list[str] = field(default_factory=list)
    section_goals: list[str] = field(default_factory=list)
    section_evidence: list[list[EvidenceLedgerEntry]] = field(default_factory=list)
    source_titles: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NewsroomDraft:
    headline: str
    dek: str
    article_markdown: str
    article_html: str
    article_mode: str
    summary: str = ""
    publish_ready: bool = False
    validation: NewsroomValidation | None = None


@dataclass(slots=True)
class NewsroomValidation:
    editorial_score: int
    structure_score: int
    grounding_score: int
    issues: list[str] = field(default_factory=list)
    publish: bool = False