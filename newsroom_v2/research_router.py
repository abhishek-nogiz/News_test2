from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from news_agent.models import ResearchPacket, ResearchSource, TrendTopic

from .models import EditorialDecision


class TavilySearchClient:
    def __init__(self, api_key: str | None = None, *, mock_mode: bool = False, timeout: int = 20) -> None:
        self.api_key = api_key or os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_API")
        self.mock_mode = mock_mode
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.mock_mode

    def search(self, query: str, *, topic_category: str | None = None, max_results: int = 3) -> list[ResearchSource]:
        if not self.available or not query.strip():
            return []

        payload = {
            "api_key": self.api_key,
            "query": query,
            "topic": "news",
            "search_depth": "advanced",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        if topic_category:
            payload["include_domains"] = []

        request = Request(
            "https://api.tavily.com/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw_payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            return []

        results = raw_payload.get("results", []) if isinstance(raw_payload, dict) else []
        sources: list[ResearchSource] = []
        for item in results[:max_results]:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or url).strip()
            content = str(item.get("content") or item.get("raw_content") or "").strip()
            publisher = str(item.get("source") or "").strip() or self._publisher_from_url(url)
            published_at = str(item.get("published_date") or item.get("date") or "").strip()
            sources.append(
                ResearchSource(
                    title=title,
                    url=url,
                    snippet=content[:280],
                    publisher=publisher,
                    published_at=published_at,
                    content=content[:3000],
                    source_tier="secondary",
                )
            )
        return sources

    def _publisher_from_url(self, url: str) -> str:
        hostname = urlsplit(url).netloc.casefold().removeprefix("www.")
        return hostname


class NewsroomResearchRouter:
    ENRICHMENT_GAPS = {"chronology", "clear consequence", "reporting depth"}
    LOW_VALUE_DOMAINS = {"msn.com", "newsbreak.com", "yahoo.com"}
    PROFILE_PATH_HINTS = {"/author/", "/authors/", "/profile/", "/profiles/", "/bio/", "/staff/"}
    VIDEO_PATH_HINTS = {"/video/", "/videos/", "/watch/"}
    LIVE_PATH_HINTS = {"/live/", "/live-blog/", "/live-updates/"}
    PRIMARY_HINTS = {".gov", ".mil", ".edu", "whitehouse.gov", "justice.gov", "sec.gov", "supremecourt.gov", "senate.gov", "house.gov"}
    WIRE_HINTS = {"reuters", "associated press", "ap news", "apnews", "afp", "bloomberg"}
    MAINSTREAM_HINTS = {
        "cnn", "the new york times", "nytimes", "washington post", "washingtonpost", "wall street journal", "wsj",
        "bbc", "nbc", "abc", "cbs", "politico", "axios", "the guardian", "guardian", "fox news",
    }

    def __init__(self, config, tavily_client: TavilySearchClient | None = None) -> None:
        self.config = config
        self.tavily_client = tavily_client or TavilySearchClient(mock_mode=config.mock_mode)

    def prioritize(
        self,
        topic: TrendTopic,
        research: ResearchPacket,
        *,
        country: str,
        rebuild_packet,
    ) -> tuple[ResearchPacket, bool]:
        prioritized_sources = self._prioritize_sources(research.sources)
        if not self._sources_changed(research.sources, prioritized_sources):
            return research, False
        return rebuild_packet(topic, prioritized_sources, country=country), True

    def filter_sources(
        self,
        topic: TrendTopic,
        research: ResearchPacket,
        *,
        country: str,
        rebuild_packet,
    ) -> tuple[ResearchPacket, int, list[str]]:
        prioritized_sources = self._prioritize_sources(research.sources)
        kept_sources: list[ResearchSource] = []
        notes: list[str] = []
        seen_domains: set[str] = set()
        duplicate_domain_candidates: list[ResearchSource] = []

        for source in prioritized_sources:
            reason = self._rejection_reason(source, seen_domains)
            if reason is not None:
                if reason == "duplicate_domain":
                    duplicate_domain_candidates.append(source)
                    continue
                notes.append(f"{source.publisher or self._source_domain(source.url) or source.url}: {reason}")
                continue
            kept_sources.append(source)
            domain = self._source_domain(source.url)
            if domain:
                seen_domains.add(domain)

        minimum_context_sources = min(3, len(prioritized_sources))
        domain_counts: dict[str, int] = {}
        for source in kept_sources:
            domain = self._source_domain(source.url)
            if not domain:
                continue
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        if len(kept_sources) < minimum_context_sources:
            for source in duplicate_domain_candidates:
                domain = self._source_domain(source.url)
                if domain and domain_counts.get(domain, 0) >= 2:
                    continue
                kept_sources.append(source)
                if domain:
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
                notes.append(f"{source.publisher or domain or source.url}: duplicate_domain_retained_for_context")
                if len(kept_sources) >= minimum_context_sources:
                    break

        retained_urls = {self._normalize_url(source.url) for source in kept_sources if source.url}
        for source in duplicate_domain_candidates:
            normalized_url = self._normalize_url(source.url)
            if normalized_url in retained_urls:
                continue
            notes.append(f"{source.publisher or self._source_domain(source.url) or source.url}: duplicate_domain")

        if not kept_sources and prioritized_sources:
            kept_sources = [prioritized_sources[0]]
            notes.append("filter_fallback_kept_top_source")

        removed_count = max(0, len(research.sources) - len(kept_sources))
        if removed_count == 0 and not notes and not self._sources_changed(research.sources, kept_sources):
            return research, 0, []
        return rebuild_packet(topic, kept_sources, country=country), removed_count, notes

    def should_enrich(self, decision: EditorialDecision) -> bool:
        if not decision.should_write:
            return False
        if decision.article_mode == "full_article":
            return False
        normalized_gaps = {item.casefold() for item in decision.missing_elements}
        return bool(normalized_gaps & self.ENRICHMENT_GAPS)

    def enrich(
        self,
        topic: TrendTopic,
        research: ResearchPacket,
        decision: EditorialDecision,
        *,
        country: str,
        topic_category: str | None,
        rebuild_packet,
    ) -> tuple[ResearchPacket, bool, int]:
        if not self.should_enrich(decision) or not self.tavily_client.available:
            return research, False, 0

        query = self._build_query(topic, decision, topic_category)
        extra_sources = self.tavily_client.search(query, topic_category=topic_category, max_results=3)
        if not extra_sources:
            return research, False, 0

        merged_sources = self._prioritize_sources(self._merge_sources(research.sources, extra_sources))
        added_count = len(merged_sources) - len(research.sources)
        if added_count <= 0:
            return research, False, 0

        enriched_packet = rebuild_packet(topic, merged_sources, country=country)
        return enriched_packet, True, added_count

    def _build_query(self, topic: TrendTopic, decision: EditorialDecision, topic_category: str | None) -> str:
        parts = [topic.keyword]
        primary_angle = decision.primary_angle.strip()
        if primary_angle and primary_angle.casefold() != topic.keyword.casefold():
            parts.append(primary_angle)
        if topic_category:
            parts.append(topic_category)

        missing = {item.casefold() for item in decision.missing_elements}
        if "chronology" in missing:
            parts.append("timeline")
        if "clear consequence" in missing:
            parts.append("impact")
        if "reporting depth" in missing:
            parts.append("official statement")
        return " ".join(part for part in parts if part).strip()

    def _merge_sources(self, existing: list[ResearchSource], extra: list[ResearchSource]) -> list[ResearchSource]:
        merged = list(existing)
        seen_urls = {self._normalize_url(source.url) for source in existing if source.url}
        for source in extra:
            normalized_source = self._normalize_source(source)
            normalized_url = self._normalize_url(normalized_source.url)
            if not normalized_url or normalized_url in seen_urls:
                continue
            merged.append(normalized_source)
            seen_urls.add(normalized_url)
        return merged

    def _prioritize_sources(self, sources: list[ResearchSource]) -> list[ResearchSource]:
        normalized_sources = [self._normalize_source(source) for source in sources]
        indexed_sources = list(enumerate(normalized_sources))
        prioritized = sorted(
            indexed_sources,
            key=lambda item: (
                self._source_priority(item[1]),
                0 if item[1].published_at.strip() else 1,
                0 if (item[1].content or item[1].snippet).strip() else 1,
                item[0],
            ),
        )
        return [source for _, source in prioritized]

    def _normalize_source(self, source: ResearchSource) -> ResearchSource:
        inferred_tier = self._infer_source_tier(source.publisher, source.url)
        source_tier = source.source_tier.strip() if source.source_tier.strip() else inferred_tier
        if self._tier_rank(inferred_tier) < self._tier_rank(source_tier):
            source_tier = inferred_tier
        return ResearchSource(
            title=source.title,
            url=source.url,
            snippet=source.snippet,
            publisher=source.publisher,
            published_at=source.published_at,
            content=source.content,
            source_tier=source_tier,
        )

    def _source_priority(self, source: ResearchSource) -> int:
        tier = source.source_tier.strip().casefold() or self._infer_source_tier(source.publisher, source.url)
        if tier == "primary":
            return 0
        if tier == "wire":
            return 1

        lowered = f"{source.publisher} {source.url}".casefold()
        if any(token in lowered for token in self.MAINSTREAM_HINTS):
            return 2
        return 3

    def _infer_source_tier(self, publisher: str, url: str) -> str:
        lowered = f"{publisher} {url}".casefold()
        if any(token in lowered for token in self.WIRE_HINTS):
            return "wire"
        if any(token in lowered for token in self.PRIMARY_HINTS):
            return "primary"
        return "secondary"

    def _tier_rank(self, tier: str) -> int:
        normalized = (tier or "secondary").casefold()
        if normalized == "primary":
            return 0
        if normalized == "wire":
            return 1
        return 2

    def _sources_changed(self, current: list[ResearchSource], prioritized: list[ResearchSource]) -> bool:
        current_signature = [(self._normalize_url(source.url), source.source_tier.strip().casefold()) for source in current]
        prioritized_signature = [(self._normalize_url(source.url), source.source_tier.strip().casefold()) for source in prioritized]
        return current_signature != prioritized_signature

    def _normalize_url(self, url: str) -> str:
        return (url or "").strip().rstrip("/")

    def _source_domain(self, url: str) -> str:
        return urlsplit(url or "").netloc.casefold().removeprefix("www.")

    def _rejection_reason(self, source: ResearchSource, seen_domains: set[str]) -> str | None:
        url = (source.url or "").strip()
        domain = self._source_domain(url)
        path = urlsplit(url).path.casefold()
        content_text = f"{source.snippet} {source.content}".strip()
        word_count = len(content_text.split())
        title = (source.title or "").casefold()
        normalized_tier = (source.source_tier or "secondary").strip().casefold()

        if any(hint in path for hint in self.PROFILE_PATH_HINTS):
            return "profile_page"
        if any(hint in path for hint in self.VIDEO_PATH_HINTS) or title.startswith("video:"):
            return "video_stub"
        if any(hint in path for hint in self.LIVE_PATH_HINTS) or "live updates" in title:
            return "live_blog"
        if domain in seen_domains:
            return "duplicate_domain"
        if domain in self.LOW_VALUE_DOMAINS:
            return "low_value_aggregation"
        minimum_words = 10 if normalized_tier in {"wire", "primary"} else 18
        if word_count < minimum_words:
            return "thin_content"
        return None