"""
MongoDB Engine for the Trend Agent Scheduler.

A self-contained, reusable engine that handles:

  1. **Connection lifecycle** — lazy singleton via ``MongoEngine.instance()``.
     Reads MONGO_URI / MONGO_USERNAME / MONGO_PASSWORD from environment
     (or accepts them as parameters).  Works with Atlas SRV URIs.

  2. **Auto-creation** — the first call to ``engine.ensure_collections()``
     creates the database, collections, indexes, and counters atomically.
     MongoDB creates databases and collections implicitly on first write;
     this method guarantees indexes exist *before* the app writes data.

  3. **Auto-incrementing integer IDs** — ``engine.next_id(collection_name)``
     uses an atomic ``find_one_and_update`` on a ``_counters`` collection
     so the rest of the codebase can keep using integer ``id`` fields
     (matching the SQLite convention).

  4. **Generic CRUD helpers** — ``insert``, ``find_one``, ``find_many``,
     ``update_one``, ``delete_one``, ``delete_many`` that strip ``_id``
     automatically so callers never see ObjectId.

  5. **Health check** — ``engine.ping()`` verifies connectivity.

Usage::

    from app.mongo_engine import MongoEngine

    engine = MongoEngine.instance()
    engine.ensure_collections()
    doc = engine.insert("scheduler_jobs", {"id": engine.next_id("scheduler_jobs"), ...})
    rows = engine.find_many("scheduler_jobs", sort=[("created_at", -1)])
"""

from __future__ import annotations

import os
from typing import Any, Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure

from dotenv import load_dotenv
import os

load_dotenv(override=True)  # Load .env variables into os.environ


# ── Collection schema registry ───────────────────────────────────────────
# Each entry declares the indexes that must exist for a collection.
# The engine creates them in ``ensure_collections()``.

COLLECTION_SCHEMA: dict[str, list[dict]] = {
    "scheduler_jobs": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_jobs_id"},
        {"keys": [("status", 1), ("next_run_at", 1)], "name": "idx_jobs_status_next"},
        {"keys": [("created_at", -1)], "name": "idx_jobs_created"},
    ],
    "execution_history": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_history_id"},
        {"keys": [("job_id", 1)], "name": "idx_history_job"},
        {"keys": [("started_at", -1)], "name": "idx_history_started"},
    ],
    "_counters": [
        # {"keys": [("_id", 1)], "unique": True, "name": "idx_counters_id"},
    ],
    # ── Blog metadata: tracks all generated articles across deploys ──
    "blog_metadata": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_blog_meta_id"},
        {"keys": [("run_id", 1)], "unique": True, "name": "idx_blog_meta_run_id"},
        {"keys": [("topic", 1)], "name": "idx_blog_meta_topic"},
        {"keys": [("status", 1)], "name": "idx_blog_meta_status"},
        {"keys": [("created_at", -1)], "name": "idx_blog_meta_created"},
    ],
    # ── Scheduler status: replaces scheduler_status.json ─────────────
    "scheduler_status": [
        # {"keys": [("_id", 1)], "unique": True, "name": "idx_sched_status_id"},
    ],
    # ── Run cache: replaces storage/cache/*.json files ───────────────
    "run_cache": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_run_cache_id"},
        {"keys": [("run_id", 1)], "name": "idx_run_cache_run_id"},
        {"keys": [("cache_type", 1)], "name": "idx_run_cache_type"},
        {"keys": [("run_id", 1), ("cache_type", 1)], "name": "idx_run_cache_run_type"},
    ],
    # ── Published topics: dedup check across deploys ─────────────────
    "published_topics": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_pub_topics_id"},
        {"keys": [("topic", 1)], "name": "idx_pub_topics_topic"},
        {"keys": [("published_at", -1)], "name": "idx_pub_topics_date"},
    ],
    # ── Editorial memory: replaces storage/memory/editorial_memory.jsonl
    "editorial_memory": [
        {"keys": [("id", 1)], "unique": True, "name": "idx_ed_mem_id"},
        {"keys": [("created_at", -1)], "name": "idx_ed_mem_created"},
    ],
}


