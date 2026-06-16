from __future__ import annotations

try:
    from serpapi import GoogleSearch
except ImportError:
    GoogleSearch = None

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import safe_int, topic_category_trends_filter_id


class TrendAcquisitionService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def fetch(self, country: str, limit: int) -> list[TrendTopic]:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return self._mock_trends(limit)

        params = {
            "engine": "google_trends_trending_now",
            "geo": country,
            "hours": self.config.trend_window_hours,
            "api_key": self.config.serpapi_key,
        }
        category_id = topic_category_trends_filter_id(self.config.topic_category)
        if category_id is not None:
            params["category_id"] = category_id
        response = GoogleSearch(params).get_dict()
        return self.parse(response, limit)

    def parse(self, response: dict, limit: int) -> list[TrendTopic]:
        items = response.get("trending_searches", [])
        trends: list[TrendTopic] = []

        for item in items[:limit]:
            trends.append(
                TrendTopic(
                    keyword=item.get("query", "").strip(),
                    traffic=safe_int(item.get("search_volume")),
                    source="google_trends",
                )
            )
        return trends

    def from_seed_topics(self, topics: list[str], limit: int) -> list[TrendTopic]:
        return [
            TrendTopic(keyword=topic, traffic=1_000_000 - (index * 50_000), source="seed")
            for index, topic in enumerate(topics[:limit])
        ]

    def _mock_trends(self, limit: int) -> list[TrendTopic]:
        demo = [
            TrendTopic(keyword="AI agents", traffic=2_000_000, source="mock"),
            TrendTopic(keyword="OpenAI product launch", traffic=1_700_000, source="mock"),
            TrendTopic(keyword="NVIDIA earnings", traffic=1_500_000, source="mock"),
            TrendTopic(keyword="India digital payments", traffic=950_000, source="mock"),
            TrendTopic(keyword="Cloud security trends", traffic=870_000, source="mock"),
        ]
        return demo[:limit]


class TrendAgent(BaseAgent):
    stage_name = "trends"

    def __init__(self, service: TrendAcquisitionService, publisher, logger: PipelineLogger, config: AppConfig) -> None:
        self.service = service
        self.publisher = publisher
        self.logger = logger
        self.config = config

    def execute(self, context: AgentContext) -> None:
        if context.run is None:
            raise RuntimeError("Run context is missing before trend acquisition")

        if context.seed_topics:
            trends = self.service.from_seed_topics(context.seed_topics, self.config.max_topics)
        else:
            trends = self.service.fetch(context.run.country, context.run.max_topics)

        context.run.trends = trends
        self.logger.info(context.run, f"Collected {len(trends)} trend signals")
        self.logger.transition(context.run, "trends_fetched")
        self.publisher.save_trends(context.run.run_id, trends)