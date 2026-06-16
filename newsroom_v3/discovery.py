from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path

from config import AppConfig

from .fetchers import SerpApiTrendFetcher
from .models import RunRequest, TopicCandidate, TrendSignal


class TrendDiscoveryService:
    GENERIC_TERMS = {
        "news", "latest", "update", "updates", "today", "live", "breaking", "election", "primary", "results",
    }
    STOP_WORDS = {
        "the", "a", "an", "vs", "live", "today", "news", "latest", "new", "update", "match", "score", "scorecard", "highlights", "2026", "2025",
    }
    LOW_MONETIZATION_HINTS = {"match", "score", "scorecard", "live", "highlights", "vs"}
    HIGH_MONETIZATION_HINTS = {
        "ai", "agent", "openai", "nvidia", "software", "startup", "cloud", "security", "market", "business", "finance", "payment", "saas", "tech",
    }
    CATEGORY_TOKENS = {
        "politics": {"election", "elections", "trump", "biden", "senate", "congress", "government", "policy", "vote", "voting", "campaign", "republican", "democrat"},
        "business": {"business", "economy", "company", "companies", "ceo", "deal", "revenue", "funding", "merger", "acquisition"},
        "tech": {"ai", "openai", "nvidia", "apple", "google", "meta", "microsoft", "software", "cloud", "security", "cybersecurity", "agent", "agents"},
        "stock_market": {"stock", "stocks", "market", "markets", "nasdaq", "dow", "sp500", "shares", "earnings", "finance", "ipo", "fed"},
        "sports": {"nba", "nfl", "nhl", "mlb", "ipl", "cricket", "football", "soccer", "tennis", "golf", "match", "playoffs", "score", "coach", "team"},
        "travel": {"travel", "tourism", "flight", "flights", "airline", "hotel", "visa", "airport", "destination", "trip", "vacation"},
    }

    def __init__(
        self,
        config: AppConfig,
        *,
        trend_fetcher: SerpApiTrendFetcher | None = None,
    ) -> None:
        self.config = config
        self.trend_fetcher = trend_fetcher or SerpApiTrendFetcher(config)
        self.topic_registry_path = Path(config.storage_root) / "cache" / "published_topics.json"

    def discover(self, request: RunRequest) -> list[TopicCandidate]:
        trends = self._fetch_trends(request)
        ranked = self._rank(trends)
        filtered = ranked if self._uses_native_trends_filter(request) else self._filter_by_category(ranked, request.category)

        recent_cluster_keys = self._recently_published_cluster_keys()
        skipped_recent_topics = 0
        candidates: list[TopicCandidate] = []
        for index, trend in enumerate(filtered, start=1):
            if trend.cluster_key and trend.cluster_key in recent_cluster_keys:
                skipped_recent_topics += 1
                continue
            candidates.append(self._to_candidate(trend, selection_rank=index, skipped_recent_topics=skipped_recent_topics))

        if candidates:
            return candidates

        if filtered and request.seed_topics:
            fallback = self._to_candidate(filtered[0], selection_rank=1, skipped_recent_topics=skipped_recent_topics)
            fallback.duplicate_filter_exhausted = True
            return [fallback]

        if filtered:
            raise RuntimeError("No new candidate topics were available for the newsroom_v3 workflow after duplicate filtering")

        raise RuntimeError("No candidate topics were available for the newsroom_v3 workflow")

    def _fetch_trends(self, request: RunRequest) -> list[TrendSignal]:
        if request.seed_topics:
            return self.trend_fetcher.from_seed_topics(request.seed_topics, self.config.max_topics)
        return self.trend_fetcher.fetch(request.country, self.config.max_topics)

    def _to_candidate(self, trend: TrendSignal, *, selection_rank: int, skipped_recent_topics: int) -> TopicCandidate:
        source_diversity_prior = self._source_diversity_prior(trend.keyword)
        discovery_score = round((trend.trend_score or 0.0) * (trend.freshness_score or 0.0) * source_diversity_prior, 4)
        return TopicCandidate(
            keyword=trend.keyword,
            cluster_key=trend.cluster_key,
            topic_source=trend.source,
            traffic=trend.traffic,
            trend_score=trend.trend_score,
            freshness_score=trend.freshness_score,
            source_diversity_prior=source_diversity_prior,
            discovery_score=discovery_score,
            selection_rank=selection_rank,
            skipped_recent_topics=skipped_recent_topics,
        )

    def _source_diversity_prior(self, keyword: str) -> float:
        tokens = {token for token in self._tokenize(keyword) if token not in self.GENERIC_TERMS}
        if not tokens:
            return 0.25
        return round(min(1.0, 0.25 + (min(len(tokens), 4) * 0.15)), 2)

    def _rank(self, trends: list[TrendSignal]) -> list[TrendSignal]:
        if not trends:
            return []
        max_traffic = max((trend.traffic or 0) for trend in trends) or 1
        deduped: dict[str, TrendSignal] = {}
        for trend in trends:
            cluster_key = self._cluster_key(trend.keyword)
            scored = TrendSignal(keyword=trend.keyword, traffic=trend.traffic, source=trend.source, cluster_key=cluster_key)
            scored.trend_score = min((trend.traffic or 0) / max_traffic, 1.0)
            scored.freshness_score = 1.0
            final_score = round((0.4 * scored.trend_score + 0.3 * scored.freshness_score + 0.2 * self._competition_score(trend.keyword) + 0.1 * self._monetization_score(trend.keyword)) * 100, 2)
            existing = deduped.get(cluster_key)
            if existing is None or final_score > (0.4 * existing.trend_score + 0.3 * existing.freshness_score + 0.2 * self._competition_score(existing.keyword) + 0.1 * self._monetization_score(existing.keyword)) * 100:
                deduped[cluster_key] = scored
        return sorted(deduped.values(), key=lambda item: ((0.4 * item.trend_score) + (0.3 * item.freshness_score) + (0.2 * self._competition_score(item.keyword)) + (0.1 * self._monetization_score(item.keyword))), reverse=True)

    def _filter_by_category(self, trends: list[TrendSignal], category: str | None) -> list[TrendSignal]:
        normalized = self._normalize_category(category)
        if normalized is None:
            return list(trends)
        allowed = self.CATEGORY_TOKENS.get(normalized)
        if not allowed:
            return list(trends)
        return [trend for trend in trends if set(self._tokenize(trend.keyword)) & allowed]

    def _cluster_key(self, keyword: str) -> str:
        tokens = [token for token in self._tokenize(keyword) if token not in self.STOP_WORDS]
        return " ".join(sorted(tokens)) or keyword.lower().strip()

    def _competition_score(self, keyword: str) -> float:
        tokens = set(self._tokenize(keyword))
        if tokens & self.LOW_MONETIZATION_HINTS:
            return 0.05
        if len(tokens) <= 2:
            return 0.7
        return 0.85

    def _monetization_score(self, keyword: str) -> float:
        tokens = set(self._tokenize(keyword))
        strong_match = len(tokens & self.HIGH_MONETIZATION_HINTS)
        if strong_match >= 2:
            return 1.0
        if strong_match == 1:
            return 0.8
        if tokens & self.LOW_MONETIZATION_HINTS:
            return 0.05
        return 0.55

    def _recently_published_cluster_keys(self) -> set[str]:
        if not self.topic_registry_path.exists():
            return set()
        try:
            payload = json.loads(self.topic_registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if isinstance(payload, list):
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.config.duplicate_lookback_hours)
            cluster_keys: set[str] = set()
            for item in payload:
                if not isinstance(item, dict):
                    continue
                cluster_key = str(item.get("cluster_key") or "").strip()
                published_at = str(item.get("published_at") or "").strip()
                if not cluster_key or not published_at:
                    continue
                try:
                    published_time = datetime.fromisoformat(published_at)
                except ValueError:
                    continue
                if published_time >= cutoff:
                    cluster_keys.add(cluster_key)
            return cluster_keys
        return set()

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9]+", (text or "").lower())

    def _normalize_category(self, category: str | None) -> str | None:
        if category is None:
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", category.strip().lower()).strip("_")
        aliases = {
            "political": "politics",
            "technology": "tech",
            "stock_market": "stock_market",
            "stocks": "stock_market",
            "sport": "sports",
        }
        return aliases.get(normalized, normalized)

    def _uses_native_trends_filter(self, request: RunRequest) -> bool:
        if request.seed_topics:
            return False
        normalized = self._normalize_category(request.category)
        return normalized in self.CATEGORY_TOKENS