from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
import uuid
from urllib.parse import urlsplit

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
except ImportError:
    END = None
    InMemorySaver = None
    START = None
    StateGraph = None

from config import AppConfig
from news_agent.models import ResearchPacket, TrendTopic
from news_agent.services.helpers import slugify
from news_agent.services.publisher.service import PublisherService
from news_agent.services.research.service import ResearchService
from news_agent.services.selector.service import TopicIntelligenceService
from news_agent.services.trends.service import TrendAcquisitionService

from .editorial import EditorialTriageService
from .fact_spine import FactSpineBuilder
from .models import CandidateDossier, EditorialDecision, EvidenceLedgerEntry, FactSpine, NewsroomDraft, NewsroomPlan
from .planner import NewsroomPlanningService
from .research_router import NewsroomResearchRouter
from .validator import NewsroomValidationService
from .writer import NewsroomWritingService


class NewsroomRunState(TypedDict, total=False):
    seed_topics: list[str] | None
    country: str | None
    candidates: list[TrendTopic]
    selected_topic: TrendTopic
    selection_rank: int
    skipped_recent_topics: int
    duplicate_filter_exhausted: bool
    research: ResearchPacket
    filtered_source_count: int
    source_filter_notes: list[str]
    decision: EditorialDecision
    tavily_enriched: bool
    enriched_source_count: int
    fact_spine: FactSpine
    evidence_ledger: list[EvidenceLedgerEntry]
    dossier: CandidateDossier


class NewsroomDraftState(TypedDict, total=False):
    dossier: CandidateDossier
    plan: NewsroomPlan
    initial_draft: NewsroomDraft
    repaired_draft: NewsroomDraft
    draft: NewsroomDraft


