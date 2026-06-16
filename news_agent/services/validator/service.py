from __future__ import annotations

import re
from urllib.parse import urlsplit

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import ContentPlan, GeneratedArticle, ResearchPacket, ValidationResult
from ..base import AgentContext, BaseAgent


class ValidationService:
    ALLOWED_GUTENBERG_BLOCKS = {"heading", "paragraph", "list", "quote", "separator", "image"}

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def validate(self, article: GeneratedArticle, research: ResearchPacket, plan: ContentPlan) -> ValidationResult:
        issues: list[str] = []
        body = article.article_html.strip()
        words = len(re.findall(r"\b\w+\b", self._visible_text(body)))
        h1_count = self._tag_count(body, "h1")
        h2_count = self._tag_count(body, "h2")
        visible_text = self._visible_text(body)
        attribution_hits = self._attribution_hits(visible_text, research)
        keyword_in_title = plan.primary_keyword.lower() in article.catchy_title.lower()
        keyword_in_meta = plan.primary_keyword.lower() in article.meta_description.lower()
        keyword_in_h1 = plan.primary_keyword.lower() in self._extract_heading(body, "h1").lower()
        has_article_wrapper = bool(re.search(r"<article\b[^>]*>", body, flags=re.IGNORECASE))
        contains_markdown = bool(re.search(r"(^|\n)\s*#{1,6}\s+", body, flags=re.MULTILINE)) or "```" in body
        block_names = self._extract_block_names(body)
        unsupported_blocks = sorted(block_names - self.ALLOWED_GUTENBERG_BLOCKS)
        long_paragraphs = self._long_paragraphs(body)
        speculative_hits = self._speculative_sentences(visible_text)

        seo_score = 50
        quality_score = 50
        grounding_score = 40

        if body:
            quality_score += 5
        else:
            issues.append("Missing WordPress HTML body")

        if words >= self.config.min_article_words:
            quality_score += 20
        else:
            issues.append(f"Article is too short: {words} words")

        if h1_count == 1:
            quality_score += 10
        else:
            issues.append("WordPress article must contain exactly one H1")

        if 2 <= h2_count <= 3:
            quality_score += 10
        elif h2_count < 2:
            issues.append("WordPress article needs at least 1 story H2 plus Sources")
        else:
            issues.append("WordPress article should use no more than 2 story H2 sections plus Sources")

        if not long_paragraphs:
            quality_score += 10
        else:
            issues.append("Paragraphs are too long for a news-style article")

        if has_article_wrapper:
            quality_score += 5
        else:
            issues.append("WordPress article must be wrapped in an <article> element")

        if contains_markdown:
            issues.append("WordPress article HTML still contains markdown markers")
        else:
            quality_score += 5

        if block_names:
            quality_score += 10
        else:
            issues.append("WordPress article must use Gutenberg block markup")

        if unsupported_blocks:
            issues.append(f"Unsupported Gutenberg blocks used: {', '.join(unsupported_blocks)}")

        if speculative_hits:
            issues.append("Article contains speculative language without clear attribution")

        if len(article.image_prompts) == 3:
            quality_score += 10
        else:
            issues.append("Image prompt count must be exactly 3")

        if keyword_in_title:
            seo_score += 20
        else:
            issues.append("Primary keyword missing from title")

        if keyword_in_meta:
            seo_score += 15
        else:
            issues.append("Primary keyword missing from meta description")

        if keyword_in_h1:
            seo_score += 15
        else:
            issues.append("Primary keyword missing from H1")

        if attribution_hits >= 1:
            grounding_score += 20
        else:
            issues.append("Missing attributed reporting in article body")

        minimum_attribution_hits = min(2, len(research.sources))
        if attribution_hits >= minimum_attribution_hits:
            grounding_score += 30
        else:
            issues.append("Not enough attributed reporting sources in article body")

        quality_score = min(quality_score, 100)
        seo_score = min(seo_score, 100)
        grounding_score = min(grounding_score, 100)
        publish = not issues and min(quality_score, seo_score, grounding_score) >= 70

        return ValidationResult(
            quality_score=quality_score,
            seo_score=seo_score,
            grounding_score=grounding_score,
            issues=issues,
            publish=publish,
        )

    def _tag_count(self, article_html: str, tag: str) -> int:
        return len(re.findall(rf"<{tag}\b", article_html, flags=re.IGNORECASE))

    def _extract_heading(self, article_html: str, tag: str) -> str:
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", article_html, flags=re.IGNORECASE | re.DOTALL)
        if match is None:
            return ""
        return self._visible_text(match.group(1)).strip()

    def _visible_text(self, article_html: str) -> str:
        return re.sub(r"<[^>]+>", " ", article_html)

    def _extract_block_names(self, article_html: str) -> set[str]:
        return {
            match.group(1)
            for match in re.finditer(r"<!--\s*wp:([a-z0-9-]+)(?:\s+[^>]*)?-->", article_html, flags=re.IGNORECASE)
        }

    def _attribution_hits(self, visible_text: str, research: ResearchPacket) -> int:
        lowered = visible_text.lower()
        hits = 0
        seen: set[str] = set()
        for source in research.sources:
            publisher = (source.publisher or "").strip()
            if publisher and publisher.lower() not in seen and publisher.lower() in lowered:
                seen.add(publisher.lower())
                hits += 1
        if "according to" in lowered or "reported" in lowered:
            hits += 1
        return hits

    def _long_paragraphs(self, article_html: str) -> list[str]:
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", article_html, flags=re.IGNORECASE | re.DOTALL)
        long_paragraphs: list[str] = []
        for paragraph in paragraphs:
            text = self._visible_text(paragraph).strip()
            if not text:
                continue
            sentence_count = len(re.findall(r"[.!?]+", text))
            word_count = len(re.findall(r"\b\w+\b", text))
            if sentence_count > 3 or word_count > 90:
                long_paragraphs.append(text)
        return long_paragraphs

    def _speculative_sentences(self, visible_text: str) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", visible_text)
        hits: list[str] = []
        for sentence in sentences:
            lowered = sentence.lower()
            if not lowered:
                continue
            if not re.search(r"\b(may|could|likely|might)\b", lowered):
                continue
            if any(marker in lowered for marker in {"according to", "reported", "said", "expected", "projects", "forecast"}):
                continue
            hits.append(sentence.strip())
        return hits

class ReviewAgent(BaseAgent):
    stage_name = "validator"

    def __init__(self, service: ValidationService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.blog is None or context.run.research is None or context.run.plan is None:
            raise RuntimeError("Blog, research, and plan are required before validation")

        context.run.validation = self.service.validate(context.run.blog, context.run.research, context.run.plan)
        validation = context.run.validation
        self.logger.info(
            context.run,
            "Validation scores - "
            f"quality: {validation.quality_score}, seo: {validation.seo_score}, grounding: {validation.grounding_score}",
        )