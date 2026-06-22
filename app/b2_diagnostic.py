#!/usr/bin/env python3
"""
Standalone B2 connectivity diagnostic.

Run this from your project root (the folder containing app/):

    python -m app.b2_diagnostic

or:

    python app/b2_diagnostic.py

It will:
  1. Load .env from the project root
  2. Print the env vars it found (with masked secrets)
  3. Try to connect to B2
  4. List buckets
  5. Try to upload a small test file
  6. Clean up the test file
  7. Print a clear PASS/FAIL summary

If anything fails, the actual error message is printed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _mask(value: str) -> str:
    """Mask a secret string, showing only first 4 and last 4 chars."""
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}  (length={len(value)})"


def main() -> int:
    print("=" * 60)
    print("Backblaze B2 Connectivity Diagnostic")
    print("=" * 60)

    # ── Step 1: Load .env ──────────────────────────────────────────
    print("\n[1/5] Loading .env file...")
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    print(f"  Project root: {project_root}")
    print(f"  .env path:    {env_path}")
    print(f"  .env exists:  {env_path.exists()}")

    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            print("  .env loaded successfully via python-dotenv")
        except ImportError:
            print("  WARNING: python-dotenv not installed. Install with:")
            print("    pip install python-dotenv")
            print("  Falling back to shell env vars only.")
    else:
        print("  WARNING: .env not found. Relying on shell env vars only.")

    # ── Step 2: Print env vars ─────────────────────────────────────
    print("\n[2/5] Checking environment variables...")
    endpoint = os.getenv("B2_ENDPOINT_URL", "")
    access_key = os.getenv("B2_ACCESS_KEY_ID", "")
    secret_key = os.getenv("B2_SECRET_ACCESS_KEY", "")
    region = os.getenv("B2_REGION", "")
    bucket_name = os.getenv("B2_BUCKET_NAME", "trend-agent")

    print(f"  B2_ENDPOINT_URL:       {endpoint or '(not set)'}")
    print(f"  B2_ACCESS_KEY_ID:      {_mask(access_key)}")
    print(f"  B2_SECRET_ACCESS_KEY:  {_mask(secret_key)}")
    print(f"  B2_REGION:             {region or '(not set, will be derived)'}")
    print(f"  B2_BUCKET_NAME:        {bucket_name}")

    missing = []
    if not endpoint:
        missing.append("B2_ENDPOINT_URL")
    if not access_key:
        missing.append("B2_ACCESS_KEY_ID")
    if not secret_key:
        missing.append("B2_SECRET_ACCESS_KEY")

    if missing:
        print(f"\n  MISSING ENV VARS: {', '.join(missing)}")
        print(f"  Add these to your .env at {env_path}")
        return 1

    # ── Step 3: Try to connect ─────────────────────────────────────
    print("\n[3/5] Connecting to Backblaze B2...")
    try:
        # Add project root to sys.path so 'app' module is importable
        sys.path.insert(0, str(project_root))
        from app.b2_engine import B2Engine

        engine = B2Engine.instance()
        print(f"  Engine created: {engine._endpoint_url} (region={engine._region})")
        print(f"  Target bucket:  {bucket_name}")
    except Exception as exc:
        print(f"  Failed to create engine: {exc}")
        return 1

    # ── Step 4: ping (with verbose error) ──────────────────────────
    print("\n[4/5] Pinging B2 (list_buckets)...")
    ok = engine.ping(verbose=True)
    if not ok:
        print("\n  ping() FAILED")
        print("\n  Common causes:")
        print("    a. Wrong credentials — re-check keyID and applicationKey from B2 dashboard")
        print("       (Master Application Key will NOT work — create a regular application key)")
        print("    b. System clock skew — B2 rejects requests if clock is off by > 5 min")
        print("       Check with: date  (compare to world time)")
        print("    c. Wrong endpoint URL — must match your bucket's region")
        print("    d. Network firewall blocking outbound HTTPS to backblazeb2.com")
        print("    e. Key without 'listBuckets' capability")
        return 1

    print("  ping() succeeded!")

    # ── Step 5: List buckets + check configured bucket exists ──────
    print("\n[5/5] Listing buckets...")
    try:
        buckets = engine.list_buckets()
        print(f"  Found {len(buckets)} bucket(s):")
        for name in buckets:
            marker = " <-- configured" if name == bucket_name else ""
            print(f"    - {name}{marker}")

        if bucket_name not in buckets:
            print(f"\n  Configured bucket '{bucket_name}' does NOT exist.")
            print(f"  Creating it now via ensure_buckets()...")
            try:
                result = engine.ensure_buckets()
                print(f"  Result: {result}")
                if "error" in str(result.get(bucket_name, "")):
                    print(f"  Failed to create bucket. Check B2 dashboard permissions.")
                    return 1
            except Exception as exc:
                print(f"  Failed to create bucket: {exc}")
                return 1
    except Exception as exc:
        print(f"  Failed to list buckets: {exc}")
        return 1

    # ── Bonus: try upload + delete a tiny test file ────────────────
    print("\n[Bonus] Testing upload + delete cycle...")
    test_category = "blogs"
    test_key = "_diagnostic_test.txt"
    test_content = b"Hello from B2 diagnostic test."

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp.write(test_content)
            tmp_path = tmp.name

        # Show what the resolved key will look like
        from app.b2_engine import _resolve_object_key
        resolved_key = _resolve_object_key(test_category, test_key)
        print(f"  Category:    {test_category!r}")
        print(f"  Key:         {test_key!r}")
        print(f"  Resolved:    bucket={bucket_name!r}, key={resolved_key!r}")
        print(f"  Uploading {test_content!r}...")

        info = engine.upload_file(test_category, tmp_path, key=test_key)
        print(f"  Uploaded:")
        print(f"    bucket:  {info['bucket']}")
        print(f"    key:     {info['key']}")
        print(f"    size:    {info['size']} bytes")
        print(f"    url:     {info['url'][:80]}...")

        print(f"  Deleting test object...")
        deleted = engine.delete_object(test_category, test_key)
        if deleted:
            print(f"  Deleted test object")
        else:
            print(f"  WARNING: Could not delete test object (manual cleanup needed)")

        Path(tmp_path).unlink(missing_ok=True)
    except Exception as exc:
        print(f"  Upload/delete test failed: {exc}")
        print(f"  This usually means the bucket '{bucket_name}' doesn't exist")
        print(f"  or the application key lacks writeFiles capability.")
        return 1

    # ── Summary ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED — B2 is fully operational!")
    print(f"  Bucket:  {bucket_name}")
    print(f"  Region:  {region or '(derived from endpoint)'}")
    print(f"  Design:  single bucket + category prefixes")
    print(f"           (blogs/, images/, cache/, memory/, trends/, archives/)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())