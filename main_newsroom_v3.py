from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import json

from config import AppConfig
from newsroom_v3 import NewsroomV3Workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the newsroom_v3 workflow")
    parser.add_argument("--orchestrator", default=None, help="Workflow orchestrator: queue or langgraph")
    parser.add_argument("--country", default=None, help="Country code for trend acquisition")
    parser.add_argument("--max-topics", type=int, default=None, help="Maximum number of trends to fetch")
    parser.add_argument("--topic-category", default=None, help="Optional topical filter")
    parser.add_argument("--research-results", type=int, default=None, help="Maximum number of research results")
    parser.add_argument("--groq-model", default=None, help="Override the primary model used by the v3 writer")
    parser.add_argument("--groq-fallback-model", default=None, help="Override the fallback model used by the v3 writer")
    parser.add_argument("--storage-root", default=None, help="Output directory for generated artifacts")
    parser.add_argument("--mock", action="store_true", help="Run without external API calls")
    parser.add_argument("--draft", action="store_true", help="Generate and save local v3 draft artifacts")
    parser.add_argument(
        "--wordpress-status",
        choices=["draft", "publish", "auto"],
        default=None,
        help="Remote WordPress post status when sync is enabled",
    )
    parser.add_argument(
        "--wordpress-sync",
        dest="wordpress_sync",
        action="store_true",
        help="Force WordPress sync on for the v3 draft run",
    )
    parser.add_argument(
        "--no-wordpress-sync",
        dest="wordpress_sync",
        action="store_false",
        help="Keep v3 draft exports local only",
    )
    parser.set_defaults(wordpress_sync=None)
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
    wordpress_sync_enabled = config.wordpress_sync_enabled if args.wordpress_sync is None else args.wordpress_sync
    config = replace(
        config,
        country=args.country or config.country,
        orchestrator=args.orchestrator or config.orchestrator,
        max_topics=args.max_topics or config.max_topics,
        research_results=args.research_results or config.research_results,
        topic_category=args.topic_category or config.topic_category,
        groq_model=args.groq_model or config.groq_model,
        groq_fallback_model=args.groq_fallback_model or config.groq_fallback_model,
        wordpress_sync_enabled=wordpress_sync_enabled,
        wordpress_status=args.wordpress_status or config.wordpress_status,
        storage_root=Path(args.storage_root) if args.storage_root else config.storage_root,
        mock_mode=args.mock or config.mock_mode,
    )

    workflow = NewsroomV3Workflow(config)
    try:
        result = workflow.run(seed_topics=args.seed_topic or None, country=config.country, draft=args.draft)
    except RuntimeError as exc:
        if str(exc) == "No candidate topics were available for the newsroom_v3 workflow":
            print(
                json.dumps(
                    {
                        "status": "skipped",
                        "reason": "no_candidate_topics",
                        "country": config.country,
                        "topic_category": config.topic_category,
                        "orchestrator": config.orchestrator,
                    },
                    indent=2,
                )
            )
            return 0
        if str(exc) == "No new candidate topics were available for the newsroom_v3 workflow after duplicate filtering":
            print(
                json.dumps(
                    {
                        "status": "skipped",
                        "reason": "duplicate_filter_exhausted",
                        "country": config.country,
                        "topic_category": config.topic_category,
                        "orchestrator": config.orchestrator,
                    },
                    indent=2,
                )
            )
            return 0
        raise
    print(json.dumps(workflow.summarize(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())