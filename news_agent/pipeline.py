from __future__ import annotations

from typing import TypedDict
import uuid

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
except ImportError:
    END = None
    InMemorySaver = None
    START = None
    StateGraph = None

from .core import AppConfig, InMemoryStageQueue, PipelineLogger
from .models import PipelineRun
from .services import (
    AgentContext,
    AEOAgent,
    AEOService,
    # ── REMOVED: AdvancedInternalLinkingService ──
    # Replaced by IndexingService + RetrievalService
    IndexingService,
    RetrievalService,
    VectorStore,
    create_vector_store,
    # ── UNCHANGED ──
    AnchorInjectorService,
    BlogGenerationService,
    ContentPlanningService,
    EditorialMemoryService,
    ImageAgent,
    ImageEnrichmentService,
    InternalLinkAgent,
    MemoryAgent,
    PlanningAgent,
    PublisherAgent,
    PublisherService,
    ResearchAgent,
    ResearchService,
    ReviewAgent,
    SelectorAgent,
    TopicIntelligenceService,
    TrendAcquisitionService,
    TrendAgent,
    TriggerAgent,
    TriggerService,
    ValidationService,
    WritingAgent,
)

# ─────────────────────────────────────────────────────────────────────────
# NEW: SerpAPI usage tracking
# Import the contextvars bind/unbind helpers so we can attribute every
# SerpAPI call to the agent stage that issued it.
# ─────────────────────────────────────────────────────────────────────────
from .services.serpapi_usage import bind as _bind_serpapi, unbind as _unbind_serpapi


STAGE_SEQUENCE = [
    "trigger",
    "trends",
    "selector",
    "research",
    "memory",
    "planner",
    "generator",
    "aeo",
    "internal_links",
    "validator",
    "image",
    "publisher",
]


class PipelineState(TypedDict):
    trigger_source: str
    seed_topics: list[str] | None
    run: PipelineRun | None


