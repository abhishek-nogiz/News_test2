"""
MinIO Engine for the Trend Agent Scheduler.

A self-contained, reusable engine that handles:

  1. **Connection lifecycle** — lazy singleton via ``MinIOEngine.instance()``.
     Reads MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY from
     environment (or accepts them as parameters).  Works with any
     MinIO server (self-hosted, managed, Docker sidecar, etc.).

  2. **Auto-creation** — ``engine.ensure_buckets()`` creates all
     required buckets if they don't already exist.  Idempotent.

  3. **Upload / Download / Delete** — high-level helpers that work
     with local file paths and return presigned URLs for remote
     access.  Content types are auto-detected from extensions.

  4. **Health check** — ``engine.ping()`` verifies connectivity.

Environment variables (from .env)::

    MINIO_ENDPOINT=minio.example.com:9000
    MINIO_ACCESS_KEY=minioadmin
    MINIO_SECRET_KEY=minioadmin
    MINIO_SECURE=true              ← "true" for HTTPS, "false" for HTTP
    MINIO_REGION=us-east-1         ← optional

Usage::

    from app.minio_engine import MinIOEngine

    engine = MinIOEngine.instance()
    engine.ensure_buckets()

    # Upload a file
    info = engine.upload_file("blogs", "storage/blogs/article.html")

    # Upload with a specific object key
    info = engine.upload_file("blogs", "storage/blogs/article.html",
                              key="runs/49d15026/article.html")

    # Download
    path = engine.download_file("blogs", "runs/49d15026/article.html",
                                dest="/tmp/article.html")

    # Get a presigned URL (no download needed)
    url = engine.presigned_url("blogs", "runs/49d15026/article.html")

    # Check if object exists
    exists = engine.object_exists("blogs", "runs/49d15026/article.html")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error


# ── Bucket registry ──────────────────────────────────────────────────────
# Each entry declares a bucket name.  The engine creates them in
# ``ensure_buckets()``.

BUCKET_REGISTRY: list[str] = [
    "blogs",       # HTML + Markdown articles
    "images",      # Generated/downloaded images
    "cache",       # Run/research/publish JSON cache files
    "memory",      # Editorial memory and logs
    "trends",      # Trend data JSON
    "archives",    # Miscellaneous archived outputs
]


# ── Content type mapping ─────────────────────────────────────────────────

_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".xml":  "application/xml; charset=utf-8",
    ".csv":  "text/csv; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
    ".pdf":  "application/pdf",
    ".zip":  "application/zip",
}


def _content_type(path: str | Path) -> str:
    """Return the Content-Type for a file based on its extension."""
    suffix = Path(path).suffix.lower()
    return _CONTENT_TYPES.get(suffix, "application/octet-stream")


class MinIOEngine:
    """
    Lightweight MinIO engine with connection management, auto-creation,
    and file operation helpers.

    Use ``MinIOEngine.instance()`` to get the singleton, or construct
    directly for testing.
    """

    _instance: Optional[MinIOEngine] = None

    # ── Singleton ─────────────────────────────────────────────────────

    @classmethod
    def instance(
        cls,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        secure: bool | None = None,
        region: str | None = None,
    ) -> MinIOEngine:
        """
        Return the global singleton engine.

        On the very first call the engine is created with the given
        parameters (or environment variables).  Subsequent calls ignore
        the parameters and return the existing instance.
        """
        if cls._instance is None:
            cls._instance = cls(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
                region=region,
            )
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful for tests)."""
        if cls._instance is not None:
            cls._instance.close()
            cls._instance = None

    # ── Constructor ───────────────────────────────────────────────────

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        secure: bool | None = None,
        region: str | None = None,
    ) -> None:
        self._endpoint = endpoint or os.getenv("MINIO_ENDPOINT", "")
        if not self._endpoint:
            raise ValueError(
                "MinIO endpoint not configured.  Set MINIO_ENDPOINT in .env "
                "or pass endpoint to MinIOEngine()."
            )

        # Strip protocol prefix if someone includes it
        for prefix in ("https://", "http://"):
            if self._endpoint.startswith(prefix):
                self._endpoint = self._endpoint[len(prefix):]

        self._access_key = access_key or os.getenv("MINIO_ACCESS_KEY", "")
        self._secret_key = secret_key or os.getenv("MINIO_SECRET_KEY", "")

        if not self._access_key or not self._secret_key:
            raise ValueError(
                "MinIO credentials not configured.  Set MINIO_ACCESS_KEY and "
                "MINIO_SECRET_KEY in .env or pass them to MinIOEngine()."
            )

        # Secure flag: default True (HTTPS), can override via env or param
        if secure is not None:
            self._secure = secure
        else:
            self._secure = os.getenv("MINIO_SECURE", "true").lower() in (
                "true", "1", "yes",
            )

        self._region = region or os.getenv("MINIO_REGION", "")
        self._client: Optional[Minio] = None
        self._initialized = False  # True after ensure_buckets() succeeds

    # ── Connection ────────────────────────────────────────────────────

    @property
    def client(self) -> Minio:
        """Lazily connect and return the MinIO client."""
        if self._client is None:
            kwargs: dict = {
                "endpoint": self._endpoint,
                "access_key": self._access_key,
                "secret_key": self._secret_key,
                "secure": self._secure,
            }
            if self._region:
                kwargs["region"] = self._region
            self._client = Minio(**kwargs)
        return self._client

    def close(self) -> None:
        """Close the underlying client (idempotent)."""
        # Minio client doesn't have an explicit close, but we can
        # release the reference so it's GC'd.
        self._client = None

    # ── Health check ──────────────────────────────────────────────────

    def ping(self) -> bool:
        """
        Verify connectivity to the MinIO server.

        Returns True on success, False on failure.  Does not raise.
        """
        try:
            list(self.client.list_buckets())
            return True
        except Exception:
            return False

    def get_server_info(self) -> dict[str, str]:
        """Return basic server info dict.  Raises on failure."""
        buckets = list(self.client.list_buckets())
        return {
            "endpoint": self._endpoint,
            "secure": str(self._secure),
            "bucket_count": str(len(buckets)),
        }

    # ── Auto-creation ─────────────────────────────────────────────────

    def ensure_buckets(self, buckets: list[str] | None = None) -> dict[str, str]:
        """
        Create all registered buckets if they don't already exist.

        Idempotent — safe to call on every app startup.

        Returns a dict mapping bucket names to "created" or "exists".
        """
        bucket_list = buckets or BUCKET_REGISTRY
        result: dict[str, str] = {}

        for name in bucket_list:
            if self.client.bucket_exists(name):
                result[name] = "exists"
            else:
                self.client.make_bucket(name)
                result[name] = "created"

        self._initialized = True
        return result

    def register_bucket(self, name: str) -> None:
        """
        Register a custom bucket name.

        Call this **before** ``ensure_buckets()`` if you need
        additional buckets beyond the built-in ones.
        """
        if name not in BUCKET_REGISTRY:
            BUCKET_REGISTRY.append(name)

    # ── Upload ────────────────────────────────────────────────────────

    def upload_file(
        self,
        bucket: str,
        file_path: str | Path,
        key: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        """
        Upload a local file to a bucket.

        Args:
            bucket:       Target bucket name.
            file_path:    Local filesystem path to the file.
            key:          Object key in the bucket.  Defaults to the
                          filename portion of *file_path*.
            content_type: MIME type.  Auto-detected from extension if
                          not provided.

        Returns:
            Dict with bucket, key, size, content_type, etag, and
            a presigned URL valid for 7 days.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        object_key = key or path.name
        ct = content_type or _content_type(path)
        size = path.stat().st_size

        self.client.fput_object(
            bucket_name=bucket,
            object_name=object_key,
            file_path=str(path),
            content_type=ct,
        )

        url = self.presigned_url(bucket, object_key)

        return {
            "bucket": bucket,
            "key": object_key,
            "size": size,
            "content_type": ct,
            "url": url,
        }

    def upload_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """
        Upload raw bytes to a bucket.

        Returns:
            Dict with bucket, key, size, content_type, and presigned URL.
        """
        import io

        stream = io.BytesIO(data)
        size = len(data)

        self.client.put_object(
            bucket_name=bucket,
            object_name=key,
            data=stream,
            length=size,
            content_type=content_type,
        )

        url = self.presigned_url(bucket, key)

        return {
            "bucket": bucket,
            "key": key,
            "size": size,
            "content_type": content_type,
            "url": url,
        }

    # ── Download ──────────────────────────────────────────────────────

    def download_file(
        self,
        bucket: str,
        key: str,
        dest: str | Path | None = None,
    ) -> Path:
        """
        Download an object to a local file.

        Args:
            bucket: Source bucket name.
            key:    Object key in the bucket.
            dest:   Local destination path.  Defaults to the object
                    key's filename in the current working directory.

        Returns:
            Path to the downloaded file.
        """
        if dest is None:
            dest = Path(key).name
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        self.client.fget_object(
            bucket_name=bucket,
            object_name=key,
            file_path=str(dest),
        )
        return dest

    def download_bytes(self, bucket: str, key: str) -> bytes:
        """
        Download an object as raw bytes.

        Returns:
            The object content as bytes.
        """
        response = self.client.get_object(bucket_name=bucket, object_name=key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    # ── Presigned URLs ────────────────────────────────────────────────

    def presigned_url(
        self,
        bucket: str,
        key: str,
        expires: int = 7 * 24 * 3600,
    ) -> str:
        """
        Generate a presigned GET URL for an object.

        Args:
            bucket: Bucket name.
            key:    Object key.
            expires: URL expiry in seconds (default: 7 days).

        Returns:
            Presigned URL string.
        """
        from datetime import timedelta as td

        url = self.client.presigned_get_object(
            bucket_name=bucket,
            object_name=key,
            expires=td(seconds=expires),
        )
        return url

    # ── Object info ───────────────────────────────────────────────────

    def object_exists(self, bucket: str, key: str) -> bool:
        """Check if an object exists in a bucket."""
        try:
            self.client.stat_object(bucket_name=bucket, object_name=key)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            raise

    def stat_object(self, bucket: str, key: str) -> dict:
        """
        Get metadata for an object.

        Returns dict with bucket, key, size, content_type, etag,
        last_modified, and presigned URL.
        """
        obj = self.client.stat_object(bucket_name=bucket, object_name=key)
        return {
            "bucket": bucket,
            "key": key,
            "size": obj.size,
            "content_type": obj.content_type,
            "etag": obj.etag,
            "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
            "url": self.presigned_url(bucket, key),
        }

    # ── Delete ────────────────────────────────────────────────────────

    def delete_object(self, bucket: str, key: str) -> bool:
        """Delete a single object.  Returns True if deleted."""
        try:
            self.client.remove_object(bucket_name=bucket, object_name=key)
            return True
        except S3Error:
            return False

    def delete_objects(self, bucket: str, prefix: str) -> int:
        """
        Delete all objects under a prefix.

        Returns the count of deleted objects.
        """
        count = 0
        for obj in self.client.list_objects(bucket, prefix=prefix, recursive=True):
            try:
                self.client.remove_object(bucket_name=bucket, object_name=obj.object_name)
                count += 1
            except S3Error:
                pass
        return count

    # ── List ──────────────────────────────────────────────────────────

    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        recursive: bool = True,
    ) -> list[dict]:
        """
        List objects in a bucket, optionally filtered by prefix.

        Returns a list of dicts with key, size, content_type, last_modified.
        """
        objects = self.client.list_objects(
            bucket, prefix=prefix, recursive=recursive,
        )
        result = []
        for obj in objects:
            result.append({
                "key": obj.object_name,
                "size": obj.size,
                "content_type": obj.content_type,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
            })
        return result

    # ── Convenience ───────────────────────────────────────────────────

    @property
    def is_initialized(self) -> bool:
        """True if ``ensure_buckets()`` has been called successfully."""
        return self._initialized

    def list_buckets(self) -> list[str]:
        """Return the names of all existing buckets."""
        return [b.name for b in self.client.list_buckets()]