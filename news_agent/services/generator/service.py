from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
try:
    from groq import Groq
except ImportError:
    Groq = None

try:
    from markdown import markdown as render_markdown
except ImportError:
    render_markdown = None

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import ContentPlan, GeneratedArticle, ResearchPacket, TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import normalize_topic_category, slugify, tokenize


class BlogGenerationService:
    ALLOWED_GUTENBERG_BLOCKS = {"heading", "paragraph", "list", "quote", "separator", "image"}
    GENERIC_KEYWORD_TOKENS = {
        "bank", "news", "video", "clip", "live", "story", "stories", "latest", "update", "updates",
        "report", "reports", "analysis", "topic",
    }
    SPORTS_TOKENS = {
        "match", "game", "series", "playoff", "playoffs", "final", "finals", "nba", "nfl", "mlb", "nhl",
        "tennis", "soccer", "football", "baseball", "inning", "goal", "set", "tournament",
    }
    POLITICS_TOKENS = {
        "election", "elections", "trump", "biden", "senate", "congress", "government",
        "policy", "policies", "president", "minister", "parliament", "tariff", "vote", "voting",
        "campaign", "diplomacy", "diplomatic", "republican", "democrat", "geopolitics", "geopolitical",
        "primary", "runoff", "governor", "incumbent", "court", "justice",
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def generate(self, topic: TrendTopic, research: ResearchPacket, plan: ContentPlan) -> GeneratedArticle:
        if self.config.mock_mode or Groq is None or not self.config.groq_api_key:
            return self._mock_article(topic, research, plan)

        best_article: GeneratedArticle | None = None
        best_score = -1
        last_error: Exception | None = None

        for route in self._generation_routes():
            try:
                article = self._generate_with_route(route, topic, research, plan)
            except Exception as exc:
                last_error = exc
                continue

            article_score = self._draft_score(article)
            if article_score > best_score:
                best_article = article
                best_score = article_score

            if self._is_publish_ready_draft(article):
                return article

        if best_article is not None:
            return best_article
        if last_error is not None:
            raise last_error
        return self._mock_article(topic, research, plan)

    def _generation_routes(self) -> list[dict[str, str]]:
        routes: list[dict[str, str]] = []

        if self.config.groq_api_key:
            routes.append(
                {
                    "label": "primary",
                    "api_key": self.config.groq_api_key,
                    "model": self.config.groq_model,
                }
            )

        fallback_api_key = self.config.groq_fallback_api_key or self.config.groq_api_key
        fallback_model = self.config.groq_fallback_model or self.config.groq_model
        fallback_route = {
            "label": "fallback",
            "api_key": fallback_api_key or "",
            "model": fallback_model,
        }
        if fallback_route["api_key"] and fallback_route not in routes:
            routes.append(fallback_route)

        return routes

    def _generate_with_route(
        self,
        route: dict[str, str],
        topic: TrendTopic,
        research: ResearchPacket,
        plan: ContentPlan,
    ) -> GeneratedArticle:
        client = Groq(api_key=route["api_key"])
        article = self._request_article(client, route["model"], topic, research, plan)
        if self._is_publish_ready_draft(article):
            return article

        expanded_article = self._request_article(
            client,
            route["model"],
            topic,
            research,
            plan,
            retry_instruction=(
                f"The previous draft was only {self._word_count(article.article_html or article.article_markdown)} words or missed the required structure. "
                f"Regenerate the full article and make it at least {self._target_word_count()} words. "
                "Keep the H2 structure clean, avoid unnecessary H3 subheads, ensure the Gutenberg blocks are valid, and fully develop every section."
            ),
        )
        if self._draft_score(expanded_article) > self._draft_score(article):
            return expanded_article
        return article

    def _request_article(
        self,
        client: Groq,
        model: str,
        topic: TrendTopic,
        research: ResearchPacket,
        plan: ContentPlan,
        retry_instruction: str | None = None,
    ) -> GeneratedArticle:
        try:
            response = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a newsroom writer producing WordPress-ready news copy. Return JSON only. "
                            "Ground all claims in the supplied research, attribute major assertions, and do not invent facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._build_prompt(topic, research, plan, retry_instruction=retry_instruction),
                    },
                ],
                temperature=0.3,
                top_p=0.9,
                max_completion_tokens=self._max_completion_tokens(),
                stream=False,
            )
        except Exception as exc:
            if self._looks_like_completion_budget_error(exc):
                response = client.chat.completions.create(
                    model=model,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a newsroom writer producing WordPress-ready news copy. Return JSON only. "
                                "Ground all claims in the supplied research, attribute major assertions, and do not invent facts."
                            ),
                        },
                        {
                            "role": "user",
                            "content": self._build_prompt(topic, research, plan, retry_instruction=retry_instruction),
                        },
                    ],
                    temperature=0.3,
                    top_p=0.9,
                    max_completion_tokens=self._max_completion_tokens(expanded=True),
                    stream=False,
                )
            elif self._looks_like_request_budget_error(exc):
                raise RuntimeError(
                    "Groq rejected the request because the combined prompt and completion budget was too large. "
                    "Try a smaller model, fewer research results, or a lower article word target."
                ) from exc
            else:
                raise
        content = response.choices[0].message.content or "{}"
        content = content.replace("```json", "").replace("```", "").strip()
        payload = json.loads(content)
        return self._coerce_article(payload, topic, research, plan)

    def _build_prompt(
        self,
        topic: TrendTopic,
        research: ResearchPacket,
        plan: ContentPlan,
        retry_instruction: str | None = None,
    ) -> str:
        internal_article = self._select_internal_article(topic)
        prompt_sources = self._select_prompt_sources(research)
        source_digest = "\n".join(
            "\n".join(
                [
                    f"Source {index}: {self._trim_text(source.title, 140)}",
                    f"Publisher: {self._trim_text(source.publisher, 60)}",
                    f"Snippet: {self._trim_text(source.snippet, 220)}",
                    f"Extract: {self._trim_text(source.content, 260)}",
                ]
            )
            for index, source in enumerate(prompt_sources, start=1)
        )
        allowed_sources = "\n".join(
            f"- {self._trim_text(source.title, 100)} | {source.url}"
            for source in prompt_sources
            if source.url
        )
        facts = "\n".join(f"- {self._trim_text(fact, 220)}" for fact in research.facts[: len(prompt_sources)])
        present_facts = "\n".join(f"- {self._trim_text(fact, 220)}" for fact in research.present)
        past_facts = "\n".join(f"- {self._trim_text(fact, 220)}" for fact in research.past)
        future_facts = "\n".join(f"- {self._trim_text(fact, 220)}" for fact in research.future)
        claim_lines = "\n".join(
            f"- [{claim.section}/{claim.source_tier}] {self._trim_text(claim.claim, 220)} | {claim.source_title} | {claim.source_url}"
            for claim in research.claims[:6]
        )
        context_reference_lines = "\n".join(
            f"- {reference.entity} | {reference.url} | {self._trim_text(reference.title or reference.snippet or reference.entity, 120)}"
            for reference in research.context_references
        )
        sections = "\n".join(f"- {section}" for section in plan.sections)
        section_instruction = "Use these section goals as internal guidance and rewrite them into story-specific H2 headings:"
        if plan.article_type == "politics_news":
            section_instruction = "Use this flat section order and rewrite each goal into a concise, story-specific H2 heading for the visible article, with no extra subtopics or nested headings. If a section goal already gives a strong implication angle, stay close to that source-driven idea:"
        secondary_keywords = ", ".join(plan.secondary_keywords)
        retry_block = f"\nRetry instruction:\n{retry_instruction}\n" if retry_instruction else ""
        research_context = self._trim_text(research.context, 900)
        memory_notes = "\n".join(f"- {note}" for note in plan.memory_notes)
        memory_block = f"\nEditorial memory:\n{memory_notes}\n" if memory_notes else ""
        internal_article_block = (
            f"\nInternal article suggestion:\n- {internal_article['title']} | {internal_article['url']}\n"
            if internal_article
            else "\nInternal article suggestion:\n- none available; do not invent one.\n"
        )

        return f"""
Topic: {topic.keyword}
Audience: {plan.audience}
Tone: {plan.tone}
Article type: {plan.article_type}
Primary keyword: {plan.primary_keyword}
Secondary keywords: {secondary_keywords}
Brief: {plan.brief}

{section_instruction}
{sections}

Grounding facts:
{facts}

Lead fact:
- {self._trim_text(research.lead, 220)}

Present developments:
{present_facts}

Past background:
{past_facts}

What comes next:
{future_facts}

Attributed claims:
{claim_lines}

Context references:
{context_reference_lines}

Research summary:
{research_context}

Condensed source digest:
{source_digest}

{memory_block}

{internal_article_block}

{retry_block}

Allowed sources:
{allowed_sources}

Return one valid JSON object with exactly these keys:
- catchy_title: string and must start with the primary keyword naturally
- seo_keywords: array of strings
- meta_description: string under 160 characters and include the primary keyword
- blog_outline: array of strings
- article_markdown: string containing a concise internal review outline in Markdown using H1/H2 headings and short bullet points only
- article_html: string containing WordPress-ready Gutenberg block markup
- image_prompts: array of exactly 3 strings

Hard requirements:
- article_html must be wrapped in <article class="trend-agent-post">...</article>
- Inside the article, use only core Gutenberg block comments for these blocks: heading, paragraph, list, quote, separator
- Every visible heading or paragraph must be inside Gutenberg block delimiters like <!-- wp:heading --> ... <!-- /wp:heading -->
- Use exactly one <h1> near the top and include the primary keyword naturally in that H1
- Use 1 to 2 substantive <h2> sections before the final Sources section.
- Make at least one substantive <h2> include the primary keyword or a very close synonym naturally.
- Do not use any <h3>, <h4>, or deeper headings.
- Write as a news article, not a trend explainer. Do not use headings such as "Why this topic is trending now" or filler framing about virality.
- The opening paragraph must state the newest confirmed development and include the core reported fact pattern: who, what office or event, and one concrete result or number when available.
- When a second substantive section is used, it should explain why the development matters now or what it changes.
- The body should clearly connect present developments, relevant past context, and any next-step context without creating a separate template-style section just for forward-looking filler.
- Keep headings specific to the story. Avoid mechanical labels and avoid stacking multiple subheadings unless the story genuinely needs them.
- Do not reuse the section-goal text verbatim as visible headings.
- For sports stories, do not use the literal heading "Why This Matters Now". Rewrite the second H2 around the game, series, matchup, or playoff stakes.
- For politics stories, use exactly two substantive <h2> sections before Sources: first the political shift or result, then what it means for the party, race, or governing stakes.
- For politics stories, do not use the literal headings "The Political Shift" or "What This Means Now". Rewrite them into story-specific headings that name the state, party, office, or race when that information is available.
- Attribute major claims in the prose with phrases such as "according to" or "the outlet reported" when the source matters.
- Use at least two attributed source references in the body before the final Sources section whenever two or more reporting links are available.
- Use 2 to 3 inline background/reference links in the body when two or more relevant context references are supplied.
- Use context references as inline background links when they clarify a named person, institution, place, or event mentioned in the article.
- Do not place source-only citation paragraphs such as "[1] source title" inside the body. Keep reporting links in attributed prose and in the final Sources section only.
- Add a final public Sources section at the bottom using the supplied reporting links.
- External links are allowed in inline context references, in the final Sources section, and in one allowed internal Also Read link when provided.
- If an internal article suggestion is provided, insert one short mid-article Also Read callout after the first 2 or 3 paragraphs using only that title and URL. Render it as a paragraph callout, not as a heading.
- If no internal article suggestion is provided, do not invent an Also Read link.
- article_markdown is for internal review only, so keep it compact: headings plus short bullets, not full article prose
- Write at least {self._target_word_count()} words
- Keep paragraphs short: 2 to 3 sentences each.
- Keep most sentences under 20 words where possible; break long compound sentences into two shorter ones.
- Prefer active voice and direct verbs over passive constructions.
- Avoid unsourced speculation. Do not use words like "may", "could", or "likely" unless the sentence is clearly attributed.
- Do not include scripts, styles, code fences, or placeholder markup
- Do not add citations or claims that are not supported by the provided facts
- Do not wrap the JSON in code fences
- Do not add text before or after the JSON
""".strip()

    def _target_word_count(self) -> int:
        return max(self.config.min_article_words + 150, int(self.config.min_article_words * 1.2))

    def _max_completion_tokens(self, expanded: bool = False) -> int:
        ceiling = 5600 if expanded else 4600
        return min(ceiling, max(1800, int(self._target_word_count() * 2.8)))

    def _select_prompt_sources(self, research: ResearchPacket):
        prompt_sources = [source for source in research.sources if source.title or source.snippet or source.content]
        if not prompt_sources:
            return research.sources[:2]
        return prompt_sources[:4]

    def _word_count(self, article_body: str) -> int:
        visible_text = re.sub(r"<[^>]+>", " ", article_body)
        return len(re.findall(r"\b\w+\b", visible_text))

    def _draft_score(self, article: GeneratedArticle) -> int:
        body = article.article_html or article.article_markdown
        h2_count = len(re.findall(r"<h2\b", body, flags=re.IGNORECASE))
        return (
            self._word_count(body)
            + 100 * min(h2_count, 3)
            - 75 * max(0, h2_count - 3)
            - 50 * len(re.findall(r"<h3\b", body, flags=re.IGNORECASE))
            + 100 * len(re.findall(r"<!--\s*wp:", body, flags=re.IGNORECASE))
        )

    def _is_publish_ready_draft(self, article: GeneratedArticle) -> bool:
        body = article.article_html or article.article_markdown
        words = self._word_count(body)
        h1_count = len(re.findall(r"<h1\b", body, flags=re.IGNORECASE))
        h2_count = len(re.findall(r"<h2\b", body, flags=re.IGNORECASE))
        has_gutenberg_blocks = bool(re.search(r"<!--\s*wp:", body, flags=re.IGNORECASE))
        return (
            words >= self._target_word_count()
            and h1_count == 1
            and 2 <= h2_count <= 3
            and has_gutenberg_blocks
        )

    def _trim_text(self, value: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 3)].rstrip() + "..."

    def _trim_to_length(self, value: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        if len(normalized) <= limit:
            return normalized

        shortened = normalized[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
        if shortened:
            return shortened
        return normalized[:limit].rstrip(" ,;:-")

    def _looks_like_request_budget_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "request too large" in message or ("413" in message and "token" in message)

    def _looks_like_completion_budget_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "json_validate_failed" in message or "max completion tokens reached" in message

    def _coerce_article(
        self,
        payload: dict,
        topic: TrendTopic,
        research: ResearchPacket,
        plan: ContentPlan,
    ) -> GeneratedArticle:
        article = GeneratedArticle(
            catchy_title=str(payload.get("catchy_title") or f"{topic.keyword}: What it means right now"),
            seo_keywords=[str(item) for item in payload.get("seo_keywords", [])] or [plan.primary_keyword],
            meta_description=str(
                payload.get("meta_description")
                or f"A grounded breakdown of {topic.keyword} using current sources and practical analysis."
            ),
            blog_outline=[str(item) for item in payload.get("blog_outline", [])] or plan.sections,
            article_markdown=str(payload.get("article_markdown") or "").strip(),
            article_html=str(payload.get("article_html") or "").strip(),
            image_prompts=[str(item) for item in payload.get("image_prompts", [])][:3],
        )

        focus_keyword = self._normalize_focus_keyword(plan.primary_keyword, topic)
        article.seo_keywords = self._normalize_seo_keywords(article.seo_keywords, focus_keyword, plan)
        article.catchy_title = self._normalize_catchy_title(article.catchy_title, focus_keyword, topic)
        article.meta_description = self._normalize_meta_description(article.meta_description, focus_keyword, topic)

        if len(article.image_prompts) < 3:
            article.image_prompts.extend(
                [
                    f"Editorial cover image for {topic.keyword}",
                    f"Explainer graphic showing the drivers behind {topic.keyword}",
                    f"Professional thumbnail illustrating the business impact of {topic.keyword}",
                ][len(article.image_prompts):]
            )

        if not article.article_markdown:
            article.article_markdown = self._build_markdown_fallback(article.catchy_title, article.blog_outline, research)

        if not article.article_html:
            article.article_html = self._render_wordpress_html(article.article_markdown)

        article.article_html = self._normalize_article_html(
            article.article_html,
            research,
            topic,
            focus_keyword=focus_keyword,
            title=article.catchy_title,
        )
        article.article_markdown = self._normalize_article_markdown(
            article.article_markdown,
            topic,
            title=article.catchy_title,
        )

        return article

    def _normalize_focus_keyword(self, focus_keyword: str, topic: TrendTopic) -> str:
        cleaned = re.sub(r"\s+", " ", focus_keyword or "").strip()
        if cleaned:
            return cleaned
        return re.sub(r"\s+", " ", topic.keyword or "").strip()

    def _focus_keyword_content_word_count(self, focus_keyword: str | None) -> int:
        stop_tokens = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with", "from"}
        count = 0
        for word in re.split(r"\s+", focus_keyword or ""):
            normalized = re.sub(r"[^a-z0-9-]", "", word.casefold())
            if normalized and normalized not in stop_tokens:
                count += 1
        return count

    def _normalize_seo_keywords(self, seo_keywords: list[str], focus_keyword: str, plan: ContentPlan) -> list[str]:
        ordered = [focus_keyword, *seo_keywords, *plan.secondary_keywords]
        normalized: list[str] = []
        seen: set[str] = set()
        for keyword in ordered:
            candidate = re.sub(r"\s+", " ", keyword or "").strip()
            if not candidate:
                continue
            candidate_key = candidate.casefold()
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            normalized.append(candidate)
        return normalized[:8]

    def _normalize_catchy_title(self, title: str, focus_keyword: str, topic: TrendTopic) -> str:
        focus_display = self._display_focus_keyword(focus_keyword)
        normalized = re.sub(r"\s+", " ", title or "").strip(" :-")
        if not normalized:
            normalized = f"{focus_display}: what changed and why it matters"
            return self._shorten_seo_title(normalized, focus_keyword)
        if self._has_exact_focus_prefix(normalized, focus_keyword):
            return self._shorten_seo_title(normalized, focus_keyword)

        tail = ""
        for separator in [":", " - ", " | "]:
            if separator in normalized:
                tail = normalized.split(separator, 1)[1].strip()
                break
        if not tail:
            tail = normalized
        if tail.casefold().startswith(topic.keyword.casefold()):
            tail = tail[len(topic.keyword):].strip(" :-") or tail
        tail = self._dedupe_leading_focus_terms(tail, focus_keyword)
        if not tail:
            tail = "what changed and why it matters"
        return self._shorten_seo_title(f"{focus_display}: {tail}", focus_keyword)

    def _has_exact_focus_prefix(self, title: str, focus_keyword: str) -> bool:
        normalized_title = re.sub(r"\s+", " ", title or "").strip()
        normalized_focus = re.sub(r"\s+", " ", focus_keyword or "").strip()
        if not normalized_title or not normalized_focus:
            return False
        if not normalized_title.casefold().startswith(normalized_focus.casefold()):
            return False
        if len(normalized_title) == len(normalized_focus):
            return True
        next_char = normalized_title[len(normalized_focus)]
        return next_char.isspace() or next_char in {":", "-", "|", ",", "(", "/"}

    def _normalize_meta_description(self, meta_description: str, focus_keyword: str, topic: TrendTopic) -> str:
        focus_display = self._display_focus_keyword(focus_keyword)
        normalized = re.sub(r"\s+", " ", meta_description or "").strip()
        if not normalized:
            normalized = f"{topic.keyword}: what changed and why the latest development matters now."
        if focus_keyword.casefold() not in normalized.casefold():
            normalized = f"{focus_display}: {normalized}"

        prefix = f"{focus_display}: "
        if normalized.casefold().startswith(prefix.casefold()):
            tail = self._dedupe_leading_focus_terms(normalized[len(prefix):], focus_keyword)
            replacements = [
                (r"\bcampaign gains traction\b", "campaign gains ground"),
                (r"\bState Sen\.\b", "Sen."),
                (r"\bgovernor's race\b", "governor race"),
                (r"\bwhile\b", "as"),
            ]
            for pattern, replacement in replacements:
                tail = re.sub(pattern, replacement, tail, flags=re.IGNORECASE)
            normalized = f"{prefix}{tail.strip()}"

        normalized = self._limit_phrase_occurrences(
            normalized,
            focus_keyword,
            max_occurrences=2,
            replacement="this development",
        )
        normalized = self._reduce_speculative_language(normalized)

        return self._trim_to_length(normalized, 145)

    def _limit_phrase_occurrences(
        self,
        text: str,
        phrase: str,
        *,
        max_occurrences: int,
        replacement: str,
    ) -> str:
        normalized = re.sub(r"\s+", " ", text or "").strip()
        if not normalized or not phrase or max_occurrences < 0:
            return normalized

        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
        matches = list(pattern.finditer(normalized))
        if len(matches) <= max_occurrences:
            return normalized

        extras = len(matches) - max_occurrences
        updated = normalized
        for match in reversed(matches):
            if extras <= 0:
                break
            updated = updated[:match.start()] + replacement + updated[match.end():]
            extras -= 1

        updated = re.sub(r"\s+", " ", updated).strip()
        updated = re.sub(r"\s+([,.;:!?])", r"\1", updated)
        return updated

    def _display_focus_keyword(self, focus_keyword: str) -> str:
        parts: list[str] = []
        for token in focus_keyword.split():
            if token.isupper() or token.lower() in {"boe", "fed", "doj", "fda", "sec", "eu", "uk", "us"}:
                parts.append(token.upper())
            else:
                parts.append(token.capitalize())
        return " ".join(parts)

    def _dedupe_leading_focus_terms(self, value: str, focus_keyword: str) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip(" :-")
        focus_parts = self._display_focus_keyword(focus_keyword).split()
        prefixes: list[str] = []
        if focus_parts:
            prefixes.append(" ".join(focus_parts))
        if len(focus_parts) >= 2:
            prefixes.append(" ".join(focus_parts[:2]))
        if focus_parts:
            prefixes.append(focus_parts[0])

        for prefix in prefixes:
            normalized = re.sub(
                rf"^{re.escape(prefix)}(?:['’]s)?\s+",
                "",
                normalized,
                flags=re.IGNORECASE,
            )
        normalized = re.sub(r"^['’]s\s+", "", normalized, flags=re.IGNORECASE)
        return normalized.strip(" :-")

    def _shorten_seo_title(self, title: str, focus_keyword: str) -> str:
        normalized = re.sub(r"\s+", " ", title or "").strip(" :-")
        max_length = 58
        if len(normalized) <= max_length:
            return normalized

        focus_display = self._display_focus_keyword(focus_keyword)
        prefix = f"{focus_display}: "
        if normalized.casefold().startswith(prefix.casefold()):
            tail = self._dedupe_leading_focus_terms(normalized[len(prefix):], focus_keyword)
        else:
            prefix = ""
            tail = normalized

        replacements = [
            (r"\bGains Momentum in\b", "Surges in"),
            (r"\bwhat happened and why it matters now\b", "what changed"),
            (r"\bwhat changed and why it matters\b", "what changed"),
            (r"\bGovernor's Race\b", "Governor Race"),
            (r"\bRepublican Party\b", "GOP"),
        ]
        for pattern, replacement in replacements:
            tail = re.sub(pattern, replacement, tail, flags=re.IGNORECASE)

        shortened = f"{prefix}{tail}".strip()
        if len(shortened) <= max_length:
            return shortened
        if prefix:
            return f"{prefix}{self._trim_to_length(tail, max_length - len(prefix))}".strip()
        return self._trim_to_length(shortened, max_length)

    def _clean_fact_sentence(self, value: str, limit: int = 110) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        normalized = re.sub(r"^[!#*\-\s>]+", "", normalized)
        replacements = [
            (r"\bState Sen\.\b", "State Senator"),
            (r"\bSen\.\b", "Senator"),
            (r"\bGov\.\b", "Governor"),
            (r"\bRep\.\b", "Representative"),
            (r"\bU\.S\.\b", "US"),
        ]
        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized)
        if not normalized:
            return ""

        sentence = re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0].strip()
        sentence = sentence.rstrip(" .")
        return self._trim_to_length(sentence, limit)

    def _best_focus_fact(self, research: ResearchPacket, focus_keyword: str) -> tuple[str, str]:
        focus_tokens = {token for token in tokenize(focus_keyword) if len(token) > 2}
        candidates: list[tuple[str, str]] = []
        for source in research.sources[:5]:
            publisher = source.publisher or source.title or "the reporting"
            for raw_text in [source.title, source.snippet, source.content]:
                fact = self._clean_fact_sentence(raw_text)
                if fact:
                    candidates.append((fact, publisher))

        for raw_text in [*research.present[:2], research.lead, *research.future[:1], *research.past[:1]]:
            fact = self._clean_fact_sentence(raw_text)
            if fact:
                candidates.append((fact, "the latest reporting"))

        if not candidates:
            return (self._display_focus_keyword(focus_keyword), "the latest reporting")

        def score(item: tuple[str, str]) -> tuple[int, int]:
            fact, _publisher = item
            fact_tokens = set(tokenize(fact))
            return (sum(1 for token in focus_tokens if token in fact_tokens), -len(fact))

        return max(candidates, key=score)

    def _ensure_focus_keyword_intro(
        self,
        article_html: str,
        research: ResearchPacket,
        focus_keyword: str | None,
    ) -> str:
        if not focus_keyword:
            return article_html

        paragraph_pattern = re.compile(
            r"(<!--\s*wp:paragraph\s*-->\s*<p>)(.*?)(</p>\s*<!--\s*/wp:paragraph\s*-->)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = paragraph_pattern.search(article_html)
        if match is None:
            return article_html

        paragraph_text = re.sub(r"<[^>]+>", " ", match.group(2))
        paragraph_text = re.sub(r"\s+", " ", paragraph_text).strip()
        first_sentence = re.split(r"(?<=[.!?])\s+", paragraph_text, maxsplit=1)[0]
        if focus_keyword.casefold() in first_sentence.casefold():
            return article_html

        fact, publisher = self._best_focus_fact(research, focus_keyword)
        intro_sentence = (
            f"{self._display_focus_keyword(focus_keyword)} is back in focus after {publisher} reported that {fact}."
        )
        replacement = f"{match.group(1)}{escape(intro_sentence)}{match.group(3)}"
        return article_html[:match.start()] + replacement + article_html[match.end():]

    def _supporting_fact_candidates(self, research: ResearchPacket) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        for source in research.sources[:5]:
            fact = self._clean_fact_sentence(source.title or source.snippet or source.content)
            if not fact or not self._is_expandable_fact(fact):
                continue
            key = fact.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((fact, source.publisher or source.title or "the reporting"))

        for raw_text in [*research.present[:2], *research.past[:1], *research.future[:1]]:
            fact = self._clean_fact_sentence(raw_text)
            if not fact or not self._is_expandable_fact(fact):
                continue
            key = fact.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((fact, "the latest reporting"))

        return candidates

    def _is_expandable_fact(self, fact: str) -> bool:
        normalized = re.sub(r"\s+", " ", fact or "").strip(" .")
        if not normalized:
            return False

        words = re.findall(r"\b\w+\b", normalized)
        if len(words) < 6:
            return False

        lower = normalized.casefold()
        if lower.endswith((" senator", " representative", " governor", " president", " official")):
            return False

        has_signal = any(
            token in lower
            for token in (
                " is ", " are ", " was ", " were ", " has ", " have ", " had ",
                " votes ", " voted ", " vote ", " says ", " said ", " reports ",
                " reported ", " accused ", " accuses ", " joins ", " joined ",
                " blocks ", " blocked ", " fails ", " failed ", " passes ", " passed ",
            )
        )
        return has_signal

    def _build_expansion_paragraph(self, fact: str, publisher: str, variant_index: int) -> str:
        templates = [
            "{publisher} reports that {fact}, sharpening the picture of what changed.",
            "In {publisher}'s latest account, {fact}, adding a clear signal for where this story is heading.",
            "A separate report from {publisher} notes that {fact}, which helps explain the immediate stakes.",
            "Recent coverage indicates that {fact}, according to {publisher}, offering more context for the next phase.",
        ]
        template = templates[variant_index % len(templates)]
        return template.format(publisher=publisher, fact=fact)

    def _expand_short_article(
        self,
        article_html: str,
        research: ResearchPacket,
        topic: TrendTopic | None,
        focus_keyword: str | None,
    ) -> str:
        if topic is None or not focus_keyword:
            return article_html

        min_words = max(320, min(self.config.min_article_words, 360))
        article_body, sources_tail = self._split_before_sources_section(article_html)
        if self._word_count(article_body) >= min_words:
            return article_html

        focus_display = self._density_replacement_phrase(focus_keyword or (topic.keyword if topic is not None else "the story"))
        additions: list[str] = []
        variant_index = 0

        for fact, publisher in self._supporting_fact_candidates(research):
            paragraph = self._build_expansion_paragraph(fact, publisher, variant_index)
            variant_index += 1
            additions.append(
                f"\n<!-- wp:paragraph -->\n<p>{escape(paragraph)}</p>\n<!-- /wp:paragraph -->\n"
            )
            if self._word_count(article_body + "".join(additions)) >= min_words:
                break

        fallback_paragraphs = [
            (
                f"The latest reporting keeps the focus on {focus_display}. "
                "Readers now have a clearer view of why this update matters."
            ),
            (
                "That fuller context gives the story a clearer place in the current news cycle. "
                "It also helps readers follow the next confirmed development with less guesswork."
            ),
        ]
        for paragraph in fallback_paragraphs:
            if self._word_count(article_body + "".join(additions)) >= min_words:
                break
            additions.append(
                f"\n<!-- wp:paragraph -->\n<p>{escape(paragraph)}</p>\n<!-- /wp:paragraph -->\n"
            )

        return article_body + "".join(additions) + sources_tail

    def _normalize_article_markdown(
        self,
        article_markdown: str,
        topic: TrendTopic | None = None,
        title: str | None = None,
    ) -> str:
        normalized = article_markdown.strip()
        if title and re.search(r"(^|\n)#\s+.+", normalized):
            normalized = re.sub(r"(^|\n)#\s+.+", lambda match: f"{match.group(1)}# {title}", normalized, count=1)
        normalized = self._ensure_sources_markdown(normalized, topic)
        if topic is None:
            return normalized

        if not getattr(self.config, "internal_link_also_read_enabled", True):
            return normalized

        internal_article = self._select_internal_article(topic)
        if internal_article is None or "Also Read" in normalized:
            return normalized

        also_read_block = self._build_also_read_markdown(internal_article)
        return normalized + f"\n\n{also_read_block}"

    def _build_markdown_fallback(
        self,
        catchy_title: str,
        blog_outline: list[str],
        research: ResearchPacket,
    ) -> str:
        section_block = "\n\n".join(
            f"## {section}\nSee the HTML export for the finalized WordPress-ready body."
            for section in blog_outline
            if section.lower() != "sources"
        )
        sources_block = self._build_sources_markdown(research)
        if sources_block:
            return f"# {catchy_title}\n\n{section_block}\n\n{sources_block}".strip()
        return f"# {catchy_title}\n\n{section_block}".strip()

    def _render_wordpress_html(self, article_markdown: str) -> str:
        if render_markdown is None:
            raise RuntimeError("WordPress-ready HTML generation requires the 'markdown' package")

        article_html = render_markdown(article_markdown, extensions=["extra", "sane_lists"])
        return self._wrap_article_html(self._to_gutenberg_blocks(article_html))

    def _normalize_article_html(
        self,
        article_html: str,
        research: ResearchPacket,
        topic: TrendTopic | None = None,
        *,
        focus_keyword: str | None = None,
        title: str | None = None,
    ) -> str:
        stripped = self._strip_gutenberg_comments(article_html)
        stripped = self._flatten_nested_headings(stripped)
        normalized = self._wrap_article_html(self._to_gutenberg_blocks(stripped))
        normalized = self._normalize_existing_sources_section(normalized)
        normalized = self._remove_inline_sources_blocks(normalized)
        normalized = self._rewrite_bare_url_anchors(normalized, research)
        normalized = self._strip_inline_source_citation_paragraphs(normalized, research)
        normalized = self._inject_inline_context_links(normalized, research)
        normalized = self._ensure_sources_section(normalized, research)
        normalized = self._enforce_news_section_limit(normalized)
        normalized = self._ensure_also_read_block(normalized, topic)
        normalized = self._normalize_seo_headings(normalized, topic, focus_keyword=focus_keyword, title=title)
        normalized = self._ensure_focus_keyword_intro(normalized, research, focus_keyword)
        normalized = self._tighten_article_readability(normalized)
        normalized = self._expand_short_article(normalized, research, topic, focus_keyword)
        normalized = self._avoid_competing_link_anchors(normalized, research, focus_keyword)
        normalized = self._reduce_focus_keyword_density(normalized, focus_keyword)
        normalized = self._inject_source_image_block(normalized, research, focus_keyword)
        return normalized

    def _inject_source_image_block(
        self,
        article_html: str,
        research: ResearchPacket,
        focus_keyword: str | None,
    ) -> str:
        if "trend-agent-source-image" in article_html:
            return article_html

        image_source = next((source for source in research.sources if source.image_url), None)
        if image_source is None:
            return article_html

        image_url = self._safe_http_url(image_source.image_url)
        source_url = self._safe_http_url(image_source.url)
        if not image_url:
            return article_html

        credit = (image_source.image_credit or image_source.publisher or self._hostname_label(image_source.url) or "Source").strip()
        alt_text = self._source_image_alt_text(image_source, research, focus_keyword, credit)
        caption_text = f"Image source: {credit}"
        if source_url:
            caption_html = (
                f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
                f"{escape(caption_text)}"
                "</a>"
            )
        else:
            caption_html = escape(caption_text)

        figure_html = (
            "\n<!-- wp:image {\"sizeSlug\":\"large\",\"linkDestination\":\"custom\"} -->\n"
            '<figure class="wp-block-image size-large trend-agent-source-image">'
            f'<img src="{escape(image_url, quote=True)}" alt="{escape(alt_text, quote=True)}" loading="lazy" referrerpolicy="no-referrer" />'
            f"<figcaption>{caption_html}</figcaption>"
            "</figure>\n"
            "<!-- /wp:image -->\n"
        )

        heading_match = re.search(
            r"(<!--\s*wp:heading(?:\s+\{\"level\":1\})?\s*-->\s*<h1[^>]*>.*?</h1>\s*<!--\s*/wp:heading\s*-->)",
            article_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if heading_match:
            insert_at = heading_match.end()
            return article_html[:insert_at] + figure_html + article_html[insert_at:]

        article_match = re.search(r"<article[^>]*>", article_html, flags=re.IGNORECASE)
        if article_match:
            insert_at = article_match.end()
            return article_html[:insert_at] + figure_html + article_html[insert_at:]
        return figure_html + article_html

    def _source_image_alt_text(self, image_source, research: ResearchPacket, focus_keyword: str | None, credit: str) -> str:
        base_alt = (image_source.image_caption or image_source.title or research.topic or credit).strip()
        if not focus_keyword:
            return base_alt

        if focus_keyword.casefold() in base_alt.casefold():
            return base_alt

        focus_display = self._display_focus_keyword(focus_keyword)
        if not base_alt:
            return focus_display
        return f"{focus_display}: {base_alt}"

    def _safe_http_url(self, value: str) -> str:
        candidate = str(value or "").strip()
        return candidate if candidate.startswith(("https://", "http://")) else ""

    def _hostname_label(self, value: str) -> str:
        hostname = urlsplit(value or "").netloc.strip().lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname

    def _normalize_seo_headings(
        self,
        article_html: str,
        topic: TrendTopic | None,
        *,
        focus_keyword: str | None,
        title: str | None,
    ) -> str:
        normalized = article_html
        if title:
            normalized = re.sub(
                r"(<h1[^>]*>)(.*?)(</h1>)",
                lambda match: f"{match.group(1)}{escape(title)}{match.group(3)}",
                normalized,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )

        if not focus_keyword:
            return normalized

        heading_pattern = re.compile(
            r'(<!--\s*wp:heading(?:\s+\{[^>]*\})?\s*-->\s*<h2[^>]*>)(.*?)(</h2>\s*<!--\s*/wp:heading\s*-->)',
            flags=re.IGNORECASE | re.DOTALL,
        )
        matches = list(heading_pattern.finditer(normalized))
        story_headings = [match for match in matches if re.sub(r"<[^>]+>", "", match.group(2)).strip().casefold() != "sources"]
        if not story_headings:
            return normalized

        for index, match in enumerate(reversed(story_headings), start=1):
            heading_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if self._heading_mentions_focus(heading_text, focus_keyword):
                continue
            replacement = self._focus_h2_text(heading_text, focus_keyword, index=len(story_headings) - index, topic=topic)
            normalized = normalized[:match.start()] + match.group(1) + escape(replacement) + match.group(3) + normalized[match.end():]

        return normalized

    def _heading_mentions_focus(self, heading_text: str, focus_keyword: str) -> bool:
        if focus_keyword.casefold() in heading_text.casefold():
            return True
        focus_tokens = [token for token in tokenize(focus_keyword) if len(token) > 2 and token not in self.GENERIC_KEYWORD_TOKENS]
        if not focus_tokens:
            return focus_keyword.casefold() in heading_text.casefold()
        heading_tokens = set(tokenize(heading_text))
        required_hits = min(2, len(focus_tokens))
        return sum(1 for token in focus_tokens if token in heading_tokens) >= required_hits

    def _focus_h2_text(self, existing_heading: str, focus_keyword: str, index: int, topic: TrendTopic | None = None) -> str:
        focus_display = self._display_focus_keyword(focus_keyword)
        normalized_existing = re.sub(r"\s+", " ", existing_heading).strip()
        lower_heading = normalized_existing.casefold()
        topic_tokens = set(tokenize(topic.keyword)) if topic is not None else set()
        is_sports_topic = bool(topic_tokens & self.SPORTS_TOKENS)
        use_generic_followup = self._focus_keyword_content_word_count(focus_keyword) >= 4
        if index == 0:
            if is_sports_topic:
                return f"{focus_display}: the game-changing development"
            return f"{focus_display} and the policy shift"
        if lower_heading.startswith("what this means for "):
            suffix = normalized_existing[len("What This Means"):].strip()
            if not use_generic_followup:
                return f"What {focus_display} Means {suffix}".strip()
            return f"What this means {suffix}".strip()
        if lower_heading.startswith("why this matters"):
            if not use_generic_followup:
                return f"Why {focus_display} Matters Now"
            return "Why this matters now"
        if " for " in lower_heading:
            suffix = normalized_existing[lower_heading.index(" for "):]
            if not use_generic_followup:
                return f"What {focus_display} Means{suffix}"
            return f"What this means{suffix}"
        if not use_generic_followup:
            return f"Why {focus_display} Matters Now"
        return "Why this matters now"

    def _tighten_article_readability(self, article_html: str) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"(<!--\s*wp:paragraph\s*-->\s*<p>)(.*?)(</p>\s*<!--\s*/wp:paragraph\s*-->)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def repl(match: re.Match[str]) -> str:
            paragraph_body = match.group(2)
            if re.search(r"<a\b|<strong>Also Read", paragraph_body, flags=re.IGNORECASE):
                return match.group(0)

            paragraph_text = re.sub(r"<[^>]+>", "", paragraph_body)
            paragraph_text = re.sub(r"\s+", " ", paragraph_text).strip()
            if not paragraph_text:
                return match.group(0)

            revised = self._reduce_passive_voice(paragraph_text)
            revised = self._reduce_speculative_language(revised)
            revised = self._split_long_sentences(revised)
            revised = re.sub(r"\s+", " ", revised).strip()
            if revised == paragraph_text:
                return match.group(0)
            return f"{match.group(1)}{escape(revised)}{match.group(3)}"

        return paragraph_pattern.sub(repl, article_body) + sources_tail

    def _reduce_passive_voice(self, text: str) -> str:
        replacements = [
            (r"\bhas been seen as\b", "is"),
            (r"\bhave been seen as\b", "are"),
            (r"\bhas been described as\b", "is"),
            (r"\bhave been described as\b", "are"),
            (r"\bhas been attributed to\b", "stems from"),
            (r"\bhave been attributed to\b", "stem from"),
            (r"\bhas been marked by\b", "showed"),
            (r"\bhave been marked by\b", "show"),
            (r"\bhas raised questions about\b", "raises questions about"),
            (r"\bhave raised questions about\b", "raise questions about"),
        ]
        normalized = text
        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        return normalized

    def _reduce_speculative_language(self, text: str) -> str:
        replacements = [
            (r"\bcould impact\b", "has implications for"),
            (r"\bmay impact\b", "has implications for"),
            (r"\bcould affect\b", "affects"),
            (r"\bmay affect\b", "affects"),
            (r"\bcould alter\b", "reshapes"),
            (r"\bmay alter\b", "reshapes"),
            (r"\bcould bring\b", "brings"),
            (r"\bmay bring\b", "brings"),
            (r"\bcould give\b", "gives"),
            (r"\bmay give\b", "gives"),
            (r"\bcould have significant implications for\b", "has significant implications for"),
            (r"\bmay have significant implications for\b", "has significant implications for"),
            (r"\bcould influence\b", "shapes"),
            (r"\bmay influence\b", "shapes"),
            (r"\bcould shape\b", "shapes"),
            (r"\bmight shape\b", "shapes"),
            (r"\bcould be a significant factor in\b", "is a significant factor in"),
            (r"\bcould be a factor in\b", "is a factor in"),
            (r"\bwould likely be seen as\b", "is seen as"),
            (r"\blikely be seen as\b", "is seen as"),
        ]
        normalized = text
        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        return normalized

    def _split_long_sentences(self, text: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        revised_sentences: list[str] = []
        for sentence in sentences:
            current = sentence.strip()
            if not current:
                continue
            if len(re.findall(r"\b\w+\b", current)) <= 22:
                revised_sentences.append(current)
                continue

            split_parts = self._split_sentence_once(current)
            if split_parts is None:
                revised_sentences.append(current)
                continue
            revised_sentences.extend(split_parts)

        return " ".join(revised_sentences)

    def _split_sentence_once(self, sentence: str) -> list[str] | None:
        stripped = sentence.strip()
        ending = "."
        if stripped and stripped[-1] in ".!?":
            ending = stripped[-1]
            stripped = stripped[:-1]

        for separator, prefix in [
            (", but ", "But "),
            (", and ", ""),
            ("; ", ""),
            (": ", ""),
            (", while ", "While "),
            (", which ", "This "),
            (", according to ", "According to "),
            (", ", ""),
        ]:
            index = stripped.lower().find(separator)
            if index <= 0:
                continue
            left = stripped[:index].strip()
            right = stripped[index + len(separator):].strip()
            if len(re.findall(r"\b\w+\b", left)) < 6 or len(re.findall(r"\b\w+\b", right)) < 6:
                continue
            right_text = prefix + right[0].upper() + right[1:] if right else ""
            return [left + ".", right_text + ending]
        return None

    def _normalize_existing_sources_section(self, article_html: str) -> str:
        sources_heading_pattern = re.compile(
            r"<!--\s*wp:heading(?:\s+\{[^>]*\})?\s*-->\s*<h2[^>]*>\s*Sources\s*</h2>\s*<!--\s*/wp:heading\s*-->",
            flags=re.IGNORECASE | re.DOTALL,
        )
        heading_match = sources_heading_pattern.search(article_html)
        if heading_match is None:
            return article_html

        section_prefix = article_html[:heading_match.end()]
        section_suffix = article_html[heading_match.end():]
        if re.match(r"\s*<!--\s*wp:list", section_suffix, flags=re.IGNORECASE):
            return article_html

        next_heading_match = sources_heading_pattern.search(section_suffix)
        article_close_match = re.search(r"</article>\s*$", section_suffix, flags=re.IGNORECASE)
        if next_heading_match:
            section_body = section_suffix[: next_heading_match.start()]
            section_remainder = section_suffix[next_heading_match.start():]
        elif article_close_match:
            section_body = section_suffix[: article_close_match.start()]
            section_remainder = section_suffix[article_close_match.start():]
        else:
            section_body = section_suffix
            section_remainder = ""

        paragraph_pattern = re.compile(
            r"<!--\s*wp:paragraph\s*-->\s*<p>(.*?)</p>\s*<!--\s*/wp:paragraph\s*-->",
            flags=re.IGNORECASE | re.DOTALL,
        )
        items: list[str] = []
        cursor = 0
        for paragraph_match in paragraph_pattern.finditer(section_body):
            if section_body[cursor:paragraph_match.start()].strip():
                return article_html
            item = self._parse_markdown_source_item(paragraph_match.group(1))
            if item is None:
                return article_html
            items.append(item)
            cursor = paragraph_match.end()

        if not items or section_body[cursor:].strip():
            return article_html

        list_block = (
            "\n<!-- wp:list -->\n<ul>\n"
            + "\n".join(items)
            + "\n</ul>\n<!-- /wp:list -->\n"
        )
        return section_prefix + list_block + section_remainder

    def _parse_markdown_source_item(self, paragraph_html: str) -> str | None:
        match = re.match(
            r"\s*[*-]\s+\[(.+?)\]\((https?://[^\s)]+)\)\s*",
            paragraph_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match is not None:
            title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            url = match.group(2).strip()
            if not title or not url:
                return None
            return f'<li><a href="{escape(url, quote=True)}">{escape(title)}</a></li>'

        anchor_match = re.search(
            r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
            paragraph_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if anchor_match is None:
            return None

        url = anchor_match.group(1).strip()
        anchor_text = re.sub(r"<[^>]+>", " ", anchor_match.group(2))
        anchor_text = re.sub(r"\s+", " ", anchor_text).strip(" :-")
        if not anchor_text or not url:
            return None

        # Support "Source title: <a ...>Source title</a>" by preferring a non-generic
        # prefix only when it adds information beyond the anchor text.
        prefix_html = paragraph_html[:anchor_match.start()]
        prefix_text = re.sub(r"<[^>]+>", " ", prefix_html)
        prefix_text = re.sub(r"\s+", " ", prefix_text).strip(" :-")

        generic_prefix = bool(
            re.search(r"for more information|visit the following sources", prefix_text, flags=re.IGNORECASE)
        )
        if prefix_text and not generic_prefix and prefix_text.casefold() != anchor_text.casefold():
            title = prefix_text
        else:
            title = anchor_text

        return f'<li><a href="{escape(url, quote=True)}">{escape(title)}</a></li>'

    def _rewrite_bare_url_anchors(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        normalized_body = self._rewrite_bare_url_anchors_in_fragment(article_body, research, in_sources=False)
        normalized_sources = self._rewrite_bare_url_anchors_in_fragment(sources_tail, research, in_sources=True)
        return normalized_body + normalized_sources

    def _remove_inline_sources_blocks(self, article_html: str) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        inline_sources_pattern = re.compile(
            r'(?:(?:<!--\s*wp:separator\s*-->\s*)?<hr[^>]*>(?:\s*<!--\s*/wp:separator\s*-->)?\s*)?'
            r'<!--\s*wp:paragraph\s*-->\s*<p>\s*(?:<strong>\s*)?Sources:?\s*(?:</strong>)?\s*</p>\s*<!--\s*/wp:paragraph\s*-->\s*'
            r'<!--\s*wp:list\s*-->\s*<ul>.*?</ul>\s*<!--\s*/wp:list\s*-->',
            flags=re.IGNORECASE | re.DOTALL,
        )
        normalized_body = inline_sources_pattern.sub("", article_body)
        normalized_body = re.sub(r"\n{3,}", "\n\n", normalized_body)
        return normalized_body + sources_tail

    def _strip_inline_source_citation_paragraphs(self, article_html: str, research: ResearchPacket) -> str:
        article_body, sources_tail = self._split_before_sources_section(article_html)
        paragraph_pattern = re.compile(
            r"<!--\s*wp:paragraph\s*-->\s*<p>(.*?)</p>\s*<!--\s*/wp:paragraph\s*-->",
            flags=re.IGNORECASE | re.DOTALL,
        )

        def repl(match: re.Match[str]) -> str:
            paragraph_html = match.group(1).strip()
            if self._is_source_citation_paragraph(paragraph_html, research):
                return ""
            return match.group(0)

        normalized_body = paragraph_pattern.sub(repl, article_body)
        return re.sub(r"\n{3,}", "\n\n", normalized_body) + sources_tail

    def _is_source_citation_paragraph(self, paragraph_html: str, research: ResearchPacket) -> bool:
        citation_match = re.fullmatch(
            r'(?:\[\d+\]\s*)?<a\s+href="([^"]+)"[^>]*>.*?</a>\s*[.:;,!?-]*',
            paragraph_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if citation_match is None:
            return False
        return self._source_for_url(research, citation_match.group(1).strip()) is not None

    def _rewrite_bare_url_anchors_in_fragment(
        self,
        fragment: str,
        research: ResearchPacket,
        *,
        in_sources: bool,
    ) -> str:
        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def repl(match: re.Match[str]) -> str:
            href = match.group(1).strip()
            anchor_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if not self._is_bare_url_text(anchor_text, href):
                return match.group(0)

            source = self._source_for_url(research, href)
            label = self._source_anchor_label(source, href, in_sources=in_sources, surrounding_html=match.string[:match.start()])
            return f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'

        return anchor_pattern.sub(repl, fragment)

    def _is_bare_url_text(self, anchor_text: str, href: str) -> bool:
        normalized_text = self._normalize_url_like_text(anchor_text, strip_citation_prefix=True)
        normalized_href = self._normalize_url_like_text(href)
        return normalized_text == normalized_href

    def _normalize_url_like_text(self, value: str, *, strip_citation_prefix: bool = False) -> str:
        normalized = re.sub(r"\s+", " ", value or "").strip()
        if strip_citation_prefix:
            normalized = re.sub(r"^\[\d+\]\s*", "", normalized)
        normalized = normalized.rstrip(")").rstrip(".").rstrip("/")
        return normalized

    def _source_for_url(self, research: ResearchPacket, href: str):
        normalized_href = href.strip().rstrip("/")
        for source in research.sources:
            if (source.url or "").strip().rstrip("/") == normalized_href:
                return source
        return None

    def _source_anchor_label(self, source, href: str, *, in_sources: bool, surrounding_html: str) -> str:
        if source is not None:
            title = (source.title or "").strip()
            publisher = (source.publisher or "").strip()
            if in_sources:
                return title or publisher or self._friendly_link_label(href)
            if publisher and publisher.lower() in surrounding_html.lower():
                return "the report"
            return publisher or title or self._friendly_link_label(href)
        return self._friendly_link_label(href)

    def _focus_keyword_tokens(self, focus_keyword: str | None) -> list[str]:
        if not focus_keyword:
            return []
        return [token for token in tokenize(focus_keyword) if len(token) > 2 and token not in self.GENERIC_KEYWORD_TOKENS]

    def _anchor_competes_with_focus(self, anchor_text: str, focus_keyword: str | None) -> bool:
        if not focus_keyword:
            return False

        normalized = re.sub(r"<[^>]+>", " ", anchor_text or "")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return False
        if focus_keyword.casefold() in normalized.casefold():
            return True

        focus_tokens = self._focus_keyword_tokens(focus_keyword)
        if not focus_tokens:
            return False
        anchor_tokens = set(tokenize(normalized))
        return sum(1 for token in focus_tokens if token in anchor_tokens) >= min(2, len(focus_tokens))

    def _density_replacement_phrase(self, focus_keyword: str | None) -> str:
        if not focus_keyword:
            return "the campaign"

        if self._focus_keyword_content_word_count(focus_keyword) >= 4:
            return "the policy move"

        parts = self._display_focus_keyword(focus_keyword).split()
        if len(parts) >= 3:
            return " ".join(parts[:2])
        if len(parts) == 2:
            if all(part[:1].isupper() and part[1:].islower() for part in parts):
                return parts[-1]
            return parts[0]
        if parts:
            return f"the {parts[0]} story"
        return "the campaign"

    def _visible_focus_keyword_occurrences(self, article_html: str, focus_keyword: str | None) -> int:
        if not focus_keyword:
            return 0
        visible_text = re.sub(r"<[^>]+>", " ", article_html)
        visible_text = re.sub(r"\s+", " ", visible_text).strip()
        return len(re.findall(re.escape(focus_keyword), visible_text, flags=re.IGNORECASE))

    def _max_focus_keyword_occurrences(self, article_html: str, focus_keyword: str | None) -> int:
        if not focus_keyword:
            return 0
        word_count = self._word_count(article_html)
        dynamic_cap = round(max(word_count, 1) / 65)
        return min(7, max(4, dynamic_cap))

    def _replace_focus_keyword_in_html_text(
        self,
        fragment: str,
        focus_keyword: str,
        replacement: str,
        max_replacements: int,
    ) -> tuple[str, int]:
        if max_replacements <= 0:
            return fragment, 0

        pattern = re.compile(re.escape(focus_keyword), flags=re.IGNORECASE)
        segments = re.split(r"(<[^>]+>)", fragment)
        replaced = 0

        for index in range(len(segments) - 1, -1, -1):
            segment = segments[index]
            if not segment or segment.startswith("<"):
                continue
            matches = list(pattern.finditer(segment))
            if not matches:
                continue
            updated_segment = segment
            for match in reversed(matches):
                if replaced >= max_replacements:
                    break
                updated_segment = updated_segment[:match.start()] + replacement + updated_segment[match.end():]
                replaced += 1
            segments[index] = updated_segment
            if replaced >= max_replacements:
                break

        return "".join(segments), replaced

    def _reduce_focus_keyword_density(self, article_html: str, focus_keyword: str | None) -> str:
        if not focus_keyword:
            return article_html

        current_occurrences = self._visible_focus_keyword_occurrences(article_html, focus_keyword)
        max_occurrences = self._max_focus_keyword_occurrences(article_html, focus_keyword)
        if current_occurrences <= max_occurrences:
            return article_html

        replacement = self._density_replacement_phrase(focus_keyword)
        if replacement.casefold() == focus_keyword.casefold():
            return article_html

        updated = article_html
        extra_matches = current_occurrences - max_occurrences
        paragraph_pattern = re.compile(r"(<p[^>]*>)(.*?)(</p>)", flags=re.IGNORECASE | re.DOTALL)
        paragraphs = list(paragraph_pattern.finditer(article_html))
        preferred_paragraphs = paragraphs[1:] if len(paragraphs) > 1 else paragraphs

        for match in reversed(preferred_paragraphs):
            inner_html = match.group(2)
            updated_inner_html, replaced = self._replace_focus_keyword_in_html_text(
                inner_html,
                focus_keyword,
                replacement,
                extra_matches,
            )
            if replaced <= 0:
                continue
            updated = updated[:match.start(2)] + updated_inner_html + updated[match.end(2):]
            extra_matches -= replaced
            if extra_matches <= 0:
                break

        if extra_matches > 0:
            updated, _ = self._replace_focus_keyword_in_html_text(updated, focus_keyword, replacement, extra_matches)
        return updated

    def _avoid_competing_link_anchors(
        self,
        article_html: str,
        research: ResearchPacket,
        focus_keyword: str | None,
    ) -> str:
        if not focus_keyword:
            return article_html

        anchor_pattern = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

        def sanitize(fragment: str, *, in_sources: bool) -> str:
            def repl(match: re.Match[str]) -> str:
                href = match.group(1).strip()
                anchor_text = re.sub(r"<[^>]+>", " ", match.group(2))
                anchor_text = re.sub(r"\s+", " ", anchor_text).strip()
                if not self._anchor_competes_with_focus(anchor_text, focus_keyword):
                    return match.group(0)

                source = self._source_for_url(research, href)
                if in_sources:
                    label = (source.publisher or "").strip() if source is not None else ""
                    label = label or self._friendly_link_label(href)
                    return f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'

                return escape(anchor_text)

            return anchor_pattern.sub(repl, fragment)

        article_body, sources_tail = self._split_before_sources_section(article_html)
        return sanitize(article_body, in_sources=False) + sanitize(sources_tail, in_sources=True)

    def _friendly_link_label(self, href: str) -> str:
        hostname = urlsplit(href).netloc.lower()
        hostname = re.sub(r"^www\.", "", hostname)
        if not hostname:
            return href
        return hostname

    def _flatten_nested_headings(self, article_html: str) -> str:
        def repl(match: re.Match[str]) -> str:
            heading_body = re.sub(r"\s+", " ", match.group(2)).strip()
            if not heading_body:
                return ""
            return f"<p><strong>{heading_body}</strong></p>"

        return re.sub(r"<h([3-6])\b[^>]*>(.*?)</h\1>", repl, article_html, flags=re.IGNORECASE | re.DOTALL)

    def _is_politics_topic(self, topic: TrendTopic | None) -> bool:
        if topic is None:
            return False

        configured_category = normalize_topic_category(self.config.topic_category)
        if configured_category == "politics":
            return True

        return bool(set(tokenize(topic.keyword)) & self.POLITICS_TOKENS)

    def _enforce_news_section_limit(self, article_html: str) -> str:
        heading_pattern = re.compile(
            r"<!--\s*wp:heading(?:\s+\{[^>]*\})?\s*-->\s*<h2[^>]*>\s*(.*?)\s*</h2>\s*<!--\s*/wp:heading\s*-->",
            flags=re.IGNORECASE | re.DOTALL,
        )
        matches = list(heading_pattern.finditer(article_html))
        if len(matches) <= 3:
            return article_html

        sources_index = next(
            (
                index
                for index, match in enumerate(matches)
                if re.sub(r"<[^>]+>", "", match.group(1)).strip().casefold() == "sources"
            ),
            None,
        )
        if sources_index is None or sources_index <= 2:
            return article_html

        normalized = article_html
        for match in reversed(matches[2:sources_index]):
            normalized = normalized[:match.start()] + normalized[match.end():]

        return re.sub(r"\n{3,}", "\n\n", normalized)

    def _inject_inline_context_links(self, article_html: str, research: ResearchPacket) -> str:
        linked_entities: set[str] = set()
        article_body, sources_tail = self._split_before_sources_section(article_html)
        normalized = article_body

        for reference in research.context_references:
            entity = (reference.entity or "").strip()
            url = (reference.url or "").strip()
            if not entity or not url:
                continue

            entity_key = entity.casefold()
            if entity_key in linked_entities:
                continue

            paragraph_pattern = re.compile(r"(<p[^>]*>)(.*?)(</p>)", flags=re.IGNORECASE | re.DOTALL)
            replaced = False

            def replace_in_paragraph(match: re.Match[str]) -> str:
                nonlocal replaced
                if replaced:
                    return match.group(0)

                opening_tag, paragraph_body, closing_tag = match.groups()
                linked_paragraph = self._link_entity_in_paragraph_body(paragraph_body, entity, url)
                if linked_paragraph == paragraph_body:
                    return match.group(0)
                replaced = True
                return opening_tag + linked_paragraph + closing_tag

            updated = paragraph_pattern.sub(replace_in_paragraph, normalized)
            if replaced:
                normalized = updated
                linked_entities.add(entity_key)

        return normalized + sources_tail

    def _link_entity_in_paragraph_body(self, paragraph_body: str, entity: str, url: str) -> str:
        if not paragraph_body or url in paragraph_body:
            return paragraph_body

        entity_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(entity)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
        segments = re.split(r"(<a\b[^>]*>.*?</a>)", paragraph_body, flags=re.IGNORECASE | re.DOTALL)

        for index, segment in enumerate(segments):
            if not segment or re.match(r"<a\b", segment, flags=re.IGNORECASE):
                continue

            entity_match = entity_pattern.search(segment)
            if entity_match is None:
                continue

            anchor = (
                f'<a href="{escape(url, quote=True)}">'
                f'{segment[entity_match.start():entity_match.end()]}'
                "</a>"
            )
            segments[index] = (
                segment[:entity_match.start()]
                + anchor
                + segment[entity_match.end():]
            )
            return "".join(segments)

        return paragraph_body

    def _split_before_sources_section(self, article_html: str) -> tuple[str, str]:
        sources_heading_match = re.search(
            r"<!--\s*wp:heading(?:\s+\{[^>]*\})?\s*-->\s*<h2[^>]*>\s*Sources\s*</h2>\s*<!--\s*/wp:heading\s*-->",
            article_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if sources_heading_match is None:
            return article_html, ""
        return article_html[:sources_heading_match.start()], article_html[sources_heading_match.start():]

    def _is_internal_link(self, href: str) -> bool:
        normalized = href.strip()
        if normalized.startswith("/") and not normalized.startswith("//"):
            return True
        base_url = self._site_base_url()
        if not base_url:
            return False
        return normalized.startswith(base_url.rstrip("/") + "/") or normalized == base_url.rstrip("/")

    def _ensure_also_read_block(self, article_html: str, topic: TrendTopic | None) -> str:
        # DISABLED — the AnchorInjectorService in
        # news_agent/services/internalLink/service.py now handles ALL
        # internal linking via the vector store + HF embeddings.
        #
        # The old code here built URLs from WP_GRAPHQL_URL which produced
        # wrong "publisher.peoplenewstime.com" links and used keyword-only
        # matching (no semantic similarity). Letting it run would insert
        # a bad Also Read block that the injector then skips, leaving the
        # article with only the wrong link.
        return article_html

    def _build_also_read_markdown(self, internal_article: dict[str, str]) -> str:
        return (
            f"**Also Read:** [{internal_article['title']}]({internal_article['url']})"
        )

    def _build_sources_markdown(self, research: ResearchPacket) -> str:
        lines = []
        seen_urls: set[str] = set()
        for source in research.sources[:5]:
            url = (source.url or "").strip()
            title = self._trim_text(source.title or url, 120)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            lines.append(f"- [{title}]({url})")
        if not lines:
            return ""
        return "## Sources\n" + "\n".join(lines)

    def _ensure_sources_markdown(self, article_markdown: str, topic: TrendTopic | None = None) -> str:
        normalized = article_markdown.strip()
        if re.search(r"(^|\n)##\s+Sources\b", normalized, flags=re.IGNORECASE):
            return normalized
        if topic is None:
            return normalized
        return normalized

    def _ensure_sources_section(self, article_html: str, research: ResearchPacket) -> str:
        if re.search(r"<h2[^>]*>\s*Sources\s*</h2>", article_html, flags=re.IGNORECASE):
            return article_html

        source_items = []
        seen_urls: set[str] = set()
        for source in research.sources[:5]:
            url = (source.url or "").strip()
            title = self._trim_text(source.title or url, 120)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            source_items.append(
                f'<li><a href="{escape(url, quote=True)}">{escape(title)}</a></li>'
            )

        if not source_items:
            return article_html

        sources_block = (
            "\n<!-- wp:heading -->\n<h2>Sources</h2>\n<!-- /wp:heading -->\n"
            "<!-- wp:list -->\n<ul>\n"
            + "\n".join(source_items)
            + "\n</ul>\n<!-- /wp:list -->\n"
        )

        article_close = re.search(r"</article>\s*$", article_html, flags=re.IGNORECASE)
        if article_close is None:
            return article_html + sources_block
        return article_html[:article_close.start()] + sources_block + article_html[article_close.start():]

    def _build_also_read_html(self, internal_article: dict[str, str]) -> str:
        return (
            "\n<!-- wp:separator -->\n<hr />\n<!-- /wp:separator -->\n"
            "<!-- wp:paragraph -->\n"
            f"<p><strong>Also Read:</strong> <a href=\"{escape(internal_article['url'], quote=True)}\">{escape(internal_article['title'])}</a></p>\n"
            "<!-- /wp:paragraph -->\n"
            "<!-- wp:separator -->\n<hr />\n<!-- /wp:separator -->\n"
        )

    def _select_internal_article(self, topic: TrendTopic) -> dict[str, str] | None:
        base_url = self._site_base_url()
        cache_dir = Path(self.config.storage_root) / "cache"
        if not base_url or not cache_dir.exists():
            return None

        current_slug = slugify(topic.keyword)
        candidates: list[tuple[int, float, str, str]] = []
        topic_tokens = set(tokenize(topic.keyword))
        topic_category = str(
            getattr(topic, "category", "") or getattr(self.config, "topic_category", "") or ""
        ).strip().lower()

        for path in cache_dir.glob("publish-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            wordpress = payload.get("wordpress") or {}
            wordpress_sync = payload.get("wordpress_sync") or {}
            slug = str(wordpress.get("slug") or "").strip()
            title = str(wordpress.get("title") or "").strip()
            remote_status = str(wordpress_sync.get("remote_status") or wordpress.get("post_status") or "").strip().lower()
            if not slug or not title or slug == current_slug or remote_status != "publish":
                continue

            if topic_category:
                candidate_categories = [
                    str(c).strip().lower()
                    for c in (wordpress_sync.get("categories") or wordpress.get("categories") or [])
                    if str(c).strip()
                ]
                if candidate_categories and topic_category not in candidate_categories:
                    continue

            selected_topic = payload.get("run", {}).get("selected_topic", {})
            comparison_text = f"{title} {selected_topic.get('keyword', '')}"
            score = len(topic_tokens & set(tokenize(comparison_text)))
            if score <= 0:
                continue

            candidates.append((score, path.stat().st_mtime, title, f"{base_url.rstrip('/')}/{slug.strip('/')}/"))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        _score, _mtime, title, url = candidates[0]
        return {"title": title, "url": url}

    def _site_base_url(self) -> str | None:
        target = str(getattr(self.config, "internal_link_target", "people") or "people").strip().lower()
        if target == "people":
            url = (getattr(self.config, "public_site_base_url", "") or "").strip()
            if not url:
                return None
            return url.rstrip("/")

        url = (self.config.wordpress_graphql_url or "").strip()
        if not url:
            return None
        return re.sub(r"/graphql/?$", "", url)

    def _wrap_article_html(self, article_html: str) -> str:
        normalized = article_html.strip()
        if not re.search(r"<article\b", normalized, flags=re.IGNORECASE):
            normalized = f'<article class="trend-agent-post">\n{normalized}\n</article>'
        return normalized

    def _strip_gutenberg_comments(self, article_html: str) -> str:
        stripped = re.sub(r"<!--\s*/?wp:[^>]+-->", "", article_html, flags=re.IGNORECASE)
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
        return stripped.strip()

    def _to_gutenberg_blocks(self, article_html: str) -> str:
        blockified = article_html.strip()
        blockified = self._wrap_heading_blocks(blockified)
        blockified = self._wrap_tag_block(blockified, "p", "paragraph")
        blockified = self._wrap_list_blocks(blockified)
        blockified = self._wrap_tag_block(blockified, "blockquote", "quote")
        blockified = self._wrap_separator_blocks(blockified)
        return blockified

    def _wrap_heading_blocks(self, article_html: str) -> str:
        def repl(match: re.Match[str]) -> str:
            level = int(match.group(1))
            heading_html = match.group(0)
            if "<!-- wp:heading" in heading_html:
                return heading_html
            block_meta = "" if level == 2 else f' {{"level":{level}}}'
            return f'<!-- wp:heading{block_meta} -->\n{heading_html}\n<!-- /wp:heading -->'

        return re.sub(r"<h([1-6])[^>]*>.*?</h\1>", repl, article_html, flags=re.IGNORECASE | re.DOTALL)

    def _wrap_tag_block(self, article_html: str, tag: str, block_name: str) -> str:
        pattern = rf"<{tag}\b[^>]*>.*?</{tag}>"

        def repl(match: re.Match[str]) -> str:
            block_html = match.group(0)
            if f"<!-- wp:{block_name}" in block_html:
                return block_html
            return f"<!-- wp:{block_name} -->\n{block_html}\n<!-- /wp:{block_name} -->"

        return re.sub(pattern, repl, article_html, flags=re.IGNORECASE | re.DOTALL)

    def _wrap_list_blocks(self, article_html: str) -> str:
        def repl(match: re.Match[str]) -> str:
            list_html = match.group(0)
            if "<!-- wp:list" in list_html:
                return list_html
            ordered = match.group(1).lower() == "ol"
            metadata = ' {"ordered":true}' if ordered else ""
            return f"<!-- wp:list{metadata} -->\n{list_html}\n<!-- /wp:list -->"

        return re.sub(r"<(ul|ol)\b[^>]*>.*?</\1>", repl, article_html, flags=re.IGNORECASE | re.DOTALL)

    def _wrap_separator_blocks(self, article_html: str) -> str:
        def repl(match: re.Match[str]) -> str:
            separator_html = match.group(0)
            if "<!-- wp:separator" in separator_html:
                return separator_html
            return f"<!-- wp:separator -->\n{separator_html}\n<!-- /wp:separator -->"

        return re.sub(r"<hr\s*/?>", repl, article_html, flags=re.IGNORECASE)

    def _mock_article(self, topic: TrendTopic, research: ResearchPacket, plan: ContentPlan) -> GeneratedArticle:
        lead = research.lead or (research.present[0] if research.present else topic.keyword)
        why_now = research.present[1] if len(research.present) > 1 else lead
        background = research.past[0] if research.past else (research.facts[1] if len(research.facts) > 1 else lead)
        next_step = research.future[0] if research.future else f"Readers should watch for the next confirmed development tied to {topic.keyword}."
        lead_fact = self._trim_text(lead, 130).rstrip(". ")
        why_fact = self._trim_text(why_now, 130).rstrip(". ")
        background_fact = self._trim_text(background, 130).rstrip(". ")
        next_fact = self._trim_text(next_step, 130).rstrip(". ")
        lead_publisher = research.sources[0].publisher or "the lead outlet" if research.sources else "the lead outlet"
        second_publisher = research.sources[1].publisher or research.sources[0].publisher if len(research.sources) > 1 else lead_publisher

        if plan.article_type == "politics_news":
            article_markdown = "\n".join(
                [
                    f"# {topic.keyword}: What happened and what it means now",
                    "",
                    "## A major political shift",
                    f"According to {lead_publisher}, {lead_fact}.",
                    "",
                    f"The immediate significance is that this result resets the political picture around {topic.keyword} for readers following the race or governing fight.",
                    "",
                    f"The closest background is that {background_fact}. That gives readers the context behind the result without turning the piece into an explainer.",
                    "",
                    f"## {plan.sections[1]}",
                    f"According to {second_publisher}, {why_fact}.",
                    "",
                    f"The next checkpoint is straightforward: {next_fact}.",
                    "",
                    "This section should stay focused on the party stakes, the race outlook, and the next confirmed development that matters to readers.",
                    "",
                    self._build_sources_markdown(research),
                ]
            ).strip()

            article_html = self._render_wordpress_html(article_markdown)
            article_html = self._normalize_article_html(article_html, research, topic)
            article_markdown = self._normalize_article_markdown(article_markdown, topic)

            return GeneratedArticle(
                catchy_title=f"{topic.keyword}: the political shift and what it means now",
                seo_keywords=[plan.primary_keyword, *plan.secondary_keywords[:5]],
                meta_description=f"Latest political developments on {topic.keyword}, why the result matters, and what comes next.",
                blog_outline=plan.sections,
                article_markdown=article_markdown,
                article_html=article_html,
                image_prompts=[
                    f"Editorial political cover image for {topic.keyword}",
                    f"News illustration showing the political stakes behind {topic.keyword}",
                    f"Professional newsroom thumbnail for {topic.keyword}",
                ],
            )

        article_markdown = "\n".join(
            [
                f"# {topic.keyword}: What happened and why it matters now",
                "",
                "## The latest development",
                f"According to {lead_publisher}, {lead_fact}.",
                "",
                f"The latest reporting keeps the focus on the event itself rather than on search interest around {topic.keyword}.",
                "",
                f"That gives readers a direct lead with the newest verified turn and a clear statement of what changed in the story today.",
                "",
                "A newsroom-style draft should start with the decisive fact and then move directly into the evidence supporting that turn.",
                "",
                f"## {plan.sections[1]}",
                f"According to {second_publisher}, {why_fact}.",
                "",
                f"This matters because the development affects the next phase of the story and clarifies why editors are elevating it now.",
                "",
                f"The key background is that {background_fact}. That is the context readers need before moving to the next step.",
                "",
                f"Readers should leave with a clear sense of the next checkpoint and why that follow-up matters for {topic.keyword}. {next_fact}.",
                "",
                "That keeps the piece grounded in reporting while still giving readers the immediate consequence, the needed context, and the most relevant next turn in the story.",
                "",
                "A strong straight-news draft should leave the reader with the central fact, the practical significance, and the next verified thing to watch without drifting into a separate explainer section.",
                "",
                "That is the core newsroom standard here.",
                "",
                self._build_sources_markdown(research),
            ]
        ).strip()

        article_html = self._render_wordpress_html(article_markdown)
        article_html = self._normalize_article_html(article_html, research, topic)
        article_markdown = self._normalize_article_markdown(article_markdown, topic)

        return GeneratedArticle(
            catchy_title=f"{topic.keyword}: what happened and why it matters now",
            seo_keywords=[plan.primary_keyword, *plan.secondary_keywords[:5]],
            meta_description=f"Latest developments on {topic.keyword} and why the story matters now.",
            blog_outline=plan.sections,
            article_markdown=article_markdown,
            article_html=article_html,
            image_prompts=[
                f"Hero illustration for a newsroom article about {topic.keyword}",
                f"Editorial scene showing the latest development tied to {topic.keyword}",
                f"News-style article thumbnail for {topic.keyword} with a professional editorial look",
            ],
        )


class WritingAgent(BaseAgent):
    stage_name = "generator"

    def __init__(self, service: BlogGenerationService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if (
            context.run is None
            or context.run.selected_topic is None
            or context.run.research is None
            or context.run.plan is None
        ):
            raise RuntimeError("Topic, research, and plan are required before writing")

        context.run.blog = self.service.generate(context.run.selected_topic, context.run.research, context.run.plan)
        self.logger.info(context.run, "Generated article draft")
        self.logger.transition(context.run, "blog_generated")