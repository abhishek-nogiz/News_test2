
#!/usr/bin/env python3
"""
End-to-end smoke test for the internal-linking retrieval flow.

Simulates what happens during article generation (Step 3 of FLOW.md):
    1. Pull the vector store from B2 (if configured)
    2. Build a RetrievalService
    3. Pick a test topic
    4. Get the top internal-link matches
    5. Print the results so you can eyeball whether they make sense

USAGE
=====
    python scripts/test_internal_links.py \\
        --tenant peoplenewstime \\
        --topic "James Rodriguez"

If --topic is omitted, a default is used. The script does NOT modify
anything — read-only against the local cache (and B2 if needed).

EXIT CODES
==========
    0  Test succeeded (retrieved >=1 link)
    1  Configuration error
    2  Test ran but returned 0 links (index may be empty)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", default=None, help="Tenant ID")
    parser.add_argument("--topic", default="James Rodriguez", help="Test topic keyword")
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Max links to retrieve (default: 8, matches pipeline)",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    if args.tenant:
        import os
        os.environ["NEWS_AGENT_TENANT_ID"] = args.tenant

    from news_agent_working.core.config import AppConfig
    from news_agent_working.models import TrendTopic
    config = AppConfig.from_env()

    tenant_id = config.tenant_id or "_default"

    print("=" * 70)
    print("Internal Link Retrieval Test")
    print("=" * 70)
    print(f"  Tenant:   {tenant_id}")
    print(f"  Topic:    {args.topic}")
    print(f"  Limit:    {args.limit}")
    print()

    try:
        from app.cloud_sync import CloudSync
        cloud_sync = CloudSync.instance()
        cloud_sync.initialize()
        if cloud_sync.cloud_enabled:
            print("[B2] Pulling vector store from B2...")
            cloud_sync.download_vector_store(tenant_id)
    except Exception as exc:
        print(f"[CloudSync] not available: {exc} — using local cache only")
        cloud_sync = None

    from news_agent_working.services.internalLink import (
        create_vector_store,
        RetrievalService,
    )

    vector_store = create_vector_store(config, cloud_sync=cloud_sync)
    retrieval = RetrievalService(config, vector_store)

    total = vector_store.count(tenant_id)
    print(f"[Store] {total} documents indexed for tenant '{tenant_id}'")
    if total == 0:
        print()
        print("FAIL: vector store is empty.")
        print("      Run scripts/bootstrap_vector_store.py first.")
        return 2

    # TrendTopic requires keyword, traffic, source. The retrieval service
    # uses `hasattr(topic, 'category')` so the missing field is fine.
    topic = TrendTopic(keyword=args.topic, traffic=None, source="test")

    print()
    print(f"[Retrieve] Finding internal links for: '{args.topic}'...")
    links = retrieval.retrieve(
        tenant_id=tenant_id,
        topic=topic,
        plan_summary="",
        target_word_count=800,
        exclude_slug=None,
    )

    print()
    print("=" * 70)
    print(f"Retrieved {len(links)} internal links")
    print("=" * 70)
    for i, link in enumerate(links, 1):
        print(f"\n  [{i}] {link['title']}")
        print(f"      URL:   {link['url']}")
        print(f"      Slug:  {link['slug']}")
        print(f"      Score: {link['relevance_score']:.3f}")
        print(f"      Cat match: {link['category_match']}")
        print(f"      Reason: {link['reason']}")
        if link.get("anchor_candidates"):
            print(f"      Anchors:")
            for ac in link["anchor_candidates"]:
                print(f"        - (p{ac['priority']}) {ac['text']}")

    if not links:
        print()
        print("WARN: 0 links retrieved. Possible causes:")
        print("  - Tenant has no articles matching this category")
        print("  - Embeddings are missing (HF API key not set during bootstrap)")
        print("  - Sitemap is empty / unreachable")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


