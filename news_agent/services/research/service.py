from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlsplit

try:
    from newspaper import Article
except ImportError:
    Article = None

try:
    from serpapi import GoogleSearch
except ImportError:
    GoogleSearch = None

from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import ContextReference, ResearchClaim, ResearchPacket, ResearchSource, TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import slugify, topic_category_query_hint


class ResearchService:
    CONTEXT_REFERENCE_LIMIT = 3
    SOURCE_BOILERPLATE_PHRASES = {
        "manage your tracker preferences",
        "we use cookies and similar tracking technologies",
        "we use technologies like cookies",
        "privacy preference center",
        "skip to main content",
        "skip to navigation",
        "skip to key events",
        "skip navigation",
        "live coverage",
        "link copied",
        "see all topics",
        "for subscribers",
        "subscribe for unlimited access",
        "democracy dies in darkness",
        "create free account",
        "watchlist",
        "investing club",
        "join pro",
        "livestream",
        "facebooktweetemaillinkthreads",
    }
    QUERY_NOISE_TOKENS = {
        "latest", "news", "cnn", "bbc", "axios", "politico", "update", "updates", "live", "breaking",
        "analysis", "today",
    }
    PRESENT_HINTS = {"wins", "win", "won", "defeat", "beats", "projects", "projected", "advances", "passes", "announces"}
    PAST_HINTS = {"previously", "earlier", "before", "since", "last", "former", "history", "background"}
    FUTURE_HINTS = {"will", "next", "expected", "ahead", "upcoming", "plan", "plans", "could", "likely"}
    ENTITY_PATTERN = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,})){0,2}\b")
    ENTITY_STOPWORDS = {
        "CNN", "NBC News", "The New York Times", "Fox News", "Reuters", "AP", "Associated Press",
        "Live Updates", "Live Results", "Opinion", "Politics", "Breaking News", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Results", "Update", "Updates",
    }
    ENTITY_NOISE_TOKENS = {
        "vs", "v", "match", "scorecard", "highlights", "live", "result", "results", "stats",
        "preview", "today", "score", "scores", "primary", "runoff", "election", "elections",
        "midterm", "midterms", "station", "district",
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def research(self, topic: TrendTopic, country: str) -> ResearchPacket:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return self._mock_research(topic)

        query = self._build_query(topic.keyword)
        category_hint = topic_category_query_hint(self.config.topic_category)
        if category_hint:
            query = f"{query} {category_hint}".strip()

        params = {
            "engine": "google_news",
            "q": query,
            "gl": country,
            "hl": "en",
            "api_key": self.config.serpapi_key,
        }
        response = GoogleSearch(params).get_dict()
        items = response.get("news_results", [])[: self.config.research_results]

        sources: list[ResearchSource] = []
        for item in items:
            publisher = item.get("source", "")
            if isinstance(publisher, dict):
                publisher = publisher.get("name", "")
            url = item.get("link", "")
            details = self.extract_source_details(
                url,
                publisher=str(publisher),
                title=self._clean_source_text(item.get("title", topic.keyword), preserve_case=True),
            )
            sources.append(
                ResearchSource(
                    title=self._clean_source_text(item.get("title", topic.keyword), preserve_case=True),
                    url=url,
                    snippet=self._clean_source_text(item.get("snippet", "")),
                    publisher=str(publisher),
                    published_at=item.get("date", ""),
                    content=details["content"],
                    source_tier=self._source_tier(str(publisher), url),
                    image_url=details["image_url"],
                    image_caption=details["image_caption"],
                    image_credit=details["image_credit"],
                )
            )

        return self._build_packet(topic, sources)

    def _build_query(self, topic_keyword: str) -> str:
        tokens: list[str] = []
        for token in re.split(r"\s+", topic_keyword or ""):
            cleaned = token.strip(" ,.:;!?()[]{}\"'")
            if not cleaned:
                continue
            if cleaned.casefold() in self.QUERY_NOISE_TOKENS:
                continue
            tokens.append(cleaned)

        normalized = " ".join(tokens).strip()
        return normalized or topic_keyword.strip()

    def extract_article(self, url: str) -> str:
        return self.extract_source_details(url).get("content", "")[:3000]

    def extract_source_details(self, url: str, *, publisher: str = "", title: str = "") -> dict[str, str]:
        firecrawl_details = self._extract_source_with_firecrawl(url, publisher=publisher, title=title)
        if firecrawl_details["content"]:
            return firecrawl_details

        if not url or Article is None:
            return {
                "content": "",
                "image_url": "",
                "image_caption": "",
                "image_credit": publisher.strip(),
            }
        try:
            article = Article(url)
            article.download()
            article.parse()
            return {
                "content": article.text[:3000],
                "image_url": str(getattr(article, "top_image", "") or "").strip(),
                "image_caption": self._clean_source_text(title or getattr(article, "title", ""), preserve_case=True),
                "image_credit": publisher.strip() or self._credit_from_url(url),
            }
        except Exception:
            return {
                "content": "",
                "image_url": "",
                "image_caption": "",
                "image_credit": publisher.strip() or self._credit_from_url(url),
            }

    def _mock_research(self, topic: TrendTopic) -> ResearchPacket:
        slug = topic.keyword.lower().replace(" ", "-")
        sources = [
            ResearchSource(
                title=f"{topic.keyword}: what changed this week",
                url=f"https://example.com/{slug}/weekly-update",
                snippet=f"Analysts say {topic.keyword} is moving quickly because adoption and public interest rose across multiple markets.",
                publisher="Example Wire",
                published_at="2026-05-25",
                source_tier="wire",
                content=(
                    f"{topic.keyword} is moving from headline attention into execution. Operators are comparing adoption, "
                    "distribution, and commercial impact across markets."
                ),
            ),
            ResearchSource(
                title=f"Why {topic.keyword} is getting business attention",
                url=f"https://example.com/{slug}/business-impact",
                snippet=f"Operators are evaluating the revenue, operational, and product impact of {topic.keyword} for the next quarter.",
                publisher="Business Daily",
                published_at="2026-05-25",
                source_tier="secondary",
                content=(
                    f"The market is treating {topic.keyword} as a strategic topic rather than a passing headline. "
                    "Teams are using it to guide roadmap, communication, and content investment."
                ),
            ),
            ResearchSource(
                title=f"Experts outline the next phase of {topic.keyword}",
                url=f"https://example.com/{slug}/next-phase",
                snippet=f"Researchers expect {topic.keyword} to keep evolving as platforms add better tooling, regulation, and distribution.",
                publisher="Tech Review Desk",
                published_at="2026-05-25",
                source_tier="secondary",
                content=(
                    f"Experts expect {topic.keyword} to develop through better tooling, more structured workflows, and clearer governance."
                ),
            ),
        ]
        return self._build_packet(topic, sources)

    def _build_packet(
        self,
        topic: TrendTopic,
        sources: list[ResearchSource],
        country: str | None = None,
    ) -> ResearchPacket:
        facts: list[str] = []
        context_blocks: list[str] = []
        claims: list[ResearchClaim] = []
        present: list[str] = []
        past: list[str] = []
        future: list[str] = []

        for index, source in enumerate(sources, start=1):
            fact_parts = self._dedupe_fact_parts(
                [
                    self._clean_source_text(source.title, preserve_case=True),
                    self._clean_source_text(source.snippet),
                    self._clean_source_text(source.content[:350].strip()),
                ]
            )
            joined = self._collapse_repeated_sentences(". ".join(part for part in fact_parts if part).strip())
            if joined:
                facts.append(joined)
                section = self._classify_fact_section(joined)
                claims.append(
                    ResearchClaim(
                        claim=joined,
                        source_title=source.title,
                        source_url=source.url,
                        source_tier=source.source_tier,
                        section=section,
                    )
                )
                if section == "past":
                    past.append(joined)
                elif section == "future":
                    future.append(joined)
                else:
                    present.append(joined)

            context_blocks.append(
                "\n".join(
                    [
                        f"Source {index}",
                        f"Title: {source.title}",
                        f"Publisher: {source.publisher}",
                        f"Tier: {source.source_tier}",
                        f"URL: {source.url}",
                        f"Snippet: {source.snippet}",
                        f"Content: {source.content[:1200]}",
                    ]
                )
            )

        present = self._prioritize_present_facts(present, claims)
        past = self._prioritize_non_opinion(past)
        future = self._prioritize_non_opinion(future)

        if not present:
            present = facts[: min(3, len(facts))]
        if not past and len(facts) > 1:
            past = facts[1: min(3, len(facts))]
        if not future:
            future = [
                f"Watch for the next confirmed development, official response, or measurable impact related to {topic.keyword}."
            ]

        lead = present[0] if present else (facts[0] if facts else topic.keyword)

        context_references = self._build_context_references(topic, sources, country=country)

        return ResearchPacket(
            topic=topic.keyword,
            sources=sources,
            facts=facts,
            context="\n\n---\n\n".join(context_blocks),
            lead=lead,
            present=present[:3],
            past=past[:3],
            future=future[:3],
            claims=claims,
            context_references=context_references,
        )

    def _extract_source_with_firecrawl(self, url: str, *, publisher: str = "", title: str = "") -> dict[str, str]:
        if not url or not self.config.firecrawl_api_key:
            return {
                "content": "",
                "image_url": "",
                "image_caption": "",
                "image_credit": publisher.strip() or self._credit_from_url(url),
            }

        payload = json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
        request = Request(
            "https://api.firecrawl.dev/v1/scrape",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.firecrawl_api_key}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=45) as response:
                raw_payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, OSError, ValueError):
            return {
                "content": "",
                "image_url": "",
                "image_caption": "",
                "image_credit": publisher.strip() or self._credit_from_url(url),
            }

        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        if not isinstance(data, dict):
            return {
                "content": "",
                "image_url": "",
                "image_caption": "",
                "image_credit": publisher.strip() or self._credit_from_url(url),
            }

        markdown = data.get("markdown") or data.get("content") or ""
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        image_url = self._extract_firecrawl_image_url(data, metadata)
        image_caption = self._clean_source_text(
            metadata.get("title") or title or "",
            preserve_case=True,
        )
        return {
            "content": self._clean_scraped_text(str(markdown)) if markdown else "",
            "image_url": image_url,
            "image_caption": image_caption,
            "image_credit": publisher.strip() or self._credit_from_url(url),
        }

    def _extract_article_with_firecrawl(self, url: str) -> str:
        return self._extract_source_with_firecrawl(url).get("content", "")

    def _extract_firecrawl_image_url(self, data: dict, metadata: dict[str, object]) -> str:
        candidates = [
            data.get("image"),
            data.get("imageUrl"),
            data.get("image_url"),
            data.get("ogImage"),
            metadata.get("image"),
            metadata.get("imageUrl"),
            metadata.get("image_url"),
            metadata.get("ogImage"),
            metadata.get("og:image"),
            metadata.get("thumbnail"),
            metadata.get("thumbnailUrl"),
            data.get("images"),
            metadata.get("images"),
        ]
        for candidate in candidates:
            image_url = self._first_url_candidate(candidate)
            if image_url:
                return image_url
        return ""

    def _first_url_candidate(self, value: object) -> str:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
            return ""
        if isinstance(value, dict):
            for key in ("url", "src", "image", "imageUrl", "image_url"):
                candidate = self._first_url_candidate(value.get(key))
                if candidate:
                    return candidate
            return ""
        if isinstance(value, list):
            for item in value:
                candidate = self._first_url_candidate(item)
                if candidate:
                    return candidate
        return ""

    def _credit_from_url(self, url: str) -> str:
        hostname = urlsplit(url).netloc.strip().lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname

    def _clean_scraped_text(self, text: str) -> str:
        raw_text = str(text or "")
        lines = [line.strip() for line in raw_text.splitlines()]
        non_empty_lines = [line for line in lines if line]
        if len(non_empty_lines) >= 2:
            first_line = non_empty_lines[0].lstrip("# ").strip()
            second_line = non_empty_lines[1]
            if first_line and second_line and not re.search(r"[.!?]$", first_line):
                raw_text = "\n\n".join(non_empty_lines[1:])

        cleaned = self._clean_source_text(raw_text)
        had_terminal_punctuation = bool(re.search(r"[.!?]\s*$", raw_text))
        if cleaned and had_terminal_punctuation and not re.search(r"[.!?]$", cleaned):
            cleaned = f"{cleaned}."
        return cleaned

    def _clean_source_text(self, text: str, *, preserve_case: bool = False) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        normalized = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", normalized)
        normalized = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", normalized)
        normalized = re.sub(r"\b\d+\s+min\s+read\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b\d+\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes)\s+ago\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b(?:updated|published)\b[^.]{0,80}\b(?:et|utc)\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"#\s*live updates?:", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+#\s*", " ", normalized)
        normalized = re.sub(r"\bBy\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", " ", normalized)
        normalized = re.sub(r"\bAnalysis by\b[^.]{0,120}", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\*\s*\*\s*\*", " ", normalized)
        normalized = re.sub(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}(?:The\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})+\b", " ", normalized)

        lowered = normalized.casefold()
        for phrase in self.SOURCE_BOILERPLATE_PHRASES:
            phrase_index = lowered.find(phrase)
            if phrase_index != -1:
                normalized = normalized[:phrase_index]
                lowered = normalized.casefold()

        sentence_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
        if len(sentence_parts) >= 2 and sentence_parts[0].rstrip(".!?").casefold() == sentence_parts[1].rstrip(".!?").casefold():
            normalized = sentence_parts[0]

        normalized = re.sub(r"\s+", " ", normalized).strip(" -:|,.;")
        if preserve_case or not normalized:
            return normalized
        return normalized

    def _dedupe_fact_parts(self, parts: list[str]) -> list[str]:
        unique_parts: list[str] = []
        seen_keys: list[str] = []

        for part in parts:
            candidate = (part or "").strip()
            if not candidate:
                continue

            sentences = self._split_sentences(candidate)
            while sentences:
                sentence_key = self._fact_key(sentences[0])
                if sentence_key and any(sentence_key == key or sentence_key in key for key in seen_keys):
                    sentences.pop(0)
                    continue
                break

            candidate = " ".join(sentences).strip() if sentences else ""
            candidate_key = self._fact_key(candidate)
            if not candidate_key:
                continue
            if any(candidate_key == key or candidate_key in key or key in candidate_key for key in seen_keys):
                continue

            unique_parts.append(candidate)
            seen_keys.append(candidate_key)

        return unique_parts

    def _collapse_repeated_sentences(self, text: str) -> str:
        unique_sentences: list[str] = []
        seen_keys: list[str] = []

        for sentence in self._split_sentences(text):
            sentence_key = self._fact_key(sentence)
            if not sentence_key:
                continue
            if any(sentence_key == key or sentence_key in key or key in sentence_key for key in seen_keys):
                continue
            unique_sentences.append(sentence.rstrip(".!?"))
            seen_keys.append(sentence_key)

        if not unique_sentences:
            return ""
        return ". ".join(unique_sentences).strip() + "."

    def _split_sentences(self, text: str) -> list[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text or "") if part.strip()]

    def _fact_key(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()

    def _classify_fact_section(self, fact: str) -> str:
        lowered = fact.lower()
        if "opinion" in lowered:
            return "past"
        if any(token in lowered for token in self.PRESENT_HINTS):
            return "present"
        if any(token in lowered for token in self.PAST_HINTS):
            return "past"
        if any(token in lowered for token in self.FUTURE_HINTS):
            return "future"
        return "present"

    def _prioritize_present_facts(self, present: list[str], claims: list[ResearchClaim]) -> list[str]:
        ordered: list[str] = []
        preferred_claims = sorted(
            [claim for claim in claims if claim.section == "present"],
            key=lambda claim: (
                "opinion" in claim.source_title.lower(),
                0 if claim.source_tier in {"wire", "primary"} else 1,
            ),
        )
        for claim in preferred_claims:
            if claim.claim not in ordered:
                ordered.append(claim.claim)
        for fact in present:
            if fact not in ordered:
                ordered.append(fact)
        return ordered

    def _prioritize_non_opinion(self, facts: list[str]) -> list[str]:
        non_opinion = [fact for fact in facts if "opinion" not in fact.lower()]
        return non_opinion or facts

    def _source_tier(self, publisher: str, url: str) -> str:
        lowered = f"{publisher} {url}".lower()
        if any(token in lowered for token in {"reuters", "associated press", "ap news", "afp", "bloomberg"}):
            return "wire"
        if any(token in lowered for token in {".gov", ".mil", ".edu", "whitehouse.gov", "sec.gov", "supremecourt.gov"}):
            return "primary"
        return "secondary"

    def _build_context_references(
        self,
        topic: TrendTopic,
        sources: list[ResearchSource],
        country: str | None = None,
    ) -> list[ContextReference]:
        if not sources:
            return []

        candidates = self._extract_reference_candidates(topic, sources)
        if not candidates:
            return []

        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return self._mock_context_references(candidates)

        references: list[ContextReference] = []
        for candidate in candidates[: self.CONTEXT_REFERENCE_LIMIT]:
            reference = self._search_context_reference(candidate, country or self.config.country)
            if reference is not None:
                references.append(reference)
        return references

    def _extract_reference_candidates(self, topic: TrendTopic, sources: list[ResearchSource]) -> list[str]:
        scores: dict[str, int] = defaultdict(int)
        display_names: dict[str, str] = {}
        banned_publishers = {source.publisher.strip().casefold() for source in sources if source.publisher.strip()}

        def add_candidate(candidate: str, weight: int) -> None:
            cleaned = self._normalize_entity_candidate(candidate)
            if not cleaned:
                return
            key = cleaned.casefold()
            scores[key] += weight
            current = display_names.get(key, "")
            if not current or self._display_priority(cleaned) > self._display_priority(current):
                display_names[key] = cleaned

        topic_text = topic.keyword.strip()
        if topic_text:
            add_candidate(topic_text, 5)

        for source in sources:
            for candidate in self.ENTITY_PATTERN.findall(source.title or ""):
                add_candidate(candidate, 3)
            snippet_text = f"{source.snippet} {source.content[:500]}"
            for candidate in self.ENTITY_PATTERN.findall(snippet_text):
                add_candidate(candidate, 1)

        ordered = sorted(
            scores.items(),
            key=lambda item: (
                display_names.get(item[0], "").casefold() in banned_publishers,
                len(display_names.get(item[0], "").split()) == 1,
                -item[1],
                display_names.get(item[0], ""),
            ),
        )
        return [display_names[key] for key, _ in ordered if display_names.get(key, "").casefold() not in banned_publishers][: self.CONTEXT_REFERENCE_LIMIT + 2]

    def _display_priority(self, value: str) -> tuple[int, int, int]:
        return (
            sum(1 for char in value if char.isupper()),
            int(any(char.isupper() for char in value)),
            len(value),
        )

    def _normalize_entity_candidate(self, candidate: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", candidate).strip(" -:|,.;()[]{}")
        cleaned = re.sub(
            r"^(?:President|Senator|Sen\.?|Representative|Rep\.?|Governor|Gov\.?|Attorney General|State Representative|State Rep\.?|State Senator|State Sen\.?|U\.S\. Representative|U\.S\. Rep\.?|U\.S\. Senator|U\.S\. Sen\.?)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if not cleaned:
            return None
        if cleaned in self.ENTITY_STOPWORDS:
            return None
        if len(cleaned) < 3:
            return None
        if re.search(r"[A-Za-z]", cleaned) and re.search(r"[^\x00-\x7F]", cleaned):
            return None
        if len(cleaned.split()) > 4:
            return None
        if cleaned.isupper() and len(cleaned) > 5:
            return None
        tokens = {token.casefold() for token in re.findall(r"[A-Za-z0-9']+", cleaned)}
        if tokens & self.ENTITY_NOISE_TOKENS:
            return None
        if cleaned.islower():
            cleaned = " ".join(word.capitalize() for word in cleaned.split())
        return cleaned

    def _mock_context_references(self, candidates: list[str]) -> list[ContextReference]:
        references: list[ContextReference] = []
        for candidate in candidates[: self.CONTEXT_REFERENCE_LIMIT]:
            references.append(
                ContextReference(
                    entity=candidate,
                    title=f"{candidate} background",
                    url=f"https://en.wikipedia.org/wiki/{slugify(candidate).replace('-', '_')}",
                    snippet=f"Reference background for {candidate}.",
                    source="wikipedia",
                    summary=f"Background context for {candidate} that helps explain the current story.",
                )
            )
        return references

    def _search_context_reference(self, entity: str, country: str) -> ContextReference | None:
        queries = [f"{entity} wikipedia", entity]

        for query in queries:
            params = {
                "engine": "google",
                "q": query,
                "gl": country,
                "hl": "en",
                "num": 5,
                "api_key": self.config.serpapi_key,
            }
            try:
                payload = GoogleSearch(params).get_dict()
            except Exception:
                continue

            organic_results = payload.get("organic_results", [])
            if not organic_results:
                continue

            preferred = next(
                (item for item in organic_results if "wikipedia.org" in str(item.get("link", "")).lower()),
                organic_results[0],
            )
            url = str(preferred.get("link", "")).strip()
            if not url or "translate.google." in url.lower():
                continue

            snippet = self._clean_reference_text(str(preferred.get("snippet", "") or ""))
            title = str(preferred.get("title", entity)).strip() or entity
            summary = self._clean_reference_text(self.extract_article(url))[:220] if self.config.firecrawl_api_key else ""
            source = "wikipedia" if "wikipedia.org" in url.lower() else "background"

            return ContextReference(
                entity=entity,
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                summary=summary,
            )

        return None

    def _clean_reference_text(self, value: str) -> str:
        cleaned = value or ""
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", cleaned)
        cleaned = re.sub(r"<br\s*/?>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"https?://\S+", " ", cleaned)
        cleaned = re.sub(r"\bFile:[^\s|)]+", " ", cleaned)
        cleaned = re.sub(r"[\[\]{}|]", " ", cleaned)
        cleaned = re.sub(r"#+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|,.;")
        return cleaned


class ResearchAgent(BaseAgent):
    stage_name = "research"

    def __init__(self, service: ResearchService, publisher, logger: PipelineLogger) -> None:
        self.service = service
        self.publisher = publisher
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None:
            raise RuntimeError("Selected topic is required before research")

        research = self.service.research(context.run.selected_topic, context.run.country)
        context.run.research = research
        self.publisher.save_research_cache(context.run.run_id, research)
        self.logger.info(context.run, f"Built research packet with {len(research.sources)} sources")
        self.logger.transition(context.run, "research_done")