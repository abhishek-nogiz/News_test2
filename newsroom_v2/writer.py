from __future__ import annotations

from html import escape
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

try:
    from groq import Groq
except ImportError:
    Groq = None

from config import AppConfig
from news_agent.models import ResearchPacket

from .models import CandidateDossier, NewsroomDraft, NewsroomPlan


class NewsroomWritingService:
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

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def can_repair(self) -> bool:
        return not self.config.mock_mode and Groq is not None and bool(self.config.groq_api_key)

    def draft(self, dossier: CandidateDossier, plan: NewsroomPlan) -> NewsroomDraft:
        if self.config.mock_mode or Groq is None or not self.config.groq_api_key:
            return self._mock_draft(dossier, plan)

        best_draft: NewsroomDraft | None = None
        for route in self._generation_routes():
            try:
                draft = self._request_draft(route["api_key"], route["model"], dossier, plan)
            except Exception:
                continue
            best_draft = draft
            if draft.publish_ready:
                return draft

        return best_draft or self._mock_draft(dossier, plan)

    def repair(self, dossier: CandidateDossier, plan: NewsroomPlan, failed_draft: NewsroomDraft) -> NewsroomDraft:
        if not self.can_repair():
            return failed_draft

        retry_instruction = self._build_retry_instruction(failed_draft)
        if not retry_instruction:
            return failed_draft

        best_draft: NewsroomDraft | None = None
        for route in self._generation_routes():
            try:
                repaired = self._request_draft(
                    route["api_key"],
                    route["model"],
                    dossier,
                    plan,
                    retry_instruction=retry_instruction,
                )
            except Exception:
                continue
            best_draft = repaired
            if repaired.publish_ready:
                return repaired

        return best_draft or failed_draft

    def save(self, draft: NewsroomDraft, dossier: CandidateDossier) -> dict[str, str]:
        slug = self._slugify(dossier.topic.keyword)
        output_dir = Path(self.config.storage_root) / "newsroom-v2"
        output_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = output_dir / f"{slug}.md"
        html_path = output_dir / f"{slug}.html"
        json_path = output_dir / f"{slug}.json"

        markdown_path.write_text(draft.article_markdown, encoding="utf-8")
        html_path.write_text(draft.article_html, encoding="utf-8")
        json_path.write_text(
            json.dumps(
                {
                    "topic": dossier.topic.keyword,
                    "article_mode": dossier.decision.article_mode,
                    "headline": draft.headline,
                    "dek": draft.dek,
                    "summary": draft.summary,
                    "publish_ready": draft.publish_ready,
                    "source_diagnostics": self._source_diagnostics(dossier),
                    "validation": {
                        "editorial_score": draft.validation.editorial_score if draft.validation else None,
                        "structure_score": draft.validation.structure_score if draft.validation else None,
                        "grounding_score": draft.validation.grounding_score if draft.validation else None,
                        "issues": draft.validation.issues if draft.validation else [],
                        "publish": draft.validation.publish if draft.validation else None,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "markdown": str(markdown_path),
            "html": str(html_path),
            "json": str(json_path),
        }

    def _generation_routes(self) -> list[dict[str, str]]:
        routes: list[dict[str, str]] = []
        if self.config.groq_api_key:
            routes.append({"api_key": self.config.groq_api_key, "model": self.config.groq_model})

        fallback_api_key = self.config.groq_fallback_api_key or self.config.groq_api_key
        fallback_model = self.config.groq_fallback_model or self.config.groq_model
        fallback_route = {"api_key": fallback_api_key or "", "model": fallback_model}
        if fallback_route["api_key"] and fallback_route not in routes:
            routes.append(fallback_route)
        return routes

    def _request_draft(
        self,
        api_key: str,
        model: str,
        dossier: CandidateDossier,
        plan: NewsroomPlan,
        retry_instruction: str | None = None,
    ) -> NewsroomDraft:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a senior news writer. Return JSON only. "
                        "Write from the supplied fact spine, keep one editorial angle, avoid filler, and do not invent facts."
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_prompt(dossier, plan, retry_instruction=retry_instruction),
                },
            ],
            temperature=0.2,
            top_p=0.9,
            max_completion_tokens=3200,
            stream=False,
        )
        content = (response.choices[0].message.content or "{}").replace("```json", "").replace("```", "").strip()
        payload = json.loads(content)
        return self._coerce_draft(payload, dossier, plan)

    def _build_prompt(
        self,
        dossier: CandidateDossier,
        plan: NewsroomPlan,
        retry_instruction: str | None = None,
    ) -> str:
        fact_spine = dossier.fact_spine
        section_lines = "\n".join(
            self._section_lock_line(index, heading, goal, plan)
            for index, (heading, goal) in enumerate(zip(plan.section_heads, plan.section_goals))
        )
        timeline = "\n".join(f"- {item}" for item in fact_spine.timeline[:6])
        evidence = "\n".join(
            (
                f"- [{entry.section}] {entry.claim}\n"
                f"  source: {entry.source_title} | tier={entry.source_tier} | published={entry.published_at or 'unknown'}\n"
                f"  url: {entry.source_url}\n"
                f"  snippet: {entry.supporting_snippet}"
            )
            for entry in dossier.evidence_ledger[:10]
        ) or "- none"
        constraints = "\n".join(f"- {item}" for item in plan.constraints)
        open_questions = "\n".join(f"- {item}" for item in fact_spine.open_questions[:4]) or "- none"
        retry_block = f"\nRepair instructions:\n{retry_instruction}\n" if retry_instruction else ""
        section_evidence = "\n\n".join(
            self._section_evidence_block(index, heading, plan)
            for index, heading in enumerate(plan.section_heads)
        ) or "- none"
        target_words = self._target_word_range(dossier)

        return f"""
Topic: {dossier.topic.keyword}
Article mode: {plan.article_mode}
Headline lane: {plan.headline}
Angle: {plan.angle}
Lead focus: {plan.lead_focus}
Nut graf: {plan.nut_graf}

Section plan:
{section_lines}

Timeline:
{timeline}

Key facts:
{evidence}

Why it matters:
- {fact_spine.why_it_matters}

Consequence:
- {fact_spine.consequence}

Open questions:
{open_questions}

Target length:
- {target_words}

Evidence ledger only:
- Draft only from the evidence ledger above.
- If a claim is not in the ledger, do not write it.

Section locks:
{section_evidence}

Constraints:
{constraints}

{retry_block}

Return one valid JSON object with exactly these keys:
- headline: string
- dek: string
- summary: string
- article_markdown: string
- article_html: string

Hard requirements:
- Keep one clear angle.
- Write like a real news article, not SEO aggregation.
- Hit the target length only when the evidence supports it; add context and chronology, not filler.
- The first 2 paragraphs must establish event and chronology.
- Use exactly one h1, two substantive h2 sections, and one Sources h2.
- Each H2 may use only the evidence assigned to that section lock.
- Keep sourcing natural and visible only where needed.
- Use natural reference hyperlinks in the body, not citation-style source links.
- Use at most one hyperlink in a paragraph.
- Hyperlink the important phrase or named entity, not the publisher name or a raw source title.
- Keep Sources at the bottom.
- Wikipedia links are allowed only for background entities such as people, organizations, or places, never as proof of the core reporting claim.
- No filler phrases like significant development, raises questions, could signal, may be seen.
- Use only the evidence ledger for factual claims.
- article_html must be wrapped in <article class=\"trend-agent-post\">...</article>.
""".strip()

    def _build_retry_instruction(self, failed_draft: NewsroomDraft) -> str:
        if failed_draft.validation is None or not failed_draft.validation.issues:
            return ""

        issue_lines = "\n".join(f"- {issue}" for issue in failed_draft.validation.issues[:8])
        return (
            "The previous draft failed newsroom validation. Rewrite the article from scratch and fix all of these issues:\n"
            f"{issue_lines}\n"
            "Keep the same core angle, but improve chronology, consequence, sourcing, and article structure where needed."
        )

    def _coerce_draft(self, payload: dict, dossier: CandidateDossier, plan: NewsroomPlan) -> NewsroomDraft:
        headline = str(payload.get("headline") or plan.headline).strip()
        dek = str(payload.get("dek") or plan.nut_graf).strip()
        summary = str(payload.get("summary") or dossier.decision.reasoning).strip()
        article_markdown = str(payload.get("article_markdown") or "").strip()
        article_html = str(payload.get("article_html") or "").strip()

        if not article_markdown or not article_html:
            return self._mock_draft(dossier, plan)

        article_html = self._normalize_html(article_html, dossier.research, headline)
        if self._should_use_evidence_scaffold(article_html, dossier):
            return self._evidence_scaffold_draft(dossier, plan, headline=headline, dek=dek, summary=summary)
        return NewsroomDraft(
            headline=headline,
            dek=dek,
            article_markdown=article_markdown,
            article_html=article_html,
            article_mode=plan.article_mode,
            summary=summary,
            publish_ready=self._is_publish_ready(article_html),
        )

    def _should_use_evidence_scaffold(self, article_html: str, dossier: CandidateDossier) -> bool:
        visible_text = self._plain_text(article_html)
        word_count = len(re.findall(r"\b\w+\b", visible_text))
        has_block_markup = "<!-- wp:" in article_html
        return word_count < self._minimum_target_words(dossier) or not has_block_markup

    def _minimum_target_words(self, dossier: CandidateDossier) -> int:
        article_mode = dossier.decision.article_mode
        evidence_count = len(dossier.evidence_ledger)
        source_count = dossier.research_source_count or len(dossier.research.sources)

        if article_mode == "full_article":
            return 650 if evidence_count >= 4 and source_count >= 4 else 550
        if article_mode == "explainer":
            return 500 if evidence_count >= 3 and source_count >= 3 else 400
        return 350 if evidence_count >= 2 and source_count >= 2 else 250

    def _evidence_scaffold_draft(
        self,
        dossier: CandidateDossier,
        plan: NewsroomPlan,
        *,
        headline: str,
        dek: str,
        summary: str,
    ) -> NewsroomDraft:
        if "single editorial angle" in dossier.decision.missing_elements:
            return self._mixed_angle_brief_draft(dossier, plan, headline=headline, dek=dek, summary=summary)

        intro_paragraphs = self._scaffold_intro_paragraphs(dossier)
        section_one_body, section_two_body = self._scaffold_section_bodies(dossier, plan, seen=intro_paragraphs)
        source_items = "\n".join(
            f"- [{source.title}]({source.url})"
            for source in dossier.research.sources[:5]
            if source.title and source.url
        )
        article_markdown = "\n\n".join(
            [
                f"# {headline}",
                *intro_paragraphs,
                f"## {plan.section_heads[0]}\n" + "\n\n".join(section_one_body),
                f"## {plan.section_heads[1]}\n" + "\n\n".join(section_two_body),
                "## Sources\n" + source_items,
            ]
        ).strip()

        html_parts = ['<article class="trend-agent-post">', self._heading_block(1, headline)]
        html_parts.extend(self._paragraph_block(text) for text in intro_paragraphs if text)
        html_parts.append(self._heading_block(2, plan.section_heads[0]))
        html_parts.extend(self._paragraph_block(text) for text in section_one_body if text)
        html_parts.append(self._heading_block(2, plan.section_heads[1]))
        html_parts.extend(self._paragraph_block(text) for text in section_two_body if text)
        html_parts.append(self._heading_block(2, "Sources"))
        html_parts.append(self._sources_block(dossier.research))
        html_parts.append("</article>")
        article_html = "".join(html_parts)
        return NewsroomDraft(
            headline=headline,
            dek=dek,
            article_markdown=article_markdown,
            article_html=article_html,
            article_mode=plan.article_mode,
            summary=summary,
            publish_ready=self._is_publish_ready(article_html),
        )

    def _mixed_angle_brief_draft(
        self,
        dossier: CandidateDossier,
        plan: NewsroomPlan,
        *,
        headline: str,
        dek: str,
        summary: str,
    ) -> NewsroomDraft:
        coverage_lines = self._coverage_lines(dossier)
        intro_paragraphs = [
            "Current reporting under this topic mixes separate story lines rather than one confirmed development.",
            self._coverage_overview(dossier, coverage_lines),
            dossier.decision.reasoning,
        ]
        intro_paragraphs = [text for text in self._dedupe_paragraphs(intro_paragraphs) if text]

        section_one_body = coverage_lines[:3] or ["The current source bundle is split across separate developments that should not be written as one article."]
        section_two_body = [
            "Those pieces do not yet point to one shared jurisdiction, consequence, or editorial angle.",
            "Keep this topic as a brief until one verified development clearly dominates the reporting bundle.",
        ]

        source_items = "\n".join(
            f"- [{source.title}]({source.url})"
            for source in dossier.research.sources[:5]
            if source.title and source.url
        )
        article_markdown = "\n\n".join(
            [
                f"# {headline}",
                *intro_paragraphs,
                f"## {plan.section_heads[0]}\n" + "\n\n".join(section_one_body),
                f"## {plan.section_heads[1]}\n" + "\n\n".join(section_two_body),
                "## Sources\n" + source_items,
            ]
        ).strip()

        html_parts = ['<article class="trend-agent-post">', self._heading_block(1, headline)]
        html_parts.extend(self._paragraph_block(text) for text in intro_paragraphs if text)
        html_parts.append(self._heading_block(2, plan.section_heads[0]))
        html_parts.extend(self._paragraph_block(text) for text in section_one_body if text)
        html_parts.append(self._heading_block(2, plan.section_heads[1]))
        html_parts.extend(self._paragraph_block(text) for text in section_two_body if text)
        html_parts.append(self._heading_block(2, "Sources"))
        html_parts.append(self._sources_block(dossier.research))
        html_parts.append("</article>")
        article_html = "".join(html_parts)
        return NewsroomDraft(
            headline=headline,
            dek=dek,
            article_markdown=article_markdown,
            article_html=article_html,
            article_mode=plan.article_mode,
            summary=summary,
            publish_ready=self._is_publish_ready(article_html),
        )

    def _scaffold_intro_paragraphs(self, dossier: CandidateDossier) -> list[str]:
        fact_spine = dossier.fact_spine
        paragraphs = [fact_spine.core_event]

        timeline_bits = [self._clean_timeline_point(item) for item in fact_spine.timeline[:2] if item]
        timeline_bits = [item for item in timeline_bits if item]
        timeline_bits = self._dedupe_paragraphs(timeline_bits, seen=paragraphs)
        if timeline_bits:
            paragraphs.append(" ".join(timeline_bits))

        why_it_matters = fact_spine.why_it_matters or fact_spine.consequence or dossier.decision.reasoning
        if why_it_matters and why_it_matters not in paragraphs:
            paragraphs.append(why_it_matters)
        return self._dedupe_paragraphs(paragraphs)[:3]

    def _scaffold_section_bodies(
        self,
        dossier: CandidateDossier,
        plan: NewsroomPlan,
        *,
        seen: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        section_one_entries = self._section_entries(plan, 0) or dossier.evidence_ledger[:3]
        section_two_entries = self._section_entries(plan, 1) or dossier.evidence_ledger[3:6] or dossier.evidence_ledger[:2]
        section_one_body = [self._evidence_paragraph(entry, dossier) for entry in section_one_entries[:3]]
        section_two_body = [self._evidence_paragraph(entry, dossier) for entry in section_two_entries[:3]]
        seen_paragraphs = list(seen or [])
        section_one_body = self._dedupe_paragraphs(section_one_body)
        section_two_body = self._dedupe_paragraphs(section_two_body, seen=section_one_body)

        if not section_one_body:
            section_one_body = self._dedupe_paragraphs([dossier.fact_spine.core_event])
        if not section_two_body:
            fallback = dossier.fact_spine.why_it_matters or dossier.fact_spine.consequence or dossier.decision.reasoning
            section_two_body = self._dedupe_paragraphs([fallback], seen=seen_paragraphs + section_one_body)
        if not section_two_body:
            section_two_body = [dossier.decision.reasoning or dossier.fact_spine.why_it_matters or dossier.fact_spine.core_event]
        return section_one_body, section_two_body

    def _evidence_paragraph(self, entry, dossier: CandidateDossier) -> str:
        source = self._source_for_url(dossier.research, entry.source_url)
        publisher = source.publisher.strip() if source is not None else ""
        attributed_claim = self._attributed_claim(entry.claim, publisher)
        parts = [attributed_claim]

        snippet = self._clean_supporting_snippet(entry.supporting_snippet, entry.claim)
        if snippet:
            parts.append(snippet)
        return " ".join(part for part in parts if part).strip()

    def _attributed_claim(self, claim: str, publisher: str) -> str:
        normalized_claim = claim.strip().rstrip(".")
        if not normalized_claim:
            return ""
        if not publisher:
            return normalized_claim + "."
        lowered_claim = normalized_claim[0].lower() + normalized_claim[1:] if normalized_claim[:1].isupper() else normalized_claim
        return f"According to {publisher}, {lowered_claim}."

    def _clean_supporting_snippet(self, snippet: str, claim: str) -> str:
        normalized = self._clean_source_boilerplate(snippet)
        if not normalized:
            return ""
        if normalized.casefold() in claim.casefold():
            return ""
        return normalized + "."

    def _clean_timeline_point(self, value: str) -> str:
        normalized = self._clean_source_boilerplate(value)
        normalized = re.sub(r"^(?:background|now|next):\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(
            r"^\d{1,2}/\d{1,2}/\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM),\s+[+-]\d{4}\s+UTC:\s*",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        return normalized.strip().rstrip(".") + ("." if normalized else "")

    def _clean_source_boilerplate(self, value: str) -> str:
        cleaned = self._plain_text(value or "")
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", cleaned)
        cleaned = re.sub(r"\b\d+\s+min\s+read\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bBy\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", " ", cleaned)
        cleaned = re.sub(
            r"\b\d{1,2}/\d{1,2}/\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM),\s+[+-]\d{4}\s+UTC:?\s*",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\b(?:updated|published)\b[^.]{0,80}\b(?:et|utc)\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"#\s*live updates?:", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\*\s*\*\s*\*", " ", cleaned)

        lowered = cleaned.casefold()
        for phrase in self.SOURCE_BOILERPLATE_PHRASES:
            phrase_index = lowered.find(phrase)
            if phrase_index != -1:
                cleaned = cleaned[:phrase_index]
                lowered = cleaned.casefold()

        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|,.;")
        return cleaned

    def _coverage_lines(self, dossier: CandidateDossier) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for source in dossier.research.sources[:4]:
            title = self._coverage_title(source.title)
            if not title:
                continue
            key = self._paragraph_key(title)
            if not key or key in seen:
                continue
            seen.add(key)
            publisher = source.publisher.strip() or "One source"
            lines.append(f"{publisher} is covering {title[0].lower() + title[1:] if len(title) > 1 else title.lower()}.")
        return lines

    def _coverage_overview(self, dossier: CandidateDossier, coverage_lines: list[str]) -> str:
        if not coverage_lines:
            return "The current source bundle does not yet line up behind one publishable news angle."
        if len(coverage_lines) == 1:
            return coverage_lines[0]
        joined = "; ".join(line.rstrip(".") for line in coverage_lines[:3])
        return joined + "."

    def _coverage_title(self, title: str) -> str:
        normalized = self._clean_source_boilerplate(title)
        normalized = re.sub(r"\s*\|\s*.*$", "", normalized)
        normalized = re.sub(r"\bLive Results\b.*$", "", normalized, flags=re.IGNORECASE)
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
        if len(sentences) > 1 and re.search(r"\bI\b|\bmy\b|\bwe\b", sentences[1], flags=re.IGNORECASE):
            normalized = sentences[0]
        return normalized.strip().rstrip(".?!")

    def _dedupe_paragraphs(self, paragraphs: list[str], *, seen: list[str] | None = None) -> list[str]:
        deduped: list[str] = []
        accepted_keys = [self._paragraph_key(value) for value in (seen or []) if self._paragraph_key(value)]

        for paragraph in paragraphs:
            candidate = (paragraph or "").strip()
            candidate_key = self._paragraph_key(candidate)
            if not candidate_key:
                continue
            if any(candidate_key == key or candidate_key in key or key in candidate_key for key in accepted_keys):
                continue
            deduped.append(candidate)
            accepted_keys.append(candidate_key)

        return deduped

    def _paragraph_key(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", self._plain_text(value or "").casefold()).strip()

    def _mock_draft(self, dossier: CandidateDossier, plan: NewsroomPlan) -> NewsroomDraft:
        fact_spine = dossier.fact_spine
        section_one_body, section_two_body = self._mock_section_bodies(dossier, plan)
        source_items = "\n".join(
            f"- [{source.title}]({source.url})"
            for source in dossier.research.sources[:5]
            if source.title and source.url
        )
        article_markdown = "\n\n".join(
            [
                f"# {plan.headline}",
                fact_spine.core_event,
                f"{fact_spine.timeline[0] if fact_spine.timeline else fact_spine.core_event} {fact_spine.key_facts[0] if fact_spine.key_facts else ''}".strip(),
                f"## {plan.section_heads[0]}\n" + "\n\n".join(section_one_body),
                f"## {plan.section_heads[1]}\n" + "\n\n".join(section_two_body),
                "## Sources\n" + source_items,
            ]
        ).strip()
        article_html = self._build_mock_html(plan, dossier, section_one_body, section_two_body)
        return NewsroomDraft(
            headline=plan.headline,
            dek=plan.nut_graf,
            article_markdown=article_markdown,
            article_html=article_html,
            article_mode=plan.article_mode,
            summary=dossier.decision.reasoning,
            publish_ready=self._is_publish_ready(article_html),
        )

    def _build_mock_html(
        self,
        plan: NewsroomPlan,
        dossier: CandidateDossier,
        section_one_body: list[str],
        section_two_body: list[str],
    ) -> str:
        fact_spine = dossier.fact_spine

        html_parts = [
            '<article class="trend-agent-post">',
            self._heading_block(1, plan.headline),
            self._paragraph_block(fact_spine.core_event),
            self._paragraph_block(fact_spine.timeline[0] if fact_spine.timeline else fact_spine.why_it_matters),
            self._heading_block(2, plan.section_heads[0]),
        ]
        html_parts.extend(self._paragraph_block(text) for text in section_one_body if text)
        html_parts.append(self._heading_block(2, plan.section_heads[1]))
        html_parts.extend(self._paragraph_block(text) for text in section_two_body if text)
        html_parts.append(self._heading_block(2, "Sources"))
        html_parts.append(self._sources_block(dossier.research))
        html_parts.append("</article>")
        return "".join(html_parts)

    def _mock_section_bodies(self, dossier: CandidateDossier, plan: NewsroomPlan) -> tuple[list[str], list[str]]:
        fact_spine = dossier.fact_spine
        section_one_body = self._section_claims(plan, 0)
        section_two_body = self._section_claims(plan, 1)

        if not section_one_body:
            section_one_body = [fact_spine.key_facts[0] if fact_spine.key_facts else fact_spine.core_event]
            if len(fact_spine.timeline) > 1:
                section_one_body.append(fact_spine.timeline[1])

        if not section_two_body:
            section_two_body = [fact_spine.why_it_matters or fact_spine.consequence or dossier.decision.reasoning]
            if fact_spine.consequence and fact_spine.consequence not in section_two_body:
                section_two_body.append(fact_spine.consequence)

        return section_one_body[:2], section_two_body[:2]

    def _section_lock_line(self, index: int, heading: str, goal: str, plan: NewsroomPlan) -> str:
        evidence_labels = ", ".join(entry.section for entry in self._section_entries(plan, index)) or "none"
        return f"- {heading}: {goal} | allowed evidence buckets: {evidence_labels}"

    def _target_word_range(self, dossier: CandidateDossier) -> str:
        article_mode = dossier.decision.article_mode
        evidence_count = len(dossier.evidence_ledger)
        source_count = dossier.research_source_count or len(dossier.research.sources)

        if article_mode == "full_article":
            return "800-1100 words" if evidence_count >= 4 and source_count >= 4 else "650-850 words"
        if article_mode == "explainer":
            return "650-900 words" if evidence_count >= 3 and source_count >= 3 else "500-700 words"
        return "450-650 words" if evidence_count >= 2 and source_count >= 2 else "300-450 words"

    def _section_evidence_block(self, index: int, heading: str, plan: NewsroomPlan) -> str:
        lines = [f"- {heading}:"]
        entries = self._section_entries(plan, index)
        if not entries:
            lines.append("  - none")
            return "\n".join(lines)

        for entry in entries:
            lines.append(
                "  - "
                f"[{entry.section}] {entry.claim} | {entry.source_title} | tier={entry.source_tier} | url={entry.source_url}"
            )
        return "\n".join(lines)

    def _section_entries(self, plan: NewsroomPlan, index: int):
        if index >= len(plan.section_evidence):
            return []
        return plan.section_evidence[index]

    def _section_claims(self, plan: NewsroomPlan, index: int) -> list[str]:
        return [entry.claim for entry in self._section_entries(plan, index) if entry.claim]

    def _normalize_html(self, article_html: str, research: ResearchPacket, headline: str) -> str:
        normalized = article_html.strip()
        if not normalized.startswith("<article"):
            normalized = f'<article class="trend-agent-post">{normalized}</article>'
        if "<h2>Sources</h2>" not in normalized:
            normalized = normalized.replace("</article>", self._heading_block(2, "Sources") + self._sources_block(research) + "</article>")
        if not re.search(r"<h1\b", normalized, flags=re.IGNORECASE):
            normalized = normalized.replace("<article class=\"trend-agent-post\">", f'<article class="trend-agent-post">{self._heading_block(1, headline)}', 1)
        normalized = self._strip_inline_source_citation_paragraphs(normalized, research)
        normalized = self._rewrite_publisher_name_links(normalized, research)
        normalized = self._demote_citation_style_body_links(normalized, research)
        normalized = self._limit_body_links_per_paragraph(normalized)
        normalized = self._sanitize_html(normalized, research)
        return normalized

    def _is_publish_ready(self, article_html: str) -> bool:
        h1_count = len(re.findall(r"<h1\b", article_html, flags=re.IGNORECASE))
        h2_count = len(re.findall(r"<h2\b", article_html, flags=re.IGNORECASE))
        has_sources = "<h2>Sources</h2>" in article_html
        return h1_count == 1 and h2_count >= 3 and has_sources

    def _strip_inline_source_citation_paragraphs(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def repl(match: re.Match[str]) -> str:
            paragraph_html = match.group(2).strip()
            if self._is_source_citation_paragraph(paragraph_html, research):
                return ""
            return match.group(0)

        normalized = paragraph_pattern.sub(repl, article_body)
        return re.sub(r"\n{3,}", "\n\n", normalized) + sources_tail

    def _demote_citation_style_body_links(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def rewrite_paragraph(match: re.Match[str]) -> str:
            paragraph_body = match.group(2)

            def rewrite_anchor(anchor_match: re.Match[str]) -> str:
                href = anchor_match.group(1).strip()
                anchor_text = self._plain_text(anchor_match.group(2)).strip()
                source = self._source_for_url(research, href)
                if source is None:
                    return anchor_match.group(0)
                if not self._is_citation_style_anchor(anchor_text, source.title, source.publisher, href):
                    return anchor_match.group(0)
                return escape(anchor_text)

            rewritten_body = anchor_pattern.sub(rewrite_anchor, paragraph_body)
            return match.group(1) + rewritten_body + match.group(3)

        return paragraph_pattern.sub(rewrite_paragraph, article_body) + sources_tail

    def _limit_body_links_per_paragraph(self, article_html: str) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def rewrite_paragraph(match: re.Match[str]) -> str:
            paragraph_body = match.group(2)
            anchors = list(anchor_pattern.finditer(paragraph_body))
            if len(anchors) <= 1:
                return match.group(0)

            kept = 0

            def rewrite_anchor(anchor_match: re.Match[str]) -> str:
                nonlocal kept
                kept += 1
                if kept == 1:
                    return anchor_match.group(0)
                return escape(self._plain_text(anchor_match.group(2)).strip())

            rewritten_body = anchor_pattern.sub(rewrite_anchor, paragraph_body)
            return match.group(1) + rewritten_body + match.group(3)

        return paragraph_pattern.sub(rewrite_paragraph, article_body) + sources_tail

    def _rewrite_publisher_name_links(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def rewrite_paragraph(match: re.Match[str]) -> str:
            paragraph_body = match.group(2)
            anchor_match = anchor_pattern.search(paragraph_body)
            if anchor_match is None:
                return match.group(0)

            href = anchor_match.group(1).strip()
            anchor_text = self._plain_text(anchor_match.group(2)).strip()
            source = self._source_for_url(research, href)
            if source is None or not self._looks_like_publisher_anchor(anchor_text, source.publisher):
                return match.group(0)

            replacement = self._rewrite_anchor_to_reference(paragraph_body, anchor_match, href)
            if replacement == paragraph_body:
                return match.group(0)
            return match.group(1) + replacement + match.group(3)

        return paragraph_pattern.sub(rewrite_paragraph, article_body) + sources_tail

    def _sanitize_html(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        article_body = re.sub(r"\[(?:insert|add|tbd)[^\]]*\]", "", article_body, flags=re.IGNORECASE)
        article_body = re.sub(r"\bTBD\b", "", article_body, flags=re.IGNORECASE)
        article_body = self._strip_profile_links(article_body)
        article_body = self._strip_generic_more_info_paragraphs(article_body)
        article_body = self._strip_source_boilerplate_paragraphs(article_body)
        article_body = self._rewrite_raw_url_labels(article_body, research)
        sources_tail = self._rewrite_raw_url_labels(sources_tail, research)
        return article_body + sources_tail

    def _strip_source_boilerplate_paragraphs(self, fragment: str) -> str:
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def repl(match: re.Match[str]) -> str:
            paragraph_html = match.group(2)
            plain_text = self._plain_text(paragraph_html)
            lowered = plain_text.casefold()
            has_boilerplate = any(phrase in lowered for phrase in self.SOURCE_BOILERPLATE_PHRASES)
            has_raw_timestamp = bool(
                re.search(
                    r"\b\d{1,2}/\d{1,2}/\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM),\s+[+-]\d{4}\s+UTC\b",
                    plain_text,
                    flags=re.IGNORECASE,
                )
            )
            if not has_boilerplate and not has_raw_timestamp:
                return match.group(0)

            cleaned = self._clean_source_boilerplate(paragraph_html)
            if not cleaned:
                return ""
            return match.group(1) + escape(cleaned) + match.group(3)

        return paragraph_pattern.sub(repl, fragment)

    def _strip_profile_links(self, fragment: str) -> str:
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def repl(match: re.Match[str]) -> str:
            href = match.group(1).casefold()
            if any(token in href for token in {"/author/", "/authors/", "/profile/", "/profiles/", "/bio/", "/staff/"}):
                return escape(self._plain_text(match.group(2)).strip())
            return match.group(0)

        return anchor_pattern.sub(repl, fragment)

    def _strip_generic_more_info_paragraphs(self, fragment: str) -> str:
        paragraph_pattern = re.compile(
            r"((?:<!--\s*wp:paragraph\s*-->\s*)?<p[^>]*>)(.*?)(</p>\s*(?:<!--\s*/wp:paragraph\s*-->)?)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def repl(match: re.Match[str]) -> str:
            text = self._plain_text(match.group(2)).casefold()
            if "for more information" in text:
                return ""
            return match.group(0)

        return paragraph_pattern.sub(repl, fragment)

    def _rewrite_raw_url_labels(self, fragment: str, research: ResearchPacket) -> str:
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def repl(match: re.Match[str]) -> str:
            href = match.group(1).strip()
            anchor_text = self._plain_text(match.group(2)).strip()
            normalized_text = self._normalize_link_text(anchor_text)
            normalized_href = self._normalize_link_text(href)
            if normalized_text != normalized_href:
                return match.group(0)
            source = self._source_for_url(research, href)
            label = source.title.strip() if source is not None and source.title.strip() else urlsplit(href).netloc.removeprefix("www.")
            return f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'

        return anchor_pattern.sub(repl, fragment)

    def _rewrite_anchor_to_reference(self, paragraph_body: str, anchor_match: re.Match[str], href: str) -> str:
        after_anchor = paragraph_body[anchor_match.end():]
        clause_match = re.match(r'(\s*,\s*)([^.]{12,160}?)(?=(?:</?[^>]+>)*[.;]|$)', after_anchor, flags=re.DOTALL)
        if clause_match is None:
            return paragraph_body

        reference_text = self._plain_text(clause_match.group(2)).strip(" ,;:")
        if not reference_text or len(reference_text.split()) < 3:
            return paragraph_body

        linked_reference = f'<a href="{escape(href, quote=True)}">{escape(reference_text)}</a>'
        return (
            paragraph_body[:anchor_match.start()]
            + escape(self._plain_text(anchor_match.group(2)).strip())
            + clause_match.group(1)
            + linked_reference
            + after_anchor[clause_match.end():]
        )

    def _is_source_citation_paragraph(self, paragraph_html: str, research: ResearchPacket) -> bool:
        citation_match = re.fullmatch(
            r'(?:\[\d+\]\s*)?<a\s+href="([^"]+)"[^>]*>.*?</a>\s*[.:;,!?-]*',
            paragraph_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if citation_match is None:
            return False
        return self._source_for_url(research, citation_match.group(1).strip()) is not None

    def _is_citation_style_anchor(self, anchor_text: str, title: str, publisher: str, href: str) -> bool:
        normalized_text = self._normalize_link_text(anchor_text)
        if not normalized_text:
            return False
        candidates = {
            self._normalize_link_text(title),
            self._normalize_link_text(publisher),
            self._normalize_link_text(href),
        }
        return normalized_text in {candidate for candidate in candidates if candidate}

    def _looks_like_publisher_anchor(self, anchor_text: str, publisher: str) -> bool:
        normalized_text = self._normalize_link_text(anchor_text)
        normalized_publisher = self._normalize_link_text(publisher)
        return bool(normalized_text and normalized_publisher and normalized_text == normalized_publisher)

    def _source_for_url(self, research: ResearchPacket, href: str):
        normalized_href = href.strip().rstrip("/")
        for source in research.sources:
            if (source.url or "").strip().rstrip("/") == normalized_href:
                return source
        return None

    def _split_before_sources_section(self, article_html: str) -> tuple[str, str]:
        sources_heading_match = re.search(r"<h2[^>]*>\s*Sources\s*</h2>", article_html, flags=re.IGNORECASE | re.DOTALL)
        if sources_heading_match is None:
            return article_html, ""
        return article_html[:sources_heading_match.start()], article_html[sources_heading_match.start():]

    def _plain_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()

    def _normalize_link_text(self, value: str) -> str:
        normalized = self._plain_text(value).casefold()
        normalized = re.sub(r"^\[\d+\]\s*", "", normalized)
        return normalized.rstrip(". ,;:!?").rstrip("/")

    def _heading_block(self, level: int, text: str) -> str:
        marker = '{"level":1}' if level == 1 else ""
        opening = f'<!-- wp:heading {marker} -->' if marker else '<!-- wp:heading -->'
        return f'{opening}<h{level}>{escape(text)}</h{level}><!-- /wp:heading -->'

    def _paragraph_block(self, text: str) -> str:
        return f'<!-- wp:paragraph --><p>{escape(text)}</p><!-- /wp:paragraph -->'

    def _sources_block(self, research: ResearchPacket) -> str:
        items = []
        for source in research.sources[:5]:
            if not source.url.strip():
                continue
            title = source.title.strip() or source.url.strip()
            items.append(f'<li><a href="{escape(source.url.strip(), quote=True)}">{escape(title)}</a></li>')
        return '<!-- wp:list --><ul>' + ''.join(items) + '</ul><!-- /wp:list -->'

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug or "draft"

    def _source_diagnostics(self, dossier: CandidateDossier) -> list[dict[str, str | int | bool]]:
        diagnostics: list[dict[str, str | int | bool]] = []
        for index, source in enumerate(dossier.research.sources, start=1):
            diagnostics.append(
                {
                    "rank": index,
                    "title": self._trim(source.title, limit=140),
                    "publisher": source.publisher.strip(),
                    "source_tier": source.source_tier.strip(),
                    "published_at": source.published_at.strip(),
                    "url": source.url.strip(),
                    "has_content": bool((source.content or source.snippet).strip()),
                }
            )
        return diagnostics

    def _trim(self, value: str, limit: int) -> str:
        normalized = " ".join((value or "").split()).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 3)].rstrip() + "..."