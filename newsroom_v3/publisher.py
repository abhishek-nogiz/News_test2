from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from config import AppConfig
from news_agent.services import PublisherService
from news_agent.services.helpers import serialize, slugify

from .models import AuditRecord, DiscoveryAssessment, WorkflowResult, build_artifact_envelope


class LocalArtifactPublisher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = Path(config.storage_root)
        self.cache_dir = self.root / "cache"
        self.newsroom_dir = self.root / "newsroom-v3"
        self.runs_dir = self.root / "runs"
        self.topic_registry_path = self.cache_dir / "published_topics.json"
        self.wordpress_publisher = PublisherService(config)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.newsroom_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def build_audit_record(self, result: WorkflowResult) -> AuditRecord:
        sources = []
        if result.writer_input is not None:
            for document in result.writer_input.sources:
                claim_ids = [claim_id for claim_id, urls in result.writer_input.claim_source_map.items() if document.url in urls]
                sources.append({"url": document.url, "publisher": document.publisher, "claim_ids_used": claim_ids})
        validation_scores = {
            "passed": result.validation.passed if result.validation else False,
            "blocking_failures": len(result.validation.blocking_failures) if result.validation else 0,
            "structural_failures": len(result.validation.structural_failures) if result.validation else 0,
            "formatting_failures": len(result.validation.formatting_failures) if result.validation else 0,
        }
        return AuditRecord(
            article_id=f"{result.run_request.run_id}:{result.topic_candidate.cluster_key or slugify(result.topic_candidate.keyword)}",
            used_claim_ids=[claim.claim_id for claim in result.verified_claims],
            quarantined_claim_ids=[item.fingerprint for item in result.quarantine_items],
            source_urls=[document.url for document in result.raw_documents if document.url],
            validation_scores=validation_scores,
            sources=sources,
        )

    def save(self, result: WorkflowResult) -> dict[str, object]:
        story_id = result.writer_input.story_id if result.writer_input is not None else (result.topic_candidate.cluster_key or slugify(result.topic_candidate.keyword))
        attempt_number = result.draft.attempt_number if result.draft is not None else 1
        run_dir = self.runs_dir / result.run_request.run_id / story_id / f"attempt_{attempt_number}"
        raw_documents_dir = run_dir / "raw_documents"
        claim_candidates_dir = run_dir / "claim_candidates"
        atomic_claims_dir = run_dir / "atomic_claims"
        claim_clusters_dir = run_dir / "claim_clusters"
        raw_documents_dir.mkdir(parents=True, exist_ok=True)
        claim_candidates_dir.mkdir(parents=True, exist_ok=True)
        atomic_claims_dir.mkdir(parents=True, exist_ok=True)
        claim_clusters_dir.mkdir(parents=True, exist_ok=True)

        run_request_envelope = build_artifact_envelope(
            artifact_type="RunRequest",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="trigger",
            attempt_number=attempt_number,
            payload=result.run_request,
        )
        self._write_json(run_dir / "run_request.json", serialize(run_request_envelope))

        discovery_assessment = DiscoveryAssessment(
            discovery_score=result.topic_candidate.discovery_score,
            source_diversity_prior=result.topic_candidate.source_diversity_prior,
            selection_rank=result.topic_candidate.selection_rank,
            skipped_recent_topics=result.topic_candidate.skipped_recent_topics,
            duplicate_filter_exhausted=result.topic_candidate.duplicate_filter_exhausted,
        )
        discovery_envelope = build_artifact_envelope(
            artifact_type="DiscoveryAssessment",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="discovery",
            attempt_number=attempt_number,
            payload=discovery_assessment,
            parent_artifact_ids=[run_request_envelope.artifact_id],
        )
        self._write_json(run_dir / "discovery_assessment.json", serialize(discovery_envelope))

        topic_envelope = build_artifact_envelope(
            artifact_type="TopicCandidate",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="discovery",
            attempt_number=attempt_number,
            payload=result.topic_candidate,
            parent_artifact_ids=[discovery_envelope.artifact_id],
        )
        self._write_json(run_dir / "topic_candidate.json", serialize(topic_envelope))

        triage_envelope = build_artifact_envelope(
            artifact_type="TriageDecision",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="triage",
            attempt_number=attempt_number,
            payload=result.triage_decision,
            parent_artifact_ids=[topic_envelope.artifact_id],
        )
        self._write_json(run_dir / "triage_decision.json", serialize(triage_envelope))

        research_plan_envelope = build_artifact_envelope(
            artifact_type="ResearchPlan",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="research_routing",
            attempt_number=attempt_number,
            payload=result.research_plan,
            parent_artifact_ids=[triage_envelope.artifact_id],
        )
        self._write_json(run_dir / "research_plan.json", serialize(research_plan_envelope))

        raw_document_ids: list[str] = []
        for index, document in enumerate(result.raw_documents, start=1):
            envelope = build_artifact_envelope(
                artifact_type="RawDocument",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="fetching",
                attempt_number=attempt_number,
                payload=document,
                parent_artifact_ids=[research_plan_envelope.artifact_id],
            )
            raw_document_ids.append(envelope.artifact_id)
            self._write_json(raw_documents_dir / f"raw_document_{index}.json", serialize(envelope))

        claim_candidate_ids: list[str] = []
        for index, claim_candidate in enumerate(result.claim_candidates, start=1):
            envelope = build_artifact_envelope(
                artifact_type="AtomicClaimCandidate",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="candidate_extraction",
                attempt_number=attempt_number,
                payload=claim_candidate,
                parent_artifact_ids=raw_document_ids,
            )
            claim_candidate_ids.append(envelope.artifact_id)
            self._write_json(claim_candidates_dir / f"claim_candidate_{index}.json", serialize(envelope))

        claim_envelopes = []
        for index, claim in enumerate(result.claims, start=1):
            envelope = build_artifact_envelope(
                artifact_type="AtomicClaim",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="evidence",
                attempt_number=attempt_number,
                payload=claim,
                parent_artifact_ids=claim_candidate_ids or raw_document_ids,
            )
            claim_envelopes.append(envelope)
            self._write_json(atomic_claims_dir / f"atomic_claim_{index}.json", serialize(envelope))

        claim_cluster_ids: list[str] = []
        for index, cluster in enumerate(result.claim_clusters, start=1):
            envelope = build_artifact_envelope(
                artifact_type="ClaimCluster",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="clustering",
                attempt_number=attempt_number,
                payload=cluster,
                parent_artifact_ids=[envelope.artifact_id for envelope in claim_envelopes],
            )
            claim_cluster_ids.append(envelope.artifact_id)
            self._write_json(claim_clusters_dir / f"claim_cluster_{index}.json", serialize(envelope))

        verification_envelope = build_artifact_envelope(
            artifact_type="VerificationOutcome",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="verification",
            attempt_number=attempt_number,
            payload=result.verification,
            parent_artifact_ids=claim_cluster_ids or [envelope.artifact_id for envelope in claim_envelopes],
        )
        self._write_json(run_dir / "verification.json", serialize(verification_envelope))

        verified_envelope = build_artifact_envelope(
            artifact_type="VerifiedClaimsBank",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="verification",
            attempt_number=attempt_number,
            payload=result.verified_claims,
            parent_artifact_ids=[verification_envelope.artifact_id],
        )
        self._write_json(run_dir / "verified_claims.json", serialize(verified_envelope))

        quarantine_envelope = build_artifact_envelope(
            artifact_type="QuarantineSet",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="verification",
            attempt_number=attempt_number,
            payload=result.quarantine_items,
            parent_artifact_ids=[verified_envelope.artifact_id],
        )
        self._write_json(run_dir / "quarantine.json", serialize(quarantine_envelope))

        writer_input_envelope = None
        if result.writer_input is not None:
            writer_input_envelope = build_artifact_envelope(
                artifact_type="WriterInput",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="planning",
                attempt_number=attempt_number,
                payload=result.writer_input,
                parent_artifact_ids=[verified_envelope.artifact_id, quarantine_envelope.artifact_id],
            )
            self._write_json(run_dir / "writer_input.json", serialize(writer_input_envelope))

        draft_html_path = run_dir / "draft.html"
        draft_json_path = run_dir / "draft.json"
        validation_result_path = run_dir / "validation_result.json"
        audit_log_path = run_dir / "audit_log.json"
        metrics_path = run_dir / "metrics.json"
        if result.draft is not None:
            draft_html_path.write_text(result.draft.html, encoding="utf-8")
            self._write_json(draft_json_path, serialize(result.draft))

        validation_envelope = None
        if result.validation is not None:
            validation_envelope = build_artifact_envelope(
                artifact_type="ValidationResult",
                run_id=result.run_request.run_id,
                story_id=story_id,
                stage_name="validation",
                attempt_number=attempt_number,
                payload=result.validation,
                parent_artifact_ids=[writer_input_envelope.artifact_id] if writer_input_envelope is not None else [verified_envelope.artifact_id],
            )
            self._write_json(validation_result_path, serialize(validation_envelope))

        if result.audit_record is None:
            result.audit_record = self.build_audit_record(result)
        audit_envelope = build_artifact_envelope(
            artifact_type="AuditRecord",
            run_id=result.run_request.run_id,
            story_id=story_id,
            stage_name="publish",
            attempt_number=attempt_number,
            payload=result.audit_record,
            parent_artifact_ids=[validation_envelope.artifact_id] if validation_envelope is not None else [verified_envelope.artifact_id],
        )
        self._write_json(audit_log_path, serialize(audit_envelope))
        self._write_json(metrics_path, serialize(result.metrics))

        slug = slugify(result.topic_candidate.keyword)
        html_export_path = self.newsroom_dir / f"{slug}.html"
        markdown_export_path = self.newsroom_dir / f"{slug}.md"
        json_export_path = self.newsroom_dir / f"{slug}.json"
        if result.draft is not None:
            html_export_path.write_text(result.draft.html, encoding="utf-8")
            markdown_export_path.write_text(result.draft.markdown, encoding="utf-8")
        self._write_json(
            json_export_path,
            {
                "topic": result.topic_candidate.keyword,
                "topic_family": result.topic_candidate.topic_family,
                "verification_score": result.verification.verification_score if result.verification else None,
                "disposition": result.verification.disposition if result.verification else None,
                "validation": serialize(result.validation),
                "audit": serialize(result.audit_record),
            },
        )

        saved_paths: dict[str, object] = {
            "run_dir": str(run_dir),
            "audit_log": str(audit_log_path),
            "metrics": str(metrics_path),
            "json_export": str(json_export_path),
        }
        if validation_envelope is not None:
            saved_paths["validation_result"] = str(validation_result_path)
        if result.draft is not None:
            saved_paths["draft_html"] = str(draft_html_path)
            saved_paths["draft_json"] = str(draft_json_path)
            saved_paths["html_export"] = str(html_export_path)
            saved_paths["markdown_export"] = str(markdown_export_path)
            if self.config.wordpress_sync_enabled:
                artifact = self.wordpress_publisher.publish_newsroom_v3_draft(result, saved_paths)
                saved_paths["wordpress_sync"] = {
                    "synced": bool(artifact.wordpress_sync and artifact.wordpress_sync.synced),
                    "post_id": artifact.wordpress_sync.post_id if artifact.wordpress_sync else None,
                    "remote_status": artifact.wordpress_sync.remote_status if artifact.wordpress_sync else None,
                    "response_path": artifact.wordpress_sync.response_path if artifact.wordpress_sync else None,
                }
            else:
                self._record_publication(result)
        return saved_paths

    def _load_topic_registry(self) -> list[dict]:
        if not self.topic_registry_path.exists():
            return []
        try:
            payload = json.loads(self.topic_registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _record_publication(self, result: WorkflowResult) -> None:
        entries = self._load_topic_registry()
        entries.append(
            {
                "run_id": result.run_request.run_id,
                "keyword": result.topic_candidate.keyword,
                "cluster_key": result.topic_candidate.cluster_key or slugify(result.topic_candidate.keyword),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._write_json(self.topic_registry_path, entries)

    def _write_json(self, path: Path, payload) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")