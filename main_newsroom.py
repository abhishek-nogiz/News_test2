from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import json

from config import AppConfig
from news_agent.services import PublisherService
from news_agent.services.helpers import serialize
from newsroom_v2 import NewsroomWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the newsroom workflow prototype")
    parser.add_argument("--orchestrator", default=None, help="Workflow orchestrator: queue or langgraph")
    parser.add_argument("--country", default=None, help="Country code for trend acquisition")
    parser.add_argument("--max-topics", type=int, default=None, help="Maximum number of trends to fetch")
    parser.add_argument("--topic-category", default=None, help="Optional topical filter")
    parser.add_argument("--research-results", type=int, default=None, help="Maximum number of research results")
    parser.add_argument("--groq-model", default=None, help="Override the primary model used by the newsroom writer")
    parser.add_argument("--groq-fallback-model", default=None, help="Override the fallback model used by the newsroom writer")
    parser.add_argument("--storage-root", default=None, help="Output directory for generated artifacts")
    parser.add_argument("--mock", action="store_true", help="Run without external API calls")
    parser.add_argument("--draft", action="store_true", help="Generate and save a newsroom draft artifact")
    parser.add_argument(
        "--wordpress-check-auth",
        action="store_true",
        help="Check which WordPress user WPGraphQL sees for the configured credentials without creating a post",
    )
    parser.add_argument(
        "--wordpress-status",
        choices=["draft", "publish", "auto"],
        default=None,
        help="Remote WordPress post status when sync is enabled; defaults to draft for newsroom draft runs",
    )
    parser.add_argument(
        "--wordpress-sync",
        dest="wordpress_sync",
        action="store_true",
        help="Force WordPress sync on for the newsroom draft run",
    )
    parser.add_argument(
        "--no-wordpress-sync",
        dest="wordpress_sync",
        action="store_false",
        help="Keep newsroom draft exports local only",
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
    wordpress_sync_enabled = config.wordpress_sync_enabled
    if args.wordpress_sync is not None:
        wordpress_sync_enabled = args.wordpress_sync
    elif args.draft and not args.mock:
        wordpress_sync_enabled = True

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
        wordpress_status=args.wordpress_status or ("draft" if args.draft else config.wordpress_status),
        storage_root=Path(args.storage_root) if args.storage_root else config.storage_root,
        mock_mode=args.mock or config.mock_mode,
    )

    if args.wordpress_check_auth:
        auth = PublisherService(config).check_wordpress_auth()
        print(json.dumps({"wordpress_auth": serialize(auth)}, indent=2))
        return 0 if auth.authenticated else 1

    workflow = NewsroomWorkflow(config)
    dossier = workflow.run(seed_topics=args.seed_topic or None, country=config.country)
    summary = workflow.summarize(dossier)
    if args.draft:
        if workflow.should_skip_duplicate_publication(dossier):
            summary["draft"] = {
                "skipped": True,
                "reason": "duplicate_filter_exhausted",
                "cluster_key": dossier.topic.cluster_key,
            }
            summary["wordpress_sync_enabled"] = config.wordpress_sync_enabled
            print(json.dumps(summary, indent=2))
            return 0

        draft = workflow.draft(dossier)
        summary["draft"] = {
            "headline": draft.headline,
            "dek": draft.dek,
            "publish_ready": draft.publish_ready,
            "validation": {
                "editorial_score": draft.validation.editorial_score if draft.validation else None,
                "structure_score": draft.validation.structure_score if draft.validation else None,
                "grounding_score": draft.validation.grounding_score if draft.validation else None,
                "issues": draft.validation.issues if draft.validation else [],
                "publish": draft.validation.publish if draft.validation else None,
            },
            "saved_paths": workflow.save_draft(draft, dossier),
        }
        summary["wordpress_sync_enabled"] = config.wordpress_sync_enabled
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())