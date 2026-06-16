from __future__ import annotations

from urllib.parse import urlsplit

from news_agent.models import ResearchPacket, TrendTopic

from .models import EditorialDecision


class EditorialTriageService:
    GENERIC_TOPIC_TERMS = {
        "election",
        "elections",
        "primary",
        "runoff",
        "vote",
        "voting",
        "results",
        "race",
        "campaign",
        "politics",
        "news",
        "latest",
        "live",
    }
    OPINION_MARKERS = {"opinion", "analysis", "comment", "i have", "essay", "column", "|"}
    STORY_TYPE_MARKERS = {
        "results": {"live results", "results", "runoff", "election"},
        "reform": {"ranked-choice", "voting rights", "voting order", "ballot", "reform"},
        "turnout": {"turnout", "gen z", "voters", "vote in", "polling place"},
        "candidate_race": {"campaign", "race", "candidate", "scandals", "nomination"},
    }
    JURISDICTION_MARKERS = {
        "texas",
        "california",
        "florida",
        "new york",
        "presidential",
        "democratic",
        "republican",
        "senate",
        "house",
    }

    DOMAIN_SIGNALS = {
        "health": {"cancer", "diagnosis", "diagnosed", "surgery", "treatment", "hospital", "illness", "health"},
        "tech": {"ai", "artificial intelligence", "panel", "model", "startup", "software", "chip", "tech"},
        "politics": {"trump", "biden", "senate", "house", "campaign", "election", "white house", "administration", "republican", "democrat"},
        "legal": {"lawsuit", "court", "judge", "justice", "doj", "ruling", "appeal", "investigation"},
        "business": {"earnings", "shares", "revenue", "guidance", "deal", "merger", "market"},
        "sports": {"game", "series", "playoffs", "goal", "final", "semifinal", "match", "runoff"},
    }
    CONSEQUENCE_HINTS = {
        "appoint", "appointment", "lawsuit", "ruling", "election", "runoff", "policy", "panel", "vote",
        "guidance", "earnings", "wins", "defeat", "release", "launch", "hearing", "treatment",
    }

    def decide(self, topic: TrendTopic, research: ResearchPacket) -> EditorialDecision:
        corpus = self._corpus(topic, research)
        source_count = len(research.sources)
        unique_publishers = self._unique_publishers(research)
        timeline_depth = sum(1 for bucket in (research.present, research.past, research.future) if bucket)
        domains = [name for name, signals in self.DOMAIN_SIGNALS.items() if any(signal in corpus for signal in signals)]
        fused_story = self._is_fused_story(topic, research, domains, corpus)
        has_clear_event = bool(research.lead or research.present)
        has_consequence = any(token in corpus for token in self.CONSEQUENCE_HINTS) and bool(research.future or research.past)

        missing_elements: list[str] = []
        if fused_story:
            missing_elements.append("single editorial angle")
        if source_count < 3 or unique_publishers < 2:
            missing_elements.append("reporting depth")
        if not research.past:
            missing_elements.append("chronology")
        if not has_consequence:
            missing_elements.append("clear consequence")
        if not has_clear_event:
            missing_elements.append("clear current development")

        if not has_clear_event or source_count == 0:
            article_mode = "skip"
        elif fused_story:
            article_mode = "brief"
        elif source_count >= 4 and unique_publishers >= 3 and timeline_depth >= 2 and has_consequence:
            article_mode = "full_article"
        elif source_count >= 3 and timeline_depth >= 2:
            article_mode = "explainer"
        else:
            article_mode = "brief"

        evidence_strength = "high" if source_count >= 4 and unique_publishers >= 3 else "medium" if source_count >= 2 else "low"
        confidence = 0.35
        if has_clear_event:
            confidence += 0.2
        if timeline_depth >= 2:
            confidence += 0.15
        if has_consequence:
            confidence += 0.15
        if unique_publishers >= 3:
            confidence += 0.1
        if fused_story:
            confidence -= 0.15
        confidence = max(0.0, min(0.95, round(confidence, 2)))

        primary_angle = self._primary_angle(topic, research, fused_story)
        reasoning = self._reasoning(article_mode, fused_story, source_count, unique_publishers, timeline_depth, has_consequence)
        return EditorialDecision(
            topic=topic.keyword,
            article_mode=article_mode,
            primary_angle=primary_angle,
            reasoning=reasoning,
            confidence=confidence,
            should_write=article_mode != "skip",
            evidence_strength=evidence_strength,
            missing_elements=self._dedupe(missing_elements),
        )

    def _primary_angle(self, topic: TrendTopic, research: ResearchPacket, fused_story: bool) -> str:
        if fused_story:
            generic_label = self._clean_topic_label(topic.keyword)
            return f"{generic_label} coverage still lacks one clear news angle" if generic_label else topic.keyword
        if research.present:
            return research.present[0].strip()
        if research.lead:
            return research.lead.strip()
        if research.sources:
            return research.sources[0].title.strip()
        return topic.keyword

    def _corpus(self, topic: TrendTopic, research: ResearchPacket) -> str:
        return " ".join(
            [
                topic.keyword,
                research.lead,
                *research.present[:2],
                *research.past[:2],
                *research.future[:2],
                *(source.title for source in research.sources[:4]),
            ]
        ).casefold()

    def _is_fused_story(self, topic: TrendTopic, research: ResearchPacket, domains: list[str], corpus: str) -> bool:
        domain_set = set(domains)
        if "health" in domain_set and ("tech" in domain_set or "business" in domain_set):
            return True
        if "health" in domain_set and "politics" in domain_set and any(token in corpus for token in {"panel", "appointment", "ai"}):
            return True

        topic_is_generic = self._is_generic_topic(topic.keyword)
        story_types = self._story_types(research)
        jurisdictions = self._jurisdictions(research)
        has_opinion = self._has_opinion_source(research)

        if topic_is_generic and len(story_types) >= 2:
            return True
        if topic_is_generic and len(jurisdictions) >= 2:
            return True
        if has_opinion and ("results" in story_types or "turnout" in story_types or "reform" in story_types):
            return True
        return False

    def _is_generic_topic(self, keyword: str) -> bool:
        tokens = [token.strip(" ,.:;!?()[]{}\"'").casefold() for token in (keyword or "").split()]
        tokens = [token for token in tokens if token]
        if not tokens:
            return True
        if len(tokens) <= 3 and all(token in self.GENERIC_TOPIC_TERMS for token in tokens):
            return True
        generic_count = sum(1 for token in tokens if token in self.GENERIC_TOPIC_TERMS)
        return generic_count >= 2 and generic_count == len(tokens)

    def _story_types(self, research: ResearchPacket) -> set[str]:
        story_types: set[str] = set()
        for source in research.sources[:5]:
            lowered = source.title.casefold()
            for story_type, markers in self.STORY_TYPE_MARKERS.items():
                if any(marker in lowered for marker in markers):
                    story_types.add(story_type)
        return story_types

    def _jurisdictions(self, research: ResearchPacket) -> set[str]:
        jurisdictions: set[str] = set()
        for source in research.sources[:5]:
            lowered = source.title.casefold()
            for marker in self.JURISDICTION_MARKERS:
                if marker in lowered:
                    jurisdictions.add(marker)
        return jurisdictions

    def _has_opinion_source(self, research: ResearchPacket) -> bool:
        for source in research.sources[:5]:
            lowered = f"{source.title} {source.url}".casefold()
            if any(marker in lowered for marker in self.OPINION_MARKERS):
                return True
        return False

    def _clean_topic_label(self, keyword: str) -> str:
        normalized = " ".join((keyword or "").split()).strip()
        if not normalized:
            return ""
        return normalized[:1].upper() + normalized[1:]

    def _unique_publishers(self, research: ResearchPacket) -> int:
        publishers: set[str] = set()
        for source in research.sources:
            publisher = source.publisher.strip().casefold()
            if publisher:
                publishers.add(publisher)
                continue
            hostname = urlsplit(source.url).netloc.casefold().removeprefix("www.")
            if hostname:
                publishers.add(hostname)
        return len(publishers)

    def _reasoning(
        self,
        article_mode: str,
        fused_story: bool,
        source_count: int,
        unique_publishers: int,
        timeline_depth: int,
        has_consequence: bool,
    ) -> str:
        if article_mode == "skip":
            return "The topic does not yet support a defensible article because the current development is too thin or missing."
        if fused_story:
            return (
                "The topic appears to combine separate angles into one trend signal. Limit it to a brief until a single supported angle is chosen."
            )
        if article_mode == "full_article":
            return (
                f"The research bundle has {source_count} sources across {unique_publishers} publishers, enough chronology, and a supported consequence."
            )
        if article_mode == "explainer":
            return (
                f"The topic has enough reporting and timeline context for an explainer, but it still needs tighter consequence framing for a full article."
            )
        if has_consequence:
            return "There is a real event here, but the reporting depth is still better suited to a brief than a full article."
        return f"The topic has {source_count} sources and timeline depth {timeline_depth}, but the consequence is not yet strong enough for a deeper piece."

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered