from __future__ import annotations

from dataclasses import dataclass
import re

from config import AppConfig
from news_agent.services.helpers import tokenize

from .models import ClaimCandidateFragment, RawDocument


class LLMGatewayError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    raw_truncated: bool = False


@dataclass(slots=True)
class TriageResult:
    topic_family: str
    confidence: float
    reasoning: str


@dataclass(slots=True)
class ClaimCandidates:
    candidates: list[ClaimCandidateFragment]
    status: str = "candidate"


class LLMGateway:
    POLITICS_TOKENS = {
        "election", "elections", "vote", "voting", "senate", "house", "campaign", "court", "judge",
        "policy", "government", "trump", "biden", "legal", "lawsuit", "runoff", "politics",
    }
    SPORTS_TOKENS = {
        "nfl", "nba", "mlb", "nhl", "game", "match", "series", "playoffs", "final", "retirement",
        "touchdown", "goal", "coach", "sports", "score", "standings",
    }
    BUSINESS_TOKENS = {
        "earnings", "revenue", "market", "markets", "shares", "stocks", "guidance", "ipo", "merger",
        "business", "company", "finance",
    }
    CELEBRITY_TOKENS = {
        "actor", "actress", "singer", "celebrity", "dating", "married", "pregnant", "health update",
    }
    SCIENCE_TOKENS = {
        "study", "research", "science", "health", "trial", "hospital", "disease", "diagnosis", "journal",
        "preprint", "treatment", "medical",
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def classify_topic_family(self, topic: str, *, category: str | None = None) -> TriageResult:
        tokens = set(tokenize(topic))
        normalized_category = (category or "").strip().lower()
        if normalized_category == "politics":
            return TriageResult("politics_legal_policy", 0.95, "Configured category maps the topic to politics/legal/policy.")

        if tokens & self.POLITICS_TOKENS:
            return TriageResult("politics_legal_policy", 0.8, "Topic tokens align with politics, law, or public-policy coverage.")
        if tokens & self.SPORTS_TOKENS:
            return TriageResult("sports_results_standings", 0.8, "Topic tokens align with sports, results, or standings coverage.")
        if tokens & self.BUSINESS_TOKENS:
            return TriageResult("business_earnings_markets", 0.75, "Topic tokens align with business, earnings, or market coverage.")
        if tokens & self.CELEBRITY_TOKENS:
            return TriageResult("celebrity_personal_update", 0.7, "Topic tokens align with celebrity or personal-update coverage.")
        if tokens & self.SCIENCE_TOKENS:
            return TriageResult("science_health_research", 0.75, "Topic tokens align with science, health, or research coverage.")
        return TriageResult("default_unclassified", 0.5, "The topic did not strongly match a named family, so it falls back to the default branch.")

    def extract_claim_candidates(self, raw_documents: list[RawDocument]) -> ClaimCandidates:
        candidates: list[ClaimCandidateFragment] = []
        for document in raw_documents:
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", document.cleaned_text or "") if part.strip()]
            for sentence in sentences[:2]:
                if len(sentence.split()) < 6:
                    continue
                candidates.append(
                    ClaimCandidateFragment(
                        text=sentence.rstrip(".!?"),
                        claim_type="fact",
                        evidence_span=sentence[:180],
                        section="present",
                    )
                )
        return ClaimCandidates(candidates=candidates, status="candidate")