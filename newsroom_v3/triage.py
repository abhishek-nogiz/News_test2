from __future__ import annotations

from config import AppConfig

from .llm_gateway import LLMGateway
from .models import TriageDecision, TopicCandidate


class TopicTriageService:
    def __init__(self, config: AppConfig, *, gateway: LLMGateway | None = None) -> None:
        self.config = config
        self.gateway = gateway or LLMGateway(config)

    def classify(self, candidate: TopicCandidate) -> TriageDecision:
        result = self.gateway.classify_topic_family(candidate.keyword, category=self.config.topic_category)
        candidate.topic_family = result.topic_family
        candidate.topic_family_confidence = result.confidence
        candidate.triage_reasoning = result.reasoning
        candidate.triage_decision = "proceed"
        return TriageDecision(
            topic_family=result.topic_family,
            decision="proceed",
            confidence=result.confidence,
            reasoning=result.reasoning,
        )

    def post_verification_disposition(
        self,
        *,
        candidate: TopicCandidate,
        verified_claim_count: int,
        article_threshold: int,
        brief_threshold: int,
    ) -> str:
        if verified_claim_count >= article_threshold:
            return "proceed"
        if verified_claim_count >= brief_threshold:
            candidate.triage_decision = "downgrade"
            return "downgrade"
        candidate.triage_decision = "skip"
        return "skip"