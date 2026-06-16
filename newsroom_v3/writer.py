from __future__ import annotations

from html import escape
import re

from .models import ContextReference, DraftArticle, WriterInput


class NewsroomWriter:
    DISPLAY_ANCHORS = (
        "A federal judge",
        "A U.S. judge",
        "A United States judge",
        "Federal judge",
        "Court lets",
        "The legal battle",
        "The challengers argued",
        "The executive order",
        "Until then",
    )

    def draft(self, writer_input: WriterInput, *, attempt_number: int = 1, compact: bool = False) -> DraftArticle:
        lead_text = writer_input.claim_lookup.get(writer_input.lead_claim_id, writer_input.headline)
        lead_text = self._polish_rendered_text(lead_text, is_lead=True)
        dek = lead_text[:155].rstrip(".!?")
        intro_paragraphs = [self._trim_sentence(lead_text, word_limit=42 if compact else 55)]
        source_by_url = {document.url: document for document in writer_input.sources if document.url}

        body_sections: list[tuple[str, list[tuple[str, list[str]]]]] = []
        for heading, claim_ids in zip(writer_input.section_heads, writer_input.section_claim_ids):
            section_paragraphs: list[tuple[str, list[str]]] = []
            for claim_id in claim_ids:
                text = writer_input.claim_lookup.get(claim_id, "").strip()
                if not text:
                    continue
                text = self._polish_rendered_text(text)
                primary_sentence, supporting_sentence = self._split_claim_text(text)
                section_paragraphs.append((self._trim_sentence(primary_sentence, word_limit=60 if compact else 80), [claim_id]))
                if supporting_sentence:
                    section_paragraphs.append((self._trim_sentence(supporting_sentence, word_limit=60 if compact else 80), [claim_id]))
            if not section_paragraphs:
                continue
            body_sections.append((heading, section_paragraphs))

        body_html: list[str] = [
            f'<article class="trend-agent-post" data-story-id="{escape(writer_input.story_id)}" data-run-id="">',
            self._heading_block(1, writer_input.headline),
        ]
        markdown_parts = [f"# {writer_input.headline}", intro_paragraphs[0]]
        sentences_with_claim_ids = [{"sentence": intro_paragraphs[0], "claim_ids": [writer_input.lead_claim_id]}]

        for paragraph in intro_paragraphs:
            body_html.append(self._paragraph_block(paragraph))

        linked_paragraph_streak = 0
        linked_entities: set[str] = set()
        for heading, paragraphs in body_sections:
            body_html.append(self._heading_block(2, heading))
            markdown_parts.append(f"## {heading}")
            for paragraph_text, claim_ids in paragraphs:
                context_reference = self._context_reference(paragraph_text, writer_input.context_references, linked_entities)
                reference_url, reference_label = (None, None)
                if context_reference is None:
                    reference_url, reference_label = self._body_reference(claim_ids, writer_input, source_by_url)
                if linked_paragraph_streak >= 2:
                    context_reference = None
                    reference_url = None
                    reference_label = None
                body_html.append(
                    self._paragraph_block(
                        paragraph_text,
                        source_url=reference_url,
                        source_label=reference_label,
                        context_reference=context_reference,
                    )
                )
                markdown_parts.append(
                    self._markdown_paragraph(
                        paragraph_text,
                        source_url=reference_url,
                        source_label=reference_label,
                        context_reference=context_reference,
                    )
                )
                sentences_with_claim_ids.append({"sentence": paragraph_text, "claim_ids": claim_ids})
                if context_reference is not None:
                    linked_entities.add(context_reference.entity.casefold())
                linked_paragraph_streak = linked_paragraph_streak + 1 if (context_reference is not None or (reference_url and reference_label)) else 0

        body_html.append(self._sources_block(writer_input))
        markdown_parts.append("## Sources")
        for document in writer_input.sources:
            label = self._source_link_label(document)
            markdown_parts.append(f"- [{label}]({document.url})")
        body_html.append("</article>")

        return DraftArticle(
            headline=writer_input.headline,
            dek=dek,
            html="".join(body_html) + "\n",
            markdown="\n\n".join(markdown_parts).strip() + "\n",
            sentences_with_claim_ids=sentences_with_claim_ids,
            attempt_number=attempt_number,
            article_type=writer_input.article_type,
        )

    def _heading_block(self, level: int, text: str) -> str:
        text = escape(text.rstrip(".!?" if level == 1 else ""))
        return f'<!-- wp:heading {{"level":{level},"textAlign":"left"}} --><h{level}>{text}</h{level}><!-- /wp:heading -->'

    def _paragraph_block(
        self,
        text: str,
        *,
        source_url: str | None = None,
        source_label: str | None = None,
        context_reference: ContextReference | None = None,
    ) -> str:
        html_text = self._html_text_with_context_reference(text, context_reference)
        if context_reference is None and source_url and source_label:
            html_text += f' According to <a href="{escape(source_url)}">{escape(source_label)}</a>.'
        return f"<!-- wp:paragraph --><p>{html_text}</p><!-- /wp:paragraph -->"

    def _markdown_paragraph(
        self,
        text: str,
        *,
        source_url: str | None = None,
        source_label: str | None = None,
        context_reference: ContextReference | None = None,
    ) -> str:
        markdown_text = self._markdown_text_with_context_reference(text, context_reference)
        if context_reference is None and source_url and source_label:
            return f"{text} According to [{source_label}]({source_url})."
        return markdown_text

    def _sources_block(self, writer_input: WriterInput) -> str:
        items = []
        for document in writer_input.sources:
            claim_ids = [claim_id for claim_id, urls in writer_input.claim_source_map.items() if document.url in urls]
            joined_claim_ids = ",".join(claim_ids)
            label = self._source_link_label(document)
            items.append(
                f'<li data-claim-ids="{escape(joined_claim_ids)}"><a href="{escape(document.url)}">{escape(label)}</a></li>'
            )
        item_markup = "".join(items)
        return (
            '<!-- wp:heading {"level":2,"textAlign":"left"} --><h2>Sources</h2><!-- /wp:heading -->'
            f'<section data-type="sources"><ul>{item_markup}</ul></section>'
        )

    def _source_link_label(self, document: object) -> str:
        title = getattr(document, "title", "")
        publisher = getattr(document, "publisher", "")
        url = getattr(document, "url", "")
        return (title or publisher or url).strip() or url

    def _trim_sentence(self, text: str, *, word_limit: int) -> str:
        words = text.split()
        if len(words) <= word_limit:
            return text.rstrip()
        return " ".join(words[:word_limit]).rstrip(" ,;:") + "..."

    def _body_reference(
        self,
        claim_ids: list[str],
        writer_input: WriterInput,
        source_by_url: dict[str, object],
    ) -> tuple[str | None, str | None]:
        if len(claim_ids) != 1:
            return None, None
        urls = writer_input.claim_source_map.get(claim_ids[0], [])
        if not urls:
            return None, None
        url = urls[0]
        document = source_by_url.get(url)
        label = getattr(document, "publisher", "") if document is not None else ""
        return url, (label or url)

    def _context_reference(
        self,
        text: str,
        context_references: list[ContextReference],
        linked_entities: set[str],
    ) -> ContextReference | None:
        ordered = sorted(context_references, key=lambda reference: len(reference.entity), reverse=True)
        for reference in ordered:
            entity = (reference.entity or "").strip()
            if not entity or entity.casefold() in linked_entities or not reference.url:
                continue
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(entity)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
                return reference
        return None

    def _html_text_with_context_reference(self, text: str, context_reference: ContextReference | None) -> str:
        if context_reference is None:
            return escape(text)
        entity = context_reference.entity.strip()
        if not entity:
            return escape(text)
        match = re.search(rf"(?<![A-Za-z0-9]){re.escape(entity)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE)
        if match is None:
            return escape(text)
        return (
            escape(text[: match.start()])
            + f'<a href="{escape(context_reference.url)}">{escape(text[match.start():match.end()])}</a>'
            + escape(text[match.end():])
        )

    def _markdown_text_with_context_reference(self, text: str, context_reference: ContextReference | None) -> str:
        if context_reference is None:
            return text
        entity = context_reference.entity.strip()
        if not entity:
            return text
        match = re.search(rf"(?<![A-Za-z0-9]){re.escape(entity)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE)
        if match is None:
            return text
        return (
            text[: match.start()]
            + f'[{text[match.start():match.end()]}]({context_reference.url})'
            + text[match.end():]
        )

    def _split_claim_text(self, text: str) -> tuple[str, str]:
        sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", text.strip()) if segment.strip()]
        if len(sentences) <= 1:
            return text.strip(), ""
        primary = sentences[0]
        supporting = " ".join(sentences[1:]).strip()
        return primary, supporting

    def _polish_rendered_text(self, text: str, *, is_lead: bool = False) -> str:
        normalized = str(text or "")
        normalized = re.sub(r"Enter your email addressSubscribe", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Loading\s+\*\s*\*\s*\*.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"Most Read.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"This page has been blocked.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"wapo\.zeustechnology\.com.*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"change your local station", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"^Save\s+", "", normalized, flags=re.IGNORECASE)

        anchor_positions = [
            match.start()
            for anchor in self.DISPLAY_ANCHORS
            for match in [re.search(re.escape(anchor), normalized, flags=re.IGNORECASE)]
            if match is not None
        ]
        if anchor_positions:
            first_anchor = min(anchor_positions)
            prefix = normalized[:first_anchor]
            if first_anchor > 0 and re.search(r"\b(politics|associated press|pbs news|summary|reuters|your|save|loading|most read)\b", prefix, flags=re.IGNORECASE):
                normalized = normalized[first_anchor:]

        normalized = re.sub(r"\s+", " ", normalized).strip(" -:|,.;")
        first_alpha_match = re.search(r"[A-Za-z]", normalized)
        if first_alpha_match is not None:
            index = first_alpha_match.start()
            normalized = normalized[:index] + normalized[index].upper() + normalized[index + 1:]
        if is_lead and normalized and normalized[-1] not in ".!?":
            normalized = normalized.rstrip(" ,;:") + "."
        return normalized