class MongoEngine:
    """
    Lightweight MongoDB engine with connection management, auto-creation,
    and generic CRUD helpers.

    Use ``MongoEngine.instance()`` to get the singleton, or construct
    directly for testing.
    """

    _instance: Optional[MongoEngine] = None

    # ── Singleton ─────────────────────────────────────────────────────

    @classmethod
    def instance(
        cls,
        mongo_uri: str | None = None,
        db_name: str = "trend_scheduler",
    ) -> MongoEngine:
        """
        Return the global singleton engine.

        On the very first call the engine is created with the given
        parameters (or environment variables).  Subsequent calls ignore
        the parameters and return the existing instance.
        """
        if cls._instance is None:
            cls._instance = cls(mongo_uri=mongo_uri, db_name=db_name)
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
        mongo_uri: str | None = None,
        db_name: str = "trend_scheduler",
    ) -> None:
        # ── Resolve URI ──────────────────────────────────────────────
        self._uri = mongo_uri or os.getenv("MONGO_URI", "")
        if not self._uri:
            raise ValueError(
                "MongoDB URI not configured.  Set MONGO_URI in .env "
                "or pass mongo_uri to MongoEngine()."
            )

        # Inject separate username/password if they aren't already in the URI
        mongo_user = os.getenv("MONGO_USERNAME", "").strip()
        mongo_pass = os.getenv("MONGO_PASSWORD", "").strip()
        if mongo_user and mongo_pass and mongo_user not in self._uri:
            if "://" in self._uri:
                proto, rest = self._uri.split("://", 1)
                self._uri = f"{proto}://{mongo_user}:{mongo_pass}@{rest}"

        self._db_name = db_name
        self._client: Optional[MongoClient] = None
        self._initialized = False  # True after ensure_collections() succeeds

    # ── Connection ────────────────────────────────────────────────────

    @property
    def db(self):
        """
        Lazily connect and return the database handle.

        The actual TCP connection is deferred until the first operation,
        so constructing the engine is always safe even if MongoDB is
        temporarily down.
        """
        if self._client is None:
            self._client = MongoClient(
                self._uri,
                serverSelectionTimeoutMS=5000,
            )
        return self._client[self._db_name]

    def collection(self, name: str) -> Collection:
        """Return a collection handle by name."""
        return self.db[name]

    def close(self) -> None:
        """Close the underlying client (idempotent)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── Health check ──────────────────────────────────────────────────

    def ping(self) -> bool:
        """
        Verify connectivity to the MongoDB server.

        Returns True on success, False on failure.  Does not raise.
        """
        try:
            self.db.client.admin.command("ping")
            return True
        except ConnectionFailure:
            return False

    def get_server_info(self) -> dict[str, Any]:
        """Return server info dict (version, host, etc.).  Raises on failure."""
        return self.db.client.admin.command("buildInfo")

    # ── Auto-creation ─────────────────────────────────────────────────

    def ensure_collections(self) -> dict[str, str]:
        """
        Create all registered collections and their indexes if they
        don't already exist.

        MongoDB creates databases and collections lazily on first write,
        but indexes need to be created explicitly.  This method is
        idempotent — safe to call on every app startup.

        Returns a dict mapping collection names to "created" or "ok"
        (already existed with correct indexes).
        """
        result: dict[str, str] = {}
        db = self.db

        for col_name, index_specs in COLLECTION_SCHEMA.items():
            # Accessing the collection handle does NOT create it, but
            # creating an index on it will.  This is fine — empty
            # collections are harmless and will be cleaned up by MongoDB
            # TTL rules if never written to.
            col = db[col_name]

            existing_indexes = {idx["name"] for idx in col.list_indexes()}

            for spec in index_specs:
                idx_name = spec.get("name")
                if idx_name and idx_name not in existing_indexes:
                    col.create_index(
                        spec["keys"],
                        unique=spec.get("unique", False),
                        name=idx_name,
                    )

            result[col_name] = "ok"

        self._initialized = True
        return result

    def register_collection(self, name: str, indexes: list[dict]) -> None:
        """
        Register a custom collection with its index spec.

        Call this **before** ``ensure_collections()`` if you need
        additional collections beyond the built-in ones.

        Example::

            engine.register_collection("custom_logs", [
                {"keys": [("id", 1)], "unique": True, "name": "idx_logs_id"},
                {"keys": [("timestamp", -1)], "name": "idx_logs_ts"},
            ])
            engine.ensure_collections()
        """
        COLLECTION_SCHEMA[name] = indexes

    # ── Auto-incrementing IDs ─────────────────────────────────────────

    def next_id(self, collection_name: str) -> int:
        """
        Atomically increment and return the next integer ID for
        *collection_name*.

        Uses a ``_counters`` collection with documents like::

            { "_id": "scheduler_jobs", "seq": 42 }

        The first call for a collection returns 1.
        """
        doc = self.db["_counters"].find_one_and_update(
            {"_id": collection_name},
            {"$inc": {"seq": 1}},
            upsert=True,
        )
        if doc is None:
            # First insert — find_one_and_update returns None on upsert
            # but the document was created with seq=1.
            return 1
        return doc["seq"] + 1

    def current_id(self, collection_name: str) -> int:
        """Return the current counter value without incrementing (0 if unset)."""
        doc = self.db["_counters"].find_one({"_id": collection_name})
        return doc["seq"] if doc else 0

    # ── Generic CRUD helpers ──────────────────────────────────────────

    @staticmethod
    def _clean(doc: dict | None) -> dict:
        """Strip ``_id`` from a document so callers never see ObjectId."""
        if doc is None:
            return {}
        doc.pop("_id", None)
        return doc

    def insert(self, collection_name: str, document: dict) -> dict:
        """
        Insert a document and return it (without ``_id``).

        The caller is responsible for setting the ``id`` field via
        ``engine.next_id()`` before calling this.
        """
        col = self.collection(collection_name)
        col.insert_one(document)
        return self._clean(document)

    def find_one(self, collection_name: str, query: dict) -> dict | None:
        """Find a single document and return it without ``_id``."""
        col = self.collection(collection_name)
        doc = col.find_one(query)
        return self._clean(doc) if doc else None

    def find_many(
        self,
        collection_name: str,
        query: dict | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict]:
        """
        Find multiple documents.

        Args:
            collection_name: Target collection.
            query:           MongoDB query dict (default: {}).
            sort:            List of (field, direction) tuples, e.g. [("created_at", -1)].
            limit:           Max docs to return (0 = unlimited).
            skip:            Number of docs to skip.
        """
        col = self.collection(collection_name)
        cursor = col.find(query or {})

        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)

        return [self._clean(d) for d in cursor]

    def update_one(self, collection_name: str, query: dict, updates: dict) -> dict | None:
        """
        Update a single document with ``$set`` and return the updated doc
        (without ``_id``).  Returns None if no document matched.
        """
        col = self.collection(collection_name)
        col.update_one(query, {"$set": updates})
        doc = col.find_one(query)
        return self._clean(doc) if doc else None

    def delete_one(self, collection_name: str, query: dict) -> bool:
        """Delete a single document.  Returns True if anything was deleted."""
        col = self.collection(collection_name)
        result = col.delete_one(query)
        return result.deleted_count > 0

    def delete_many(self, collection_name: str, query: dict) -> int:
        """Delete all matching documents.  Returns the count deleted."""
        col = self.collection(collection_name)
        result = col.delete_many(query)
        return result.deleted_count

    def count(self, collection_name: str, query: dict | None = None) -> int:
        """Return the count of documents matching the query."""
        col = self.collection(collection_name)
        return col.count_documents(query or {})

    # ── Convenience ───────────────────────────────────────────────────

    @property
    def is_initialized(self) -> bool:
        """True if ``ensure_collections()`` has been called successfully."""
        return self._initialized

    def list_collections(self) -> list[str]:
        """Return the names of all collections in the current database."""
        return self.db.list_collection_names()

    def drop_database(self) -> None:
        """
        Drop the entire database.  **Use only in tests.**
        """
        self.db.client.drop_database(self._db_name)
        self._initialized = False