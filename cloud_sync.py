"""
Cloud Sync — bridges local filesystem to Backblaze B2 + MongoDB.

After ``news_agent`` writes files locally (storage/blogs, storage/cache,
storage/images, etc.), this module:

  1. Uploads the files to Backblaze B2 buckets.
  2. Saves metadata in MongoDB (blog_metadata, run_cache, etc.).
  3. Returns presigned URLs and object keys for downstream consumers.

Design principle: **local-first with cloud backup**.

  - During the same container lifecycle, local files are fast.
  - After a deploy wipes the container, MongoDB metadata tells us
    where to find files in Backblaze B2.
  - Consumers (like publish_local_html.py) try local first, then
    fall back to B2 via presigned URL.

This module is the ONLY place that touches both B2 and MongoDB
for file sync.  It keeps the rest of the app decoupled.

Why B2 instead of MinIO:
  - B2 is cloud-hosted → survives CI/CD container recreation.
  - No server to manage → no Docker sidecar.
  - S3-compatible API → standard boto3 library.
  - Cheap: first 10 GB free, free egress.

Usage::

    from app.cloud_sync import CloudSync

    sync = CloudSync.instance()

    # After news_agent finishes a run:
    result = sync.sync_run(run_id="49d15026-...")

    # Lookup a previous run's files:
    meta = sync.get_blog_metadata(run_id="49d15026-...")

    # Fetch HTML content (local first, B2 fallback):
    html = sync.get_html_content(run_id="49d15026-...")

    # Scheduler status (replaces scheduler_status.json):
    sync.save_scheduler_status({"last_triggered_at": "...", ...})
    status = sync.load_scheduler_status()
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── Load .env if python-dotenv is available ─────────────────────────────
# This makes env vars from .env available to os.getenv() calls.
# If python-dotenv is not installed, falls back to shell env vars only.
try:
    from dotenv import load_dotenv
    # Look for .env in the project root (parent of app/)
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars

from app.mongo_engine import MongoEngine


# ── Category mapping ──────────────────────────────────────────────────────
# Logical category names → key prefixes within the single configured B2 bucket.
# The B2Engine takes a "category" argument and resolves it to a prefix
# (e.g. "blogs" → "blogs/", "trend-blogs" → "blogs/").  Both short and
# legacy names are supported.  All uploads go to the same bucket, organized
# by prefix.  See b2_engine.py for details.

CATEGORIES = ["blogs", "images", "cache", "memory", "trends", "archives"]


# ── Project roots ────────────────────────────────────────────────────────

def _project_root() -> Path:
    """Return the project root (parent of app/)."""
    return Path(__file__).resolve().parent.parent


class CloudSync:
    """
    Orchestrates local → Backblaze B2 upload + MongoDB metadata save.

    Uses the singleton pattern so the engines are initialized once.
    """

    _instance: Optional[CloudSync] = None

    # ── Singleton ─────────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> CloudSync:
        """Return the global singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful for tests)."""
        cls._instance = None

    # ── Constructor ───────────────────────────────────────────────────

    def __init__(self) -> None:
        self._mongo = MongoEngine.instance()
        self._b2 = None  # Lazy — B2 might not be configured yet
        self._root = _project_root()
        self._cloud_enabled: Optional[bool] = None

    # ── Cloud availability check ──────────────────────────────────────

    @property
    def cloud_enabled(self) -> bool:
        """
        True if Backblaze B2 is configured and reachable.

        If B2 is not configured (missing env vars), cloud sync
        is disabled and the app falls back to local-only mode.
        Diagnostic messages are printed so the operator knows why.
        """
        if self._cloud_enabled is None:
            try:
                from app.b2_engine import B2Engine
                engine = B2Engine.instance()
                # Use verbose=True so we see the EXACT reason ping failed
                self._cloud_enabled = engine.ping(verbose=True)
                if self._cloud_enabled:
                    print("[CloudSync] Backblaze B2 is connected and ready.")
                    try:
                        buckets = engine.list_buckets()
                        print(f"[CloudSync] Visible buckets: {buckets}")
                    except Exception as exc:
                        print(f"[CloudSync] Could not list buckets: {exc}")
                else:
                    print("[CloudSync] WARNING: B2 ping() failed. Cloud uploads DISABLED.")
            except ValueError as exc:
                self._cloud_enabled = False
                print(f"[CloudSync] WARNING: B2 not configured — {exc}.")
                print(f"  Cloud uploads DISABLED.")
                # Print which env vars are missing
                missing = []
                if not os.getenv("B2_ENDPOINT_URL"):
                    missing.append("B2_ENDPOINT_URL")
                if not os.getenv("B2_ACCESS_KEY_ID"):
                    missing.append("B2_ACCESS_KEY_ID")
                if not os.getenv("B2_SECRET_ACCESS_KEY"):
                    missing.append("B2_SECRET_ACCESS_KEY")
                if missing:
                    print(f"  Missing env vars: {', '.join(missing)}")
                    env_path = Path(__file__).resolve().parent.parent / ".env"
                    print(f"  Expected .env at: {env_path}")
                    print(f"  .env exists: {env_path.exists()}")
            except Exception as exc:
                self._cloud_enabled = False
                print(f"[CloudSync] WARNING: B2 initialization error — {exc}.")
                print(f"  Cloud uploads DISABLED.")
        return self._cloud_enabled

    def _get_b2(self):
        """Lazily get the B2 engine (raises if not configured)."""
        if self._b2 is None:
            from app.b2_engine import B2Engine
            self._b2 = B2Engine.instance()
        return self._b2

    # ── Find local files for a run ────────────────────────────────────

    def _find_html_for_run(self, run_id: str) -> Path | None:
        """Find the generated HTML file for a run_id in storage/blogs/."""
        blogs_dir = self._root / "storage" / "blogs"
        if not blogs_dir.exists():
            return None
        candidates = sorted(
            blogs_dir.glob(f"*-{run_id}.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _find_md_for_run(self, run_id: str) -> Path | None:
        """Find the generated Markdown file for a run_id in storage/blogs/."""
        blogs_dir = self._root / "storage" / "blogs"
        if not blogs_dir.exists():
            return None
        candidates = sorted(
            blogs_dir.glob(f"*-{run_id}.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _find_images_for_run(self, run_id: str) -> list[Path]:
        """Find generated images associated with a run."""
        images_dir = self._root / "storage" / "images"
        if not images_dir.exists():
            return []
        results = list(images_dir.glob(f"*{run_id}*"))
        if not results:
            import time
            cutoff = time.time() - 600
            results = [
                p for p in images_dir.iterdir()
                if p.is_file() and p.stat().st_mtime > cutoff
            ]
        return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)

    def _find_cache_files(self, run_id: str) -> list[Path]:
        """Find cache JSON files for a run_id."""
        cache_dir = self._root / "storage" / "cache"
        if not cache_dir.exists():
            return []
        return sorted(cache_dir.glob(f"*{run_id}*.json"))

    # ── Upload helpers ────────────────────────────────────────────────

    def _upload_if_exists(
        self,
        bucket: str,
        local_path: Path | None,
        key: str,
    ) -> dict | None:
        """Upload a file to B2 if it exists locally.  Returns upload info or None."""
        if not local_path or not local_path.exists():
            return None
        if not self.cloud_enabled:
            print(f"[CloudSync] Skipping B2 upload for {key} — cloud not enabled (local-only mode)")
            return {
                "bucket": bucket,
                "key": key,
                "local_path": str(local_path),
                "url": None,  # No cloud — local only
            }

        b2 = self._get_b2()
        info = b2.upload_file(bucket, str(local_path), key=key)
        info["local_path"] = str(local_path)
        print(f"[CloudSync] Uploaded {key} → {bucket} ({info.get('size', '?')} bytes)")
        return info

    def _upload_cache_file(self, local_path: Path, run_id: str) -> dict | None:
        """Upload a single cache JSON file.  Returns upload info or None."""
        if not local_path.exists():
            return None

        stem = local_path.name.split("-")[0]  # e.g. "research", "run", "publish"
        key = f"{run_id}/{local_path.name}"

        # Parse JSON content for MongoDB storage
        try:
            content = json.loads(local_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            content = None

        cache_doc = {
            "id": self._mongo.next_id("run_cache"),
            "run_id": run_id,
            "cache_type": stem,
            "filename": local_path.name,
            "local_path": str(local_path),
            "content": content,
            "created_at": datetime.now().isoformat(),
        }

        # Add B2 key if cloud is enabled
        if self.cloud_enabled:
            b2 = self._get_b2()
            b2.upload_file("cache", str(local_path), key=key)
            cache_doc["b2_bucket"] = "cache"
            cache_doc["b2_key"] = key

        # Save to MongoDB regardless of cloud status
        self._mongo.insert("run_cache", cache_doc)

        return {
            "cache_type": stem,
            "filename": local_path.name,
            "local_path": str(local_path),
            "b2_key": key if self.cloud_enabled else None,
        }

    # ── Public: Sync a complete run ───────────────────────────────────

    def sync_run(
        self,
        run_id: str,
        topic: str | None = None,
        job_id: int | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """
        Sync all artifacts for a pipeline run to cloud + MongoDB.

        This should be called AFTER ``news_agent`` finishes and has
        written its output files locally.

        Args:
            run_id:   The pipeline run UUID.
            topic:    The topic/keyword that was processed.
            job_id:   The scheduler job ID (if applicable).
            category: The category (e.g. "sports", "politics").

        Returns:
            A dict with upload results for each artifact type.
        """
        now = datetime.now().isoformat()

        # ── Find local files ──────────────────────────────────────────
        html_path = self._find_html_for_run(run_id)
        md_path = self._find_md_for_run(run_id)
        image_paths = self._find_images_for_run(run_id)
        cache_paths = self._find_cache_files(run_id)

        # ── Build B2 object keys (structured: runs/{run_id}/...) ──────
        html_key = f"runs/{run_id}/article.html" if html_path else None
        md_key = f"runs/{run_id}/article.md" if md_path else None

        # ── Upload to B2 ──────────────────────────────────────────────
        html_info = self._upload_if_exists("blogs", html_path, html_key) if html_key else None
        md_info = self._upload_if_exists("blogs", md_path, md_key) if md_key else None

        image_infos = []
        image_url_map: dict[str, str] = {}  # filename → B2 presigned URL
        for img_path in image_paths:
            # Key WITHOUT the "images/" prefix — the engine's category
            # resolution adds it automatically.  Final key:
            #   images/runs/{run_id}/{filename}
            img_key = f"runs/{run_id}/{img_path.name}"
            info = self._upload_if_exists("images", img_path, img_key)
            if info:
                image_infos.append(info)
                # Build a filename → URL map for HTML rewriting later
                if info.get("url"):
                    image_url_map[img_path.name] = info["url"]

        cache_infos = []
        for cache_path in cache_paths:
            info = self._upload_cache_file(cache_path, run_id)
            if info:
                cache_infos.append(info)

        # ── Save blog_metadata to MongoDB ─────────────────────────────
        blog_doc = {
            "id": self._mongo.next_id("blog_metadata"),
            "run_id": run_id,
            "topic": topic,
            "job_id": job_id,
            "category": category,
            "status": "generated",
            "created_at": now,
            "updated_at": now,

            # Local paths (valid during same container lifecycle)
            "local_html_path": str(html_path) if html_path else None,
            "local_md_path": str(md_path) if md_path else None,

            # B2 references (survive deploys)
            "b2_html_key": html_info.get("key") if html_info else None,
            "b2_html_url": html_info.get("url") if html_info else None,
            "b2_md_key": md_info.get("key") if md_info else None,
            "b2_md_url": md_info.get("url") if md_info else None,
            "b2_image_keys": [i.get("key") for i in image_infos if i.get("key")],
            "b2_image_urls": [i.get("url") for i in image_infos if i.get("url")],
            # Map of local image filename → B2 presigned URL.
            # Used by rewrite_image_urls() to fix <img src="..."> tags
            # in HTML retrieved from cloud after a deploy.
            "b2_image_map": image_url_map,

            # Publish tracking
            "published": False,
            "publish_status": None,
            "wordpress_synced": False,
        }

        # Check if metadata for this run_id already exists → update instead
        existing = self._mongo.find_one("blog_metadata", {"run_id": run_id})
        if existing:
            updates = {k: v for k, v in blog_doc.items() if k != "id" and v is not None}
            self._mongo.update_one("blog_metadata", {"run_id": run_id}, updates)
            blog_meta_id = existing.get("id")
        else:
            self._mongo.insert("blog_metadata", blog_doc)
            blog_meta_id = blog_doc["id"]

        return {
            "run_id": run_id,
            "cloud_enabled": self.cloud_enabled,
            "html": html_info,
            "md": md_info,
            "images": image_infos,
            "cache_files": cache_infos,
            "blog_metadata_id": blog_meta_id,
        }

    # ── Public: Retrieve content ──────────────────────────────────────

    def get_blog_metadata(self, run_id: str) -> dict | None:
        """
        Get blog metadata for a run_id from MongoDB.

        Returns the metadata document with both local paths and
        B2 references, or None if not found.
        """
        return self._mongo.find_one("blog_metadata", {"run_id": run_id})

    def get_html_content(self, run_id: str) -> str | None:
        """
        Get the HTML content for a run_id.

        Tries local file first (fast), then falls back to B2.
        Returns the HTML string, or None if not found anywhere.
        """
        meta = self.get_blog_metadata(run_id)
        if not meta:
            return None

        # ── Try local file first ──────────────────────────────────────
        local_path = meta.get("local_html_path")
        if local_path:
            path = Path(local_path)
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")

        # ── Try finding by pattern if metadata local path is stale ────
        html_path = self._find_html_for_run(run_id)
        if html_path and html_path.exists():
            return html_path.read_text(encoding="utf-8", errors="replace")

        # ── Fallback to B2 ────────────────────────────────────────────
        b2_key = meta.get("b2_html_key")
        if b2_key and self.cloud_enabled:
            try:
                b2 = self._get_b2()
                bucket = meta.get("b2_html_bucket", "blogs")
                data = b2.download_bytes(bucket, b2_key)
                return data.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"CloudSync: failed to fetch HTML from B2: {exc}")

        return None

    def get_md_content(self, run_id: str) -> str | None:
        """Get the Markdown content for a run_id (local first, B2 fallback)."""
        meta = self.get_blog_metadata(run_id)
        if not meta:
            return None

        local_path = meta.get("local_md_path")
        if local_path:
            path = Path(local_path)
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")

        md_path = self._find_md_for_run(run_id)
        if md_path and md_path.exists():
            return md_path.read_text(encoding="utf-8", errors="replace")

        b2_key = meta.get("b2_md_key")
        if b2_key and self.cloud_enabled:
            try:
                b2 = self._get_b2()
                bucket = meta.get("b2_md_bucket", "blogs")
                data = b2.download_bytes(bucket, b2_key)
                return data.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"CloudSync: failed to fetch MD from B2: {exc}")

        return None

    def rewrite_image_urls(self, html: str, run_id: str) -> str:
        """
        Rewrite local image paths in HTML to B2 presigned URLs.

        After a CI/CD deploy, local image files are gone.  When the HTML
        is fetched from B2, its ``<img src="...">`` tags still reference
        local paths like ``/storage/images/foo.jpg`` or ``images/foo.jpg``.
        This method rewrites those URLs to point to the corresponding
        B2 presigned URLs stored in blog_metadata.b2_image_map.

        Args:
            html:    The HTML content (typically fetched from B2).
            run_id:  The run_id to look up image URLs for.

        Returns:
            HTML with local image paths replaced by B2 presigned URLs.
            If no metadata or no image map is found, returns the HTML
            unchanged.
        """
        if not html:
            return html

        meta = self.get_blog_metadata(run_id)
        if not meta:
            return html

        image_map: dict[str, str] = meta.get("b2_image_map") or {}
        if not image_map:
            # Fall back to the parallel b2_image_keys / b2_image_urls lists
            keys = meta.get("b2_image_keys") or []
            urls = meta.get("b2_image_urls") or []
            for key, url in zip(keys, urls):
                # key looks like "images/runs/{run_id}/{filename}"
                filename = key.rsplit("/", 1)[-1] if key else ""
                if filename and url:
                    image_map[filename] = url

        if not image_map:
            return html

        # Regex to find <img ... src="..." ...> tags
        import re

        def _replace_src(match: re.Match) -> str:
            full_tag = match.group(0)
            src_match = re.search(
                r'src\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                full_tag,
                flags=re.IGNORECASE,
            )
            if not src_match:
                return full_tag

            src_value = src_match.group(2) or src_match.group(3) or src_match.group(4) or ""
            if not src_value:
                return full_tag

            # Skip absolute URLs (http://, https://, //)
            if src_value.startswith(("http://", "https://", "//")):
                return full_tag

            # Skip data URIs
            if src_value.startswith("data:"):
                return full_tag

            # Extract filename from the local path
            # /storage/images/foo.jpg → foo.jpg
            # images/foo.jpg           → foo.jpg
            # foo.jpg                  → foo.jpg
            filename = src_value.rsplit("/", 1)[-1]
            # Strip query string / hash
            filename = filename.split("?", 1)[0].split("#", 1)[0]

            new_url = image_map.get(filename)
            if not new_url:
                return full_tag

            # Replace the src value in the original tag
            new_tag = (
                full_tag[:src_match.start(2)]
                + new_url
                + full_tag[src_match.end(2):]
            ) if src_match.group(2) else (
                full_tag[:src_match.start(3)]
                + new_url
                + full_tag[src_match.end(3):]
            ) if src_match.group(3) else (
                full_tag[:src_match.start(4)]
                + new_url
                + full_tag[src_match.end(4):]
            )
            return new_tag

        # Match <img ...> tags (self-closing or with attributes)
        img_pattern = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)
        return img_pattern.sub(_replace_src, html)

    def mark_published(self, run_id: str, publish_status: str = "success") -> None:
        """Update blog_metadata to mark a run as published."""
        self._mongo.update_one(
            "blog_metadata",
            {"run_id": run_id},
            {
                "published": True,
                "publish_status": publish_status,
                "updated_at": datetime.now().isoformat(),
            },
        )

    def mark_wordpress_synced(self, run_id: str, post_id: int | None = None) -> None:
        """Update blog_metadata to mark WordPress sync as done."""
        updates = {
            "wordpress_synced": True,
            "updated_at": datetime.now().isoformat(),
        }
        if post_id is not None:
            updates["wordpress_post_id"] = post_id
        self._mongo.update_one("blog_metadata", {"run_id": run_id}, updates)

    # ── Scheduler status (replaces scheduler_status.json) ─────────────

    def save_scheduler_status(self, data: dict) -> None:
        """
        Persist scheduler status to MongoDB.

        Replaces the old _write_status() pattern that wrote to
        scheduler_status.json.  Uses a singleton document with
        _id = "scheduler".
        """
        self._mongo.db["scheduler_status"].update_one(
            {"_id": "scheduler"},
            {"$set": {**data, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )

    def load_scheduler_status(self) -> dict[str, Any]:
        """
        Load scheduler status from MongoDB.

        Replaces the old _read_status() pattern.  Returns default
        values if no status document exists yet.
        """
        doc = self._mongo.db["scheduler_status"].find_one({"_id": "scheduler"})
        if doc:
            doc.pop("_id", None)
            return doc
        return {
            "last_triggered_at": None,
            "last_main_status": "never",
            "last_main_at": None,
            "last_main_run_id": None,
            "last_api_status": "never",
            "last_api_at": None,
            "last_api_html": None,
            "last_error": None,
        }

    # ── Published topics dedup (replaces published_topics.json) ───────

    def is_topic_published(self, topic: str, lookback_hours: int = 48) -> bool:
        """
        Check if a topic was already published within the lookback window.

        Uses MongoDB instead of published_topics.json, so the dedup
        check survives deploys.
        """
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(hours=lookback_hours)).isoformat()
        doc = self._mongo.find_one("published_topics", {
            "topic": {"$regex": f"^{topic}$", "$options": "i"},
            "published_at": {"$gte": cutoff},
        })
        return doc is not None

    def record_published_topic(self, topic: str, run_id: str) -> None:
        """Record that a topic was published (for dedup)."""
        existing = self._mongo.find_one("published_topics", {"topic": topic})
        if existing:
            self._mongo.update_one("published_topics", {"topic": topic}, {
                "published_at": datetime.now().isoformat(),
                "run_id": run_id,
            })
        else:
            self._mongo.insert("published_topics", {
                "id": self._mongo.next_id("published_topics"),
                "topic": topic,
                "run_id": run_id,
                "published_at": datetime.now().isoformat(),
            })

    # ── Run cache helpers ─────────────────────────────────────────────

    def get_run_cache(self, run_id: str, cache_type: str | None = None) -> list[dict]:
        """
        Get cached data for a run from MongoDB.

        Args:
            run_id:     The pipeline run UUID.
            cache_type: Optional filter (e.g. "research", "run", "publish").
        """
        query: dict = {"run_id": run_id}
        if cache_type:
            query["cache_type"] = cache_type
        return self._mongo.find_many("run_cache", query, sort=[("created_at", -1)])

    # ── Initialize on startup ─────────────────────────────────────────

    def initialize(self) -> dict[str, Any]:
        """
        Initialize both engines (MongoDB collections + B2 buckets).

        Call this once on app startup.  Returns a summary of what
        was created.
        """
        result: dict[str, Any] = {}

        # MongoDB collections + indexes
        result["mongo"] = self._mongo.ensure_collections()

        # B2 buckets (only if configured)
        if self.cloud_enabled:
            try:
                b2 = self._get_b2()
                result["b2"] = b2.ensure_buckets()
            except Exception as exc:
                result["b2"] = {"error": str(exc)}
        else:
            result["b2"] = "not_configured"

        return result