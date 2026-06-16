from __future__ import annotations

import re

try:
    from serpapi import GoogleSearch
except ImportError:
    GoogleSearch = None

from config import AppConfig

from ..models import TrendSignal


def _safe_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").replace("+", "")
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])", text.upper())
    if not match:
        return None
    base = float(match.group(1))
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return int(base * scale)


def _normalize_topic_category(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()
    if not normalized:
        return None
    aliases = {
        "politics": "14",
        "political": "14",
        "business": "3",
        "stock market": "3",
        "stock market": "3",
        "stocks": "3",
        "tech": "18",
        "technology": "18",
        "sports": "17",
        "sport": "17",
        "travel": "19",
    }
    return aliases.get(normalized)


class SerpApiTrendFetcher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def fetch(self, country: str, limit: int) -> list[TrendSignal]:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return self.from_seed_topics([
                "AI agents",
                "OpenAI product launch",
                "NVIDIA earnings",
                "India digital payments",
                "Cloud security trends",
            ], limit, source="mock")

        params = {
            "engine": "google_trends_trending_now",
            "geo": country,
            "hours": self.config.trend_window_hours,
            "api_key": self.config.serpapi_key,
        }
        category_id = _normalize_topic_category(self.config.topic_category)
        if category_id is not None:
            params["category_id"] = category_id
        response = GoogleSearch(params).get_dict()
        return self.parse(response, limit)

    def parse(self, response: dict, limit: int) -> list[TrendSignal]:
        items = response.get("trending_searches", [])
        return [
            TrendSignal(
                keyword=item.get("query", "").strip(),
                traffic=_safe_int(item.get("search_volume")),
                source="google_trends",
            )
            for item in items[:limit]
            if str(item.get("query", "")).strip()
        ]

    def from_seed_topics(self, topics: list[str], limit: int, *, source: str = "seed") -> list[TrendSignal]:
        return [
            TrendSignal(keyword=topic, traffic=1_000_000 - (index * 50_000), source=source)
            for index, topic in enumerate(topics[:limit])
        ]