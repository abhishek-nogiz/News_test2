from __future__ import annotations

import re

from config import AppConfig

from .models import CandidateDossier, NewsroomDraft, NewsroomPlan, NewsroomValidation


class NewsroomValidationService:
    ALLOWED_BLOCKS = {"heading", "paragraph", "list", "quote", "separator"}
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
    }
    FILLER_PHRASES = {
        "significant development",
        "strong track record",
        "raises questions",
        "may be seen",
        "could signal",
        "likely involve",
        "what this means now",
        "why this matters now",
    }
    BODY_SPECULATION = {"may", "could", "might", "likely"}
    CHRONOLOGY_CUES = {"previously", "earlier", "before", "after", "now", "next", "timeline", "on ", "202"}

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def validate(self, draft: NewsroomDraft, dossier: CandidateDossier, plan: NewsroomPlan) -> NewsroomValidation:
        body = draft.article_html.strip()
        visible_text = self._visible_text(body)
        article_body, _sources_tail = self._split_before_sources(body)
        body_text = self._visible_text(article_body)
        issues: list[str] = []

        editorial_score = 50
        structure_score = 50
        grounding_score = 40

        if not dossier.decision.should_write:
            issues.append("Editorial triage rejected this topic before drafting")
        elif dossier.decision.article_mode == "brief":
            issues.append("Editorial triage downgraded this topic to a brief; do not publish as a full article")
        else:
            editorial_score += 10

        for gap in dossier.decision.missing_elements:
            if gap == "chronology":
                issues.append("Research dossier still lacks chronology support")
            elif gap == "clear consequence":
                issues.append("Research dossier still lacks a clear consequence")
            elif gap == "reporting depth":
                issues.append("Research dossier still lacks reporting depth")

        words = len(re.findall(r"\b\w+\b", visible_text))
        minimum_words = self._minimum_words(dossier.decision.article_mode)
        if words >= minimum_words:
            editorial_score += 15
        else:
            issues.append(f"Draft is too short for {dossier.decision.article_mode}: {words} words")

        h1_count = self._tag_count(body, "h1")
        h2_count = self._tag_count(body, "h2")
        sources_count = len(re.findall(r"<h2[^>]*>\s*Sources\s*</h2>", body, flags=re.IGNORECASE))
        block_names = self._extract_block_names(body)
        unsupported_blocks = sorted(block_names - self.ALLOWED_BLOCKS)

        if h1_count == 1:
            structure_score += 10
        else:
            issues.append("Draft must contain exactly one H1")

        if h2_count == 3 and sources_count == 1:
            structure_score += 15
        else:
            issues.append("Draft must contain two story H2 sections plus one Sources H2")

        if block_names:
            structure_score += 10
        else:
            issues.append("Draft must use Gutenberg block markup")

        if unsupported_blocks:
            issues.append(f"Unsupported Gutenberg blocks used: {', '.join(unsupported_blocks)}")

        if re.search(r"<article\b[^>]*>", body, flags=re.IGNORECASE):
            structure_score += 5
        else:
            issues.append("Draft must be wrapped in an article element")

        if self._has_chronology(body_text, dossier):
            editorial_score += 10
        else:
            issues.append("Draft does not establish chronology clearly enough")

        if self._has_consequence(body_text, dossier):
            editorial_score += 10
        else:
            issues.append("Draft does not explain the consequence clearly enough")

        filler_hits = self._filler_hits(visible_text)
        if filler_hits:
            issues.append("Draft contains generic filler language")
        else:
            editorial_score += 10

        citation_hits = self._citation_style_link_hits(article_body, dossier)
        if citation_hits:
            issues.append("Draft uses citation-style source links in the body")
        else:
            editorial_score += 5

        if self._placeholder_hits(visible_text):
            issues.append("Draft contains placeholder text")

        if self._profile_link_hits(article_body):
            issues.append("Draft links to author or profile pages in the body")

        if self._raw_url_label_hits(body):
            issues.append("Draft contains raw URL link labels")

        if self._generic_more_info_hits(body_text):
            issues.append("Draft uses generic 'for more information' boilerplate")

        if self._source_boilerplate_hits(visible_text):
            issues.append("Draft contains source-site boilerplate")

        if self._raw_timestamp_hits(body_text):
            issues.append("Draft contains raw source timestamps")

        if self._paragraphs_with_excess_links(article_body):
            issues.append("Draft uses more than one hyperlink in a body paragraph")
        else:
            structure_score += 5

        speculative_hits = self._speculative_hits(body_text)
        if speculative_hits:
            issues.append("Draft contains unsupported speculative language")
        else:
            editorial_score += 5

        attribution_hits = self._attribution_hits(body_text, dossier)
        minimum_attribution_hits = min(2, len(dossier.research.sources))
        if attribution_hits >= 1:
            grounding_score += 20
        else:
            issues.append("Draft is missing attributed reporting in the body")

        if attribution_hits >= minimum_attribution_hits:
            grounding_score += 25
        else:
            issues.append("Draft does not use enough reporting sources in the body")

        if dossier.tavily_enriched or dossier.research_source_count >= 3:
            grounding_score += 10

        editorial_score = min(editorial_score, 100)
        structure_score = min(structure_score, 100)
        grounding_score = min(grounding_score, 100)
        publish = not issues and min(editorial_score, structure_score, grounding_score) >= 70
        return NewsroomValidation(
            editorial_score=editorial_score,
            structure_score=structure_score,
            grounding_score=grounding_score,
            issues=issues,
            publish=publish,
        )

    def _minimum_words(self, article_mode: str) -> int:
        if article_mode == "full_article":
            return self.config.min_article_words
        if article_mode == "explainer":
            return max(450, self.config.min_article_words - 200)
        return 250

    def _visible_text(self, html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()

    def _split_before_sources(self, html: str) -> tuple[str, str]:
        match = re.search(r"<h2[^>]*>\s*Sources\s*</h2>", html, flags=re.IGNORECASE)
        if match is None:
            return html, ""
        return html[:match.start()], html[match.start():]

    def _tag_count(self, html: str, tag: str) -> int:
        return len(re.findall(rf"<{tag}\b", html, flags=re.IGNORECASE))

    def _extract_block_names(self, html: str) -> set[str]:
        return {
            match.group(1)
            for match in re.finditer(r"<!--\s*wp:([a-z0-9-]+)(?:\s+[^>]*)?-->", html, flags=re.IGNORECASE)
        }

    def _has_chronology(self, body_text: str, dossier: CandidateDossier) -> bool:
        lowered = body_text.casefold()
        if any(cue in lowered for cue in self.CHRONOLOGY_CUES):
            return True
        timeline_fragments = []
        for item in dossier.fact_spine.timeline[:3]:
            fragment = item.split(":", 1)[-1].strip().casefold()
            if fragment:
                timeline_fragments.append(fragment[:50])
        return any(fragment and fragment in lowered for fragment in timeline_fragments)

    def _has_consequence(self, body_text: str, dossier: CandidateDossier) -> bool:
        lowered = body_text.casefold()
        consequence = (dossier.fact_spine.consequence or dossier.fact_spine.why_it_matters or "").casefold().strip()
        if consequence and consequence[:60] in lowered:
            return True
        return any(token in lowered for token in {"impact", "stakes", "means", "matters", "next", "consequence"})

    def _filler_hits(self, visible_text: str) -> list[str]:
        lowered = visible_text.casefold()
        return [phrase for phrase in self.FILLER_PHRASES if phrase in lowered]

    def _speculative_hits(self, body_text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", body_text)
        hits: list[str] = []
        for sentence in sentences:
            lowered = sentence.casefold()
            if not lowered:
                continue
            if not any(token in lowered for token in self.BODY_SPECULATION):
                continue
            if any(marker in lowered for marker in {"according to", "reported", "said", "expects", "forecast", "projects"}):
                continue
            hits.append(sentence.strip())
        return hits

    def _attribution_hits(self, body_text: str, dossier: CandidateDossier) -> int:
        lowered = body_text.casefold()
        hits = 0
        seen: set[str] = set()
        for source in dossier.research.sources:
            publisher = source.publisher.strip().casefold()
            if publisher and publisher not in seen and publisher in lowered:
                seen.add(publisher)
                hits += 1
        if "according to" in lowered or "reported" in lowered:
            hits += 1
        return hits

    def _citation_style_link_hits(self, article_body: str, dossier: CandidateDossier) -> int:
        count = 0
        paragraph_pattern = re.compile(r"<p[^>]*>(.*?)</p>", flags=re.IGNORECASE | re.DOTALL)
        for paragraph_match in paragraph_pattern.finditer(article_body):
            paragraph_html = paragraph_match.group(1).strip()
            citation_match = re.fullmatch(
                r'(?:\[\d+\]\s*)?<a\s+href="([^"]+)"[^>]*>.*?</a>\s*[.:;,!?-]*',
                paragraph_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if citation_match is None:
                continue
            href = citation_match.group(1).strip().rstrip("/")
            if any((source.url or "").strip().rstrip("/") == href for source in dossier.research.sources):
                count += 1
        return count

    def _placeholder_hits(self, visible_text: str) -> list[str]:
        return re.findall(r"\[(?:insert|add|tbd)[^\]]*\]|\bTBD\b", visible_text, flags=re.IGNORECASE)

    def _profile_link_hits(self, article_body: str) -> int:
        return len(
            re.findall(
                r'<a\s+href="[^"]*(?:/author/|/authors/|/profile/|/profiles/|/bio/|/staff/)[^"]*"',
                article_body,
                flags=re.IGNORECASE,
            )
        )

    def _raw_url_label_hits(self, html: str) -> int:
        return len(
            re.findall(
                r'<a\s+href="([^"]+)"[^>]*>\s*(https?://[^<\s]+)\s*</a>',
                html,
                flags=re.IGNORECASE,
            )
        )

    def _generic_more_info_hits(self, body_text: str) -> int:
        lowered = body_text.casefold()
        return 1 if "for more information" in lowered else 0

    def _source_boilerplate_hits(self, visible_text: str) -> list[str]:
        lowered = visible_text.casefold()
        return [phrase for phrase in self.SOURCE_BOILERPLATE_PHRASES if phrase in lowered]

    def _raw_timestamp_hits(self, body_text: str) -> list[str]:
        return re.findall(
            r"\b\d{1,2}/\d{1,2}/\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM),\s+[+-]\d{4}\s+UTC\b",
            body_text,
            flags=re.IGNORECASE,
        )

    def _paragraphs_with_excess_links(self, article_body: str) -> int:
        count = 0
        for paragraph_html in re.findall(r"<p[^>]*>(.*?)</p>", article_body, flags=re.IGNORECASE | re.DOTALL):
            if len(re.findall(r"<a\b[^>]*>", paragraph_html, flags=re.IGNORECASE)) > 1:
                count += 1
        return count