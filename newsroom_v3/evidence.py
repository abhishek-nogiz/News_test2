from __future__ import annotations

from collections import defaultdict
import re
import uuid
from urllib.parse import urlsplit

from .llm_gateway import LLMGateway
from .models import AtomicClaim, AtomicClaimCandidate, ClaimCluster, RawDocument, ResearchBundle, TopicCandidate


class EvidenceService:
    def __init__(self, gateway: LLMGateway) -> None:
        self.gateway = gateway

    def build_raw_documents(self, research: ResearchBundle) -> list[RawDocument]:
        documents: list[RawDocument] = []
        for source in research.sources:
            cleaned_text = " ".join((source.content or source.snippet or source.title or "").split()).strip()
            documents.append(
                RawDocument(
                    url=source.url,
                    title=source.title,
                    publisher=source.publisher,
                    fetched_by=source.fetched_by,
                    fetched_at=source.fetched_at,
                    cleaned_text=cleaned_text,
                    extraction_status=source.extraction_status if source.extraction_status else ("succeeded" if cleaned_text else "failed"),
                    published_at=source.published_at,
                    source_tier=source.source_tier,
                )
            )
        return documents

    def extract_claim_candidates(
        self,
        candidate: TopicCandidate,
        research: ResearchBundle,
        raw_documents: list[RawDocument],
    ) -> list[AtomicClaimCandidate]:
        claim_candidates: list[AtomicClaimCandidate] = []
        for claim in research.claims:
            normalized_text = claim.claim.strip()
            if not normalized_text:
                continue
            claim_candidates.append(
                AtomicClaimCandidate(
                    claim_id=f"cand-{uuid.uuid4().hex[:12]}",
                    text=normalized_text,
                    section=claim.section.strip() or "present",
                    claim_type="fact",
                    evidence_span=normalized_text[:180],
                    fingerprint=self.fingerprint(normalized_text),
                    source_url=claim.source_url,
                    source_title=claim.source_title,
                    source_tier=claim.source_tier,
                )
            )

        llm_candidates = self.gateway.extract_claim_candidates(raw_documents)
        for fragment in llm_candidates.candidates:
            fingerprint = self.fingerprint(fragment.text)
            if not fingerprint or any(existing.fingerprint == fingerprint for existing in claim_candidates):
                continue
            claim_candidates.append(
                AtomicClaimCandidate(
                    claim_id=f"cand-{uuid.uuid4().hex[:12]}",
                    text=fragment.text.strip(),
                    section=fragment.section,
                    claim_type=fragment.claim_type,
                    evidence_span=fragment.evidence_span,
                    fingerprint=fingerprint,
                )
            )

        for section, values in (("present", research.present), ("past", research.past), ("future", research.future)):
            for value in values:
                normalized_text = value.strip()
                fingerprint = self.fingerprint(normalized_text)
                if not normalized_text or any(existing.fingerprint == fingerprint for existing in claim_candidates):
                    continue
                claim_candidates.append(
                    AtomicClaimCandidate(
                        claim_id=f"cand-{uuid.uuid4().hex[:12]}",
                        text=normalized_text,
                        section=section,
                        claim_type="fact",
                        evidence_span=normalized_text[:180],
                        fingerprint=fingerprint,
                    )
                )

        return claim_candidates

    def normalize_claims(self, candidates: list[AtomicClaimCandidate]) -> list[AtomicClaim]:
        normalized: list[AtomicClaim] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            key = (candidate.section, candidate.fingerprint)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                AtomicClaim(
                    claim_id=candidate.claim_id.replace("cand-", "claim-"),
                    text=candidate.text,
                    section=candidate.section,
                    claim_type=candidate.claim_type,
                    evidence_span=candidate.evidence_span,
                    fingerprint=candidate.fingerprint,
                    source_url=candidate.source_url,
                    source_title=candidate.source_title,
                    source_tier=candidate.source_tier,
                )
            )
        return normalized

    def cluster_claims(self, claims: list[AtomicClaim]) -> list[ClaimCluster]:
        grouped: dict[tuple[str, str], list[AtomicClaim]] = defaultdict(list)
        for claim in claims:
            grouped[(claim.section, claim.fingerprint)].append(claim)

        clusters: list[ClaimCluster] = []
        for (section, _fingerprint), section_claims in grouped.items():
            canonical = section_claims[0]
            clusters.append(
                ClaimCluster(
                    cluster_id=f"cluster-{uuid.uuid4().hex[:12]}",
                    canonical_claim=canonical.text,
                    fingerprint=canonical.fingerprint,
                    section=section,
                    supporting_claim_ids=[claim.claim_id for claim in section_claims],
                    contradictory_claim_ids=[],
                )
            )
        return clusters

    def source_domains(self, raw_documents: list[RawDocument]) -> dict[str, str]:
        return {
            document.url: urlsplit(document.url).netloc.casefold().removeprefix("www.")
            for document in raw_documents
            if document.url
        }

    @staticmethod
    def fingerprint(value: str) -> str:
        lowered = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())
        return " ".join(lowered.split())