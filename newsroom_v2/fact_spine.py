from __future__ import annotations

from news_agent.models import ResearchPacket, TrendTopic

from .models import EditorialDecision, FactSpine


class FactSpineBuilder:
    OFFICIAL_HINTS = {"white house", "department", "justice", "court", "campaign", "official", "administration", "spokesperson"}

    def build(self, topic: TrendTopic, research: ResearchPacket, decision: EditorialDecision) -> FactSpine:
        timeline = self._build_timeline(research)
        key_facts = self._dedupe(
            [
                research.lead,
                *research.present[:2],
                *research.past[:2],
                *[claim.claim for claim in research.claims[:3]],
            ]
        )
        official_points = self._official_points(research)
        consequence = self._consequence(research)
        why_it_matters = consequence or (research.future[0] if research.future else "")
        source_urls = self._dedupe([source.url.strip() for source in research.sources if source.url.strip()])

        return FactSpine(
            topic=topic.keyword,
            primary_angle=decision.primary_angle,
            article_mode=decision.article_mode,
            core_event=research.present[0] if research.present else (research.lead or topic.keyword),
            why_it_matters=why_it_matters,
            timeline=timeline,
            key_facts=key_facts[:6],
            official_points=official_points[:3],
            consequence=consequence,
            open_questions=[f"Needs stronger support for {item}." for item in decision.missing_elements],
            source_urls=source_urls[:6],
            confidence=decision.confidence,
        )

    def _build_timeline(self, research: ResearchPacket) -> list[str]:
        timeline: list[str] = []
        for source in research.sources[:4]:
            if source.published_at.strip():
                timeline.append(f"{source.published_at.strip()}: {source.title.strip()}")
        for item in research.past[:2]:
            timeline.append(f"Background: {item.strip()}")
        for item in research.present[:2]:
            timeline.append(f"Now: {item.strip()}")
        for item in research.future[:2]:
            timeline.append(f"Next: {item.strip()}")
        return self._dedupe(timeline)

    def _official_points(self, research: ResearchPacket) -> list[str]:
        points: list[str] = []
        for source in research.sources:
            title = source.title.strip()
            lowered = f"{source.publisher} {title}".casefold()
            if any(hint in lowered for hint in self.OFFICIAL_HINTS):
                points.append(title)
        for claim in research.claims:
            lowered = claim.claim.casefold()
            if any(hint in lowered for hint in self.OFFICIAL_HINTS):
                points.append(claim.claim.strip())
        return self._dedupe(points)

    def _consequence(self, research: ResearchPacket) -> str:
        if research.future:
            return research.future[0].strip()
        if len(research.present) >= 2:
            return research.present[1].strip()
        return ""

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered