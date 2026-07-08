"""
Backblaze B2 Engine for the Trend Agent Scheduler.

A self-contained, reusable engine that handles:

  1. **Connection lifecycle** — lazy singleton via ``B2Engine.instance()``.
     Reads B2 credentials from environment variables (or accepts them
     as parameters).  Uses the S3-compatible API via ``boto3``, so
     Backblaze B2 works exactly like AWS S3.

  2. **Auto-creation** — ``engine.ensure_buckets()`` creates the
     configured bucket if it doesn't already exist.  Idempotent.

  3. **Upload / Download / Delete** — high-level helpers that work
     with local file paths and return presigned URLs for remote
     access.  Content types are auto-detected from extensions.

  4. **Health check** — ``engine.ping()`` verifies connectivity.

  5. **Category-based organization** — instead of multiple buckets,
     the engine uses ONE configurable bucket + key prefixes per
     category (blogs/, images/, cache/, memory/, trends/, archives/).
     This is cheaper (fewer B2 transactions, less bucket-management
     overhead) and easier to manage than 6 separate buckets.

Why a single bucket with prefixes?
  - B2 charges per-transaction fees — fewer buckets = fewer ops.
  - Free tier allows limited buckets (100 max) — don't waste them.
  - All files share the same bucket policy, CORS, lifecycle rules.
  - Same logical separation via prefixes (blogs/, images/, etc.).
  - The public API still accepts "category" names like "blogs",
    "trend-blogs", "images" — they all map to a prefix within the
    single bucket.

Why Backblaze B2 instead of MinIO?
  - B2 is cloud-hosted → survives CI/CD container recreation.
  - S3-compatible API → uses standard boto3 library.
  - Cheap: first 10 GB storage free, egress is free.
  - No server to manage, no Docker sidecar, no license.

Environment variables (from .env)::

    B2_ENDPOINT_URL=https://s3.us-west-002.backblazeb2.com
    B2_ACCESS_KEY_ID=your-key-id
    B2_SECRET_ACCESS_KEY=your-secret-key
    B2_REGION=us-west-002                    ← optional, derived from endpoint
    B2_BUCKET_NAME=trend-agent               ← optional, default "trend-agent"

Usage::

    from app.b2_engine import B2Engine

    engine = B2Engine.instance()
    engine.ensure_buckets()

    # Upload a file (category is "blogs" → key prefix "blogs/")
    info = engine.upload_file("blogs", "storage/blogs/article.html")

    # Upload with a specific object key (will be prefixed: "blogs/runs/49d15026/article.html")
    info = engine.upload_file("blogs", "storage/blogs/article.html",
                              key="runs/49d15026/article.html")

    # Download (same category + key)
    path = engine.download_file("blogs", "runs/49d15026/article.html",
                                dest="/tmp/article.html")

    # Get a presigned URL (no download needed)
    url = engine.presigned_url("blogs", "runs/49d15026/article.html")

    # Check if object exists
    exists = engine.object_exists("blogs", "runs/49d15026/article.html")

Backward compatibility:
    The old bucket names ("trend-blogs", "trend-images", "trend-cache",
    "trend-memory", "trend-trends", "trend-archives") are also accepted
    as category arguments and map to the same prefixes.  This means
    existing callers don't need to change their code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# ── Bucket configuration ──────────────────────────────────────────────────
# Single configurable bucket, organized by key prefixes per category.
# Cheaper than 6 buckets, same organization, fewer B2 transactions.

DEFAULT_BUCKET_NAME = "trend-agent"

# Category → key prefix mapping.
# The "category" parameter on upload/download/list/etc. methods is looked
# up here to determine the key prefix within the single bucket.
# Both short ("blogs") and legacy ("trend-blogs") names are supported.
CATEGORY_PREFIXES: dict[str, str] = {
    # Short names (preferred)
    "blogs":    "blogs/",
    "images":   "images/",
    "cache":    "cache/",
    "memory":   "memory/",
    "trends":   "trends/",
    "archives": "archives/",
    # Legacy multi-bucket names (backward compat — same prefixes)
    "trend-blogs":    "blogs/",
    "trend-images":   "images/",
    "trend-cache":    "cache/",
    "trend-memory":   "memory/",
    "trend-trends":   "trends/",
    "trend-archives": "archives/",
}


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


def _bucket_name() -> str:
    """Return the single configured B2 bucket name (from env or default)."""
    name = os.getenv("B2_BUCKET_NAME", DEFAULT_BUCKET_NAME).strip()
    return name or DEFAULT_BUCKET_NAME


def _resolve_category_prefix(category: str) -> str:
    """
    Return the key prefix for a given category.

    Examples:
        "blogs"         → "blogs/"
        "trend-blogs"   → "blogs/"  (legacy name)
        "images"        → "images/"
        "custom-thing"  → "custom-thing/"  (default: use name as prefix)
    """
    if not category:
        return ""
    if category in CATEGORY_PREFIXES:
        return CATEGORY_PREFIXES[category]
    # Default: use the category name itself as the prefix
    return f"{category.strip('/')}/"


def _resolve_object_key(category: str, key: str | None) -> str:
    """
    Combine the category prefix with the object key.

    If the key already starts with the prefix, it's returned unchanged
    (idempotent).  If key is empty, only the prefix is returned.

    Examples:
        ("blogs", "runs/abc/article.html") → "blogs/runs/abc/article.html"
        ("blogs", "blogs/runs/abc/x.html")  → "blogs/runs/abc/x.html"  (already prefixed)
        ("blogs", None)                     → "blogs/"
        ("blogs", "")                       → "blogs/"
    """
    prefix = _resolve_category_prefix(category)
    if not key:
        return prefix.rstrip("/")
    if prefix and key.startswith(prefix):
        return key
    return f"{prefix}{key}"


class B2Engine:
    """
    Lightweight Backblaze B2 engine using boto3 (S3-compatible API).

    Provides connection management, auto-creation, and file operation
    helpers.  Use ``B2Engine.instance()`` to get the singleton, or
    construct directly for testing.

    All file operations take a ``category`` argument (e.g. "blogs",
    "images", "cache") which determines the key prefix within the
    single configured bucket.  This is transparent to callers —
    they just pass a category name and the engine handles the rest.
    """

    _instance: Optional[B2Engine] = None

    # ── Singleton ─────────────────────────────────────────────────────

    @classmethod
    def instance(
        cls,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str | None = None,
    ) -> B2Engine:
        """
        Return the global singleton engine.

        On the very first call the engine is created with the given
        parameters (or environment variables).  Subsequent calls ignore
        the parameters and return the existing instance.
        """
        if cls._instance is None:
            cls._instance = cls(
                endpoint_url=endpoint_url,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
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
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url or os.getenv("B2_ENDPOINT_URL", "")
        if not self._endpoint_url:
            raise ValueError(
                "Backblaze B2 endpoint not configured.  Set B2_ENDPOINT_URL in .env "
                "or pass endpoint_url to B2Engine()."
            )

        # Ensure URL has scheme
        if not self._endpoint_url.startswith("http"):
            self._endpoint_url = f"https://{self._endpoint_url}"

        self._access_key_id = access_key_id or os.getenv("B2_ACCESS_KEY_ID", "")
        self._secret_access_key = secret_access_key or os.getenv("B2_SECRET_ACCESS_KEY", "")

        if not self._access_key_id or not self._secret_access_key:
            raise ValueError(
                "Backblaze B2 credentials not configured.  Set B2_ACCESS_KEY_ID and "
                "B2_SECRET_ACCESS_KEY in .env or pass them to B2Engine()."
            )

        # Derive region from endpoint if not provided
        # e.g. https://s3.us-west-002.backblazeb2.com → us-west-002
        if region:
            self._region = region
        elif os.getenv("B2_REGION", "").strip():
            self._region = os.getenv("B2_REGION").strip()
        else:
            self._region = self._extract_region(self._endpoint_url)

        # Bucket name is read fresh from env each time via _bucket_name(),
        # but we cache it here for diagnostics / introspection.
        self._bucket_name = _bucket_name()

        self._s3_client = None
        self._s3_resource = None
        self._initialized = False  # True after ensure_buckets() succeeds

    @staticmethod
    def _extract_region(endpoint_url: str) -> str:
        """Extract region from B2 endpoint URL."""
        # https://s3.us-west-002.backblazeb2.com → us-west-002
        import re
        m = re.search(r's3[.]([a-z0-9-]+)[.]backblazeb2', endpoint_url)
        if m:
            return m.group(1)
        return "us-west-002"  # Default

    # ── Connection ────────────────────────────────────────────────────

    @property
    def client(self):
        """Lazily create and return the boto3 S3 client."""
        if self._s3_client is None:
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self._region,
            )
        return self._s3_client

    @property
    def resource(self):
        """Lazily create and return the boto3 S3 resource."""
        if self._s3_resource is None:
            self._s3_resource = boto3.resource(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self._region,
            )
        return self._s3_resource

    def close(self) -> None:
        """Close the underlying client (idempotent)."""
        self._s3_client = None
        self._s3_resource = None

    # ── Health check ────────────────────────────────────────────────

    def ping(self, verbose: bool = False) -> bool:
        """
        Verify connectivity to the Backblaze B2 service.

        Args:
            verbose: If True, print the actual exception on failure so
                     the operator can see WHY the call failed (auth,
                     clock skew, network, region mismatch, etc.).

        Returns True on success, False on failure.  Does not raise.
        """
        try:
            self.client.list_buckets()
            return True
        except Exception as exc:
            if verbose:
                print(f"[B2Engine] ping() failed:")
                print(f"  Exception type: {type(exc).__name__}")
                print(f"  Exception msg:  {exc}")
                # Print boto3-specific error details if available
                if hasattr(exc, 'response'):
                    resp = exc.response
                    print(f"  HTTP status:    {resp.get('ResponseMetadata', {}).get('HTTPStatusCode', '?')}")
                    print(f"  Error code:     {resp.get('Error', {}).get('Code', '?')}")
                    print(f"  Error message:  {resp.get('Error', {}).get('Message', '?')}")
                self.print_config()
            return False

    def print_config(self) -> None:
        """Print the current B2 configuration (with masked credentials) for debugging."""
        masked_key = self._access_key_id[:4] + "..." + self._access_key_id[-4:] if len(self._access_key_id) > 8 else "***"
        masked_secret = self._secret_access_key[:4] + "..." + self._secret_access_key[-4:] if len(self._secret_access_key) > 8 else "***"
        print(f"  Endpoint:       {self._endpoint_url}")
        print(f"  Region:         {self._region}")
        print(f"  Bucket:         {self._bucket_name}")
        print(f"  Access Key ID:  {masked_key}  (length={len(self._access_key_id)})")
        print(f"  Secret Key:     {masked_secret}  (length={len(self._secret_access_key)})")

    def get_server_info(self) -> dict[str, str]:
        """Return basic server info dict.  Raises on failure."""
        resp = self.client.list_buckets()
        buckets = [b["Name"] for b in resp.get("Buckets", [])]
        return {
            "endpoint_url": self._endpoint_url,
            "region": self._region,
            "bucket_name": self._bucket_name,
            "bucket_count": str(len(buckets)),
            "buckets": ", ".join(buckets[:10]),
        }

    # ── Auto-creation ─────────────────────────────────────────────────

    def ensure_buckets(self, _categories: list[str] | None = None) -> dict[str, str]:
        """
        Ensure the configured B2 bucket exists.  Idempotent.

        The ``_categories`` argument is accepted for backward compatibility
        but ignored — in the new single-bucket design, only the configured
        bucket (B2_BUCKET_NAME) needs to exist.

        Returns:
            Dict with one entry: {bucket_name: "created" | "exists" | "error: ..."}
        """
        bucket = _bucket_name()
        try:
            existing = {b["Name"] for b in self.client.list_buckets().get("Buckets", [])}
        except Exception as exc:
            return {bucket: f"error: {exc}"}

        if bucket in existing:
            self._initialized = True
            return {bucket: "exists"}

        try:
            self.client.create_bucket(Bucket=bucket)
            self._initialized = True
            return {bucket: "created"}
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code", "")
            if err_code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                self._initialized = True
                return {bucket: "exists"}
            print(f"B2Engine: failed to create bucket '{bucket}': {exc}")
            return {bucket: f"error: {exc}"}

    def register_bucket(self, name: str) -> None:
        """
        No-op in the new single-bucket design.

        Kept for backward compatibility — older code may call this.
        Category prefixes are now fixed in ``CATEGORY_PREFIXES``.
        """
        return  # no-op

    # ── Upload ────────────────────────────────────────────────────────

    def upload_file(
        self,
        bucket: str,
        file_path: str | Path,
        key: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        """
        Upload a local file to the configured B2 bucket, organized under
        a category-based key prefix.

        Args:
            bucket:       Logical category name.  Determines the key
                          prefix within the single bucket.
                          Examples: "blogs", "images", "cache",
                          or legacy "trend-blogs", "trend-images".
            file_path:    Local filesystem path to the file.
            key:          Object key WITHIN the category prefix.
                          Defaults to the filename portion of *file_path*.
                          The prefix is prepended automatically.
            content_type: MIME type.  Auto-detected from extension if
                          not provided.

        Returns:
            Dict with bucket, category, key (prefixed), size,
            content_type, and presigned URL.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key or path.name)
        ct = content_type or _content_type(path)
        size = path.stat().st_size

        self.client.upload_file(
            Filename=str(path),
            Bucket=actual_bucket,
            Key=object_key,
            ExtraArgs={"ContentType": ct},
        )

        url = self.presigned_url(actual_bucket, object_key)

        return {
            "bucket": actual_bucket,
            "category": bucket,
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
        Upload raw bytes to the configured bucket, organized under
        a category-based key prefix.

        Args:
            bucket: Logical category (e.g. "blogs", "images").
            key:    Object key WITHIN the category prefix.
                    The prefix is prepended automatically.
            data:   Raw bytes to upload.
            content_type: MIME type.

        Returns:
            Dict with bucket, category, key (prefixed), size,
            content_type, and presigned URL.
        """
        import io

        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)
        size = len(data)
        stream = io.BytesIO(data)

        self.client.upload_fileobj(
            Fileobj=stream,
            Bucket=actual_bucket,
            Key=object_key,
            ExtraArgs={"ContentType": content_type},
        )

        url = self.presigned_url(actual_bucket, object_key)

        return {
            "bucket": actual_bucket,
            "category": bucket,
            "key": object_key,
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
        Download an object from the configured bucket.

        Args:
            bucket: Logical category (e.g. "blogs").  Used to resolve
                    the key prefix if *key* doesn't already include it.
            key:    Object key.  If it doesn't start with the category
                    prefix, the prefix is prepended automatically.
            dest:   Local destination path.  Defaults to the object
                    key's filename in the current working directory.

        Returns:
            Path to the downloaded file.
        """
        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)

        if dest is None:
            dest = Path(key).name
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        self.client.download_file(
            Bucket=actual_bucket,
            Key=object_key,
            Filename=str(dest),
        )
        return dest

    def download_bytes(self, bucket: str, key: str) -> bytes:
        """
        Download an object as raw bytes.

        Args:
            bucket: Logical category (e.g. "blogs").  Used to resolve
                    the key prefix if *key* doesn't already include it.
            key:    Object key.  Prefix is prepended if not present.

        Returns:
            The object content as bytes.
        """
        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)

        response = self.client.get_object(
            Bucket=actual_bucket,
            Key=object_key,
        )
        return response["Body"].read()

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
            bucket: Either the actual bucket name (in which case *key*
                    is used as-is) OR a category name (in which case
                    the prefix is prepended to *key*).
            key:    Object key.
            expires: URL expiry in seconds (default: 7 days).

        Returns:
            Presigned URL string, or "" on error.
        """
        # If caller passed the actual configured bucket name, treat key as-is
        if bucket == _bucket_name():
            actual_bucket = bucket
            actual_key = key
        else:
            # Treat bucket as a category and resolve prefix
            actual_bucket = _bucket_name()
            actual_key = _resolve_object_key(bucket, key)

        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": actual_bucket, "Key": actual_key},
                ExpiresIn=expires,
            )
            return url
        except Exception:
            return ""

    # ── Object info ───────────────────────────────────────────────────

    def object_exists(self, bucket: str, key: str) -> bool:
        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)
        try:
            self.client.head_object(Bucket=actual_bucket, Key=object_key)
            return True
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            # B2's S3-compat layer often returns 403 (not 404) for missing keys
            # when using a scoped Application Key — treat it the same as "not found".
            if err_code in ("404", "NoSuchKey") or status in (403, 404):
                return False
            raise

    def stat_object(self, bucket: str, key: str) -> dict:
        """
        Get metadata for an object.

        Args:
            bucket: Logical category (prefix is auto-prepended if needed).
            key:    Object key.

        Returns dict with bucket, category, key, size, content_type,
        etag, last_modified, and presigned URL.
        """
        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)
        resp = self.client.head_object(Bucket=actual_bucket, Key=object_key)

        return {
            "bucket": actual_bucket,
            "category": bucket,
            "key": object_key,
            "size": resp.get("ContentLength", 0),
            "content_type": resp.get("ContentType", ""),
            "etag": resp.get("ETag", "").strip('"'),
            "last_modified": resp.get("LastModified").isoformat() if resp.get("LastModified") else None,
            "url": self.presigned_url(actual_bucket, object_key),
        }

    # ── Delete ────────────────────────────────────────────────────────

    def delete_object(self, bucket: str, key: str) -> bool:
        """
        Delete a single object.  Returns True if deleted.

        Args:
            bucket: Logical category (prefix is auto-prepended if needed).
            key:    Object key.
        """
        actual_bucket = _bucket_name()
        object_key = _resolve_object_key(bucket, key)
        try:
            self.client.delete_object(Bucket=actual_bucket, Key=object_key)
            return True
        except ClientError:
            return False

    def delete_objects(self, bucket: str, prefix: str) -> int:
        """
        Delete all objects under a category + prefix.

        Args:
            bucket: Logical category (e.g. "blogs").
            prefix: Sub-prefix within the category (e.g. "runs/abc/").
                    The category prefix is prepended automatically.

        Returns:
            Count of deleted objects.
        """
        actual_bucket = _bucket_name()
        full_prefix = _resolve_object_key(bucket, prefix)
        count = 0

        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=actual_bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                try:
                    self.client.delete_object(
                        Bucket=actual_bucket,
                        Key=obj["Key"],
                    )
                    count += 1
                except ClientError:
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
        List objects in the configured bucket under a category.

        Args:
            bucket: Logical category (e.g. "blogs").  Used as the
                    top-level prefix for listing.
            prefix: Additional sub-prefix within the category.
                    (e.g. "runs/abc/" to list files for run abc).
            recursive: If True, list all matching objects recursively.

        Returns:
            List of dicts with key (prefixed), size, last_modified.
        """
        actual_bucket = _bucket_name()
        full_prefix = _resolve_object_key(bucket, prefix)
        result = []

        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=actual_bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                result.append({
                    "key": obj["Key"],
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
                })

        return result

    # ── Convenience ───────────────────────────────────────────────────

    @property
    def is_initialized(self) -> bool:
        """True if ``ensure_buckets()`` has been called successfully."""
        return self._initialized

    def list_buckets(self) -> list[str]:
        """Return the names of all existing buckets in the B2 account."""
        resp = self.client.list_buckets()
        return [b["Name"] for b in resp.get("Buckets", [])]

    @property
    def bucket_name(self) -> str:
        """The configured bucket name (read from B2_BUCKET_NAME env var)."""
        return _bucket_name()