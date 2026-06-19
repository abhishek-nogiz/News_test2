#!/usr/bin/env python3
"""
Daily refresh: incremental update of the vector-store library.

This is "Step 2" from FLOW.md:
    1. Download the existing library from B2
    2. Check sitemap for NEW articles published since yesterday
    3. Vectorize ONLY the new ones (not everything from zero)
    4. Add them to the library
    5. Upload back to B2

USAGE
=====
Schedule via cron (Linux) or Railway's cron service:

    # Daily at 03:00 UTC
    0 3 * * *  cd /app && python scripts/refresh_vector_store.py >> /var/log/vector-refresh.log 2>&1

Or run manually:

    python scripts/refresh_vector_store.py --tenant peoplenewstime

EXIT CODES
==========
    0  Success (or nothing to do)
    1  Configuration error (missing env vars, B2 unreachable, etc.)
    2  Partial failure (some articles failed to index — see logs)
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
        "--force",
        action="store_true",
        help="Force re-indexing of every article (same as bootstrap)",
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

    from news_agent.core.config import AppConfig
    config = AppConfig.from_env()

    tenant_id = config.tenant_id or "_default"

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] refresh_vector_store start — tenant={tenant_id}")

    if not config.sitemap_url:
        print("ERROR: NEWS_AGENT_SITEMAP_URL not set — nothing to refresh.")
        return 1

    try:
        from app.cloud_sync import CloudSync
        cloud_sync = CloudSync.instance()
        cloud_sync.initialize()
    except Exception as exc:
        print(f"ERROR: CloudSync unavailable: {exc}")
        cloud_sync = None

    from news_agent.services.internalLink import (
        create_vector_store,
        IndexingService,
    )

    vector_store = create_vector_store(config, cloud_sync=cloud_sync)
    indexing = IndexingService(config, vector_store)

    if cloud_sync is not None and cloud_sync.cloud_enabled:
        print("[B2] Downloading existing vector store...")
        cloud_sync.download_vector_store(tenant_id)
    else:
        print("[B2] Cloud not enabled — using existing local cache only.")

    t0 = time.time()
    result = indexing.index_tenant(tenant_id, force_refresh=args.force)
    elapsed = time.time() - t0

    print(
        f"[Index] indexed={result['documents_indexed']} "
        f"skipped={result['documents_skipped']} "
        f"errors={result['errors']} "
        f"duration={elapsed:.1f}s"
    )

    if result["documents_indexed"] > 0 and cloud_sync is not None and cloud_sync.cloud_enabled:
        print("[B2] Uploading refreshed vector store...")
        cloud_sync.upload_vector_store(tenant_id)
    else:
        print("[B2] No changes — skipping upload.")

    if result["errors"] > 0:
        print(f"WARN: {result['errors']} errors during indexing — investigate.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())