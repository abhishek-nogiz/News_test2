from __future__ import annotations

import re

from news_agent.services.helpers import slugify

from .models import ContextReference, RawDocument, TopicCandidate, VerifiedClaim, WriterInput


class PlanningService:
    def build(
        self,
        candidate: TopicCandidate,
        verified_claims: list[VerifiedClaim],
        raw_documents: list[RawDocument],
        context_references: list[ContextReference] | None = None,
    ) -> WriterInput:
        article_type = "article" if len(verified_claims) >= 4 else "brief"
        story_id = candidate.cluster_key or slugify(candidate.keyword)
        headline = self._headline(candidate, verified_claims)
        lead_claim = verified_claims[0]
        max_total_claims = max(2, min(len(verified_claims), 6))
        ranked_body_claims = sorted(verified_claims[1:], key=self._body_claim_priority, reverse=True)
        body_claims = self._select_body_claims(lead_claim, ranked_body_claims, max_claims=max_total_claims - 1)
        ordered_claims = [lead_claim, *body_claims]
        section_heads = self._section_heads(candidate, lead_claim=lead_claim, body_claims=body_claims, raw_documents=raw_documents)
        section_claim_ids = self._section_claim_ids(body_claims)
        claim_lookup = {claim.claim_id: claim.text for claim in ordered_claims}
        claim_source_map = {claim.claim_id: claim.supporting_sources for claim in ordered_claims}
        referenced_urls = {url for urls in claim_source_map.values() for url in urls}
        sources = [document for document in raw_documents if document.url in referenced_urls] or raw_documents[:3]
        return WriterInput(
            story_id=story_id,
            article_type=article_type,
            headline=headline,
            target_length=500 if article_type == "article" else 250,
            lead_claim_id=lead_claim.claim_id,
            section_heads=section_heads,
            section_claim_ids=section_claim_ids,
            claim_lookup=claim_lookup,
            claim_source_map=claim_source_map,
            sources=sources,
            context_references=list(context_references or []),
        )

    def _headline(self, candidate: TopicCandidate, verified_claims: list[VerifiedClaim]) -> str:
        if verified_claims:
            headline = verified_claims[0].text.rstrip(".!?")
            if len(headline.split()) <= 12:
                return headline
        return candidate.keyword.title().rstrip(".!?")

    def _section_heads(
        self,
        candidate: TopicCandidate,
        *,
        lead_claim: VerifiedClaim,
        body_claims: list[VerifiedClaim],
        raw_documents: list[RawDocument],
    ) -> list[str]:
        primary_claim = body_claims[0] if body_claims else lead_claim
        main_heading = self._heading_from_claim(primary_claim.text)
        corpus = " ".join(
            [
                candidate.keyword,
                lead_claim.text,
                *(claim.text for claim in body_claims[:2]),
                *(document.title for document in raw_documents[:3]),
            ]
        ).casefold()

        if candidate.topic_family == "politics_legal_policy":
            return [main_heading, self._politics_implication_heading(corpus)]
        if candidate.topic_family == "sports_results_standings":
            return [main_heading, self._sports_implication_heading(corpus)]
        if candidate.topic_family == "business_earnings_markets":
            return [main_heading, "What It Means for Markets"]
        if candidate.topic_family == "science_health_research":
            return [main_heading, "Why the Finding Matters"]
        return [main_heading, self._general_implication_heading(corpus)]

    def _heading_from_claim(self, text: str) -> str:
        lowered = text.casefold()
        if any(token in lowered for token in {"legal battle", "lawsuit", "injunction", "appeal"}):
            return "Where the Legal Fight Moves Next"
        if "shifts to" in lowered:
            return "What Happens Next"
        first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
        cleaned = re.sub(r"\s+", " ", first_sentence).strip(" -:|,;")
        words = cleaned.split()
        if len(words) > 12:
            cleaned = " ".join(words[:12]).rstrip(" ,;:")
        return cleaned.rstrip(".!?") or "The Main Development"

    def _politics_implication_heading(self, corpus: str) -> str:
        if any(token in corpus for token in {"redistrict", "district map", "congressional map", "gerrymander"}):
            return "What This Means for the Map Fight"
        if "republican" in corpus and "democrat" not in corpus and "democratic" not in corpus:
            return "What This Means for the Republican Party"
        if "democrat" in corpus or "democratic" in corpus:
            return "What This Means for Democrats"
        if "supreme court" in corpus or "court ruling" in corpus or "justice" in corpus or "judge" in corpus:
            return "Why the Ruling Matters Now"
        if any(token in corpus for token in {"budget", "bill", "policy", "lawmakers", "congress"}):
            return "What This Means in Washington"
        if any(token in corpus for token in {"senate", "house", "governor", "primary", "runoff", "election", "race"}):
            return "What This Means for the Race Ahead"
        if "trump" in corpus:
            return "What This Means for Trump's Political Push"
        return "Why This Political Fight Matters Now"

    def _sports_implication_heading(self, corpus: str) -> str:
        if any(token in corpus for token in {"game 7", "game 6", "game 5", "game 4"}):
            return "What This Game Means for the Series"
        if any(token in corpus for token in {"western conference finals", "eastern conference finals", "finals"}):
            return "What It Means for the Series"
        if any(token in corpus for token in {"playoffs", "playoff"}):
            return "What It Means for the Playoff Race"
        return "What It Means for the Matchup"

    def _general_implication_heading(self, corpus: str) -> str:
        if any(token in corpus for token in {"court", "ruling", "judge", "supreme"}):
            return "Why the Ruling Matters"
        if any(token in corpus for token in {"lawsuit", "charges", "trial", "investigation"}):
            return "Why the Case Matters"
        if any(token in corpus for token in {"release", "launch", "debut", "rollout", "update"}):
            return "Why This Release Matters"
        return "Why This Matters Now"

    def _select_distinct_claims(self, verified_claims: list[VerifiedClaim], *, max_claims: int) -> list[VerifiedClaim]:
        selected: list[VerifiedClaim] = []
        for claim in verified_claims:
            if any(self._are_near_duplicate_claims(claim, existing) for existing in selected):
                continue
            selected.append(claim)
            if len(selected) >= max_claims:
                return selected

        minimum_claims = min(max_claims, len(verified_claims), 2)
        if len(selected) >= minimum_claims:
            return selected

        for claim in verified_claims:
            if any(existing.claim_id == claim.claim_id for existing in selected):
                continue
            selected.append(claim)
            if len(selected) >= minimum_claims:
                break
        return selected

    def _select_body_claims(self, lead_claim: VerifiedClaim, verified_claims: list[VerifiedClaim], *, max_claims: int) -> list[VerifiedClaim]:
        selected: list[VerifiedClaim] = []
        comparison_set: list[VerifiedClaim] = [lead_claim]
        for claim in verified_claims:
            if self._body_claim_priority(claim) < 0:
                continue
            if any(self._are_near_duplicate_claims(claim, existing) for existing in comparison_set):
                continue
            selected.append(claim)
            comparison_set.append(claim)
            if len(selected) >= max_claims:
                return selected

        minimum_claims = min(max_claims, len(verified_claims), 1)
        if len(selected) >= minimum_claims:
            return selected

        for claim in verified_claims:
            if any(existing.claim_id == claim.claim_id for existing in selected):
                continue
            selected.append(claim)
            if len(selected) >= minimum_claims:
                break
        return selected

    def _body_claim_priority(self, claim: VerifiedClaim) -> int:
        lowered = claim.text.casefold()
        score = 0
        if any(token in lowered for token in {"judge", "court", "order", "vote", "election", "mail", "ballot"}):
            score += 2
        if any(token in lowered for token in {"kept", "keep", "refuses", "refused", "rejects", "rejected", "declined", "blocked", "block", "ruled", "ruling", "signed", "issued", "filed"}):
            score += 4
        if any(token in lowered for token in {"shifts", "lawsuit", "appeal", "next", "ahead", "impact", "matters", "means", "seeking"}):
            score += 3
        if any(token in lowered for token in {"years", "falsely", "without evidence", "history", "previously", "after his defeat", "independent reviews"}):
            score -= 4

        word_count = len(claim.text.split())
        if 10 <= word_count <= 28:
            score += 2
        elif word_count > 35:
            score -= 1
        return score

    def _are_near_duplicate_claims(self, left: VerifiedClaim, right: VerifiedClaim) -> bool:
        if left.section != right.section:
            return False

        left_tokens = self._claim_signature_tokens(left.text)
        right_tokens = self._claim_signature_tokens(right.text)
        if not left_tokens or not right_tokens:
            return False

        overlap = len(left_tokens & right_tokens)
        threshold = max(5, min(len(left_tokens), len(right_tokens)) // 2)
        return overlap >= threshold

    def _claim_signature_tokens(self, text: str) -> set[str]:
        first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
        stopwords = {
            "that", "this", "with", "from", "have", "has", "were", "will", "into", "while", "where", "which", "their",
            "after", "before", "under", "than", "they", "them", "then", "also", "amid", "over", "more", "than", "now",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9']+", first_sentence.casefold())
            if len(token) > 3 and token not in stopwords
        }

    def _section_claim_ids(self, body_claims: list[VerifiedClaim]) -> list[list[str]]:
        lanes: list[list[VerifiedClaim]] = [[], []]
        overflow: list[VerifiedClaim] = []
        for claim in body_claims:
            lane = self._claim_lane(claim)
            if lane is None:
                overflow.append(claim)
                continue
            lanes[lane].append(claim)

        for claim in overflow:
            lanes[0].append(claim)

        if not lanes[0] and lanes[1]:
            lanes[0].append(lanes[1].pop(0))

        if not lanes[1] and len(lanes[0]) > 1:
            lanes[1].extend(lanes[0][1:3])
            lanes[0] = lanes[0][:1]

        return [[claim.claim_id for claim in lane[:3]] for lane in lanes]

    def _claim_lane(self, claim: VerifiedClaim) -> int | None:
        lowered = claim.text.casefold()
        if claim.section == "future":
            return 1
        if claim.section == "past":
            return 0
        if any(token in lowered for token in {"means", "matters", "impact", "stakes", "next", "ahead", "battle", "lawsuit", "appeal", "series", "playoff", "race", "market"}):
            return 1
        if any(token in lowered for token in {"announced", "approved", "blocked", "refused", "rejected", "won", "lost", "passed", "signed", "ruled"}):
            return 0
        return None