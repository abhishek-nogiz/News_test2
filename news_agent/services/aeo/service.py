from __future__ import annotations

import sys

from ...core.config import AppConfig
from ...core.logger import PipelineLogger
from ..base import AgentContext, BaseAgent


class AEOService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def optimize(self, run) -> dict[str, object]:
        run_id_short = run.run_id[:8]
        topic_keyword = run.selected_topic.keyword if run.selected_topic else "N/A"

        # ── Gate 1: blog data check ──
        if run.selected_topic is None or run.blog is None:
            msg = "Selected topic and blog are required before AEO optimization"
            print(f"[AEO:{run_id_short}] FAIL {msg}")
            raise RuntimeError(msg)

        article_html = (run.blog.article_html or "").strip()
        html_len = len(article_html)
        print(f"[AEO:{run_id_short}] GATE blog.article_html present: size={html_len} chars")

        if not article_html:
            print(f"[AEO:{run_id_short}] SKIP empty_article_html")
            return {
                "applied": False,
                "reason": "empty_article_html",
                "article_html_size": html_len,
            }

        # ── Gate 2: mock mode ──
        print(f"[AEO:{run_id_short}] GATE mock_mode={self.config.mock_mode}")
        if self.config.mock_mode:
            print(f"[AEO:{run_id_short}] SKIP mock_mode")
            return {
                "applied": False,
                "reason": "mock_mode",
            }

        # ── Gate 3: Groq API key ──
        groq_api_key = (self.config.groq_api_key or "").strip()
        has_key = bool(groq_api_key)
        print(f"[AEO:{run_id_short}] GATE groq_api_key present={has_key} (len={len(groq_api_key)})")
        if not groq_api_key:
            print(f"[AEO:{run_id_short}] SKIP missing_groq_api_key")
            return {
                "applied": False,
                "reason": "missing_groq_api_key",
            }

        # ── Gate 4: import ai_optimization_v2 ──
        print(f"[AEO:{run_id_short}] GATE importing ai_optimization_v2...")
        print(f"[AEO:{run_id_short}]   sys.path[0]={sys.path[0] if sys.path else '(empty)'}")
        try:
            from ai_optimization_v2 import AIOptimizationService
            print(f"[AEO:{run_id_short}] IMPORT ok  ai_optimization_v2.AIOptimizationService loaded")
        except Exception as exc:
            print(f"[AEO:{run_id_short}] IMPORT FAILED: {exc}")
            return {
                "applied": False,
                "reason": "import_failed",
                "error": str(exc),
            }

        # ── Gather research sources ──
        sources: list[dict[str, str]] = []
        if run.research is not None:
            raw_sources = getattr(run.research, "sources", []) or []
            for source in raw_sources:
                sources.append(
                    {
                        "title": getattr(source, "title", "") or "",
                        "url": getattr(source, "url", "") or "",
                        "publisher": getattr(source, "publisher", "") or "",
                    }
                )
        print(f"[AEO:{run_id_short}] SOURCES count={len(sources)}")

        # ── Create service ──
        serpapi_key = (self.config.serpapi_key or "").strip()
        groq_model = (self.config.groq_model or "").strip() or "llama-3.3-70b-versatile"
        site_url = (self.config.public_site_base_url or "https://www.peoplenewstime.com").rstrip("/")
        print(f"[AEO:{run_id_short}] CFG groq_model={groq_model} serpapi_present={bool(serpapi_key)} site_url={site_url}")

        service = AIOptimizationService(
            groq_api_key=groq_api_key,
            groq_model=groq_model,
            serpapi_key=serpapi_key,
            site_url=site_url,
            site_name="People News Time",
        )

        # ── Run optimization ──
        print(f"[AEO:{run_id_short}] RUN optimize(topic={topic_keyword!r}, html_size={html_len})")
        import time as _time
        t0 = _time.time()
        result = service.optimize(
            article_html=article_html,
            topic=topic_keyword,
            sources=sources,
            author_name="People News Time Editorial",
            original_date=getattr(run, "started_at", None),
        )
        elapsed = _time.time() - t0

        # ── Gate 5: output check ──
        optimized_html = (result.reorganized_html or "").strip()
        opt_len = len(optimized_html)
        print(f"[AEO:{run_id_short}] DONE in {elapsed:.1f}s  reorganized_html size={opt_len}")

        if not optimized_html:
            print(f"[AEO:{run_id_short}] SKIP empty_output (optimization returned no reorganized_html)")
            return {
                "applied": False,
                "reason": "empty_output",
            }

        # ── Apply ──
        run.blog.article_html = optimized_html
        print(f"[AEO:{run_id_short}] APPLIED to run.blog.article_html (was {html_len}, now {opt_len})")

        # ── Scores & quality breakdown ──
        qr = result.quality_report
        entity_count = result.entities.total_count()
        qa_count = len(result.qa_pairs)
        issues = list(qr.issues) if qr else []
        quality_passed = bool(qr.passed) if qr else False

        entity_breakdown = (
            f"persons={len(result.entities.person)} "
            f"orgs={len(result.entities.organization)} "
            f"teams={len(result.entities.team)} "
            f"locations={len(result.entities.location)} "
            f"events={len(result.entities.event)} "
            f"dates={len(result.entities.date)}"
        )
        print(f"[AEO:{run_id_short}] SCORES:")
        print(f"     entities:  {entity_count} ({entity_breakdown})")
        print(f"     qa_pairs:  {qa_count}")
        print(f"     quality:   {'PASS' if quality_passed else 'FAIL'}")
        print(f"     issues:    {issues if issues else '(none)'}")
        if qr:
            print(f"     details:   entity_count={qr.entity_count} qa_count={qr.qa_count} "
                  f"answer_first_words={qr.answer_first_words} "
                  f"citation_count={qr.citation_count} "
                  f"factual={qr.factual_consistency_score:.2f} "
                  f"retrievability={qr.retrievability_score:.2f}")
        print(f"     time:      {result.processing_time_seconds:.1f}s")

        return {
            "applied": True,
            "reason": "ok",
            "entity_count": entity_count,
            "qa_count": qa_count,
            "issues": issues,
            "quality_passed": quality_passed,
            "processing_time_seconds": result.processing_time_seconds,
            "original_html_size": html_len,
            "optimized_html_size": opt_len,
            "score_retrievability": qr.retrievability_score if qr else 0.0,
            "score_factual": qr.factual_consistency_score if qr else 0.0,
            "lead_word_count": qr.answer_first_words if qr else 0,
        }


