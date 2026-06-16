from __future__ import annotations

from collections import defaultdict
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    from newspaper import Article
except ImportError:
    Article = None

try:
    from serpapi import GoogleSearch
except ImportError:
    GoogleSearch = None

from config import AppConfig
from news_agent.services.helpers import slugify

from ..models import ContextReference, FetchedSource, ResearchBundle, SourceClaim, TrendSignal


def _normalize_topic_category(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()
    if not normalized:
        return None
    aliases = {
        "politics": "politics",
        "political": "politics",
        "business": "business",
        "tech": "technology",
        "technology": "technology",
        "stock market": "stock market",
        "stocks": "stock market",
        "sports": "sports",
        "sport": "sports",
        "travel": "travel",
    }
    return aliases.get(normalized, normalized)


class TavilySearchClient:
    def __init__(self, api_key: str | None = None, *, mock_mode: bool = False, timeout: int = 20) -> None:
        import os

        self.api_key = api_key or os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_API")
        self.mock_mode = mock_mode
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.mock_mode

    def search(self, query: str, *, topic_category: str | None = None, max_results: int = 3) -> list[FetchedSource]:
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
        sources: list[FetchedSource] = []
        for item in results[:max_results]:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or url).strip()
            content = str(item.get("content") or item.get("raw_content") or "").strip()
            publisher = str(item.get("source") or "").strip() or urlsplit(url).netloc.casefold().removeprefix("www.")
            published_at = str(item.get("published_date") or item.get("date") or "").strip()
            sources.append(
                FetchedSource(
                    title=title,
                    url=url,
                    snippet=content[:280],
                    publisher=publisher,
                    published_at=published_at,
                    content=content[:3000],
                    source_tier="secondary",
                    fetched_by="tavily",
                    extraction_status="succeeded" if content else "failed",
                )
            )
        return sources


