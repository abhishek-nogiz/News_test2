from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import uuid

from config import AppConfig
from news_agent.services.helpers import slugify

from .discovery import TrendDiscoveryService
from .evidence import EvidenceService
from .fetchers import SerpApiNewsFetcher
from .formatter import NewsroomFormatter
from .llm_gateway import LLMGateway
from .models import RunRequest, TrendSignal, WorkflowResult
from .planner import PlanningService
from .publisher import LocalArtifactPublisher
from .repair import NewsroomRepairService
from .research_router import DeterministicResearchRouter
from .triage import TopicTriageService
from .validator import NewsroomValidator
from .verifier import MIN_VERIFIED_CLAIMS_FOR_ARTICLE, MIN_VERIFIED_CLAIMS_FOR_BRIEF, VerificationService
from .writer import NewsroomWriter


class NewsroomV3Workflow:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.gateway = LLMGateway(config)
        self.discovery_service = TrendDiscoveryService(config)
        self.triage_service = TopicTriageService(config, gateway=self.gateway)
        self.research_router = DeterministicResearchRouter(config)
        self.research_service = SerpApiNewsFetcher(config)
        self.evidence_service = EvidenceService(self.gateway)
        self.verifier = VerificationService()
        self.planner = PlanningService()
        self.writer = NewsroomWriter()
        self.formatter = NewsroomFormatter()
        self.validator = NewsroomValidator()
        self.repair_service = NewsroomRepairService(writer=self.writer, formatter=self.formatter, validator=self.validator)
        self.publisher = LocalArtifactPublisher(config)

    def run(self, *, seed_topics: list[str] | None = None, country: str | None = None, trigger_source: str = "manual", draft: bool = False) -> WorkflowResult:
        effective_country = country or self.config.country
        request = RunRequest(
            run_id=str(uuid.uuid4()),
            trigger_source=trigger_source,
            mode="seed" if seed_topics else "auto",
            country=effective_country,
            category=self.config.topic_category,
            seed_topics=list(seed_topics or []),
        )

        candidates = self.discovery_service.discover(request)
        candidate = candidates[0]
        triage_decision = self.triage_service.classify(candidate)
        research_plan = self.research_router.plan(candidate)

        topic = TrendSignal(
            keyword=candidate.keyword,
            traffic=candidate.traffic,
            source=candidate.topic_source,
            cluster_key=candidate.cluster_key,
            trend_score=candidate.trend_score,
            freshness_score=candidate.freshness_score,
        )
        research_packet = self.research_service.research(topic, effective_country)
        rebuild_bundle = getattr(self.research_service, "_build_bundle", None)
        if rebuild_bundle is None:
            raise RuntimeError("The configured v3 research service does not expose _build_bundle for router execution")
        research_packet, tavily_enriched, enriched_source_count, filtered_source_count, source_filter_notes = self.research_router.execute(
            topic=topic,
            research_packet=research_packet,
            plan=research_plan,
            country=effective_country,
            topic_category=self.config.topic_category,
            rebuild_bundle=rebuild_bundle,
        )
        raw_documents = self.evidence_service.build_raw_documents(research_packet)
        claim_candidates = self.evidence_service.extract_claim_candidates(candidate, research_packet, raw_documents)
        claims = self.evidence_service.normalize_claims(claim_candidates)
        claim_clusters = self.evidence_service.cluster_claims(claims)
        claims_by_id = {claim.claim_id: claim for claim in claims}
        verified_claims, quarantine_items, verification = self.verifier.verify(
            candidate=candidate,
            claim_clusters=claim_clusters,
            claims_by_id=claims_by_id,
            raw_documents=raw_documents,
        )

        disposition = self.triage_service.post_verification_disposition(
            candidate=candidate,
            verified_claim_count=len(verified_claims),
            article_threshold=MIN_VERIFIED_CLAIMS_FOR_ARTICLE,
            brief_threshold=MIN_VERIFIED_CLAIMS_FOR_BRIEF,
        )
        verification.disposition = disposition

        result = WorkflowResult(
            run_request=request,
            topic_candidate=candidate,
            triage_decision=triage_decision,
            research_plan=research_plan,
            raw_documents=raw_documents,
            tavily_enriched=tavily_enriched,
            enriched_source_count=enriched_source_count,
            research_source_count=len(research_packet.sources),
            filtered_source_count=filtered_source_count,
            source_filter_notes=source_filter_notes,
            claim_candidates=claim_candidates,
            claims=claims,
            claim_clusters=claim_clusters,
            verified_claims=verified_claims,
            quarantine_items=quarantine_items,
            verification=verification,
        )

        if disposition == "skip":
            result.metrics = self._metrics(result)
            if draft:
                result.audit_record = self.publisher.build_audit_record(result)
                result.saved_paths = self.publisher.save(result)
            return result

        writer_input = self.planner.build(candidate, verified_claims, raw_documents, research_packet.context_references)
        result.writer_input = writer_input
        draft_article = self.writer.draft(writer_input, attempt_number=1)
        draft_article = self.formatter.format(draft_article, writer_input)
        validation = self.validator.validate(draft_article, writer_input, raw_documents, quarantine_items)
        if not validation.passed:
            draft_article, validation = self.repair_service.attempt(
                draft=draft_article,
                validation=validation,
                writer_input=writer_input,
                raw_documents=raw_documents,
                quarantine_items=quarantine_items,
            )
        result.draft = draft_article
        result.validation = validation
        result.audit_record = self.publisher.build_audit_record(result)
        result.metrics = self._metrics(result)
        if draft:
            result.saved_paths = self.publisher.save(result)
        return result

    def summarize(self, result: WorkflowResult) -> dict:
        summary = {
            "run_id": result.run_request.run_id,
            "topic": result.topic_candidate.keyword,
            "story_id": result.writer_input.story_id if result.writer_input else (result.topic_candidate.cluster_key or slugify(result.topic_candidate.keyword)),
            "topic_family": result.topic_candidate.topic_family,
            "discovery_score": result.topic_candidate.discovery_score,
            "triage_decision": result.topic_candidate.triage_decision,
            "verification_score": result.verification.verification_score if result.verification else None,
            "disposition": result.verification.disposition if result.verification else None,
            "verified_claim_count": len(result.verified_claims),
            "quarantine_count": len(result.quarantine_items),
            "research_source_count": result.research_source_count,
            "filtered_source_count": result.filtered_source_count,
            "tavily_enriched": result.tavily_enriched,
            "enriched_source_count": result.enriched_source_count,
            "saved_paths": result.saved_paths,
        }
        if result.validation is not None:
            summary["validation"] = {
                "passed": result.validation.passed,
                "blocking_failures": len(result.validation.blocking_failures),
                "structural_failures": len(result.validation.structural_failures),
                "formatting_failures": len(result.validation.formatting_failures),
            }
        return summary

    def _metrics(self, result: WorkflowResult) -> dict:
        return {
            "run_id": result.run_request.run_id,
            "story_id": result.writer_input.story_id if result.writer_input else (result.topic_candidate.cluster_key or slugify(result.topic_candidate.keyword)),
            "topic": result.topic_candidate.keyword,
            "topic_family": result.topic_candidate.topic_family,
            "verified_claim_count": len(result.verified_claims),
            "quarantine_count": len(result.quarantine_items),
            "research_source_count": result.research_source_count,
            "filtered_source_count": result.filtered_source_count,
            "tavily_enriched": result.tavily_enriched,
            "enriched_source_count": result.enriched_source_count,
            "disposition": result.verification.disposition if result.verification else None,
            "validation_passed": result.validation.passed if result.validation else False,
            "skip_reason": None if (result.verification and result.verification.disposition != "skip") else "insufficient_verified_claims",
        }