class ContentPipeline:
    def __init__(
        self,
        config: AppConfig,
        trigger_service: TriggerService | None = None,
        trend_service: TrendAcquisitionService | None = None,
        topic_service: TopicIntelligenceService | None = None,
        research_service: ResearchService | None = None,
        editorial_memory_service: EditorialMemoryService | None = None,
        planner_service: ContentPlanningService | None = None,
        generator_service: BlogGenerationService | None = None,
        # ── CHANGED: internal_link_service replaced by vector_store + indexing + retrieval ──
        vector_store: VectorStore | None = None,
        indexing_service: IndexingService | None = None,
        retrieval_service: RetrievalService | None = None,
        anchor_injector_service: AnchorInjectorService | None = None,
        # ── END CHANGES ──
        validation_service: ValidationService | None = None,
        image_service: ImageEnrichmentService | None = None,
        publisher_service: PublisherService | None = None,
    ) -> None:
        self.config = config
        self.logger = PipelineLogger()
        self.editorial_memory_service = editorial_memory_service or EditorialMemoryService(config)
        self.publisher_service = publisher_service or PublisherService(config)
        self.trigger_agent = TriggerAgent(trigger_service or TriggerService(config, self.logger))
        self.trend_agent = TrendAgent(
            trend_service or TrendAcquisitionService(config),
            self.publisher_service,
            self.logger,
            config,
        )
        self.selector_agent = SelectorAgent(
            topic_service or TopicIntelligenceService(),
            self.publisher_service,
            self.logger,
            config,
        )
        self.research_agent = ResearchAgent(
            research_service or ResearchService(config),
            self.publisher_service,
            self.logger,
        )
        self.memory_agent = MemoryAgent(self.editorial_memory_service, self.logger)
        self.planning_agent = PlanningAgent(planner_service or ContentPlanningService(config), self.logger)
        self.writing_agent = WritingAgent(generator_service or BlogGenerationService(config), self.logger)
        self.aeo_agent = AEOAgent(AEOService(config), self.logger)
        print("[PIPELINE] AEOAgent registered at stage 'aeo' (position 7/11)")
        try:
            import ai_optimization_v2
            print(f"[PIPELINE] ai_optimization_v2 resolved OK (version={getattr(ai_optimization_v2, '__version__', 'unknown')})")
        except ImportError:
            print("[PIPELINE] WARNING ai_optimization_v2 NOT importable — AEO stage will silently skip")

        # ═══════════════════════════════════════════════════════════════
        # CHANGED: Internal Linking wiring (v2)
        # ═══════════════════════════════════════════════════════════════
        #
        # Old (v1):
        #   self.internal_link_agent = InternalLinkAgent(
        #       internal_link_service or AdvancedInternalLinkingService(config),
        #       anchor_injector_service or AnchorInjectorService(config),
        #       self.logger,
        #   )
        #
        # New (v2):
        #   1. VectorStore — shared between indexing and retrieval
        #   2. IndexingService — background only (cron/webhook)
        #   3. RetrievalService — used during pipeline execution
        #   4. InternalLinkAgent — takes retrieval_service instead of internal_link_service
        #
        # ── CHANGED: pass CloudSync to create_vector_store so we get the
        # SyncedJSONVectorStore (B2-backed) variant when B2 is configured.
        # This is a soft import — if app.cloud_sync isn't available (e.g.
        # running the pipeline outside the scheduler app), we fall back
        # to plain JSONVectorStore. ──
        if vector_store is None and getattr(config, "vector_store_b2_sync_enabled", True):
            try:
                from app.cloud_sync import CloudSync
                cloud_sync = CloudSync.instance()
                self.vector_store = create_vector_store(config, cloud_sync=cloud_sync)
            except Exception:
                self.vector_store = create_vector_store(config)
        else:
            self.vector_store = vector_store or create_vector_store(config)

        self.indexing_service = indexing_service or IndexingService(config, self.vector_store)
        self.retrieval_service = retrieval_service or RetrievalService(config, self.vector_store)

        anchor_injector = anchor_injector_service or AnchorInjectorService(config)
        self.internal_link_agent = InternalLinkAgent(
            retrieval_service=self.retrieval_service,
            injector=anchor_injector,
            logger=self.logger,
            tenant_id=getattr(config, "tenant_id", ""),
        )
        # ═══════════════════════════════════════════════════════════════

        self.review_agent = ReviewAgent(validation_service or ValidationService(config), self.logger)
        self.image_agent = ImageAgent(image_service or ImageEnrichmentService(config), self.logger)
        self.publisher_agent = PublisherAgent(self.publisher_service, self.editorial_memory_service, self.logger)
        self.agents = {
            self.trigger_agent.stage_name: self.trigger_agent,
            self.trend_agent.stage_name: self.trend_agent,
            self.selector_agent.stage_name: self.selector_agent,
            self.research_agent.stage_name: self.research_agent,
            self.memory_agent.stage_name: self.memory_agent,
            self.planning_agent.stage_name: self.planning_agent,
            self.writing_agent.stage_name: self.writing_agent,
            self.aeo_agent.stage_name: self.aeo_agent,
            self.internal_link_agent.stage_name: self.internal_link_agent,
            self.review_agent.stage_name: self.review_agent,
            self.image_agent.stage_name: self.image_agent,
            self.publisher_agent.stage_name: self.publisher_agent,
        }
        self._graph_app = None
        self._graph_checkpointer = InMemorySaver() if InMemorySaver is not None else None
        self._last_graph_run: PipelineRun | None = None
        self._last_graph_thread_id: str | None = None

    def run(self, trigger_source: str = "manual", seed_topics: list[str] | None = None) -> PipelineRun:
        context = AgentContext(trigger_source=trigger_source, seed_topics=seed_topics)

        try:
            if self.config.orchestrator == "langgraph":
                return self._run_langgraph(context)

            return self._run_queue(context)

        except Exception as exc:
            if context.run is None and self._last_graph_run is not None:
                context.run = self._last_graph_run
            if context.run is not None:
                self.logger.fail(context.run, f"Pipeline failed: {exc}")
                self.publisher_service.save_run_cache(context.run)
            raise

    def _run_queue(self, context: AgentContext) -> PipelineRun:
        stage_queue = InMemoryStageQueue(STAGE_SEQUENCE)

        while stage_queue:
            stage_event = stage_queue.dequeue()
            # CHANGED: wrap execute() so SerpAPI calls get attributed to the stage
            self._execute_with_serpapi_tracking(self.agents[stage_event.name], context)

        if context.run is None:
            raise RuntimeError("Pipeline finished without a run context")
        # NEW: log per-stage SerpAPI breakdown at the end of the run
        self._log_serpapi_summary(context.run)
        return context.run

    def _run_langgraph(self, context: AgentContext) -> PipelineRun:
        if StateGraph is None or START is None or END is None or self._graph_checkpointer is None:
            raise RuntimeError("LangGraph orchestration is not available. Install the 'langgraph' package.")

        self._last_graph_run = None
        thread_id = f"pipeline-{uuid.uuid4()}"
        self._last_graph_thread_id = thread_id
        initial_state: PipelineState = {
            "trigger_source": context.trigger_source,
            "seed_topics": context.seed_topics,
            "run": context.run,
        }
        final_state = self._get_graph_app().invoke(initial_state, config={"configurable": {"thread_id": thread_id}})
        context.run = final_state.get("run")

        if context.run is None:
            raise RuntimeError("LangGraph pipeline finished without a run context")
        # NEW: log per-stage SerpAPI breakdown at the end of the run
        self._log_serpapi_summary(context.run)
        return context.run

    def _get_graph_app(self):
        if self._graph_app is None:
            self._graph_app = self._build_langgraph_app()
        return self._graph_app

    def _build_langgraph_app(self):
        builder = StateGraph(PipelineState)
        for stage_name in STAGE_SEQUENCE:
            builder.add_node(stage_name, self._graph_node(stage_name))

        builder.add_edge(START, STAGE_SEQUENCE[0])
        for current_stage, next_stage in zip(STAGE_SEQUENCE, STAGE_SEQUENCE[1:]):
            builder.add_edge(current_stage, next_stage)
        builder.add_edge(STAGE_SEQUENCE[-1], END)
        return builder.compile(checkpointer=self._graph_checkpointer)

    def _graph_node(self, stage_name: str):
        agent = self.agents[stage_name]

        def run_stage(state: PipelineState) -> dict[str, PipelineRun | None]:
            context = AgentContext(
                trigger_source=state["trigger_source"],
                seed_topics=state.get("seed_topics"),
                run=state.get("run"),
            )
            # CHANGED: wrap execute() so SerpAPI calls get attributed to the stage
            self._execute_with_serpapi_tracking(agent, context)
            self._last_graph_run = context.run
            return {"run": context.run}

        return run_stage

    # ═══════════════════════════════════════════════════════════════════
    # NEW: SerpAPI usage tracking helpers
    # ═══════════════════════════════════════════════════════════════════
    def _execute_with_serpapi_tracking(self, agent, context: AgentContext) -> None:
        """Run a single agent with the (run, stage) bound so that any
        SerpAPI call made by the agent (or any of its helpers) is
        attributed to `agent.stage_name` on `context.run.serpapi_by_stage`.

        The `trigger` stage has no `PipelineRun` yet (it creates one), so
        we skip the binding for it — it makes 0 SerpAPI calls anyway.
        """
        if context.run is None:
            agent.execute(context)
            return

        token = _bind_serpapi(context.run, agent.stage_name)
        try:
            agent.execute(context)
        finally:
            _unbind_serpapi(token)

        stats = context.run.serpapi_by_stage.get(agent.stage_name)
        if stats is not None and (stats.calls or stats.errors):
            self.logger.info(
                context.run,
                f"[{agent.stage_name}] SerpAPI: "
                f"calls={stats.calls} errors={stats.errors} "
                f"engines={dict(stats.by_engine)}",
            )

    def _log_serpapi_summary(self, run: PipelineRun) -> None:
        """Print a per-stage SerpAPI usage breakdown at the end of the run."""
        if not run.serpapi_by_stage:
            return
        total = sum(s.calls for s in run.serpapi_by_stage.values())
        total_err = sum(s.errors for s in run.serpapi_by_stage.values())
        self.logger.info(
            run,
            f"=== SerpAPI usage summary: {total} calls, {total_err} errors ===",
        )
        for stage in STAGE_SEQUENCE:
            stats = run.serpapi_by_stage.get(stage)
            if stats is None or not (stats.calls or stats.errors):
                continue
            self.logger.info(
                run,
                f"  {stage:15s}  calls={stats.calls:3d}  "
                f"errors={stats.errors}  engines={dict(stats.by_engine)}",
            )