class SerpApiNewsFetcher:
    CONTEXT_REFERENCE_LIMIT = 3
    CLAIM_TEXT_ANCHORS = (
        "A federal judge",
        "A U.S. judge",
        "A United States judge",
        "Federal judge",
        "US judge",
        "Court lets",
        "The legal battle",
        "The challengers argued",
        "Nichols",
        "Plaintiffs",
        "Trump has",
    )
    QUERY_NOISE_TOKENS = {
        "latest", "news", "cnn", "bbc", "axios", "politico", "update", "updates", "live", "breaking", "analysis", "today",
    }
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
        "this copy is for your personal, non-commercial use only",
        "distribution and use of this material are governed by our subscriber agreement",
        "for non-personal use or to order multiple copies",
        "dow jones reprints",
        "already a subscriber",
        "continue reading your article with a wsj subscription",
        "skip to select what to read next",
        "content frame",
        "an error has occurred",
        "please try again later",
        "site search home news sport business technology",
        "add as preferred on google",
        "skip to content",
        "trinity player getting your trinity audio player ready",
        "suggest a correction",
        "read next >",
        "provided by nexstar media group",
        "you’re currently following this author",
        "you're currently following this author",
        "read in app",
        "loading audio narration",
        "listen to this article",
        "power partner awards",
        "apply today",
        "linkedinfacebookxblueskylink",
    }
    ENTITY_PATTERN = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,})){0,3}\b")
    ENTITY_STOPWORDS = {
        "CNN", "NBC News", "The New York Times", "Fox News", "Reuters", "AP", "Associated Press", "Live Updates", "Live Results",
        "Opinion", "Politics", "Breaking News", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday",
        "Results", "Update", "Updates", "PBS News", "Federal",
    }
    ENTITY_NOISE_TOKENS = {
        "vs", "v", "match", "scorecard", "highlights", "live", "result", "results", "stats", "preview", "today", "score", "scores",
        "primary", "runoff", "election", "elections", "midterm", "midterms", "station", "district",
    }
    PRESENT_HINTS = {"wins", "win", "won", "defeat", "beats", "projects", "projected", "advances", "passes", "announces"}
    PAST_HINTS = {"previously", "earlier", "before", "since", "last", "former", "history", "background"}
    FUTURE_HINTS = {"will", "next", "expected", "ahead", "upcoming", "plan", "plans", "could", "likely"}

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def research(self, topic: TrendSignal, country: str) -> ResearchBundle:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return self._mock_research(topic)

        query = self._build_query(topic.keyword)
        category_hint = _normalize_topic_category(self.config.topic_category)
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
        sources: list[FetchedSource] = []
        for item in items:
            publisher = item.get("source", "")
            if isinstance(publisher, dict):
                publisher = publisher.get("name", "")
            url = str(item.get("link") or "")
            sources.append(
                FetchedSource(
                    title=self._clean_source_text(item.get("title", topic.keyword), preserve_case=True),
                    url=url,
                    snippet=self._clean_source_text(item.get("snippet", "")),
                    publisher=str(publisher),
                    published_at=str(item.get("date", "")),
                    content=self.extract_article(url),
                    source_tier=self._source_tier(str(publisher), url),
                    fetched_by="serpapi",
                    extraction_status="succeeded",
                )
            )
        return self._build_bundle(topic, sources, country=country)

    def _mock_research(self, topic: TrendSignal) -> ResearchBundle:
        slug = topic.keyword.lower().replace(" ", "-")
        sources = [
            FetchedSource(
                title=f"{topic.keyword}: what changed this week",
                url=f"https://example.com/{slug}/weekly-update",
                snippet=f"Analysts say {topic.keyword} is moving quickly because adoption and public interest rose across multiple markets.",
                publisher="Example Wire",
                published_at="2026-05-25",
                source_tier="wire",
                fetched_by="mock_serpapi",
                content=(
                    f"{topic.keyword} is moving from headline attention into execution. Operators are comparing adoption, distribution, and commercial impact across markets."
                ),
            ),
            FetchedSource(
                title=f"Why {topic.keyword} is getting business attention",
                url=f"https://example.com/{slug}/business-impact",
                snippet=f"Operators are evaluating the revenue, operational, and product impact of {topic.keyword} for the next quarter.",
                publisher="Business Daily",
                published_at="2026-05-25",
                source_tier="secondary",
                fetched_by="mock_serpapi",
                content=(
                    f"The market is treating {topic.keyword} as a strategic topic rather than a passing headline. Teams are using it to guide roadmap, communication, and content investment."
                ),
            ),
            FetchedSource(
                title=f"Experts outline the next phase of {topic.keyword}",
                url=f"https://example.com/{slug}/next-phase",
                snippet=f"Researchers expect {topic.keyword} to keep evolving as platforms add better tooling, regulation, and distribution.",
                publisher="Tech Review Desk",
                published_at="2026-05-25",
                source_tier="secondary",
                fetched_by="mock_serpapi",
                content=(
                    f"Experts expect {topic.keyword} to develop through better tooling, more structured workflows, and clearer governance."
                ),
            ),
        ]
        return self._build_bundle(topic, sources)

    def _build_bundle(self, topic: TrendSignal, sources: list[FetchedSource], country: str | None = None) -> ResearchBundle:
        facts: list[str] = []
        context_blocks: list[str] = []
        claims: list[SourceClaim] = []
        present: list[str] = []
        past: list[str] = []
        future: list[str] = []

        for index, source in enumerate(sources, start=1):
            source_claims = self._extract_source_claims(source)
            if source_claims:
                facts.extend(source_claims)
            for claim_text in source_claims:
                section = self._classify_fact_section(claim_text)
                claims.append(
                    SourceClaim(
                        claim=claim_text,
                        source_title=source.title,
                        source_url=source.url,
                        source_tier=source.source_tier,
                        section=section,
                    )
                )
                if section == "past":
                    past.append(claim_text)
                elif section == "future":
                    future.append(claim_text)
                else:
                    present.append(claim_text)

            context_blocks.append(
                "\n".join([
                    f"Source {index}",
                    f"Title: {source.title}",
                    f"Publisher: {source.publisher}",
                    f"Tier: {source.source_tier}",
                    f"URL: {source.url}",
                    f"Snippet: {source.snippet}",
                    f"Content: {source.content[:1200]}",
                ])
            )

        if not present:
            present = facts[: min(3, len(facts))]
        if not past and len(facts) > 1:
            past = facts[1: min(3, len(facts))]
        if not future:
            future = [f"Watch for the next confirmed development, official response, or measurable impact related to {topic.keyword}."]
        lead = present[0] if present else (facts[0] if facts else topic.keyword)
        context_references = self._build_context_references(topic, sources, claims, country=country)
        return ResearchBundle(
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

    def extract_article(self, url: str) -> str:
        firecrawl_content = self._extract_article_with_firecrawl(url)
        if firecrawl_content:
            return firecrawl_content[:3000]
        if not url or Article is None:
            return ""
        try:
            article = Article(url)
            article.download()
            article.parse()
            return self._clean_scraped_text(article.text)[:3000]
        except Exception:
            return ""

    def _extract_article_with_firecrawl(self, url: str) -> str:
        if not url or not self.config.firecrawl_api_key:
            return ""
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
            return ""
        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        if not isinstance(data, dict):
            return ""
        markdown = data.get("markdown") or data.get("content") or ""
        return self._clean_scraped_text(str(markdown)) if markdown else ""

    def _build_query(self, topic_keyword: str) -> str:
        tokens = []
        for token in re.split(r"\s+", topic_keyword or ""):
            cleaned = token.strip(" ,.:;!?()[]{}\"'")
            if not cleaned or cleaned.casefold() in self.QUERY_NOISE_TOKENS:
                continue
            tokens.append(cleaned)
        normalized = " ".join(tokens).strip()
        return normalized or topic_keyword.strip()

    def _source_tier(self, publisher: str, url: str) -> str:
        lowered = f"{publisher} {url}".casefold()
        if any(token in lowered for token in {"reuters", "associated press", "ap news", "apnews", "afp", "bloomberg"}):
            return "wire"
        if any(token in lowered for token in {".gov", ".mil", ".edu", "whitehouse.gov", "justice.gov", "sec.gov", "supremecourt.gov", "senate.gov", "house.gov"}):
            return "primary"
        return "secondary"

    def _classify_fact_section(self, text: str) -> str:
        lowered = text.casefold()
        if any(token in lowered for token in self.FUTURE_HINTS):
            return "future"
        if any(token in lowered for token in self.PAST_HINTS):
            return "past"
        return "present"

    def _dedupe_fact_parts(self, parts: list[str]) -> list[str]:
        unique_parts: list[str] = []
        seen: set[str] = set()
        for part in parts:
            candidate = (part or "").strip()
            if not candidate:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique_parts.append(candidate)
        return unique_parts

    def _collapse_repeated_sentences(self, text: str) -> str:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        collapsed: list[str] = []
        seen: set[str] = set()
        for sentence in sentences:
            key = sentence.rstrip(".!?").casefold()
            if key in seen:
                continue
            seen.add(key)
            collapsed.append(sentence)
        return " ".join(collapsed).strip()

    def _build_source_claim_text(self, source: FetchedSource) -> str:
        title = self._normalize_sentence(self._clean_source_text(source.title, preserve_case=True))
        snippet = self._first_substantive_sentence(
            self._clean_source_text(source.snippet, preserve_case=True),
            anchor_text=title,
        )
        content = ""
        if not snippet:
            content = self._first_substantive_sentence(
                self._clean_source_text(source.content, preserve_case=True),
                anchor_text=title,
            )

        parts: list[str] = []
        seen: set[str] = set()
        for piece in [title, snippet, content]:
            key = self._sentence_key(piece)
            if not piece or not key or key in seen:
                continue
            if title and piece != title and key in self._sentence_key(title):
                continue
            parts.append(piece)
            seen.add(key)
            if len(parts) >= 2:
                break

        claim_text = self._collapse_repeated_sentences(" ".join(parts).strip())
        if len(claim_text.split()) > 32:
            claim_text = self._truncate_words(claim_text, 32)
        return claim_text or title

    def _extract_source_claims(self, source: FetchedSource) -> list[str]:
        claims: list[str] = []
        seen: set[str] = set()
        title = self._normalize_sentence(self._clean_source_text(source.title, preserve_case=True))
        for candidate in [
            *self._substantive_sentences(source.snippet, anchor_text=title, max_sentences=2),
            *self._substantive_sentences(source.content, anchor_text=title, max_sentences=2, require_anchor_overlap=True),
            title,
        ]:
            candidate = self._normalize_sentence(self._sanitize_claim_text(candidate))
            if not candidate:
                continue
            key = self._sentence_key(candidate)
            if not key or key in seen:
                continue
            if any(self._are_similar_claim_candidates(candidate, existing) for existing in claims):
                continue
            seen.add(key)
            claims.append(candidate)
            if len(claims) >= 2:
                break

        if claims:
            return claims

        claim_text = self._build_source_claim_text(source)
        return [claim_text] if claim_text else []

    def _substantive_sentences(
        self,
        text: str,
        *,
        anchor_text: str = "",
        min_words: int = 6,
        max_sentences: int = 2,
        require_anchor_overlap: bool = False,
    ) -> list[str]:
        cleaned_text = self._clean_source_text(text, preserve_case=True)
        anchor_tokens = self._meaningful_tokens(anchor_text)
        sentences: list[tuple[int, str]] = []
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned_text or ""):
            candidate = self._normalize_sentence(sentence)
            if not self._is_substantive_sentence(candidate, anchor_tokens=set(), min_words=min_words):
                continue
            overlap = len(self._meaningful_tokens(candidate) & anchor_tokens) if anchor_tokens else 0
            if require_anchor_overlap and overlap < min(3, len(anchor_tokens) or 3):
                continue
            sentences.append((overlap + self._claim_sentence_priority(candidate), candidate))

        sentences.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in sentences[:max_sentences]]

    def _are_similar_claim_candidates(self, left: str, right: str) -> bool:
        left_tokens = self._meaningful_tokens(left)
        right_tokens = self._meaningful_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        overlap = len(left_tokens & right_tokens)
        threshold = max(4, min(len(left_tokens), len(right_tokens)) - 1)
        return overlap >= threshold

    def _claim_sentence_priority(self, text: str) -> int:
        lowered = text.casefold()
        score = 0
        if any(token in lowered for token in {"judge", "court", "lawsuit", "injunction", "order", "ballot", "voting", "mail"}):
            score += 2
        if any(token in lowered for token in {"ruled", "ruling", "rejected", "rejects", "declined", "declines", "refused", "refuses", "blocked", "block", "halt", "halted", "seeking", "shifts"}):
            score += 3
        if any(token in lowered for token in {"years", "falsely", "without evidence", "history", "after his defeat", "independent reviews"}):
            score -= 4
        return score

    def _first_substantive_sentence(self, text: str, *, anchor_text: str = "", min_words: int = 8) -> str:
        anchor_tokens = self._meaningful_tokens(anchor_text)
        best_sentence = ""
        best_score = -1
        for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
            candidate = self._normalize_sentence(sentence)
            if not self._is_substantive_sentence(candidate, anchor_tokens=anchor_tokens, min_words=min_words):
                continue
            overlap = len(self._meaningful_tokens(candidate) & anchor_tokens) if anchor_tokens else 0
            if overlap > best_score:
                best_sentence = candidate
                best_score = overlap
        return best_sentence

    def _is_substantive_sentence(self, text: str, *, anchor_tokens: set[str], min_words: int) -> bool:
        if not text:
            return False
        if len(text.split()) < min_words:
            return False

        lowered = text.casefold()
        if any(phrase in lowered for phrase in self.SOURCE_BOILERPLATE_PHRASES):
            return False
        if re.search(
            r"\b(donate|donation|subscribe|shop|watchlist|watch now|listenlisten|share|newsletter|sign up|choose station|official site|file photo|photo by|live tv|my station|my list|cabinet room|copylink|read more|recommended stories)\b",
            lowered,
        ):
            return False
        if re.search(
            r"\b(most read|comments|blocked by an extension|try disabling your extensions|reload|enter your email address|get instant alerts|yes, keep me updated|change your local station|brought to you by|learn more about|wapo\.zeustechnology\.com|googleadd al jazeera|loading)\b",
            lowered,
        ):
            return False
        if "##" in text or "**" in text:
            return False
        if lowered.startswith("read more") or lowered.startswith("recommended stories"):
            return False
        if anchor_tokens:
            overlap = len(self._meaningful_tokens(text) & anchor_tokens)
            if overlap < min(2, len(anchor_tokens)):
                return False
        return True

    def _normalize_sentence(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip(" -:|,;")
        if normalized and normalized[-1] not in ".!?":
            normalized = f"{normalized}."
        return normalized

    def _sentence_key(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()

    def _meaningful_tokens(self, text: str) -> set[str]:
        stopwords = {
            "the", "and", "for", "with", "from", "that", "this", "into", "order", "now", "what", "when", "will", "have",
            "has", "had", "who", "can", "more", "their", "they", "your", "about", "after", "before", "than", "then",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9']+", (text or "").casefold())
            if len(token) > 3 and token not in stopwords
        }

    def _truncate_words(self, text: str, max_words: int) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]).rstrip(" ,;:") + "..."

    def _sanitize_claim_text(self, text: str) -> str:
        normalized = str(text or "")
        normalized = re.sub(r"\[File:[^\]]+\]", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Enter your email addressSubscribe", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"list of \d+ items.*?end of list", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Loading\s+\*\s*\*\s*\*.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Most Read.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"wapo\.zeustechnology\.com.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"This page has been blocked.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"https?://\S+", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\*\s*\*\s*\*", " ", normalized)
        normalized = re.sub(r"Listen\s*\(\s*\d+\s*(?:min|mins|minute|minutes)\s*\)", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"An error (?:has )?occurred", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Please try again later", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Already a subscriber\??\s*Sign in", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Continue reading your article with a WSJ subscription", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"This copy is for your personal, non-commercial use only\.", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Distribution and use of this material are governed by our Subscriber Agreement and by copyright law\.", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"For non-personal use or to order multiple copies, please contact Dow Jones Reprints at [0-9\-]+ or visit www\.djreprints\.com", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Site search Home News Sport Business Technology Health Culture Arts Travel Earth Audio Video Live Documentaries Weather Newsletters Watch Live", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Add as preferred on Google", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Add as preferred source on Google", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Power Partner Awards.*?Apply Today", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bLead\s+(?=[A-Z])", "", normalized)
        normalized = re.sub(r"LinkedInFacebookXBlueskyLink", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"(?:FILE\s+)?Photo:\s*[^\n.]{1,80}?(?=\s+(?:Listen to this Article|More info|$)|\n)", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Listen to this Article", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"More info\s*\d+:\d+\s*/\s*\d+:\d+", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"You're currently following this author!?", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Want to unfollow\?.*?Follow", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Read in app", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"SaveSaved", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Loading audio narration", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"This story is available exclusively to Business Insider subscribers\..*?(?:Have an account\?\s*Log in|start reading now\.)", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Have an account\?\s*Log in", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bBy[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", " ", normalized)
        normalized = re.sub(r"(?:Skip\s+)?to content", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"(?:PUBLISHED|Posted):\s*[^.]{0,120}\b(?:PST|PDT|EST|EDT|UTC)\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"UPDATED:\s*[^.]{0,120}\b(?:PST|PDT|EST|EDT|UTC)\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"by:\s*[A-Z][^\n]{0,60}", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Trinity Player Getting your Trinity Audio player ready", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Tap to unmute", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Watch on", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"KTLA\s*5\s*KTLA\s*[0-9.]+M\s*subscribers", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Suggest a Correction.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Read Next >.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Provided by Nexstar Media Group, Inc\..*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Enter your email.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Unlock Live News or No Thanks.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"^Save\s+", "", normalized, flags=re.IGNORECASE)

        lowered = normalized.casefold()
        anchor_positions = [
            match.start()
            for anchor in self.CLAIM_TEXT_ANCHORS
            for match in [re.search(re.escape(anchor), normalized, flags=re.IGNORECASE)]
            if match is not None
        ]
        if anchor_positions:
            sorted_anchors = sorted(anchor_positions)
            first_anchor = sorted_anchors[0]
            if len(sorted_anchors) > 1:
                second_anchor = sorted_anchors[1]
                prefix_to_second = lowered[:second_anchor]
                if re.search(r"\b(politics|associated press|pbs news|summary|reuters)\b", prefix_to_second):
                    first_anchor = second_anchor
            prefix = lowered[:first_anchor]
            if first_anchor > 0 and re.search(
                r"\b(your|save|loading|change your local station|pbs news|associated press|exclusive news|comments|most read|blocked|newsletter|subscribe)\b",
                prefix,
            ):
                normalized = normalized[first_anchor:]

        normalized = re.sub(r"\s+", " ", normalized).strip(" -:|,.;")
        lowered = normalized.casefold()
        if any(phrase in lowered for phrase in self.SOURCE_BOILERPLATE_PHRASES):
            return ""
        return normalized

    def _clean_scraped_text(self, text: str) -> str:
        raw_text = str(text or "").replace("\r", "\n")
        raw_text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw_text)
        raw_text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", raw_text)
        raw_text = re.sub(r"https?://\S+", " ", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"<br\s*/?>", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = raw_text.replace("##", "\n")
        raw_text = raw_text.replace("**READ MORE:**", "\nREAD MORE:")
        raw_text = raw_text.replace("**", "")
        raw_text = re.sub(r"\[File:[^\]]+\]", " ", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Loading\s+\*\s*\*\s*\*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Listen\s*\(\s*\d+\s*(?:min|mins|minute|minutes)\s*\)", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Most Read", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"View\s+\d+\s+more stories", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Earlier today", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"This page has been blocked by an extension.*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"blocked by an extension", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Try disabling your extensions", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"An error (?:has )?occurred", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Please try again later", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Already a subscriber\??\s*Sign in", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Continue reading your article with a WSJ subscription", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"This copy is for your personal, non-commercial use only\.", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Distribution and use of this material are governed by our Subscriber Agreement and by copyright law\.", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"For non-personal use or to order multiple copies, please contact Dow Jones Reprints at [0-9\-]+ or visit www\.djreprints\.com", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Site search Home News Sport Business Technology Health Culture Arts Travel Earth Audio Video Live Documentaries Weather Newsletters Watch Live", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Add as preferred on Google", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Add as preferred source on Google", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Power Partner Awards.*?Apply Today", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"LinkedInFacebookXBlueskyLink", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"(?:FILE\s+)?Photo:\s*[^\n.]{1,80}?(?=\s+(?:Listen to this Article|More info|$)|\n)", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Listen to this Article", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"More info\s*\d+:\d+\s*/\s*\d+:\d+", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"You're currently following this author!?", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Want to unfollow\?.*?Follow", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Read in app", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"SaveSaved", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Loading audio narration", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"This story is available exclusively to Business Insider subscribers\..*?(?:Have an account\?\s*Log in|start reading now\.)", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Have an account\?\s*Log in", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"\bBy[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", "\n", raw_text)
        raw_text = re.sub(r"(?:Skip\s+)?to content", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"(?:PUBLISHED|Posted):\s*[^.]{0,120}\b(?:PST|PDT|EST|EDT|UTC)\b", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"UPDATED:\s*[^.]{0,120}\b(?:PST|PDT|EST|EDT|UTC)\b", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"by:\s*[A-Z][^\n]{0,60}", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Trinity Player Getting your Trinity Audio player ready", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Tap to unmute", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Watch on", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"KTLA\s*5\s*KTLA\s*[0-9.]+M\s*subscribers", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Suggest a Correction.*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Read Next >.*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Provided by Nexstar Media Group, Inc\..*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Enter your email.*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"Unlock Live News or No Thanks.*", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"notification-important", "\n", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"list of \d+ items.*?end of list", "\n", raw_text, flags=re.IGNORECASE | re.DOTALL)
        raw_text = re.sub(r"^Save\s+", "", raw_text, flags=re.IGNORECASE | re.MULTILINE)
        raw_text = re.sub(r"\bLead\s+(?=[A-Z])", "", raw_text, flags=re.IGNORECASE)

        cleaned_lines: list[str] = []
        for line in re.split(r"\n+", raw_text):
            candidate = re.sub(r"\s+", " ", line).strip(" -:|,.;")
            if not candidate or self._is_noise_line(candidate):
                continue
            cleaned_lines.append(candidate)

        normalized = " ".join(cleaned_lines)
        normalized = self._clean_source_text(normalized, preserve_case=True)
        normalized = self._collapse_repeated_sentences(normalized)
        return normalized

    def _is_noise_line(self, value: str) -> bool:
        lowered = value.casefold()
        if any(phrase in lowered for phrase in self.SOURCE_BOILERPLATE_PHRASES):
            return True
        if re.search(
            r"\b(donate|donation|subscribe|shop|watchlist|watch now|listenlisten|share|newsletter|sign up|choose station|official site|file photo|leave your feedback|live tv|my station|my list|cabinet room|copylink|read more|recommended stories|educate your inbox|please check your inbox|thank you\.?|visit official site|most read|comments|enter your email address|get instant alerts|yes, keep me updated)\b",
            lowered,
        ):
            return True
        if re.search(r"\b(change your local station|brought to you by|learn more about|wapo\.zeustechnology\.com|googleadd al jazeera|this page has been blocked|reload|published on)\b", lowered):
            return True
        if re.search(r"\b(?:live tv|pbs shows|my station|my list|shop|donate)(?:\s*-\s*(?:live tv|pbs shows|my station|my list|shop|donate|choose station|more from))*\b", lowered):
            return True
        if re.search(r"^by\s+[a-z].*", lowered):
            return True
        if re.search(r"^[a-z]+\s+\d{1,2},\s+\d{4}", lowered):
            return True
        if value.count(" - ") >= 3 and len(value.split()) <= 18:
            return True
        if len(value.split()) <= 5 and value.isupper():
            return True
        return False

    def _clean_source_text(self, text: str, *, preserve_case: bool = False) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        normalized = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", normalized)
        normalized = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", normalized)
        normalized = re.sub(r"\b\d+\s+min\s+read\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b\d+\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes)\s+ago\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b(?:updated|published)\b[^.]{0,80}\b(?:et|utc|edt)\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"#\s*live updates?:", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bBy\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4}\b", " ", normalized)
        normalized = re.sub(r"\bAnalysis by\b[^.]{0,120}", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bFILE PHOTO:\b[^.]{0,180}", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"#+", " ", normalized)
        lowered = normalized.casefold()
        for phrase in self.SOURCE_BOILERPLATE_PHRASES:
            phrase_index = lowered.find(phrase)
            if phrase_index != -1:
                normalized = normalized[:phrase_index]
                lowered = normalized.casefold()
        normalized = re.sub(r"\s+", " ", normalized).strip(" -:|,.;")
        sentence_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
        if len(sentence_parts) >= 2 and sentence_parts[0].rstrip(".!?").casefold() == sentence_parts[1].rstrip(".!?").casefold():
            normalized = sentence_parts[0]
        normalized = re.sub(r"\s+", " ", normalized).strip(" -:|,.;")
        if preserve_case or not normalized:
            return normalized
        return normalized

    def _build_context_references(
        self,
        topic: TrendSignal,
        sources: list[FetchedSource],
        claims: list[SourceClaim],
        country: str | None = None,
    ) -> list[ContextReference]:
        if not sources:
            return []

        candidates = self._extract_reference_candidates(topic, sources, claims)
        if not candidates:
            return []

        references: list[ContextReference] = []
        for candidate in candidates[: self.CONTEXT_REFERENCE_LIMIT]:
            reference = self._search_context_reference(candidate, country or self.config.country)
            if reference is None:
                reference = self._fallback_context_reference(candidate)
            if reference is not None:
                references.append(reference)
        return references

    def _extract_reference_candidates(self, topic: TrendSignal, sources: list[FetchedSource], claims: list[SourceClaim]) -> list[str]:
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
            add_candidate(topic_text, 2)

        for source in sources:
            for candidate in self.ENTITY_PATTERN.findall(source.title or ""):
                add_candidate(candidate, 3)

        for claim in claims:
            for candidate in self.ENTITY_PATTERN.findall(claim.claim or ""):
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
        return [
            display_names[key]
            for key, _ in ordered
            if display_names.get(key, "").casefold() not in banned_publishers
        ][: self.CONTEXT_REFERENCE_LIMIT + 2]

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
        if not cleaned or cleaned in self.ENTITY_STOPWORDS or len(cleaned) < 3:
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

    def _search_context_reference(self, entity: str, country: str) -> ContextReference | None:
        if self.config.mock_mode or GoogleSearch is None or not self.config.serpapi_key:
            return None

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
            source = "wikipedia" if "wikipedia.org" in url.lower() else "background"
            if entity.casefold() == "postal service" and re.search(r"\b(indie pop|band|group|album|song)\b", f"{title} {snippet}".casefold()):
                return None
            return ContextReference(
                entity=entity,
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                summary=snippet[:220],
            )
        return None

    def _fallback_context_reference(self, entity: str) -> ContextReference:
        return ContextReference(
            entity=entity,
            title=f"{entity} background",
            url=f"https://en.wikipedia.org/wiki/{slugify(entity).replace('-', '_')}",
            snippet=f"Reference background for {entity}.",
            source="wikipedia",
            summary=f"Background context for {entity} that helps explain the current story.",
        )

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