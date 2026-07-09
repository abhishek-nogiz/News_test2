
#!/usr/bin/env python3
"""
One-time bootstrap: build the full vector-store library from scratch.

This is "Step 1" from FLOW.md:
    1. Visit every article on the site (via sitemap)
    2. Send each article's text to HuggingFace for embedding
    3. Save all articles + vectors locally
    4. Upload the library to B2

USAGE
=====
From the project root (the folder containing app/ and news_agent/):

    python scripts/bootstrap_vector_store.py \\
        --tenant peoplenewstime \\
        --sitemap https://peoplenewstime.com/sitemap.xml

Or with env vars (preferred in production):

    export NEWS_AGENT_TENANT_ID=peoplenewstime
    export NEWS_AGENT_SITEMAP_URL=https://peoplenewstime.com/sitemap.xml
    python scripts/bootstrap_vector_store.py

This script is idempotent — running it twice will refresh the index
(only re-embed articles whose last_modified has changed). To force a
full rebuild, pass --force.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant ID (defaults to $NEWS_AGENT_TENANT_ID or '_default')",
    )
    parser.add_argument(
        "--sitemap",
        default=None,
        help="Sitemap URL (defaults to $NEWS_AGENT_SITEMAP_URL)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-indexing of every article, even if unchanged",
    )
    parser.add_argument(
        "--max-urls",
        type=int,
        default=None,
        help="Cap on URLs to crawl (defaults to config value, 500)",
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
    if args.sitemap:
        import os
        os.environ["NEWS_AGENT_SITEMAP_URL"] = args.sitemap
    if args.max_urls:
        import os
        os.environ["NEWS_AGENT_SITEMAP_MAX_URLS"] = str(args.max_urls)

    from news_agent.core.config import AppConfig
    config = AppConfig.from_env()

    tenant_id = config.tenant_id or "_default"
    sitemap_url = config.sitemap_url

    print("=" * 70)
    print("Vector Store Bootstrap (Step 1 of FLOW.md)")
    print("=" * 70)
    print(f"  Tenant:     {tenant_id}")
    print(f"  Sitemap:    {sitemap_url or '(not configured!)'}")
    print(f"  HF model:   {config.hf_embedding_model}")
    print(f"  HF API key: {'set' if config.hf_api_key else 'MISSING'}")
    print(f"  Force:      {args.force}")
    print()

    if not sitemap_url:
        print("ERROR: sitemap URL is required. Pass --sitemap or set NEWS_AGENT_SITEMAP_URL.")
        return 1

    if not config.hf_api_key:
        print("ERROR: HUGGINGFACE_API_KEY is not set.")
        print("       Without it, no embeddings will be generated — articles")
        print("       will be indexed keyword-only (no semantic search).")
        print("       Get one at: https://huggingface.co/settings/tokens")
        proceed = input("Continue anyway? [y/N] ").strip().lower()
        if proceed != "y":
            return 1

    # ── Bring up CloudSync (B2) ──────────────────────────────────────
    try:
        from app.cloud_sync import CloudSync
        cloud_sync = CloudSync.instance()
        cloud_sync.initialize()
        print(f"\n[CloudSync] cloud_enabled = {cloud_sync.cloud_enabled}")
    except Exception as exc:
        print(f"\n[CloudSync] not available: {exc}")
        print("Continuing in local-only mode (no B2 backup).")
        cloud_sync = None

    from news_agent.services.internalLink import (
        create_vector_store,
        IndexingService,
    )

    vector_store = create_vector_store(config, cloud_sync=cloud_sync)
    indexing = IndexingService(config, vector_store)

    if cloud_sync is not None and cloud_sync.cloud_enabled:
        print(f"\n[B2] Pulling existing vector store from B2 (if any)...")
        cloud_sync.download_vector_store(tenant_id)

    print(f"\n[Index] Starting full index of tenant '{tenant_id}'...")
    t0 = time.time()
    result = indexing.index_tenant(tenant_id, force_refresh=args.force)
    elapsed = time.time() - t0

    print()
    print("=" * 70)
    print("Bootstrap complete")
    print("=" * 70)
    print(f"  Documents indexed:  {result['documents_indexed']}")
    print(f"  Documents skipped:  {result['documents_skipped']} (already up-to-date)")
    print(f"  Errors:             {result['errors']}")
    print(f"  Duration:           {elapsed:.1f}s")
    print(f"  Total in store:     {vector_store.count(tenant_id)}")

    if cloud_sync is not None and cloud_sync.cloud_enabled:
        print(f"\n[B2] Uploading refreshed vector store to B2...")
        upload_result = cloud_sync.upload_vector_store(tenant_id)
        if upload_result.get("uploaded"):
            for fname, info in upload_result["uploaded"].items():
                print(f"  + {fname}: {info.get('size', '?')} bytes -> {info.get('key')}")
        if upload_result.get("errors"):
            for fname, err in upload_result["errors"].items():
                print(f"  X {fname}: {err}")

    print()
    print("Next steps:")
    print("  1. Schedule scripts/refresh_vector_store.py to run daily")
    print("  2. Article generation will now automatically pull relevant")
    print("     internal links via RetrievalService (no manual action)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


