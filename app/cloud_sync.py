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
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

from app.mongo_engine import MongoEngine


# ── Category mapping ──────────────────────────────────────────────────────
# Logical category names → key prefixes within the single configured B2 bucket.

# NOTE: "vector-store" is NOT in CATEGORIES because it's handled by
# SyncedJSONVectorStore directly (it uses its own tenant-scoped key layout,
# not the run-id-scoped layout of blogs/images/cache). It IS a valid B2
# category prefix though — see b2_engine.py's _resolve_category_prefix
# fallback for unknown category names.
CATEGORIES = ["blogs", "images", "cache", "memory", "trends", "archives"]


# ── Project roots ────────────────────────────────────────────────────────

def _project_root() -> Path:
    """Return the project root (parent of app/)."""
    return Path(__file__).resolve().parent.parent


class CloudSync:
    """
    Orchestrates local → Backblaze B2 upload + MongoDB metadata save.
    """

    _instance: Optional[CloudSync] = None

    @classmethod
    def instance(cls) -> CloudSync:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def __init__(self) -> None:
        self._mongo = MongoEngine.instance()
        self._b2 = None
        self._root = _project_root()
        self._cloud_enabled: Optional[bool] = None

    @property
    def cloud_enabled(self) -> bool:
        if self._cloud_enabled is None:
            try:
                from app.b2_engine import B2Engine
                engine = B2Engine.instance()
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
        if self._b2 is None:
            from app.b2_engine import B2Engine
            self._b2 = B2Engine.instance()
        return self._b2

    # ── Find local files for a run ────────────────────────────────────

    def _find_html_for_run(self, run_id: str) -> Path | None:
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
        cache_dir = self._root / "storage" / "cache"
        if not cache_dir.exists():
            return []
        return sorted(cache_dir.glob(f"*{run_id}*.json"))

    # ── Upload helpers ────────────────────────────────────────────────

    def _upload_if_exists(self, bucket: str, local_path: Path | None, key: str) -> dict | None:
        if not local_path or not local_path.exists():
            return None
        if not self.cloud_enabled:
            print(f"[CloudSync] Skipping B2 upload for {key} — cloud not enabled (local-only mode)")
            return {
                "bucket": bucket,
                "key": key,
                "local_path": str(local_path),
                "url": None,
            }
        b2 = self._get_b2()
        info = b2.upload_file(bucket, str(local_path), key=key)
        info["local_path"] = str(local_path)
        print(f"[CloudSync] Uploaded {key} → {bucket} ({info.get('size', '?')} bytes)")
        return info

    def _upload_cache_file(self, local_path: Path, run_id: str) -> dict | None:
        if not local_path.exists():
            return None
        stem = local_path.name.split("-")[0]
        key = f"{run_id}/{local_path.name}"
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
        if self.cloud_enabled:
            b2 = self._get_b2()
            b2.upload_file("cache", str(local_path), key=key)
            cache_doc["b2_bucket"] = "cache"
            cache_doc["b2_key"] = key
        self._mongo.insert("run_cache", cache_doc)
        return {
            "cache_type": stem,
            "filename": local_path.name,
            "local_path": str(local_path),
            "b2_key": key if self.cloud_enabled else None,
        }

    # ── Public: Sync a complete run ───────────────────────────────────

    def sync_run(self, run_id: str, topic: str | None = None, job_id: int | None = None, category: str | None = None) -> dict[str, Any]:
        now = datetime.now().isoformat()
        html_path = self._find_html_for_run(run_id)
        md_path = self._find_md_for_run(run_id)
        image_paths = self._find_images_for_run(run_id)
        cache_paths = self._find_cache_files(run_id)
        html_key = f"runs/{run_id}/article.html" if html_path else None
        md_key = f"runs/{run_id}/article.md" if md_path else None
        html_info = self._upload_if_exists("blogs", html_path, html_key) if html_key else None
        md_info = self._upload_if_exists("blogs", md_path, md_key) if md_key else None
        image_infos = []
        image_url_map: dict[str, str] = {}
        for img_path in image_paths:
            img_key = f"runs/{run_id}/{img_path.name}"
            info = self._upload_if_exists("images", img_path, img_key)
            if info:
                image_infos.append(info)
                if info.get("url"):
                    image_url_map[img_path.name] = info["url"]
        cache_infos = []
        for cache_path in cache_paths:
            info = self._upload_cache_file(cache_path, run_id)
            if info:
                cache_infos.append(info)
        blog_doc = {
            "id": self._mongo.next_id("blog_metadata"),
            "run_id": run_id,
            "topic": topic,
            "job_id": job_id,
            "category": category,
            "status": "generated",
            "created_at": now,
            "updated_at": now,
            "local_html_path": str(html_path) if html_path else None,
            "local_md_path": str(md_path) if md_path else None,
            "b2_html_key": html_info.get("key") if html_info else None,
            "b2_html_url": html_info.get("url") if html_info else None,
            "b2_md_key": md_info.get("key") if md_info else None,
            "b2_md_url": md_info.get("url") if md_info else None,
            "b2_image_keys": [i.get("key") for i in image_infos if i.get("key")],
            "b2_image_urls": [i.get("url") for i in image_infos if i.get("url")],
            "b2_image_map": image_url_map,
            "published": False,
            "publish_status": None,
            "wordpress_synced": False,
        }
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
        return self._mongo.find_one("blog_metadata", {"run_id": run_id})

    def get_html_content(self, run_id: str) -> str | None:
        meta = self.get_blog_metadata(run_id)
        if not meta:
            return None
        local_path = meta.get("local_html_path")
        if local_path:
            path = Path(local_path)
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
        html_path = self._find_html_for_run(run_id)
        if html_path and html_path.exists():
            return html_path.read_text(encoding="utf-8", errors="replace")
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
        if not html:
            return html
        meta = self.get_blog_metadata(run_id)
        if not meta:
            return html
        image_map: dict[str, str] = meta.get("b2_image_map") or {}
        if not image_map:
            keys = meta.get("b2_image_keys") or []
            urls = meta.get("b2_image_urls") or []
            for key, url in zip(keys, urls):
                filename = key.rsplit("/", 1)[-1] if key else ""
                if filename and url:
                    image_map[filename] = url
        if not image_map:
            return html
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
            if src_value.startswith(("http://", "https://", "//")):
                return full_tag
            if src_value.startswith("data:"):
                return full_tag
            filename = src_value.rsplit("/", 1)[-1]
            filename = filename.split("?", 1)[0].split("#", 1)[0]
            new_url = image_map.get(filename)
            if not new_url:
                return full_tag
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

        img_pattern = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)
        return img_pattern.sub(_replace_src, html)

    def mark_published(self, run_id: str, publish_status: str = "success") -> None:
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
        updates = {
            "wordpress_synced": True,
            "updated_at": datetime.now().isoformat(),
        }
        if post_id is not None:
            updates["wordpress_post_id"] = post_id
        self._mongo.update_one("blog_metadata", {"run_id": run_id}, updates)

    # ── Scheduler status (replaces scheduler_status.json) ─────────────

    def save_scheduler_status(self, data: dict) -> None:
        self._mongo.db["scheduler_status"].update_one(
            {"_id": "scheduler"},
            {"$set": {**data, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )

    def load_scheduler_status(self) -> dict[str, Any]:
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
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=lookback_hours)).isoformat()
        doc = self._mongo.find_one("published_topics", {
            "topic": {"$regex": f"^{topic}$", "$options": "i"},
            "published_at": {"$gte": cutoff},
        })
        return doc is not None

    def record_published_topic(self, topic: str, run_id: str) -> None:
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
        query: dict = {"run_id": run_id}
        if cache_type:
            query["cache_type"] = cache_type
        return self._mongo.find_many("run_cache", query, sort=[("created_at", -1)])

    # ── Initialize on startup ─────────────────────────────────────────

    def initialize(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            result["mongo"] = self._mongo.ensure_collections()
        except Exception as exc:
            result["mongo"] = {"error": str(exc)}
            print(f"[CloudSync] WARNING: Mongo ensure_collections() failed — {exc}")
            print("  Continuing without Mongo. B2-only features (vector store) are unaffected.")

        if self.cloud_enabled:
            try:
                b2 = self._get_b2()
                result["b2"] = b2.ensure_buckets()
            except Exception as exc:
                result["b2"] = {"error": str(exc)}
        else:
            result["b2"] = "not_configured"
        return result

    # ───────────────────────────────────────────────────────────────────────
    # NEW: Vector store sync helpers
    #
    # These wrap B2 upload/download for the internal-linking vector store
    # so the rest of the codebase doesn't need to know about B2 keys.
    # They are called by SyncedJSONVectorStore in
    # news_agent/services/internalLink/service.py.
    #
    # Key layout (under the configured B2 bucket):
    #   vector-store/tenants/{tenant_id}/documents.json
    #   vector-store/tenants/{tenant_id}/embeddings.json
    #
    # This layout mirrors the on-disk layout:
    #   storage/vector-store/tenants/{tenant_id}/documents.json
    #   storage/vector-store/tenants/{tenant_id}/embeddings.json
    # ───────────────────────────────────────────────────────────────────────

    VECTOR_STORE_CATEGORY = "vector-store"
    VECTOR_STORE_FILES = ("documents.json", "embeddings.json")

    def vector_store_local_dir(self, tenant_id: str) -> Path:
        """Return the local on-disk directory for a tenant's vector store."""
        return self._root / "storage" / "vector-store" / "tenants" / tenant_id

    def vector_store_b2_key(self, tenant_id: str, filename: str) -> str:
        """Return the B2 object key for a tenant's vector store file."""
        return f"tenants/{tenant_id}/{filename}"

    def upload_vector_store(self, tenant_id: str) -> dict[str, Any]:
        """
        Upload a tenant's local vector store files to B2.

        Uploads both ``documents.json`` and ``embeddings.json`` if they
        exist locally. Returns a summary dict with per-file upload info.

        This is called automatically by ``SyncedJSONVectorStore`` after
        every bulk_upsert, but can also be called manually by a
        maintenance script (e.g. after a full re-index) to make sure
        the B2 copy is current.
        """
        result: dict[str, Any] = {
            "tenant_id": tenant_id,
            "cloud_enabled": self.cloud_enabled,
            "uploaded": {},
        }
        if not self.cloud_enabled:
            return result
        local_dir = self.vector_store_local_dir(tenant_id)
        b2 = self._get_b2()
        for filename in self.VECTOR_STORE_FILES:
            local_path = local_dir / filename
            if not local_path.exists():
                continue
            key = self.vector_store_b2_key(tenant_id, filename)
            try:
                info = b2.upload_file(self.VECTOR_STORE_CATEGORY, str(local_path), key=key)
                result["uploaded"][filename] = info
                print(f"[CloudSync] Uploaded {key} → {info.get('bucket')} "
                      f"({info.get('size', '?')} bytes)")
            except Exception as exc:
                result.setdefault("errors", {})[filename] = str(exc)
                print(f"[CloudSync] ERROR uploading {key}: {exc}")
        return result

    def download_vector_store(self, tenant_id: str) -> dict[str, Any]:
        """
        Download a tenant's vector store files from B2 to local disk.

        Overwrites local files. If a file doesn't exist in B2, it is
        skipped (local file, if any, is left untouched).
        """
        result: dict[str, Any] = {
            "tenant_id": tenant_id,
            "cloud_enabled": self.cloud_enabled,
            "downloaded": {},
        }
        if not self.cloud_enabled:
            return result
        local_dir = self.vector_store_local_dir(tenant_id)
        local_dir.mkdir(parents=True, exist_ok=True)
        b2 = self._get_b2()
        for filename in self.VECTOR_STORE_FILES:
            key = self.vector_store_b2_key(tenant_id, filename)
            if not b2.object_exists(self.VECTOR_STORE_CATEGORY, key):
                continue
            local_path = local_dir / filename
            try:
                b2.download_file(self.VECTOR_STORE_CATEGORY, key, dest=local_path)
                result["downloaded"][filename] = str(local_path)
                print(f"[CloudSync] Downloaded {key} → {local_path}")
            except Exception as exc:
                result.setdefault("errors", {})[filename] = str(exc)
                print(f"[CloudSync] ERROR downloading {key}: {exc}")
        return result

    def vector_store_b2_status(self, tenant_id: str) -> dict[str, Any]:
        """
        Report what vector-store files exist in B2 for a tenant.

        Returns a dict like:
            {
              "tenant_id": "peoplenewstime",
              "cloud_enabled": True,
              "files": {
                "documents.json": {"exists": True, "size": 1234567},
                "embeddings.json": {"exists": True, "size": 4567890},
              }
            }
        """
        result: dict[str, Any] = {
            "tenant_id": tenant_id,
            "cloud_enabled": self.cloud_enabled,
            "files": {},
        }
        if not self.cloud_enabled:
            for f in self.VECTOR_STORE_FILES:
                result["files"][f] = {"exists": False}
            return result
        b2 = self._get_b2()
        for filename in self.VECTOR_STORE_FILES:
            key = self.vector_store_b2_key(tenant_id, filename)
            exists = b2.object_exists(self.VECTOR_STORE_CATEGORY, key)
            result["files"][filename] = {"exists": exists}
        return result