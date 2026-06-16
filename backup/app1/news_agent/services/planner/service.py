from __future__ import annotations

import re

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ...models import ContentPlan, EditorialMemoryPacket, ResearchPacket, TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import extract_keywords, normalize_topic_category, tokenize


class ContentPlanningService:
    PRIMARY_KEYWORD_STOP_TOKENS = {
        "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with", "from",
        "after", "before", "into", "over", "under", "through", "across", "amid", "latest", "breaking",
    }
    GENERIC_PRIMARY_KEYWORD_TOKENS = {
        "bank", "news", "video", "clip", "live", "story", "stories", "latest", "update", "updates",
        "watch", "report", "reports", "analysis", "market", "politics",
    }
    ISSUE_PHRASE_RULES = [
        ({"interest", "rates"}, "interest rates"),
        ({"management", "risk"}, "management risk"),
        ({"mail", "voting"}, "mail voting"),
        ({"budget", "vote"}, "budget vote"),
        ({"election", "runoff"}, "election runoff"),
        ({"primary", "runoff"}, "primary runoff"),
        ({"ceasefire", "deal"}, "ceasefire deal"),
        ({"supreme", "court"}, "Supreme Court"),
        ({"executive", "order"}, "executive order"),
        ({"stock", "market"}, "stock market"),
        ({"inflation"}, "inflation"),
        ({"election"}, "election"),
        ({"runoff"}, "runoff"),
        ({"policy"}, "policy"),
        ({"lawsuit"}, "lawsuit"),
        ({"ruling"}, "ruling"),
        ({"tariff"}, "tariff"),
        ({"earnings"}, "earnings"),
        ({"layoffs"}, "layoffs"),
    ]
    ENTITY_SKIP_PHRASES = {
        "The Wall Street Journal", "Financial Times", "The New York Times", "Politico", "Reuters", "CNN", "BBC",
        "The Guardian", "Washington Post", "Fox News", "Fox Business", "AP", "Associated Press",
    }
    SPORTS_TOKENS = {
        "match", "game", "series", "playoffs", "playoff", "final", "finals", "nba", "nfl", "mlb", "nhl",
        "cup", "semifinal", "quarterfinal", "conference", "western", "eastern", "vs", "fixture", "round",
    }
    POLITICS_TOKENS = {
        "election", "elections", "trump", "biden", "senate", "congress", "government",
        "policy", "policies", "president", "minister", "parliament", "tariff", "vote", "voting",
        "campaign", "diplomacy", "diplomatic", "republican", "republicans", "democrat", "democrats", "democratic",
        "geopolitics", "geopolitical", "primary", "runoff", "governor", "attorney", "general", "incumbent",
    }

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config

    def _is_politics_topic(self, topic: TrendTopic, research: ResearchPacket | None = None) -> bool:
        configured_category = normalize_topic_category(self.config.topic_category) if self.config is not None else None
        if configured_category == "politics":
            return True

        keyword_tokens = set(tokenize(topic.keyword))
        if keyword_tokens & self.POLITICS_TOKENS:
            return True

        if research is None:
            return False

        title_tokens = set()
        for source in research.sources[:4]:
            title_tokens.update(tokenize(source.title))

        research_tokens = set()
        for snippet in [research.lead, *research.present[:2], *research.past[:1], *research.future[:1]]:
            research_tokens.update(tokenize(snippet))

        return bool((title_tokens | research_tokens) & self.POLITICS_TOKENS)

    def _article_type(self, topic: TrendTopic, research: ResearchPacket) -> str:
        keyword_tokens = set(tokenize(topic.keyword))
        research_tokens = set()
        for source in research.sources[:4]:
            research_tokens.update(tokenize(source.title))
        for snippet in [research.lead, *research.present[:2], *research.past[:1], *research.future[:1]]:
            research_tokens.update(tokenize(snippet))

        if self._is_politics_topic(topic, research):
            return "politics_news"
        if keyword_tokens & {"earnings", "stocks", "fed", "nasdaq", "dow"}:
            return "market_impact"
        if (keyword_tokens | research_tokens) & self.SPORTS_TOKENS:
            return "sports_news"
        if research.future:
            return "developing_news"
        return "news_analysis"

    def _politics_implication_heading(self, topic: TrendTopic, research: ResearchPacket) -> str:
        corpus = " ".join(
            [
                topic.keyword,
                *(source.title for source in research.sources[:4]),
                *research.present[:2],
                *research.past[:1],
                *research.future[:1],
            ]
        ).casefold()

        if any(token in corpus for token in {"redistrict", "district map", "congressional map", "gerrymander"}):
            return "What This Means for the Map Fight"
        if "republican" in corpus and "democrat" not in corpus and "democratic" not in corpus:
            return "What This Means for the Republican Party"
        if "democrat" in corpus or "democratic" in corpus:
            return "What This Means for Democrats"
        if "supreme court" in corpus or "court ruling" in corpus or "justice" in corpus:
            return "Why the Ruling Matters Now"
        if any(token in corpus for token in {"budget", "bill", "policy", "lawmakers", "congress"}):
            return "What This Means in Washington"
        if any(token in corpus for token in {"senate", "house", "governor", "primary", "runoff", "election", "race"}):
            return "What This Means for the Race Ahead"
        if "trump" in corpus:
            return "What This Means for Trump's Political Push"
        return "Why This Political Fight Matters Now"

    def _news_implication_heading(self, topic: TrendTopic, research: ResearchPacket, article_type: str) -> str:
        corpus = " ".join(
            [
                topic.keyword,
                *(source.title for source in research.sources[:4]),
                *research.present[:2],
                *research.past[:1],
                *research.future[:1],
            ]
        ).casefold()

        if article_type == "market_impact":
            return "What It Means for Markets"
        if article_type == "sports_news":
            if any(token in corpus for token in {"game 7", "game 6", "game 5", "game 4"}):
                return "What Game 5 Means for the Series" if "game 5" in corpus else "What This Game Means for the Series"
            if any(token in corpus for token in {"western conference finals", "eastern conference finals", "finals"}):
                return "What It Means for the Series"
            if any(token in corpus for token in {"playoffs", "playoff"}):
                return "What It Means for the Playoff Race"
            return "What It Means for the Matchup"
        if any(token in corpus for token in {"court", "ruling", "judge", "supreme"}):
            return "Why the Ruling Matters"
        if any(token in corpus for token in {"lawsuit", "charges", "trial", "investigation"}):
            return "Why the Case Matters"
        if any(token in corpus for token in {"release", "launch", "debut", "rollout", "update"}):
            return "Why This Release Matters"
        return "Why This Matters Now"

    def _topic_needs_keyword_upgrade(self, topic: TrendTopic) -> bool:
        raw_tokens = tokenize(topic.keyword)
        if not raw_tokens:
            return True

        if len(raw_tokens) > 1:
            return False

        token = raw_tokens[0]
        if token in self.GENERIC_PRIMARY_KEYWORD_TOKENS:
            return True
        return True

    def _focus_entity_phrase(self, title: str) -> str | None:
        for acronym in re.findall(r"\b[A-Z]{2,6}\b", title or ""):
            if acronym not in {"US", "UK", "EU", "UN"}:
                return acronym

        for match in re.finditer(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", title or ""):
            candidate = match.group(0).strip()
            if candidate in self.ENTITY_SKIP_PHRASES:
                continue
            if candidate in {"Gov", "Governor", "President", "Prime Minister"}:
                continue
            return candidate

        return None

    def _focus_issue_phrase(self, topic: TrendTopic, research: ResearchPacket, article_type: str) -> str | None:
        corpus_tokens = set(
            tokenize(
                " ".join(
                    [
                        topic.keyword,
                        research.lead,
                        *research.present[:2],
                        *research.past[:1],
                        *research.future[:1],
                        *(source.title for source in research.sources[:3]),
                    ]
                )
            )
        )
        for required_tokens, phrase in self.ISSUE_PHRASE_RULES:
            if required_tokens <= corpus_tokens:
                return phrase
        if article_type == "politics_news":
            return "politics"
        return None

    def _derive_primary_keyword(self, topic: TrendTopic, research: ResearchPacket, article_type: str) -> str:
        cleaned_topic = re.sub(r"\s+", " ", topic.keyword or "").strip()
        if not self._topic_needs_keyword_upgrade(topic):
            return self._compact_primary_keyword(cleaned_topic)

        entity = None
        for source in research.sources[:3]:
            entity = self._focus_entity_phrase(source.title)
            if entity:
                break

        issue = self._focus_issue_phrase(topic, research, article_type)
        if entity and issue:
            return self._compact_primary_keyword(f"{entity} {issue}".strip())
        if issue:
            return self._compact_primary_keyword(issue)
        if entity:
            return self._compact_primary_keyword(entity)
        return self._compact_primary_keyword(cleaned_topic)

    def _compact_primary_keyword(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value or "").strip()
        if not cleaned:
            return ""

        words = cleaned.split()
        content_words = [
            word
            for word in words
            if re.sub(r"[^a-z0-9-]", "", word.casefold())
            and re.sub(r"[^a-z0-9-]", "", word.casefold()) not in self.PRIMARY_KEYWORD_STOP_TOKENS
        ]
        if len(content_words) <= 4:
            return cleaned

        compact_words: list[str] = []
        content_count = 0
        for word in words:
            normalized = re.sub(r"[^a-z0-9-]", "", word.casefold())
            if not normalized:
                continue
            if normalized in self.PRIMARY_KEYWORD_STOP_TOKENS and not compact_words:
                continue
            compact_words.append(word)
            if normalized not in self.PRIMARY_KEYWORD_STOP_TOKENS:
                content_count += 1
            if content_count >= 4:
                break

        compact = " ".join(compact_words).strip(" ,;:-")
        return compact or cleaned

    def build(
        self,
        topic: TrendTopic,
        research: ResearchPacket,
        memory: EditorialMemoryPacket | None = None,
    ) -> ContentPlan:
        audience = "tech and business readers"
        is_politics_topic = self._is_politics_topic(topic, research)
        if is_politics_topic or any(token in tokenize(topic.keyword) for token in {"cricket", "match", "ipl", "football"}):
            audience = "general news readers"

        article_type = self._article_type(topic, research)
        sections = [
            "Main development",
            self._news_implication_heading(topic, research, article_type),
            "Sources",
        ]
        if article_type == "politics_news":
            sections = [
                "The political shift",
                self._politics_implication_heading(topic, research),
                "Sources",
            ]
        keywords = extract_keywords(topic.keyword, research.sources)
        primary_keyword = self._derive_primary_keyword(topic, research, article_type)
        if primary_keyword and primary_keyword not in keywords:
            keywords = [primary_keyword, *keywords][:6]
        brief = (
            f"Write for {audience}. Treat this as a {article_type.replace('_', ' ')} for a US news audience. "
            f"Open with the newest confirmed development about {topic.keyword}, follow with a nut graf on why it matters now, "
            "then connect the present development to relevant background and the next likely turn in the story. "
            "Keep the structure tight and news-like: a few strong H2 sections, not a stack of explainer subheads."
        )
        brief += (
            f" The editorial implication angle for this story is '{sections[1]}'. Treat the section list as internal guidance only and rewrite it into story-specific newsroom headings rather than printing the labels verbatim."
        )
        if article_type == "politics_news":
            brief += (
                " For politics pieces, keep the format flat: after the lede and nut graf, use one H2 for the main political shift or result, "
                f"then one H2 for what it means for the party, race, or governing stakes. The source-derived implication angle for this story is '{sections[1]}'. "
                "Write those H2s as story-specific newsroom headings in the style of 'A major political shift in Texas' and 'What this means for the Republican Party'. Do not create extra subtopics, nested explainers, or H3 headings."
            )
        else:
            brief += (
                " For standard news pieces, use no more than two substantive H2 sections before Sources. Fold forward-looking context into the second section instead of adding a separate 'what comes next' heading."
            )
        if article_type == "sports_news":
            brief += (
                " For sports stories, make the second H2 strong and specific to the game, series, or playoff stakes. Avoid generic labels like 'Why This Matters Now'. Prefer headings in the style of 'What Game 5 Means for the Series' or 'Why This Result Shifts the West Finals'."
            )
        memory_notes = memory.guidance if memory is not None else []
        if memory_notes:
            brief += " Reuse the proven editorial patterns from memory where they still fit the current topic."
        if research.present:
            brief += f" Current lead fact: {research.present[0]}"
        if research.past:
            brief += f" Background to connect: {research.past[0]}"
        if research.future:
            brief += f" Next step to watch: {research.future[0]}"

        return ContentPlan(
            audience=audience,
            tone="professional, factual, attribution-first newsroom style",
            primary_keyword=primary_keyword,
            article_type=article_type,
            secondary_keywords=keywords,
            sections=sections,
            brief=brief,
            memory_notes=memory_notes,
        )


class PlanningAgent(BaseAgent):
    stage_name = "planner"

    def __init__(self, service: ContentPlanningService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None or context.run.research is None:
            raise RuntimeError("Research packet is required before planning")

        context.run.plan = self.service.build(context.run.selected_topic, context.run.research, context.run.memory)
        self.logger.info(context.run, "Created content plan")