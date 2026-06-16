from __future__ import annotations

from urllib.parse import urlsplit

from config import AppConfig

from .fetchers import TavilySearchClient
from .models import FetchedSource, ResearchBundle, ResearchPlan, TopicCandidate, TrendSignal


class DeterministicResearchRouter:
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

    def __init__(self, config: AppConfig, *, tavily_client: TavilySearchClient | None = None) -> None:
        self.config = config
        self.tavily_client = tavily_client or TavilySearchClient(mock_mode=config.mock_mode)

    def plan(self, candidate: TopicCandidate) -> ResearchPlan:
        match candidate.topic_family:
            case "politics_legal_policy":
                return ResearchPlan(
                    topic_family=candidate.topic_family,
                    tools_to_call=["serpapi", "firecrawl", "tavily"],
                    notes=["Use Tavily only when official-source coverage remains thin after curation."],
                    max_sources=max(3, min(self.config.research_results, 5)),
                    allow_tavily_backfill=True,
                )
            case "sports_results_standings":
                return ResearchPlan(
                    topic_family=candidate.topic_family,
                    tools_to_call=["serpapi", "firecrawl"],
                    notes=["Skip Tavily unless match stakes or context remain thin after curation."],
                    max_sources=max(2, min(self.config.research_results, 4)),
                )
            case "business_earnings_markets":
                return ResearchPlan(
                    topic_family=candidate.topic_family,
                    tools_to_call=["serpapi", "firecrawl"],
                    notes=["Prefer primary-source filings and company materials over aggregation."],
                    max_sources=max(3, min(self.config.research_results, 5)),
                )
            case "celebrity_personal_update":
                return ResearchPlan(
                    topic_family=candidate.topic_family,
                    tools_to_call=["serpapi", "firecrawl"],
                    notes=["Require two independent mainstream confirmations before writing."],
                    max_sources=max(3, min(self.config.research_results, 4)),
                    require_mainstream_confirmations=2,
                )
            case "science_health_research":
                return ResearchPlan(
                    topic_family=candidate.topic_family,
                    tools_to_call=["serpapi", "firecrawl", "tavily"],
                    notes=["Flag preprints and add methodology context when available."],
                    max_sources=max(3, min(self.config.research_results, 5)),
                    allow_tavily_backfill=True,
                )
            case _:
                return ResearchPlan(
                    topic_family="default_unclassified",
                    tools_to_call=["serpapi", "firecrawl", "tavily"],
                    notes=["Default fallback branch keeps the research plan conservative and non-empty."],
                    max_sources=max(2, min(self.config.research_results, 4)),
                    fallback_used=True,
                    allow_tavily_backfill=True,
                )

    def execute(
        self,
        *,
        topic: TrendSignal,
        research_packet: ResearchBundle,
        plan: ResearchPlan,
        country: str,
        topic_category: str | None,
        rebuild_bundle,
    ) -> tuple[ResearchBundle, bool, int, int, list[str]]:
        prioritized_sources = self._prioritize_sources(research_packet.sources)
        prioritized = rebuild_bundle(topic, prioritized_sources, country=country)
        filtered, filtered_source_count, source_filter_notes = self._filter_sources(
            topic,
            prioritized,
            country=country,
            rebuild_bundle=rebuild_bundle,
        )

        tavily_enriched = False
        enriched_source_count = 0
        if plan.allow_tavily_backfill and self.tavily_client.available and self._needs_tavily_backfill(filtered, plan):
            extra_sources = self.tavily_client.search(
                self._build_tavily_query(topic, plan, topic_category),
                topic_category=topic_category,
                max_results=3,
            )
            merged_sources = self._merge_sources(filtered.sources, extra_sources)
            enriched_source_count = max(0, len(merged_sources) - len(filtered.sources))
            tavily_enriched = enriched_source_count > 0
            if tavily_enriched:
                filtered = rebuild_bundle(topic, merged_sources, country=country)
                filtered, extra_filtered_count, extra_filter_notes = self._filter_sources(
                    topic,
                    filtered,
                    country=country,
                    rebuild_bundle=rebuild_bundle,
                )
                filtered_source_count += extra_filtered_count
                source_filter_notes.extend(extra_filter_notes)

        capped_sources = filtered.sources[: plan.max_sources]
        if len(capped_sources) != len(filtered.sources):
            filtered = rebuild_bundle(topic, capped_sources, country=country)
        return filtered, tavily_enriched, enriched_source_count, filtered_source_count, source_filter_notes

    def _needs_tavily_backfill(self, research_packet: ResearchBundle, plan: ResearchPlan) -> bool:
        if len(research_packet.sources) < min(2, plan.max_sources):
            return True
        if plan.require_mainstream_confirmations:
            mainstream_count = sum(1 for source in research_packet.sources if self._is_mainstream(source.publisher, source.url))
            return mainstream_count < plan.require_mainstream_confirmations
        primary_or_wire_count = sum(1 for source in research_packet.sources if (source.source_tier or "secondary").casefold() in {"primary", "wire"})
        return primary_or_wire_count == 0

    def _is_mainstream(self, publisher: str, url: str) -> bool:
        lowered = f"{publisher} {url}".casefold()
        return any(token in lowered for token in (self.MAINSTREAM_HINTS | self.WIRE_HINTS))

    def _build_tavily_query(self, topic: TrendSignal, plan: ResearchPlan, topic_category: str | None) -> str:
        parts = [topic.keyword]
        if topic_category:
            parts.append(topic_category)
        if plan.topic_family in {"politics_legal_policy", "science_health_research", "default_unclassified"}:
            parts.extend(["official statement", "impact"])
        else:
            parts.append("reporting depth")
        return " ".join(part for part in parts if part).strip()

    def _merge_sources(self, existing: list[FetchedSource], extra: list[FetchedSource]) -> list[FetchedSource]:
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

    def _prioritize_sources(self, sources: list[FetchedSource]) -> list[FetchedSource]:
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

    def _filter_sources(
        self,
        topic: TrendSignal,
        research: ResearchBundle,
        *,
        country: str,
        rebuild_bundle,
    ) -> tuple[ResearchBundle, int, list[str]]:
        prioritized_sources = self._prioritize_sources(research.sources)
        kept_sources: list[FetchedSource] = []
        notes: list[str] = []
        seen_domains: set[str] = set()
        duplicate_domain_candidates: list[FetchedSource] = []

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
        if removed_count == 0 and not notes:
            return research, 0, []
        return rebuild_bundle(topic, kept_sources, country=country), removed_count, notes

    def _normalize_source(self, source: FetchedSource) -> FetchedSource:
        inferred_tier = self._infer_source_tier(source.publisher, source.url)
        source_tier = source.source_tier.strip() if source.source_tier.strip() else inferred_tier
        if self._tier_rank(inferred_tier) < self._tier_rank(source_tier):
            source_tier = inferred_tier
        return FetchedSource(
            title=source.title,
            url=source.url,
            snippet=source.snippet,
            publisher=source.publisher,
            published_at=source.published_at,
            content=source.content,
            source_tier=source_tier,
            fetched_by=source.fetched_by,
            fetched_at=source.fetched_at,
            extraction_status=source.extraction_status,
        )

    def _source_priority(self, source: FetchedSource) -> int:
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

    def _normalize_url(self, url: str) -> str:
        return (url or "").strip().rstrip("/")

    def _source_domain(self, url: str) -> str:
        return urlsplit(url or "").netloc.casefold().removeprefix("www.")

    def _rejection_reason(self, source: FetchedSource, seen_domains: set[str]) -> str | None:
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