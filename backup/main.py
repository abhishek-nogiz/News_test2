from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import json

from config import AppConfig
from news_agent import ContentPipeline
from news_agent.services import PublisherService
from news_agent.services.helpers import serialize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the news agent content pipeline")
    parser.add_argument("--trigger", default="manual", help="Trigger source label")
    parser.add_argument("--country", default=None, help="Country code for trend acquisition")
    parser.add_argument(
        "--orchestrator",
        choices=["queue", "langgraph"],
        default=None,
        help="Execution engine for the pipeline",
    )
    parser.add_argument(
        "--trend-window",
        choices=["4h", "24h", "48h", "7d"],
        default=None,
        help="Trend window for Google Trends Trending Now",
    )
    parser.add_argument("--groq-model", default=None, help="Override the Groq model for article generation")
    parser.add_argument(
        "--groq-fallback-model",
        default=None,
        help="Override the fallback Groq model used when the primary draft is weak or fails",
    )
    parser.add_argument("--max-topics", type=int, default=None, help="Maximum number of trends to fetch")
    parser.add_argument(
        "--research-results",
        type=int,
        default=None,
        help="Maximum number of news results to include in the research packet",
    )
    parser.add_argument(
        "--topic-category",
        default=None,
        help="Optional topical filter for trend selection and news research, e.g. politics, business, tech, sports",
    )
    parser.add_argument(
        "--wordpress-sync",
        action="store_true",
        help="Create a post in WordPress through GraphQL after local publishing completes",
    )
    parser.add_argument(
        "--wordpress-check-auth",
        action="store_true",
        help="Check which WordPress user WPGraphQL sees for the configured credentials without creating a post",
    )
    parser.add_argument(
        "--wordpress-status",
        choices=["draft", "publish", "auto"],
        default=None,
        help="Remote WordPress post status when sync is enabled; use auto to follow validation output",
    )
    parser.add_argument(
        "--duplicate-lookback-hours",
        type=int,
        default=None,
        help="Skip topics already published within this many hours",
    )
    parser.add_argument("--storage-root", default=None, help="Output directory for generated artifacts")
    parser.add_argument("--mock", action="store_true", help="Run without external API calls")
    parser.add_argument(
        "--seed-topic",
        action="append",
        default=[],
        help="Provide one or more seed topics instead of pulling live trends",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = AppConfig.from_env()
    config = replace(
        config,
        country=args.country or config.country,
        orchestrator=args.orchestrator or config.orchestrator,
        trend_window=args.trend_window or config.trend_window,
        groq_model=args.groq_model or config.groq_model,
        groq_fallback_model=args.groq_fallback_model or config.groq_fallback_model,
        max_topics=args.max_topics or config.max_topics,
        research_results=args.research_results or config.research_results,
        topic_category=args.topic_category or config.topic_category,
        wordpress_sync_enabled=args.wordpress_sync or config.wordpress_sync_enabled,
        wordpress_status=args.wordpress_status or config.wordpress_status,
        duplicate_lookback_hours=args.duplicate_lookback_hours or config.duplicate_lookback_hours,
        storage_root=Path(args.storage_root) if args.storage_root else config.storage_root,
        mock_mode=args.mock or config.mock_mode,
    )

    if args.wordpress_check_auth:
        auth = PublisherService(config).check_wordpress_auth()
        print(json.dumps({"wordpress_auth": serialize(auth)}, indent=2))
        return 0 if auth.authenticated else 1

    pipeline = ContentPipeline(config)
    run = pipeline.run(trigger_source=args.trigger, seed_topics=args.seed_topic or None)

    summary = {
        "run_id": run.run_id,
        "orchestrator": config.orchestrator,
        "status": run.status,
        "selected_topic": run.selected_topic.keyword if run.selected_topic else None,
        "topic_category": config.topic_category,
        "publish": run.validation.publish if run.validation else False,
        "wordpress_sync_enabled": config.wordpress_sync_enabled,
        "wordpress_synced": bool(run.published and run.published.wordpress_sync and run.published.wordpress_sync.synced),
        "wordpress_post_id": run.published.wordpress_sync.post_id if run.published and run.published.wordpress_sync else None,
        "wordpress_remote_status": run.published.wordpress_sync.remote_status if run.published and run.published.wordpress_sync else None,
        "wordpress_viewer_username": (
            run.published.wordpress_sync.auth.viewer_username
            if run.published and run.published.wordpress_sync and run.published.wordpress_sync.auth
            else None
        ),
        "quality_score": run.validation.quality_score if run.validation else None,
        "seo_score": run.validation.seo_score if run.validation else None,
        "grounding_score": run.validation.grounding_score if run.validation else None,
        "issues": run.validation.issues if run.validation else [],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())