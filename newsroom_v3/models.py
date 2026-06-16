from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any
import uuid


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {key: serialize_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [serialize_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialize_payload(item) for key, item in value.items()}
    return value


@dataclass(slots=True)
class ArtifactEnvelope:
    artifact_id: str
    schema_version: str
    artifact_type: str
    run_id: str
    story_id: str
    stage_name: str
    attempt_number: int
    created_at: str
    parent_artifact_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


def build_artifact_envelope(
    *,
    artifact_type: str,
    run_id: str,
    story_id: str,
    stage_name: str,
    attempt_number: int,
    payload: Any,
    parent_artifact_ids: list[str] | None = None,
    schema_version: str = "1.0.0",
) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=str(uuid.uuid4()),
        schema_version=schema_version,
        artifact_type=artifact_type,
        run_id=run_id,
        story_id=story_id,
        stage_name=stage_name,
        attempt_number=attempt_number,
        created_at=utc_now_iso(),
        parent_artifact_ids=list(parent_artifact_ids or []),
        payload=serialize_payload(payload),
    )


@dataclass(slots=True)
class RunRequest:
    run_id: str
    trigger_source: str = "manual"
    mode: str = "seed"
    country: str = "IN"
    category: str | None = None
    seed_topics: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class TrendSignal:
    keyword: str
    traffic: int | None
    source: str
    cluster_key: str = ""
    trend_score: float = 0.0
    freshness_score: float = 0.0


@dataclass(slots=True)
class SourceClaim:
    claim: str
    source_title: str
    source_url: str
    source_tier: str = "secondary"
    section: str = "present"


@dataclass(slots=True)
class FetchedSource:
    title: str
    url: str
    snippet: str
    publisher: str = ""
    published_at: str = ""
    content: str = ""
    source_tier: str = "secondary"
    fetched_by: str = "serpapi"
    fetched_at: str = ""
    extraction_status: str = "succeeded"


@dataclass(slots=True)
class ContextReference:
    entity: str
    title: str
    url: str
    snippet: str = ""
    source: str = "background"
    summary: str = ""


@dataclass(slots=True)
class ResearchBundle:
    topic: str
    sources: list[FetchedSource] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    context: str = ""
    lead: str = ""
    present: list[str] = field(default_factory=list)
    past: list[str] = field(default_factory=list)
    future: list[str] = field(default_factory=list)
    claims: list[SourceClaim] = field(default_factory=list)
    context_references: list[ContextReference] = field(default_factory=list)


@dataclass(slots=True)
class TopicCandidate:
    keyword: str
    cluster_key: str
    topic_source: str
    traffic: int | None = None
    trend_score: float = 0.0
    freshness_score: float = 0.0
    source_diversity_prior: float = 0.0
    discovery_score: float | None = None
    topic_family: str = "default_unclassified"
    topic_family_confidence: float = 0.0
    triage_decision: str | None = None
    triage_reasoning: str = ""
    verification_score: float | None = None
    selection_rank: int = 1
    skipped_recent_topics: int = 0
    duplicate_filter_exhausted: bool = False


@dataclass(slots=True)
class DiscoveryAssessment:
    discovery_score: float | None
    source_diversity_prior: float
    selection_rank: int
    skipped_recent_topics: int
    duplicate_filter_exhausted: bool = False


@dataclass(slots=True)
class TriageDecision:
    topic_family: str
    decision: str
    confidence: float
    reasoning: str = ""


@dataclass(slots=True)
class ResearchPlan:
    topic_family: str
    tools_to_call: list[str] = field(default_factory=list)
    fallback_used: bool = False
    notes: list[str] = field(default_factory=list)
    max_sources: int = 5
    require_mainstream_confirmations: int = 0
    allow_tavily_backfill: bool = False


@dataclass(slots=True)
class RawDocument:
    url: str
    title: str
    publisher: str
    fetched_by: str
    fetched_at: str
    cleaned_text: str
    extraction_status: str = "succeeded"
    published_at: str = ""
    source_tier: str = "secondary"


@dataclass(slots=True)
class ClaimCandidateFragment:
    text: str
    claim_type: str = "fact"
    evidence_span: str = ""
    section: str = "present"