class NewsroomWorkflow:
    GENERIC_TOPIC_TERMS = {
        "election",
        "elections",
        "primary",
        "primary election",
        "runoff",
        "vote",
        "voting",
        "results",
        "race",
        "campaign",
        "politics",
        "news",
        "latest",
        "update",
        "updates",
        "live",
    }

    def __init__(self, config: AppConfig, *, research_router: NewsroomResearchRouter | None = None) -> None:
        self.config = config
        self.trend_service = TrendAcquisitionService(config)
        self.topic_service = TopicIntelligenceService()
        self.publisher_service = PublisherService(config)
        self.research_service = ResearchService(config)
        self.research_router = research_router or NewsroomResearchRouter(config)
        self.triage_service = EditorialTriageService()
        self.fact_spine_builder = FactSpineBuilder()
        self.planning_service = NewsroomPlanningService()
        self.writing_service = NewsroomWritingService(config)
        self.validation_service = NewsroomValidationService(config)
        self._graph_checkpointer = InMemorySaver() if InMemorySaver is not None else None
        self._run_graph_app = None
        self._draft_graph_app = None

    def discover_topics(self, seed_topics: list[str] | None = None, country: str | None = None) -> list[TrendTopic]:
        effective_country = country or self.config.country
        if seed_topics:
            trends = self.trend_service.from_seed_topics(seed_topics, self.config.max_topics)
        else:
            trends = self.trend_service.fetch(effective_country, self.config.max_topics)

        ranked = self.topic_service.rank(trends)
        return self.topic_service.filter_by_category(ranked, self.config.topic_category)

    def run(self, seed_topics: list[str] | None = None, country: str | None = None) -> CandidateDossier:
        if self.config.orchestrator == "langgraph":
            return self._run_langgraph(seed_topics=seed_topics, country=country)
        return self._run_linear(seed_topics=seed_topics, country=country)

    def _run_linear(self, seed_topics: list[str] | None = None, country: str | None = None) -> CandidateDossier:
        candidates = self.discover_topics(seed_topics=seed_topics, country=country)
        if not candidates:
            raise RuntimeError("No candidate topics were available for the newsroom workflow")

        selected_topic, selection_rank, skipped_recent_topics, duplicate_filter_exhausted = self._select_topic(candidates)
        research = self.research_service.research(selected_topic, country or self.config.country)
        research, _reprioritized = self.research_router.prioritize(
            selected_topic,
            research,
            country=country or self.config.country,
            rebuild_packet=self.research_service._build_packet,
        )
        research, filtered_source_count, source_filter_notes = self.research_router.filter_sources(
            selected_topic,
            research,
            country=country or self.config.country,
            rebuild_packet=self.research_service._build_packet,
        )
        decision = self.triage_service.decide(selected_topic, research)
        research, tavily_enriched, enriched_source_count = self.research_router.enrich(
            selected_topic,
            research,
            decision,
            country=country or self.config.country,
            topic_category=self.config.topic_category,
            rebuild_packet=self.research_service._build_packet,
        )
        if tavily_enriched:
            research, extra_filtered_count, extra_filter_notes = self.research_router.filter_sources(
                selected_topic,
                research,
                country=country or self.config.country,
                rebuild_packet=self.research_service._build_packet,
            )
            filtered_source_count += extra_filtered_count
            source_filter_notes.extend(extra_filter_notes)
        if tavily_enriched:
            decision = self.triage_service.decide(selected_topic, research)
        fact_spine = self.fact_spine_builder.build(selected_topic, research, decision)
        evidence_ledger = self._build_evidence_ledger(research)
        return CandidateDossier(
            topic=selected_topic,
            research=research,
            decision=decision,
            fact_spine=fact_spine,
            tavily_enriched=tavily_enriched,
            enriched_source_count=enriched_source_count,
            research_source_count=len(research.sources),
            filtered_source_count=filtered_source_count,
            source_filter_notes=source_filter_notes,
            evidence_ledger=evidence_ledger,
            selection_rank=selection_rank,
            skipped_recent_topics=skipped_recent_topics,
            duplicate_filter_exhausted=duplicate_filter_exhausted,
            topic_discovery_engine=self._topic_discovery_engine(selected_topic.source),
            research_engine=self._research_engine(),
        )

    def summarize(self, dossier: CandidateDossier) -> dict:
        return {
            "topic": dossier.topic.keyword,
            "topic_source": dossier.topic.source,
            "topic_discovery_engine": dossier.topic_discovery_engine,
            "research_engine": dossier.research_engine,
            "acquisition_order": [dossier.topic_discovery_engine, dossier.research_engine],
            "selection_rank": dossier.selection_rank,
            "skipped_recent_topics": dossier.skipped_recent_topics,
            "duplicate_filter_exhausted": dossier.duplicate_filter_exhausted,
            "article_mode": dossier.decision.article_mode,
            "should_write": dossier.decision.should_write,
            "primary_angle": self._trim(dossier.decision.primary_angle),
            "reasoning": dossier.decision.reasoning,
            "confidence": dossier.decision.confidence,
            "evidence_strength": dossier.decision.evidence_strength,
            "missing_elements": dossier.decision.missing_elements,
            "core_event": self._trim(dossier.fact_spine.core_event),
            "why_it_matters": self._trim(dossier.fact_spine.why_it_matters),
            "timeline": [self._trim(item, limit=160) for item in dossier.fact_spine.timeline[:5]],
            "key_facts": [self._trim(item, limit=180) for item in dossier.fact_spine.key_facts[:5]],
            "official_points": [self._trim(item, limit=180) for item in dossier.fact_spine.official_points[:3]],
            "source_urls": dossier.fact_spine.source_urls,
            "tavily_enriched": dossier.tavily_enriched,
            "enriched_source_count": dossier.enriched_source_count,
            "research_source_count": dossier.research_source_count,
            "filtered_source_count": dossier.filtered_source_count,
            "evidence_count": len(dossier.evidence_ledger),
            "source_filter_notes": dossier.source_filter_notes[:5],
            "source_diagnostics": self._source_diagnostics(dossier),
        }

    def plan(self, dossier: CandidateDossier) -> NewsroomPlan:
        return self.planning_service.build(dossier)

    def draft(self, dossier: CandidateDossier) -> NewsroomDraft:
        if self.config.orchestrator == "langgraph":
            return self._draft_langgraph(dossier)
        return self._draft_linear(dossier)

    def _draft_linear(self, dossier: CandidateDossier) -> NewsroomDraft:
        plan = self.plan(dossier)
        draft = self.writing_service.draft(dossier, plan)
        draft.validation = self.validation_service.validate(draft, dossier, plan)
        draft.publish_ready = draft.validation.publish
        if not draft.publish_ready and self.writing_service.can_repair():
            repaired_draft = self.writing_service.repair(dossier, plan, draft)
            if repaired_draft is not draft:
                repaired_draft.validation = self.validation_service.validate(repaired_draft, dossier, plan)
                repaired_draft.publish_ready = repaired_draft.validation.publish
                draft = self._prefer_draft(draft, repaired_draft)
        return draft

    def _run_langgraph(self, seed_topics: list[str] | None = None, country: str | None = None) -> CandidateDossier:
        if StateGraph is None or START is None or END is None or self._graph_checkpointer is None:
            raise RuntimeError("LangGraph orchestration is not available. Install the 'langgraph' package.")

        initial_state: NewsroomRunState = {
            "seed_topics": seed_topics,
            "country": country or self.config.country,
        }
        final_state = self._get_run_graph_app().invoke(
            initial_state,
            config={"configurable": {"thread_id": f"newsroom-run-{uuid.uuid4()}"}},
        )
        dossier = final_state.get("dossier")
        if dossier is None:
            raise RuntimeError("LangGraph newsroom run finished without a dossier")
        return dossier

    def _draft_langgraph(self, dossier: CandidateDossier) -> NewsroomDraft:
        if StateGraph is None or START is None or END is None or self._graph_checkpointer is None:
            raise RuntimeError("LangGraph orchestration is not available. Install the 'langgraph' package.")

        initial_state: NewsroomDraftState = {"dossier": dossier}
        final_state = self._get_draft_graph_app().invoke(
            initial_state,
            config={"configurable": {"thread_id": f"newsroom-draft-{uuid.uuid4()}"}},
        )
        draft = final_state.get("draft")
        if draft is None:
            raise RuntimeError("LangGraph newsroom draft finished without a draft")
        return draft

    def _get_run_graph_app(self):
        if self._run_graph_app is None:
            self._run_graph_app = self._build_run_graph_app()
        return self._run_graph_app

    def _get_draft_graph_app(self):
        if self._draft_graph_app is None:
            self._draft_graph_app = self._build_draft_graph_app()
        return self._draft_graph_app

    def _build_run_graph_app(self):
        builder = StateGraph(NewsroomRunState)
        builder.add_node("discover_topics", self._discover_topics_node)
        builder.add_node("select_topic", self._select_topic_node)
        builder.add_node("research_topic", self._research_topic_node)
        builder.add_node("prioritize_research", self._prioritize_research_node)
        builder.add_node("filter_sources", self._filter_sources_node)
        builder.add_node("triage_topic", self._triage_topic_node)
        builder.add_node("enrich_research", self._enrich_research_node)
        builder.add_node("build_fact_spine", self._build_fact_spine_node)
        builder.add_node("assemble_dossier", self._assemble_dossier_node)

        builder.add_edge(START, "discover_topics")
        builder.add_edge("discover_topics", "select_topic")
        builder.add_edge("select_topic", "research_topic")
        builder.add_edge("research_topic", "prioritize_research")
        builder.add_edge("prioritize_research", "filter_sources")
        builder.add_edge("filter_sources", "triage_topic")
        builder.add_edge("triage_topic", "enrich_research")
        builder.add_edge("enrich_research", "build_fact_spine")
        builder.add_edge("build_fact_spine", "assemble_dossier")
        builder.add_edge("assemble_dossier", END)
        return builder.compile(checkpointer=self._graph_checkpointer)

    def _build_draft_graph_app(self):
        builder = StateGraph(NewsroomDraftState)
        builder.add_node("plan_draft", self._plan_draft_node)
        builder.add_node("write_draft", self._write_draft_node)
        builder.add_node("validate_initial_draft", self._validate_initial_draft_node)
        builder.add_node("repair_draft", self._repair_draft_node)
        builder.add_node("validate_repaired_draft", self._validate_repaired_draft_node)
        builder.add_node("finalize_draft", self._finalize_draft_node)

        builder.add_edge(START, "plan_draft")
        builder.add_edge("plan_draft", "write_draft")
        builder.add_edge("write_draft", "validate_initial_draft")
        builder.add_edge("validate_initial_draft", "repair_draft")
        builder.add_edge("repair_draft", "validate_repaired_draft")
        builder.add_edge("validate_repaired_draft", "finalize_draft")
        builder.add_edge("finalize_draft", END)
        return builder.compile(checkpointer=self._graph_checkpointer)

    def _discover_topics_node(self, state: NewsroomRunState) -> dict[str, object]:
        country = state.get("country") or self.config.country
        candidates = self.discover_topics(seed_topics=state.get("seed_topics"), country=country)
        if not candidates:
            raise RuntimeError("No candidate topics were available for the newsroom workflow")
        return {"country": country, "candidates": candidates}

    def _select_topic_node(self, state: NewsroomRunState) -> dict[str, object]:
        selected_topic, selection_rank, skipped_recent_topics, duplicate_filter_exhausted = self._select_topic(state["candidates"])
        return {
            "selected_topic": selected_topic,
            "selection_rank": selection_rank,
            "skipped_recent_topics": skipped_recent_topics,
            "duplicate_filter_exhausted": duplicate_filter_exhausted,
        }

    def _research_topic_node(self, state: NewsroomRunState) -> dict[str, object]:
        research = self.research_service.research(state["selected_topic"], state["country"] or self.config.country)
        return {"research": research}

    def _prioritize_research_node(self, state: NewsroomRunState) -> dict[str, object]:
        research, _reprioritized = self.research_router.prioritize(
            state["selected_topic"],
            state["research"],
            country=state["country"] or self.config.country,
            rebuild_packet=self.research_service._build_packet,
        )
        return {"research": research}

    def _filter_sources_node(self, state: NewsroomRunState) -> dict[str, object]:
        research, filtered_source_count, source_filter_notes = self.research_router.filter_sources(
            state["selected_topic"],
            state["research"],
            country=state["country"] or self.config.country,
            rebuild_packet=self.research_service._build_packet,
        )
        return {
            "research": research,
            "filtered_source_count": filtered_source_count,
            "source_filter_notes": source_filter_notes,
        }

    def _triage_topic_node(self, state: NewsroomRunState) -> dict[str, object]:
        decision = self.triage_service.decide(state["selected_topic"], state["research"])
        return {"decision": decision}

    def _enrich_research_node(self, state: NewsroomRunState) -> dict[str, object]:
        research, tavily_enriched, enriched_source_count = self.research_router.enrich(
            state["selected_topic"],
            state["research"],
            state["decision"],
            country=state["country"] or self.config.country,
            topic_category=self.config.topic_category,
            rebuild_packet=self.research_service._build_packet,
        )
        filtered_source_count = int(state.get("filtered_source_count", 0))
        source_filter_notes = list(state.get("source_filter_notes", []))
        if tavily_enriched:
            research, extra_filtered_count, extra_filter_notes = self.research_router.filter_sources(
                state["selected_topic"],
                research,
                country=state["country"] or self.config.country,
                rebuild_packet=self.research_service._build_packet,
            )
            filtered_source_count += extra_filtered_count
            source_filter_notes.extend(extra_filter_notes)
        decision = self.triage_service.decide(state["selected_topic"], research) if tavily_enriched else state["decision"]
        return {
            "research": research,
            "tavily_enriched": tavily_enriched,
            "enriched_source_count": enriched_source_count,
            "filtered_source_count": filtered_source_count,
            "source_filter_notes": source_filter_notes,
            "decision": decision,
        }

    def _build_fact_spine_node(self, state: NewsroomRunState) -> dict[str, object]:
        fact_spine = self.fact_spine_builder.build(state["selected_topic"], state["research"], state["decision"])
        evidence_ledger = self._build_evidence_ledger(state["research"])
        return {"fact_spine": fact_spine, "evidence_ledger": evidence_ledger}

    def _assemble_dossier_node(self, state: NewsroomRunState) -> dict[str, object]:
        dossier = CandidateDossier(
            topic=state["selected_topic"],
            research=state["research"],
            decision=state["decision"],
            fact_spine=state["fact_spine"],
            tavily_enriched=bool(state.get("tavily_enriched", False)),
            enriched_source_count=int(state.get("enriched_source_count", 0)),
            research_source_count=len(state["research"].sources),
            filtered_source_count=int(state.get("filtered_source_count", 0)),
            source_filter_notes=list(state.get("source_filter_notes", [])),
            evidence_ledger=list(state.get("evidence_ledger", [])),
            selection_rank=int(state.get("selection_rank", 1)),
            skipped_recent_topics=int(state.get("skipped_recent_topics", 0)),
            duplicate_filter_exhausted=bool(state.get("duplicate_filter_exhausted", False)),
            topic_discovery_engine=self._topic_discovery_engine(state["selected_topic"].source),
            research_engine=self._research_engine(),
        )
        return {"dossier": dossier}

    def _build_evidence_ledger(self, research: ResearchPacket) -> list[EvidenceLedgerEntry]:
        source_lookup = {(source.url or "").strip(): source for source in research.sources}
        entries: list[EvidenceLedgerEntry] = []
        seen: set[tuple[str, str]] = set()

        for claim in research.claims:
            source_url = (claim.source_url or "").strip()
            if not source_url:
                continue
            key = (claim.claim.strip(), source_url)
            if key in seen:
                continue
            seen.add(key)
            source = source_lookup.get(source_url)
            snippet = ""
            published_at = ""
            if source is not None:
                raw_snippet = (source.snippet or source.content[:240]).strip()
                snippet = self._clean_evidence_snippet(raw_snippet)
                published_at = source.published_at.strip()
            entries.append(
                EvidenceLedgerEntry(
                    claim=claim.claim.strip(),
                    section=claim.section.strip(),
                    source_url=source_url,
                    source_title=claim.source_title.strip(),
                    source_tier=claim.source_tier.strip(),
                    published_at=published_at,
                    supporting_snippet=snippet[:280],
                    source_domain=urlsplit(source_url).netloc.casefold().removeprefix("www."),
                )
            )
        return entries[:12]

    def _clean_evidence_snippet(self, snippet: str) -> str:
        cleaner = getattr(self.research_service, "_clean_source_text", None)
        if callable(cleaner):
            return cleaner(snippet, preserve_case=True)
        return " ".join((snippet or "").split()).strip()

    def _plan_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        plan = self.plan(state["dossier"])
        return {"plan": plan}

    def _write_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        draft = self.writing_service.draft(state["dossier"], state["plan"])
        return {"initial_draft": draft}

    def _validate_initial_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        draft = state["initial_draft"]
        draft.validation = self.validation_service.validate(draft, state["dossier"], state["plan"])
        draft.publish_ready = draft.validation.publish
        return {"initial_draft": draft}

    def _repair_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        initial_draft = state["initial_draft"]
        repaired_draft = initial_draft
        if not initial_draft.publish_ready and self.writing_service.can_repair():
            repaired_draft = self.writing_service.repair(state["dossier"], state["plan"], initial_draft)
        return {"repaired_draft": repaired_draft}

    def _validate_repaired_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        initial_draft = state["initial_draft"]
        repaired_draft = state.get("repaired_draft") or initial_draft
        if repaired_draft is not initial_draft:
            repaired_draft.validation = self.validation_service.validate(repaired_draft, state["dossier"], state["plan"])
            repaired_draft.publish_ready = repaired_draft.validation.publish
        return {"repaired_draft": repaired_draft}

    def _finalize_draft_node(self, state: NewsroomDraftState) -> dict[str, object]:
        initial_draft = state["initial_draft"]
        repaired_draft = state.get("repaired_draft") or initial_draft
        draft = self._prefer_draft(initial_draft, repaired_draft) if repaired_draft is not initial_draft else initial_draft
        return {"draft": draft}

    def validate(self, draft: NewsroomDraft, dossier: CandidateDossier) -> NewsroomDraft:
        plan = self.plan(dossier)
        draft.validation = self.validation_service.validate(draft, dossier, plan)
        draft.publish_ready = draft.validation.publish
        return draft

    def should_skip_duplicate_publication(self, dossier: CandidateDossier) -> bool:
        return dossier.duplicate_filter_exhausted and dossier.topic.source != "seed"

    def save_draft(self, draft: NewsroomDraft, dossier: CandidateDossier) -> dict[str, object]:
        if self.should_skip_duplicate_publication(dossier):
            return {
                "skipped_duplicate": True,
                "reason": "duplicate_filter_exhausted",
                "cluster_key": dossier.topic.cluster_key or slugify(dossier.topic.keyword),
            }

        saved_paths: dict[str, object] = self.writing_service.save(draft, dossier)
        if self.config.wordpress_sync_enabled:
            plan = self.plan(dossier)
            artifact = self.publisher_service.publish_newsroom_draft(dossier, draft, plan, saved_paths)
            saved_paths["wordpress_sync"] = {
                "synced": bool(artifact.wordpress_sync and artifact.wordpress_sync.synced),
                "post_id": artifact.wordpress_sync.post_id if artifact.wordpress_sync else None,
                "remote_status": artifact.wordpress_sync.remote_status if artifact.wordpress_sync else None,
                "response_path": artifact.wordpress_sync.response_path if artifact.wordpress_sync else None,
            }
        else:
            self._record_topic_selection(dossier.topic)
        return saved_paths

    def _select_topic(self, candidates: list[TrendTopic]) -> tuple[TrendTopic, int, int, bool]:
        recent_cluster_keys = self.publisher_service.recently_published_cluster_keys()
        skipped_recent_topics = 0
        eligible: list[tuple[int, TrendTopic]] = []
        for index, candidate in enumerate(candidates, start=1):
            if candidate.cluster_key and candidate.cluster_key in recent_cluster_keys:
                skipped_recent_topics += 1
                continue

            eligible.append((index, candidate))

        if eligible:
            specific_candidates = [item for item in eligible if not self._is_generic_topic(item[1].keyword)]
            chosen_index, chosen_candidate = (specific_candidates or eligible)[0]
            return chosen_candidate, chosen_index, skipped_recent_topics, False

        fallback = candidates[0]
        return fallback, 1, skipped_recent_topics, True

    def _is_generic_topic(self, keyword: str) -> bool:
        tokens = [token.strip(" ,.:;!?()[]{}\"'").casefold() for token in (keyword or "").split()]
        tokens = [token for token in tokens if token]
        if not tokens:
            return True
        if len(tokens) <= 3 and all(token in self.GENERIC_TOPIC_TERMS for token in tokens):
            return True
        generic_count = sum(1 for token in tokens if token in self.GENERIC_TOPIC_TERMS)
        specific_tokens = [token for token in tokens if token not in self.GENERIC_TOPIC_TERMS]
        return generic_count >= 2 and not specific_tokens

    def _record_topic_selection(self, topic: TrendTopic) -> None:
        registry_path = self.publisher_service.topic_registry_path
        entries = self._load_topic_registry(registry_path)
        entries.append(
            {
                "run_id": f"newsroom-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
                "keyword": topic.keyword,
                "cluster_key": topic.cluster_key or slugify(topic.keyword),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        registry_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_topic_registry(self, registry_path: Path) -> list[dict]:
        if not registry_path.exists():
            return []
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def _topic_discovery_engine(self, topic_source: str) -> str:
        if topic_source == "google_trends":
            return "serpapi_google_trends"
        if topic_source == "seed":
            return "manual_seed"
        if topic_source == "mock":
            return "mock_trends"
        return topic_source or "unknown"

    def _research_engine(self) -> str:
        if self.config.mock_mode:
            return "mock_research"
        return "serpapi_google_news"

    def _trim(self, value: str, limit: int = 220) -> str:
        normalized = " ".join((value or "").split()).strip()
        if len(normalized) <= limit:
            return normalized
        sentence = normalized.split(". ", 1)[0].strip()
        if sentence and len(sentence) <= limit:
            return sentence.rstrip(".") + "."
        return normalized[: limit - 3].rstrip() + "..."

    def _prefer_draft(self, current: NewsroomDraft, candidate: NewsroomDraft) -> NewsroomDraft:
        current_validation = current.validation
        candidate_validation = candidate.validation
        if current_validation is None:
            return candidate
        if candidate_validation is None:
            return current
        if candidate_validation.publish and not current_validation.publish:
            return candidate
        if current_validation.publish and not candidate_validation.publish:
            return current

        current_score = self._validation_rank(current_validation)
        candidate_score = self._validation_rank(candidate_validation)
        return candidate if candidate_score > current_score else current

    def _validation_rank(self, validation) -> tuple[int, int, int, int]:
        return (
            1 if validation.publish else 0,
            min(validation.editorial_score, validation.structure_score, validation.grounding_score),
            -(len(validation.issues)),
            validation.editorial_score + validation.structure_score + validation.grounding_score,
        )

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