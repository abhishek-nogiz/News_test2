from __future__ import annotations

try:
    # CHANGED: was `from serpapi import GoogleSearch`
    # Now uses the instrumented drop-in wrapper that auto-counts every
    # SerpAPI call against the currently running pipeline stage.
    from ..serpapi_usage import InstrumentedGoogleSearch as GoogleSearch
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
        self.last_fetch_debug: dict[str, object] = {}

    def fetch(self, country: str, limit: int, *, allow_category_filter: bool = True) -> list[TrendTopic]:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            trends = self._mock_trends(limit)
            self.last_fetch_debug = {
                "mode": "mock",
                "geo": country,
                "hours": self.config.trend_window_hours,
                "topic_category": self.config.topic_category,
                "category_id": topic_category_trends_filter_id(self.config.topic_category) if allow_category_filter else None,
                "category_filter_applied": bool(allow_category_filter),
                "trending_count": len(trends),
            }
            return trends

        params = {
            "engine": "google_trends_trending_now",
            "geo": country,
            "hours": self.config.trend_window_hours,
            "api_key": self.config.serpapi_key,
        }
        category_id = topic_category_trends_filter_id(self.config.topic_category) if allow_category_filter else None
        if category_id is not None:
            params["category_id"] = category_id
        response = GoogleSearch(params).get_dict()
        raw_items = response.get("trending_searches", [])
        self.last_fetch_debug = {
            "mode": "serpapi",
            "geo": country,
            "hours": self.config.trend_window_hours,
            "topic_category": self.config.topic_category,
            "category_id": category_id,
            "category_filter_applied": category_id is not None,
            "trending_count": len(raw_items),
            "response_keys": sorted(response.keys()),
        }
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
            initial_debug = dict(self.service.last_fetch_debug or {})
            if (
                not trends
                and self.config.topic_category
                and initial_debug.get("category_filter_applied")
            ):
                self.logger.info(
                    context.run,
                    (
                        "Native category trend feed returned 0 topics; "
                        "retrying without category_id for graceful fallback"
                    ),
                )
                trends = self.service.fetch(
                    context.run.country,
                    context.run.max_topics,
                    allow_category_filter=False,
                )

        context.run.trends = trends
        self.logger.info(context.run, f"Collected {len(trends)} trend signals")
        if not context.seed_topics:
            debug = self.service.last_fetch_debug or {}
            response_keys = debug.get("response_keys", [])
            response_keys_text = ",".join(str(key) for key in response_keys) if response_keys else "none"
            top_keywords = ", ".join(topic.keyword for topic in trends[:5]) if trends else "none"
            self.logger.info(
                context.run,
                (
                    "Trend fetch debug: "
                    f"mode={debug.get('mode', 'unknown')} "
                    f"geo={debug.get('geo', context.run.country)} "
                    f"hours={debug.get('hours', self.config.trend_window_hours)} "
                    f"topic_category={debug.get('topic_category', self.config.topic_category)} "
                    f"category_id={debug.get('category_id')} "
                    f"category_filter_applied={debug.get('category_filter_applied')} "
                    f"raw_trending_count={debug.get('trending_count', 0)} "
                    f"response_keys={response_keys_text} "
                    f"top_keywords={top_keywords}"
                ),
            )
        self.logger.transition(context.run, "trends_fetched")
        self.publisher.save_trends(context.run.run_id, trends)