@dataclass(slots=True)
class AtomicClaimCandidate:
    claim_id: str
    text: str
    section: str
    claim_type: str
    evidence_span: str
    fingerprint: str
    source_url: str | None = None
    source_title: str = ""
    source_tier: str = "secondary"
    status: str = "candidate"


@dataclass(slots=True)
class AtomicClaim:
    claim_id: str
    text: str
    section: str
    claim_type: str
    evidence_span: str
    fingerprint: str
    source_url: str | None = None
    source_title: str = ""
    source_tier: str = "secondary"
    status: str = "normalized"


@dataclass(slots=True)
class ClaimCluster:
    cluster_id: str
    canonical_claim: str
    fingerprint: str
    section: str
    supporting_claim_ids: list[str] = field(default_factory=list)
    contradictory_claim_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerifiedClaim:
    claim_id: str
    text: str
    status: str
    confidence: float
    attribution: str
    supporting_sources: list[str] = field(default_factory=list)
    contradictory_sources: list[str] = field(default_factory=list)
    section: str = "present"
    fingerprint: str = ""


@dataclass(slots=True)
class QuarantineItem:
    claim_text: str
    reason: str
    source_set: list[str] = field(default_factory=list)
    contradiction_notes: list[str] = field(default_factory=list)
    fingerprint: str = ""


@dataclass(slots=True)
class VerificationOutcome:
    verification_score: float
    disposition: str
    verified_count: int
    quarantine_count: int
    confidence_penalty: float = 0.0


@dataclass(slots=True)
class WriterInput:
    story_id: str
    article_type: str
    headline: str
    target_length: int
    lead_claim_id: str
    section_heads: list[str] = field(default_factory=list)
    section_claim_ids: list[list[str]] = field(default_factory=list)
    claim_lookup: dict[str, str] = field(default_factory=dict)
    claim_source_map: dict[str, list[str]] = field(default_factory=dict)
    sources: list[RawDocument] = field(default_factory=list)
    context_references: list[ContextReference] = field(default_factory=list)


@dataclass(slots=True)
class DraftArticle:
    headline: str
    dek: str
    html: str
    markdown: str
    sentences_with_claim_ids: list[dict[str, Any]] = field(default_factory=list)
    attempt_number: int = 1
    article_type: str = "article"


@dataclass(slots=True)
class GroundingFailure:
    sentence: str
    claim_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class QuarantineFailure:
    claim_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class AttributionFailure:
    claim_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class StructureFailure:
    reason: str


@dataclass(slots=True)
class SourcesFailure:
    reason: str


@dataclass(slots=True)
class StyleFailure:
    reason: str


@dataclass(slots=True)
class DensityFailure:
    reason: str


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    blocking_failures: list[GroundingFailure | QuarantineFailure | AttributionFailure] = field(default_factory=list)
    structural_failures: list[StructureFailure | SourcesFailure] = field(default_factory=list)
    formatting_failures: list[StyleFailure | DensityFailure] = field(default_factory=list)


@dataclass(slots=True)
class AuditRecord:
    article_id: str
    used_claim_ids: list[str] = field(default_factory=list)
    quarantined_claim_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    validation_scores: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowResult:
    run_request: RunRequest
    topic_candidate: TopicCandidate
    triage_decision: TriageDecision | None = None
    research_plan: ResearchPlan | None = None
    raw_documents: list[RawDocument] = field(default_factory=list)
    tavily_enriched: bool = False
    enriched_source_count: int = 0
    research_source_count: int = 0
    filtered_source_count: int = 0
    source_filter_notes: list[str] = field(default_factory=list)
    claim_candidates: list[AtomicClaimCandidate] = field(default_factory=list)
    claims: list[AtomicClaim] = field(default_factory=list)
    claim_clusters: list[ClaimCluster] = field(default_factory=list)
    verified_claims: list[VerifiedClaim] = field(default_factory=list)
    quarantine_items: list[QuarantineItem] = field(default_factory=list)
    verification: VerificationOutcome | None = None
    writer_input: WriterInput | None = None
    draft: DraftArticle | None = None
    validation: ValidationResult | None = None
    audit_record: AuditRecord | None = None
    saved_paths: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)