class AEOAgent(BaseAgent):
    stage_name = "aeo"

    def __init__(self, service: AEOService, logger: PipelineLogger) -> None:
        self.service = service
        self.logger = logger

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None or context.run.blog is None:
            raise RuntimeError("Selected topic and blog are required before AEO optimization")

        rid = context.run.run_id[:8]
        print(f"[AEO:{rid}] AEOAgent.execute() starting")

        try:
            outcome = self.service.optimize(context.run)
        except Exception as exc:
            print(f"[AEO:{rid}] AEOAgent.execute() EXCEPTION: {exc}")
            self.logger.info(context.run, f"AEO optimization skipped after error: {exc}")
            self.logger.transition(context.run, "aeo_skipped")
            return

        if outcome.get("applied"):
            outcome_d: dict = outcome  # narrow for pyright
            entity_count = outcome_d.get("entity_count", 0)
            qa_count = outcome_d.get("qa_count", 0)
            issues = outcome_d.get("issues", [])
            quality_passed = outcome_d.get("quality_passed", False)
            elapsed = float(outcome_d.get("processing_time_seconds", 0.0))

            # ── Detailed score log ──
            score_detail = (
                f"entities={entity_count} "
                f"qa_pairs={qa_count} "
                f"quality={'PASS' if quality_passed else 'FAIL'} "
                f"issues={len(issues)} "
                f"retriev={outcome_d.get('score_retrievability', 0.0):.2f} "
                f"factual={outcome_d.get('score_factual', 0.0):.2f} "
                f"lead_words={outcome_d.get('lead_word_count', 0)} "
                f"time={elapsed:.1f}s"
            )

            self.logger.info(context.run, f"AEO optimization applied: {score_detail}")
            self.logger.transition(context.run, "aeo_optimized")
            print(f"[AEO:{rid}] AEOAgent.execute() RESULT: {score_detail}")
            if issues:
                for i, issue in enumerate(issues, 1):
                    print(f"[AEO:{rid}]   issue #{i}: {issue}")
            return

        reason = outcome.get("reason", "unknown")
        self.logger.info(
            context.run,
            f"AEO optimization skipped: {reason}",
        )
        self.logger.transition(context.run, "aeo_skipped")
        print(f"[AEO:{rid}] AEOAgent.execute() SKIPPED reason={reason}")