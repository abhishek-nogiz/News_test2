from __future__ import annotations

import re

from .models import CandidateDossier, EvidenceLedgerEntry, NewsroomPlan


class NewsroomPlanningService:
    TOPIC_NOISE = {
        "latest", "news", "cnn", "bbc", "politico", "axios", "update", "updates", "live", "breaking",
        "exclusive", "today", "analysis",
    }

    def build(self, dossier: CandidateDossier) -> NewsroomPlan:
        topic = dossier.topic.keyword
        decision = dossier.decision
        fact_spine = dossier.fact_spine
        research = dossier.research

        headline = self._headline(dossier)
        angle = self._clean_text(decision.primary_angle or fact_spine.core_event or topic, limit=180)
        lead_focus = self._clean_text(fact_spine.core_event or angle, limit=180)
        nut_graf = self._clean_text(fact_spine.why_it_matters or fact_spine.consequence or decision.reasoning, limit=180)
        section_heads = self._section_heads(dossier)
        section_goals = self._section_goals(dossier)
        section_evidence = self._section_evidence(dossier, len(section_heads))
        source_titles = [source.title.strip() for source in research.sources[:5] if source.title.strip()]
        constraints = [
            "Write one clear news angle, not a stitched trend mashup.",
            "Open with the confirmed development and establish chronology early.",
            "Explain consequence with sourced facts, not generic filler.",
            "Use visible sources naturally and keep Sources at the bottom.",
            "Use natural body hyperlinks instead of citation-style source links; background-only Wikipedia links are allowed for named entities.",
        ]

        return NewsroomPlan(
            headline=headline,
            angle=angle,
            article_mode=decision.article_mode,
            lead_focus=lead_focus,
            nut_graf=nut_graf,
            section_heads=section_heads,
            section_goals=section_goals,
            section_evidence=section_evidence,
            source_titles=source_titles,
            constraints=constraints,
        )

    def _headline(self, dossier: CandidateDossier) -> str:
        topic = dossier.topic.keyword
        decision = dossier.decision
        fact_spine = dossier.fact_spine

        if "single editorial angle" in decision.missing_elements:
            subject = self._headline_subject(topic)
            return self._clean_text(f"{subject} coverage still lacks one clear news angle", limit=120)

        candidates = [fact_spine.core_event, decision.primary_angle, fact_spine.why_it_matters, topic]
        for candidate in candidates:
            normalized = self._clean_headline_candidate(candidate)
            if normalized and not self._looks_like_bad_headline(normalized):
                return self._clean_text(normalized, limit=120)

        return self._clean_text(self._headline_subject(topic), limit=120)

    def _section_heads(self, dossier: CandidateDossier) -> list[str]:
        topic = dossier.topic.keyword
        decision = dossier.decision
        fact_spine = dossier.fact_spine
        corpus = " ".join([topic, fact_spine.core_event, fact_spine.why_it_matters, fact_spine.consequence]).casefold()
        subject = self._subject_label(topic)

        if "single editorial angle" in decision.missing_elements:
            return [
                self._clean_heading("What current reporting is actually about"),
                self._clean_heading("Why this topic still lacks one clear angle"),
            ]

        if decision.article_mode == "brief":
            return [
                self._clean_heading(f"What changed for {subject}"),
                self._clean_heading(f"Why {subject} matters now"),
            ]
        if any(token in corpus for token in {"lawsuit", "court", "doj", "judge", "appeal"}):
            return [
                self._clean_heading(f"What the {subject} filing is trying to do"),
                self._clean_heading(f"Why the {subject} case matters now"),
            ]
        if any(token in corpus for token in {"investigation", "investigations", "prosecuted", "prosecution", "criminal"}):
            return [
                self._clean_heading(f"How the {subject} investigation is unfolding"),
                self._clean_heading(f"Why the {subject} scrutiny matters now"),
            ]
        if any(token in corpus for token in {"election", "runoff", "vote", "campaign", "republican", "democrat"}):
            return [
                self._clean_heading(f"How the {subject} result reshapes the race"),
                self._clean_heading(f"What the {subject} result means next"),
            ]
        if any(token in corpus for token in {"appoint", "appointment", "panel", "board", "nominee", "role"}):
            return [
                self._clean_heading(f"What the {subject} appointment would change"),
                self._clean_heading(f"Why the {subject} role matters now"),
            ]
        if any(token in corpus for token in {"earnings", "market", "shares", "guidance"}):
            return [
                self._clean_heading(f"What the {subject} report showed"),
                self._clean_heading(f"Why {subject} matters to markets"),
            ]
        return [
            self._clean_heading(f"What changed for {subject}"),
            self._clean_heading(f"Why {subject} matters now"),
        ]

    def _section_goals(self, dossier: CandidateDossier) -> list[str]:
        fact_spine = dossier.fact_spine
        timeline_line = fact_spine.timeline[0] if fact_spine.timeline else fact_spine.core_event
        return [
            self._clean_text(timeline_line, limit=160),
            self._clean_text(fact_spine.why_it_matters or fact_spine.consequence or dossier.decision.reasoning, limit=160),
        ]

    def _section_evidence(self, dossier: CandidateDossier, section_count: int) -> list[list[EvidenceLedgerEntry]]:
        if section_count <= 0:
            return []

        lanes: list[list[EvidenceLedgerEntry]] = [[] for _ in range(section_count)]
        overflow: list[EvidenceLedgerEntry] = []

        for entry in dossier.evidence_ledger:
            if entry.section == "future":
                target_index = 1 if section_count > 1 else 0
            elif entry.section in {"present", "past"}:
                target_index = 0
            else:
                overflow.append(entry)
                continue
            lanes[target_index].append(entry)

        for entry in overflow:
            target_index = 1 if section_count > 1 else 0
            lanes[target_index].append(entry)

        if section_count > 1 and not lanes[1]:
            remainder = lanes[0][2:]
            if remainder:
                lanes[1].extend(remainder[:2])

        return [lane[:4] for lane in lanes]

    def _clean_text(self, value: str, limit: int) -> str:
        normalized = " ".join((value or "").split()).strip()
        if len(normalized) <= limit:
            return normalized.rstrip(".") + ("." if normalized else "")

        first_sentence = normalized.split(". ", 1)[0].strip().rstrip(".")
        if first_sentence and len(first_sentence) <= limit:
            return first_sentence + "."

        trimmed = normalized[: max(0, limit - 3)].rstrip(" .,;:")
        return trimmed + "..."

    def _clean_heading(self, value: str, limit: int = 72) -> str:
        normalized = " ".join((value or "").split()).strip().rstrip(".?!")
        if len(normalized) <= limit:
            return normalized

        compact = normalized[:limit].rsplit(" ", 1)[0].rstrip(" -,:;")
        return compact or normalized[:limit].rstrip(" -,:;")

    def _subject_label(self, topic: str) -> str:
        tokens = []
        for token in topic.split():
            cleaned = token.strip(" ,.:;!?()[]{}\"'")
            if not cleaned:
                continue
            if cleaned.casefold() in self.TOPIC_NOISE:
                continue
            tokens.append(cleaned)

        subject = " ".join(tokens[:4]).strip() or topic.strip()
        return subject

    def _headline_subject(self, topic: str) -> str:
        normalized = self._subject_label(topic)
        return normalized[:1].upper() + normalized[1:] if normalized else topic

    def _clean_headline_candidate(self, value: str) -> str:
        normalized = " ".join((value or "").split()).strip()
        normalized = re.sub(r"\s*\|\s*.*$", "", normalized)
        normalized = re.sub(r"\bAdvertisement\b.*$", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bCopy Link\b.*$", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bLive Results\b.*$", "", normalized, flags=re.IGNORECASE)

        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
        if len(sentences) > 1 and re.search(r"\bI\b|\bmy\b|\bwe\b", sentences[1], flags=re.IGNORECASE):
            normalized = sentences[0]

        return normalized.strip().rstrip(".?!")

    def _looks_like_bad_headline(self, value: str) -> bool:
        lowered = value.casefold()
        return any(marker in lowered for marker in {"i have", "opinion", "analysis", "live results", "advertisement"})