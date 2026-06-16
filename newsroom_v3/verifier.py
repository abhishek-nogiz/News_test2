from __future__ import annotations

from collections import defaultdict

from .models import ClaimCluster, QuarantineItem, RawDocument, TopicCandidate, VerifiedClaim, VerificationOutcome


MIN_VERIFIED_CLAIMS_FOR_ARTICLE = 4
MIN_VERIFIED_CLAIMS_FOR_BRIEF = 2
CONFIDENCE_PENALTY_PER_FAILED_FETCH = 0.1


class VerificationService:
    def verify(
        self,
        *,
        candidate: TopicCandidate,
        claim_clusters: list[ClaimCluster],
        claims_by_id: dict[str, object],
        raw_documents: list[RawDocument],
    ) -> tuple[list[VerifiedClaim], list[QuarantineItem], VerificationOutcome]:
        document_by_url = {document.url: document for document in raw_documents if document.url}
        publisher_lookup = defaultdict(set)
        failed_fetch_count = 0
        for document in raw_documents:
            if document.extraction_status != "succeeded":
                failed_fetch_count += 1
                continue
            publisher_lookup[document.publisher.strip().casefold() or document.url].add(document.url)

        verified: list[VerifiedClaim] = []
        quarantine: list[QuarantineItem] = []
        confidence_penalty = round(failed_fetch_count * CONFIDENCE_PENALTY_PER_FAILED_FETCH, 2)

        for cluster in claim_clusters:
            supporting_urls: list[str] = []
            supporting_publishers: set[str] = set()
            supporting_tiers: set[str] = set()
            for claim_id in cluster.supporting_claim_ids:
                claim = claims_by_id.get(claim_id)
                if claim is None:
                    continue
                if getattr(claim, "source_url", None):
                    supporting_urls.append(claim.source_url)
                source_document = document_by_url.get(getattr(claim, "source_url", ""))
                if source_document is not None:
                    supporting_publishers.add(source_document.publisher.strip().casefold() or source_document.url)
                supporting_tiers.add(getattr(claim, "source_tier", "secondary"))

            unique_urls = list(dict.fromkeys(url for url in supporting_urls if url))
            has_packet_level_support = len(raw_documents) >= 2 and bool(unique_urls)
            is_verified = len(supporting_publishers) >= 2 or has_packet_level_support
            confidence = round(min(0.95, 0.55 + (0.1 * len(unique_urls)) - confidence_penalty), 2)

            if candidate.topic_family == "celebrity_personal_update" and len(supporting_publishers) < 2:
                is_verified = False

            if is_verified:
                verified.append(
                    VerifiedClaim(
                        claim_id=cluster.cluster_id,
                        text=cluster.canonical_claim,
                        status="verified",
                        confidence=max(0.1, confidence),
                        attribution="multiple_sources" if len(supporting_publishers) >= 2 else "packet_level_support",
                        supporting_sources=unique_urls,
                        contradictory_sources=[],
                        section=cluster.section,
                        fingerprint=cluster.fingerprint,
                    )
                )
                continue

            quarantine.append(
                QuarantineItem(
                    claim_text=cluster.canonical_claim,
                    reason="insufficient_support",
                    source_set=unique_urls,
                    contradiction_notes=[],
                    fingerprint=cluster.fingerprint,
                )
            )

        verification_score = round(len(verified) / max(1, len(verified) + len(quarantine)), 2)
        if len(verified) >= MIN_VERIFIED_CLAIMS_FOR_ARTICLE:
            disposition = "proceed"
        elif len(verified) >= MIN_VERIFIED_CLAIMS_FOR_BRIEF:
            disposition = "downgrade"
        else:
            disposition = "skip"

        outcome = VerificationOutcome(
            verification_score=verification_score,
            disposition=disposition,
            verified_count=len(verified),
            quarantine_count=len(quarantine),
            confidence_penalty=confidence_penalty,
        )
        candidate.verification_score = verification_score
        return verified, quarantine, outcome