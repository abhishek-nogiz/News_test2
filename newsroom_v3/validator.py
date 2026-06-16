from __future__ import annotations

import re

from .evidence import EvidenceService
from .models import (
    AttributionFailure,
    DensityFailure,
    DraftArticle,
    GroundingFailure,
    QuarantineFailure,
    QuarantineItem,
    RawDocument,
    SourcesFailure,
    StructureFailure,
    StyleFailure,
    ValidationResult,
    WriterInput,
)


class NewsroomValidator:
    SCRAPER_NOISE_PATTERN = re.compile(
        r"\b(enter your email addresssubscribe|blocked by an extension|most read|change your local station|my station|educate your inbox|list of \d+ items|end of list|loading \* \* \*|wapo\.zeustechnology\.com|googleadd al jazeera|this page has been blocked|try disabling your extensions|dow jones reprints|djreprints\.com|this copy is for your personal|an error has occurred|an error occurred|please try again later|already a subscriber|continue reading your article with a wsj subscription|site search home news sport business technology|add as preferred on google|add as preferred source on google|skip to content|trinity audio player|currently following this author|read in app|loading audio narration|listen to this article|suggest a correction|read next >|provided by nexstar media group|power partner awards|apply today|linkedinfacebookxblueskylink)\b",
        flags=re.IGNORECASE,
    )

    def validate(
        self,
        draft: DraftArticle,
        writer_input: WriterInput,
        raw_documents: list[RawDocument],
        quarantine_items: list[QuarantineItem],
    ) -> ValidationResult:
        blocking_failures: list[GroundingFailure | QuarantineFailure | AttributionFailure] = []
        structural_failures: list[StructureFailure | SourcesFailure] = []
        formatting_failures: list[StyleFailure | DensityFailure] = []

        sentence_mappings = draft.sentences_with_claim_ids or []
        for mapping in sentence_mappings:
            claim_ids = list(mapping.get("claim_ids") or [])
            sentence = str(mapping.get("sentence") or "").strip()
            if sentence and not claim_ids:
                blocking_failures.append(GroundingFailure(sentence=sentence, claim_ids=[], reason="Sentence is not grounded in a verified claim."))

        quarantine_ids = {item.fingerprint for item in quarantine_items}
        for mapping in sentence_mappings:
            sentence = str(mapping.get("sentence") or "")
            fingerprint = EvidenceService.fingerprint(sentence)
            if fingerprint and fingerprint in quarantine_ids:
                blocking_failures.append(QuarantineFailure(claim_ids=list(mapping.get("claim_ids") or []), reason="Draft sentence matches a quarantined claim fingerprint."))

        sources_section_match = re.search(r'<section data-type="sources">(.*?)</section>', draft.html, flags=re.IGNORECASE | re.DOTALL)
        sources_section_html = sources_section_match.group(1) if sources_section_match else ""
        sources_section_urls = re.findall(r'<a href="([^"]+)"', sources_section_html, flags=re.IGNORECASE | re.DOTALL)
        available_urls = {document.url for document in raw_documents if document.url}
        for claim_id, claim_urls in writer_input.claim_source_map.items():
            if claim_id not in {claim for mapping in sentence_mappings for claim in mapping.get("claim_ids", [])}:
                continue
            if not set(claim_urls) & set(sources_section_urls):
                blocking_failures.append(AttributionFailure(claim_ids=[claim_id], reason="Claim is used in the draft without a matching source URL in the Sources section."))

        h1_count = len(re.findall(r"<h1\b", draft.html, flags=re.IGNORECASE))
        h2_count = len(re.findall(r"<h2\b", draft.html, flags=re.IGNORECASE))
        first_paragraph_match = re.search(r"<p>(.*?)</p>", draft.html, flags=re.IGNORECASE | re.DOTALL)
        if h1_count != 1:
            structural_failures.append(StructureFailure("Draft must contain exactly one H1."))
        if h2_count > 4 or h2_count < 2:
            structural_failures.append(StructureFailure("Draft must contain between two and four H2 headings including Sources."))
        if first_paragraph_match is None:
            structural_failures.append(StructureFailure("Draft must contain a lead paragraph immediately after the H1."))
        else:
            lead_text = re.sub(r"<[^>]+>", " ", first_paragraph_match.group(1)).strip()
            lead_word_count = len(re.findall(r"\b\w+\b", lead_text))
            if lead_word_count > 55:
                structural_failures.append(StructureFailure("Lead paragraph exceeds the 55-word limit."))
            first_alpha_match = re.search(r"[A-Za-z]", lead_text)
            if first_alpha_match is not None and lead_text[first_alpha_match.start()].islower():
                structural_failures.append(StructureFailure("Lead paragraph must begin with a capitalized word."))
            if self.SCRAPER_NOISE_PATTERN.search(lead_text):
                formatting_failures.append(StyleFailure("Lead paragraph contains residual source chrome or scraper noise."))

        if '<section data-type="sources">' not in draft.html:
            structural_failures.append(SourcesFailure("Draft is missing the required Sources section."))
        else:
            for url in sources_section_urls:
                if url not in available_urls:
                    structural_failures.append(SourcesFailure("Sources section contains a URL that does not resolve to a RawDocument."))
                    break

        if re.search(r"\sstyle=", draft.html, flags=re.IGNORECASE):
            formatting_failures.append(StyleFailure("Draft contains inline style attributes."))
        if re.search(r"<div\b", draft.html, flags=re.IGNORECASE):
            formatting_failures.append(StyleFailure("Draft contains div elements in the article body."))
        if re.search(r"<br\s*/?>", draft.html, flags=re.IGNORECASE):
            formatting_failures.append(StyleFailure("Draft contains br tags instead of paragraph blocks."))

        body_without_sources = draft.html.split('<section data-type="sources">', 1)[0]
        paragraph_matches = re.findall(r"<p>(.*?)</p>", body_without_sources, flags=re.IGNORECASE | re.DOTALL)
        link_count = 0
        consecutive_linked_paragraphs = 0
        for paragraph in paragraph_matches:
            visible_paragraph = re.sub(r"<[^>]+>", " ", paragraph).strip()
            if self.SCRAPER_NOISE_PATTERN.search(visible_paragraph):
                formatting_failures.append(StyleFailure("Draft contains residual source chrome or scraper noise in the article body."))
                break
            paragraph_links = len(re.findall(r"<a\b", paragraph, flags=re.IGNORECASE))
            link_count += paragraph_links
            if paragraph_links > 1:
                formatting_failures.append(DensityFailure("A body paragraph contains more than one inline link."))
            if paragraph_links:
                consecutive_linked_paragraphs += 1
            else:
                consecutive_linked_paragraphs = 0
            if consecutive_linked_paragraphs > 2:
                formatting_failures.append(DensityFailure("Draft contains more than two consecutive linked paragraphs."))
                break
            paragraph_word_count = len(re.findall(r"\b\w+\b", re.sub(r"<[^>]+>", " ", paragraph)))
            if paragraph_word_count > 80:
                formatting_failures.append(DensityFailure("A body paragraph exceeds the 80-word limit."))
                break
        if link_count > 6:
            formatting_failures.append(DensityFailure("Draft exceeds the maximum inline-link density."))
        if paragraph_matches and re.search(r"<a\b", paragraph_matches[0], flags=re.IGNORECASE):
            formatting_failures.append(DensityFailure("Lead paragraph must not contain inline links."))

        passed = not blocking_failures and not structural_failures and not formatting_failures
        return ValidationResult(
            passed=passed,
            blocking_failures=blocking_failures,
            structural_failures=structural_failures,
            formatting_failures=formatting_failures,
        )