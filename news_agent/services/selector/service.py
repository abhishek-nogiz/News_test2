from __future__ import annotations

from ...core.logger import PipelineLogger
from ...models import TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import display_topic_category, tokenize, topic_category_trends_filter_id, topic_matches_category


class TopicIntelligenceService:
    STOP_WORDS = {
        "the", "a", "an", "vs", "live", "today", "news", "latest", "new", "update",
        "match", "score", "scorecard", "highlights", "2026", "2025",
    }
    LOW_MONETIZATION_HINTS = {"match", "score", "scorecard", "live", "highlights", "vs"}
    HIGH_MONETIZATION_HINTS = {
        "ai", "agent", "openai", "nvidia", "software", "startup", "cloud", "security",
        "market", "business", "finance", "payment", "saas", "tech",
    }

    def rank(self, trends: list[TrendTopic]) -> list[TrendTopic]:
        if not trends:
            return []

        max_traffic = max((trend.traffic or 0) for trend in trends) or 1
        deduped: dict[str, TrendTopic] = {}

        for trend in trends:
            cluster_key = self._cluster_key(trend.keyword)
            scored = TrendTopic(
                keyword=trend.keyword,
                traffic=trend.traffic,
                source=trend.source,
                cluster_key=cluster_key,
            )
            scored.trend_score = min((trend.traffic or 0) / max_traffic, 1.0)
            scored.freshness_score = 1.0
            scored.competition_score = self._competition_score(trend.keyword)
            scored.monetization_score = self._monetization_score(trend.keyword)
            scored.final_score = round(
                (
                    0.4 * scored.trend_score
                    + 0.3 * scored.freshness_score
                    + 0.2 * scored.competition_score
                    + 0.1 * scored.monetization_score
                )
                * 100,
                2,
            )

            existing = deduped.get(cluster_key)
            if existing is None or scored.final_score > existing.final_score:
                deduped[cluster_key] = scored

        return sorted(deduped.values(), key=lambda item: item.final_score, reverse=True)

    def select(self, trends: list[TrendTopic]) -> TrendTopic | None:
        ranked = self.rank(trends)
        return ranked[0] if ranked else None

    def filter_by_category(self, trends: list[TrendTopic], category: str | None) -> list[TrendTopic]:
        if category is None:
            return list(trends)
        return [trend for trend in trends if topic_matches_category(trend.keyword, category)]

    def _cluster_key(self, keyword: str) -> str:
        tokens = [token for token in tokenize(keyword) if token not in self.STOP_WORDS]
        return " ".join(sorted(tokens)) or keyword.lower().strip()

    def _competition_score(self, keyword: str) -> float:
        tokens = set(tokenize(keyword))
        if tokens & self.LOW_MONETIZATION_HINTS:
            return 0.05
        if len(tokens) <= 2:
            return 0.7
        return 0.85

    def _monetization_score(self, keyword: str) -> float:
        tokens = set(tokenize(keyword))
        strong_match = len(tokens & self.HIGH_MONETIZATION_HINTS)
        if strong_match >= 2:
            return 1.0
        if strong_match == 1:
            return 0.8
        if tokens & self.LOW_MONETIZATION_HINTS:
            return 0.05
        return 0.55


class SelectorAgent(BaseAgent):
    stage_name = "selector"

    def __init__(self, service: TopicIntelligenceService, publisher, logger: PipelineLogger, config=None) -> None:
        self.service = service
        self.publisher = publisher
        self.logger = logger
        self.config = config

    def execute(self, context: AgentContext) -> None:
        if context.run is None:
            raise RuntimeError("Run context is missing before topic selection")

        ranked = self.service.rank(context.run.trends)
        topic_category = self.config.topic_category if self.config is not None else None
        uses_native_trends_filter = bool(
            topic_category
            and context.seed_topics is None
            and topic_category_trends_filter_id(topic_category) is not None
        )
        category_filtered = ranked if uses_native_trends_filter else self.service.filter_by_category(ranked, topic_category)
        if topic_category:
            category_label = display_topic_category(self.config.topic_category) or self.config.topic_category
            self.logger.info(context.run, f"Requested topic filter: {category_label}")
            if not category_filtered:
                raise RuntimeError(
                    f"No {category_label} topics were found in the current {context.run.country} trend feed"
                )
            if uses_native_trends_filter:
                self.logger.info(
                    context.run,
                    f"Using Google Trends native {category_label} category filter before topic selection",
                )
            else:
                self.logger.info(
                    context.run,
                    f"Kept {len(category_filtered)} {category_label} topics after topical filtering",
                )

        recently_published = self.publisher.recently_published_cluster_keys()
        filtered_ranked = [trend for trend in category_filtered if trend.cluster_key not in recently_published]
        if recently_published:
            skipped = len(category_filtered) - len(filtered_ranked)
            if skipped > 0:
                self.logger.info(context.run, f"Skipped {skipped} recently published topics")

        selected_topic = filtered_ranked[0] if filtered_ranked else None
        if selected_topic is None:
            raise RuntimeError("No topic could be selected from the trend feed after duplicate filtering")

        context.run.trends = filtered_ranked or category_filtered
        context.run.selected_topic = selected_topic
        self.logger.info(context.run, f"Selected topic: {selected_topic.keyword}")
        self.logger.transition(context.run, "topic_selected")