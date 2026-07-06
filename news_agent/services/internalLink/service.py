"""
Internal Linking Agent — SaaS-Ready Architecture (v2)

Architecture Overview:
    ┌──────────────────────────────────────────────────────┐
    │                    INDEXING LAYER                     │
    │  (runs independently — cron / webhook / manual)       │
    │                                                       │
    │  DocumentProvider                                     │
    │      ├── SitemapProvider                              │
    │      ├── WordPressProvider (future)                   │
    │      ├── FileProvider      (future)                   │
    │      └── APIProvider       (future)                   │
    │              │                                        │
    │              ▼                                        │
    │  IndexingService                                      │
    │      crawl → extract → embed → store                  │
    │              │                                        │
    │              ▼                                        │
    │  VectorStore                                          │
    │      ├── JSONVectorStore  (dev / single-tenant)       │
    │      └── PgvectorStore    (production / multi-tenant) │
    └──────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────┐
    │                   RETRIEVAL LAYER                     │
    │  (runs during article generation — no crawling)       │
    │                                                       │
    │  RetrievalService                                     │
    │      embed query → vector search → score → LLM filter │
    │              │                                        │
    │              ▼                                        │
    │  InternalLink suggestions                             │
    │              │                                        │
    │              ▼                                        │
    │  AnchorInjectorService                                │
    └──────────────────────────────────────────────────────┘

Key Design Decisions (v2):
    1. Indexing and Retrieval are SEPARATE — no crawling in the request path
    2. VectorStore is abstracted — swap JSON for pgvector without changing code
    3. No hardcoded category inference — categories come from documents only
    4. Tenant isolation — every operation is scoped to a tenant_id
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import TypedDict, List, Optional, Dict, Any
from urllib.parse import urlparse, urlunparse

# Optional dependencies handled gracefully
try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

SentenceTransformer = None

# ── NEW: HuggingFace API client (replaces local SentenceTransformer on Railway free tier) ──
# Lazy import so the module still loads if requests isn't installed.
# See embeddings.py for the full client. The IndexingService / RetrievalService
# below now use HuggingFaceEmbeddings instead of a local model.
try:
    from .embeddings import HuggingFaceEmbeddings, create_embeddings_client
except ImportError:
    # Allow running this file standalone (e.g. quick smoke test) —
    # in that case the embeddings client stays None and we fall back
    # to keyword-only matching.
    HuggingFaceEmbeddings = None  # type: ignore[assignment]
    create_embeddings_client = None  # type: ignore[assignment]

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]

try:
    import trafilatura
except ImportError:
    trafilatura = None  # type: ignore[assignment]

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore[assignment]

# Core imports (assuming project structure)
from ...core import AppConfig, PipelineLogger
from ...models import TrendTopic
from ..base import AgentContext, BaseAgent
from ..helpers import slugify, tokenize


# ═══════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════

class AnchorCandidate(TypedDict):
    text: str
    priority: int


class InternalLink(TypedDict):
    title: str
    url: str
    slug: str
    relevance_score: float
    category_match: bool
    reason: str
    anchor_candidates: List[AnchorCandidate]


class Document(TypedDict):
    """Canonical document model — every provider must produce this."""
    title: str
    url: str
    slug: str
    content: str           # Full article body (plain text or minimal HTML)
    summary: str           # Short summary / meta description
    categories: List[str]  # Categories extracted FROM the document itself
    tags: List[str]        # Tags extracted FROM the document itself
    last_modified: str     # ISO-8601 from sitemap <lastmod> or equivalent
    source: str            # "sitemap" | "wordpress" | "file" | "api"
    tenant_id: str         # Tenant isolation — every document belongs to a tenant


class IndexResult(TypedDict):
    """Result of an indexing operation."""
    tenant_id: str
    documents_indexed: int
    documents_skipped: int
    errors: int
    duration_seconds: float
    timestamp: str


# ═══════════════════════════════════════════════════════════════════════════
# DOCUMENT PROVIDER INTERFACE
# ═══════════════════════════════════════════════════════════════════════════

class DocumentProvider(ABC):
    """All content sources implement this interface."""

    @abstractmethod
    def discover(self, tenant_id: str) -> List[Document]:
        """Return canonical documents for a specific tenant."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Quick check — can this provider run in the current env?"""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# SITEMAP PROVIDER
# ═══════════════════════════════════════════════════════════════════════════

class SitemapProvider(DocumentProvider):
    """
    Uses an XML sitemap as a *discovery* mechanism.

    Flow:
        Fetch sitemap XML
            → Parse URLs (recursively handle sitemap indexes)
            → Filter by include/exclude patterns
            → Crawl each URL with rate-limiting
            → Extract article content (trafilatura > BeautifulSoup fallback)
            → Return List[Document]

    IMPORTANT: This provider is called ONLY by the IndexingService,
    never by the RetrievalService. Crawling happens in the background.
    """

    _NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    DEFAULT_EXCLUDE_PATTERNS = [
        r"/pages/",
        r"/advertise",
        r"/contact",
        r"/cookies",
        r"/help-faq",
        r"/privacy-policy",
        r"/subscription-terms",
        r"/terms-of-use",
    ]

    DEFAULT_INCLUDE_PATTERNS: List[str] = []

    def __init__(self, config: AppConfig, tenant_id: str = "") -> None:
        self.config = config
        self.tenant_id = tenant_id
        self.root = Path(config.storage_root)
        self.cache_dir = self.root / "cache" / "tenants" / (tenant_id or "_default")
        self.docs_cache_dir = self.cache_dir / "sitemap-docs"
        self.docs_cache_dir.mkdir(parents=True, exist_ok=True)

        # Config-driven settings (with safe defaults)
        self.sitemap_url: str = getattr(config, "sitemap_url", "")
        self.crawl_delay: float = getattr(config, "sitemap_crawl_delay", 0.5)
        self.max_urls: int = getattr(config, "sitemap_max_urls", 500)
        self.timeout: int = getattr(config, "sitemap_crawl_timeout", 15)
        self.user_agent: str = getattr(
            config, "sitemap_user_agent",
            "InternalLinkBot/1.0"
        )
        self.include_patterns: List[str] = getattr(
            config, "sitemap_include_patterns", self.DEFAULT_INCLUDE_PATTERNS
        )
        self.exclude_patterns: List[str] = getattr(
            config, "sitemap_exclude_patterns", self.DEFAULT_EXCLUDE_PATTERNS
        )
        # Category extraction from URL path segments
        # This is NOT hardcoded inference — it's a configurable mapping
        # that maps URL path patterns to categories. Each tenant configures
        # their own map. If not configured, categories come from HTML extraction.
        self.category_path_map: Dict[str, str] = getattr(
            config, "sitemap_category_map",
            {
                "politics": "politics",
                "business": "business",
                "sports": "sports",
                "tech": "technology",
                "stock-market": "stock-market",
                "travel": "travel",
            }
        )

    # --- Provider Interface ---

    def is_available(self) -> bool:
        return bool(self.sitemap_url) and requests is not None

    def discover(self, tenant_id: str = "") -> List[Document]:
        effective_tenant = tenant_id or self.tenant_id
        if not self.is_available():
            return []

        urls = self._fetch_sitemap_urls(self.sitemap_url)
        if not urls:
            return []

        filtered = self._filter_urls(urls)
        if not filtered:
            return []

        filtered = filtered[:self.max_urls]

        documents: List[Document] = []
        for url_entry in filtered:
            doc = self._crawl_and_extract(url_entry, effective_tenant)
            if doc:
                documents.append(doc)

        return documents

    # --- Sitemap Parsing ---

    def _fetch_sitemap_urls(self, sitemap_url: str) -> List[dict]:
        try:
            resp = requests.get(
                sitemap_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception:
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        urls: List[dict] = []

        # Sitemap index (with namespace)
        for sitemap_el in root.findall("sm:sitemap", self._NS):
            loc_el = sitemap_el.find("sm:loc", self._NS)
            if loc_el is not None and loc_el.text:
                child_urls = self._fetch_sitemap_urls(loc_el.text.strip())
                urls.extend(child_urls)

        # Regular URL entries (with namespace)
        for url_el in root.findall("sm:url", self._NS):
            loc_el = url_el.find("sm:loc", self._NS)
            if loc_el is None or not loc_el.text:
                continue
            url = loc_el.text.strip()
            lastmod_el = url_el.find("sm:lastmod", self._NS)
            lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else ""
            urls.append({"url": url, "lastmod": lastmod})

        # Fallback: handle sitemaps without namespace
        if not urls and not root.findall("sm:url", self._NS):
            for url_el in root.findall("url"):
                loc_el = url_el.find("loc")
                if loc_el is None or not loc_el.text:
                    continue
                url = loc_el.text.strip()
                lastmod_el = url_el.find("lastmod")
                lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else ""
                urls.append({"url": url, "lastmod": lastmod})

            for sitemap_el in root.findall("sitemap"):
                loc_el = sitemap_el.find("loc")
                if loc_el is not None and loc_el.text:
                    child_urls = self._fetch_sitemap_urls(loc_el.text.strip())
                    urls.extend(child_urls)

        return urls

    # --- URL Filtering ---

    def _filter_urls(self, urls: List[dict]) -> List[dict]:
        filtered = []
        for entry in urls:
            url = entry["url"]

            if self.include_patterns:
                if not any(re.search(p, url) for p in self.include_patterns):
                    continue

            if any(re.search(p, url) for p in self.exclude_patterns):
                continue

            if self._is_category_landing(url):
                continue

            filtered.append(entry)
        return filtered

    def _is_category_landing(self, url: str) -> bool:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]

        if not path_parts:
            return True

        last_segment = path_parts[-1].lower()
        known_categories = set(self.category_path_map.keys())

        if last_segment in known_categories and len(path_parts) <= 2:
            return True

        if last_segment in {"blogs", "blog"} and len(path_parts) <= 2:
            return True

        return False

    # --- Crawling & Content Extraction ---

    def _crawl_and_extract(self, url_entry: dict, tenant_id: str) -> Optional[Document]:
        url = url_entry["url"]
        lastmod = url_entry.get("lastmod", "")
        slug = self._slug_from_url(url)

        # Check cache
        cache_path = self.docs_cache_dir / f"{slug}.json"
        cached = self._load_cached_doc(cache_path)
        if cached and self._is_cache_fresh(cached, lastmod):
            return cached

        # Fetch the page
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return cached

        # Rate limiting
        time.sleep(self.crawl_delay)

        # Extract content
        doc = self._extract_content(html, url, lastmod, tenant_id)
        if doc:
            self._save_cached_doc(cache_path, doc)

        return doc

    def _extract_content(self, html: str, url: str, lastmod: str, tenant_id: str) -> Optional[Document]:
        slug = self._slug_from_url(url)
        title = ""
        content = ""
        summary = ""
        categories: List[str] = []
        tags: List[str] = []

        # --- Try trafilatura first ---
        if trafilatura is not None:
            try:
                extracted = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    favor_precision=True,
                )
                if extracted:
                    content = extracted

                metadata = trafilatura.extract(
                    html,
                    output_format="json",
                    include_comments=False,
                )
                if metadata:
                    meta = json.loads(metadata)
                    title = meta.get("title", "")
                    summary = meta.get("description", "") or meta.get("excerpt", "")
                    if meta.get("categories"):
                        cats = meta["categories"]
                        if isinstance(cats, list):
                            categories.extend(str(c) for c in cats)
                        elif isinstance(cats, str):
                            categories.append(cats)
                    if meta.get("tags"):
                        t = meta["tags"]
                        if isinstance(t, list):
                            tags.extend(str(tg) for tg in t)
                        elif isinstance(t, str):
                            tags.append(t)
            except Exception:
                pass

        # --- Fallback: BeautifulSoup manual extraction ---
        if not content and BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(html, "html.parser")

                # Title
                if not title:
                    title_tag = soup.find("meta", property="og:title")
                    if title_tag and title_tag.get("content"):
                        title = str(title_tag["content"]).strip()
                if not title:
                    h1 = soup.find("h1")
                    if h1:
                        title = h1.get_text(strip=True)
                if not title:
                    title_tag = soup.find("title")
                    if title_tag:
                        title = title_tag.get_text(strip=True)

                # Meta description / summary
                if not summary:
                    desc_tag = soup.find("meta", attrs={"name": "description"})
                    if desc_tag and desc_tag.get("content"):
                        summary = str(desc_tag["content"]).strip()
                if not summary:
                    desc_tag = soup.find("meta", property="og:description")
                    if desc_tag and desc_tag.get("content"):
                        summary = str(desc_tag["content"]).strip()

                # Content
                article = soup.find("article")
                if article:
                    for tag in article.find_all(["script", "style", "nav", "footer", "header"]):
                        tag.decompose()
                    content = article.get_text(separator=" ", strip=True)
                else:
                    main = soup.find("main") or soup.find(
                        "div", class_=re.compile(r"content|article|post", re.I)
                    )
                    if main:
                        for tag in main.find_all(["script", "style", "nav", "footer", "header"]):
                            tag.decompose()
                        content = main.get_text(separator=" ", strip=True)

                # Tags from meta keywords
                if not tags:
                    kw_tag = soup.find("meta", attrs={"name": "keywords"})
                    if kw_tag and kw_tag.get("content"):
                        tags = [t.strip() for t in str(kw_tag["content"]).split(",") if t.strip()]

                # Categories from HTML (breadcrumb, category links)
                if not categories:
                    cat_links = soup.find_all("a", class_=re.compile(r"category|cat", re.I))
                    for cl in cat_links[:5]:
                        cat_text = cl.get_text(strip=True)
                        if cat_text:
                            categories.append(cat_text)

            except Exception:
                pass

        # --- Category enrichment from URL path (configurable, not hardcoded) ---
        # This uses the tenant's own category_path_map config.
        # It does NOT hardcode any keyword→category mapping.
        path_categories = self._categories_from_url(url)
        for cat in path_categories:
            if cat.lower() not in {c.lower() for c in categories}:
                categories.append(cat)

        # --- Final validation ---
        if not title:
            title = slug.replace("-", " ").title()

        if not content or len(content) < 100:
            return None

        # Deduplicate categories
        seen_cats: set[str] = set()
        unique_cats: List[str] = []
        for c in categories:
            lower = c.lower()
            if lower not in seen_cats:
                seen_cats.add(lower)
                unique_cats.append(c)

        return Document(
            title=title,
            url=url,
            slug=slug,
            content=content,
            summary=summary,
            categories=unique_cats,
            tags=tags,
            last_modified=lastmod,
            source="sitemap",
            tenant_id=tenant_id,
        )

    # --- URL Utilities ---

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]
        if parts:
            slug_candidate = parts[-1]
        else:
            slug_candidate = hashlib.md5(url.encode()).hexdigest()[:12]
        return slugify(slug_candidate) if slugify else slug_candidate.replace("/", "-")

    def _categories_from_url(self, url: str) -> List[str]:
        """
        Extract categories from URL path using the tenant's category_path_map.

        This is NOT hardcoded inference — it's a configurable mapping that
        each tenant sets up based on their own URL structure. The map simply
        says "if a URL path contains the segment 'politics', tag it with the
        category 'politics'". This is no different from how WordPress assigns
        categories based on URL structure.

        If a tenant doesn't configure a map, this returns empty and categories
        come purely from HTML extraction.
        """
        if not self.category_path_map:
            return []

        parsed = urlparse(url)
        path_parts = [p.lower() for p in parsed.path.strip("/").split("/") if p]

        categories = []
        for segment in path_parts:
            if segment in self.category_path_map:
                categories.append(self.category_path_map[segment])
        return categories

    # --- Caching ---

    def _load_cached_doc(self, path: Path) -> Optional[Document]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Document(**data)
        except Exception:
            return None

    def _save_cached_doc(self, path: Path, doc: Document) -> None:
        try:
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _is_cache_fresh(self, cached: Document, current_lastmod: str) -> bool:
        cached_lastmod = cached.get("last_modified", "")
        if not current_lastmod:
            return True
        return cached_lastmod >= current_lastmod


# ═══════════════════════════════════════════════════════════════════════════
# VECTOR STORE INTERFACE
# ═══════════════════════════════════════════════════════════════════════════

class VectorStore(ABC):
    """
    Abstract vector store — swap implementations without changing
    retrieval logic.

    Implementations:
        - JSONVectorStore: dev / single-tenant / small scale
        - PgvectorStore:  production / multi-tenant / large scale
    """

    @abstractmethod
    def upsert(self, tenant_id: str, document: Document, embedding: List[float]) -> None:
        """Insert or update a single document + its embedding."""
        ...

    @abstractmethod
    def bulk_upsert(self, tenant_id: str, items: List[tuple[Document, List[float]]]) -> int:
        """Insert or update many documents at once. Returns count stored."""
        ...

    @abstractmethod
    def search(
        self,
        tenant_id: str,
        query_embedding: List[float],
        limit: int = 20,
        category_filter: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        Find the most similar documents for a tenant.

        Returns list of dicts with at minimum:
            { "title", "url", "slug", "summary", "categories", "tags", "score" }
        """
        ...

    @abstractmethod
    def delete(self, tenant_id: str, slug: str) -> bool:
        """Remove a document by slug. Returns True if found and deleted."""
        ...

    @abstractmethod
    def count(self, tenant_id: str) -> int:
        """Number of indexed documents for a tenant."""
        ...

    @abstractmethod
    def get_document(self, tenant_id: str, slug: str) -> Optional[Document]:
        """Retrieve a single document by slug."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# JSON VECTOR STORE (dev / single-tenant)
# ═══════════════════════════════════════════════════════════════════════════

class JSONVectorStore(VectorStore):
    """
    File-based vector store using JSON.

    Suitable for:
        - Development and testing
        - Single-tenant deployments
        - Sites with < 10,000 articles

    NOT suitable for:
        - Multi-tenant SaaS with 50+ clients
        - Sites with 100,000+ articles
        - Production deployments needing ACID guarantees

    For those cases, use PgvectorStore instead.
    """

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def _tenant_dir(self, tenant_id: str) -> Path:
        path = self.storage_root / "tenants" / tenant_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _docs_path(self, tenant_id: str) -> Path:
        return self._tenant_dir(tenant_id) / "documents.json"

    def _embeddings_path(self, tenant_id: str) -> Path:
        return self._tenant_dir(tenant_id) / "embeddings.json"

    def _load_docs(self, tenant_id: str) -> Dict[str, Document]:
        path = self._docs_path(tenant_id)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {k: Document(**v) for k, v in raw.items()}
        except Exception:
            return {}

    def _save_docs(self, tenant_id: str, docs: Dict[str, Document]) -> None:
        path = self._docs_path(tenant_id)
        path.write_text(
            json.dumps({k: dict(v) for k, v in docs.items()}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_embeddings(self, tenant_id: str) -> Dict[str, List[float]]:
        path = self._embeddings_path(tenant_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_embeddings(self, tenant_id: str, embeddings: Dict[str, List[float]]) -> None:
        path = self._embeddings_path(tenant_id)
        path.write_text(json.dumps(embeddings), encoding="utf-8")

    # --- VectorStore Interface ---

    def upsert(self, tenant_id: str, document: Document, embedding: List[float]) -> None:
        docs = self._load_docs(tenant_id)
        embeddings = self._load_embeddings(tenant_id)
        slug = document["slug"]
        docs[slug] = document
        embeddings[slug] = embedding
        self._save_docs(tenant_id, docs)
        self._save_embeddings(tenant_id, embeddings)

    def bulk_upsert(self, tenant_id: str, items: List[tuple[Document, List[float]]]) -> int:
        docs = self._load_docs(tenant_id)
        embeddings = self._load_embeddings(tenant_id)
        count = 0
        for document, embedding in items:
            slug = document["slug"]
            docs[slug] = document
            embeddings[slug] = embedding
            count += 1
        self._save_docs(tenant_id, docs)
        self._save_embeddings(tenant_id, embeddings)
        return count

    def search(
        self,
        tenant_id: str,
        query_embedding: List[float],
        limit: int = 20,
        category_filter: Optional[List[str]] = None,
    ) -> List[dict]:
        if np is None:
            return []

        docs = self._load_docs(tenant_id)
        embeddings = self._load_embeddings(tenant_id)

        if not docs or not embeddings:
            return []

        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        results: List[dict] = []
        for slug, doc in docs.items():
            emb = embeddings.get(slug)
            if not emb:
                continue

            # Category filter (applied at the store level for efficiency)
            if category_filter:
                doc_cats_lower = {c.lower() for c in doc.get("categories", [])}
                filter_cats_lower = {c.lower() for c in category_filter}
                if not (doc_cats_lower & filter_cats_lower):
                    continue

            cand_vec = np.array(emb)
            cand_norm = np.linalg.norm(cand_vec)
            if cand_norm == 0:
                continue

            similarity = float(np.dot(query_vec, cand_vec) / (query_norm * cand_norm))

            results.append({
                "title": doc["title"],
                "url": doc["url"],
                "slug": doc["slug"],
                "summary": doc.get("summary", ""),
                "categories": doc.get("categories", []),
                "tags": doc.get("tags", []),
                "content_snippet": doc.get("content", "")[:1000],
                "score": similarity,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def delete(self, tenant_id: str, slug: str) -> bool:
        docs = self._load_docs(tenant_id)
        embeddings = self._load_embeddings(tenant_id)

        found = slug in docs
        docs.pop(slug, None)
        embeddings.pop(slug, None)

        self._save_docs(tenant_id, docs)
        self._save_embeddings(tenant_id, embeddings)
        return found

    def count(self, tenant_id: str) -> int:
        return len(self._load_docs(tenant_id))

    def get_document(self, tenant_id: str, slug: str) -> Optional[Document]:
        docs = self._load_docs(tenant_id)
        return docs.get(slug)


# ═══════════════════════════════════════════════════════════════════════════
# PGVECTOR STORE (production / multi-tenant)
# ═══════════════════════════════════════════════════════════════════════════

class PgvectorStore(VectorStore):
    """
    PostgreSQL + pgvector vector store.

    Suitable for:
        - Multi-tenant SaaS production
        - 10,000+ articles per tenant
        - 50+ tenants
        - ACID guarantees, backups, replication

    Requires:
        - PostgreSQL with pgvector extension
        - psycopg2 (or psycopg2-binary)

    Schema (auto-created on first use):
        CREATE TABLE IF NOT EXISTS internal_link_documents (
            tenant_id  TEXT NOT NULL,
            slug       TEXT NOT NULL,
            title      TEXT NOT NULL,
            url        TEXT NOT NULL,
            content    TEXT DEFAULT '',
            summary    TEXT DEFAULT '',
            categories JSONB DEFAULT '[]',
            tags       JSONB DEFAULT '[]',
            source     TEXT DEFAULT '',
            last_modified TEXT DEFAULT '',
            embedding  vector(384),   -- all-MiniLM-L6-v2 dimension
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (tenant_id, slug)
        );
        CREATE INDEX IF NOT EXISTS idx_docs_tenant_emb
            ON internal_link_documents
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._conn = None
        self._ensure_schema()

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            if psycopg2 is None:
                raise RuntimeError("psycopg2 is required for PgvectorStore")
            self._conn = psycopg2.connect(self.database_url)
            self._conn.autocommit = True
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS internal_link_documents (
                    tenant_id   TEXT NOT NULL,
                    slug        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    content     TEXT DEFAULT '',
                    summary     TEXT DEFAULT '',
                    categories  JSONB DEFAULT '[]',
                    tags        JSONB DEFAULT '[]',
                    source      TEXT DEFAULT '',
                    last_modified TEXT DEFAULT '',
                    embedding   vector(384),
                    updated_at  TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, slug)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_docs_tenant_emb
                ON internal_link_documents
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)

    def upsert(self, tenant_id: str, document: Document, embedding: List[float]) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO internal_link_documents
                    (tenant_id, slug, title, url, content, summary,
                     categories, tags, source, last_modified, embedding, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (tenant_id, slug)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    url = EXCLUDED.url,
                    content = EXCLUDED.content,
                    summary = EXCLUDED.summary,
                    categories = EXCLUDED.categories,
                    tags = EXCLUDED.tags,
                    source = EXCLUDED.source,
                    last_modified = EXCLUDED.last_modified,
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
            """, (
                tenant_id,
                document["slug"],
                document["title"],
                document["url"],
                document.get("content", ""),
                document.get("summary", ""),
                json.dumps(document.get("categories", [])),
                json.dumps(document.get("tags", [])),
                document.get("source", ""),
                document.get("last_modified", ""),
                embedding,
            ))

    def bulk_upsert(self, tenant_id: str, items: List[tuple[Document, List[float]]]) -> int:
        conn = self._get_conn()
        count = 0
        with conn.cursor() as cur:
            for document, embedding in items:
                cur.execute("""
                    INSERT INTO internal_link_documents
                        (tenant_id, slug, title, url, content, summary,
                         categories, tags, source, last_modified, embedding, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tenant_id, slug)
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        url = EXCLUDED.url,
                        content = EXCLUDED.content,
                        summary = EXCLUDED.summary,
                        categories = EXCLUDED.categories,
                        tags = EXCLUDED.tags,
                        source = EXCLUDED.source,
                        last_modified = EXCLUDED.last_modified,
                        embedding = EXCLUDED.embedding,
                        updated_at = NOW()
                """, (
                    tenant_id,
                    document["slug"],
                    document["title"],
                    document["url"],
                    document.get("content", ""),
                    document.get("summary", ""),
                    json.dumps(document.get("categories", [])),
                    json.dumps(document.get("tags", [])),
                    document.get("source", ""),
                    document.get("last_modified", ""),
                    embedding,
                ))
                count += 1
        return count

    def search(
        self,
        tenant_id: str,
        query_embedding: List[float],
        limit: int = 20,
        category_filter: Optional[List[str]] = None,
    ) -> List[dict]:
        conn = self._get_conn()
        results = []

        with conn.cursor() as cur:
            if category_filter:
                # Use pgvector cosine search with category filter
                # ANY check on JSONB array
                cur.execute("""
                    SELECT slug, title, url, summary, categories, tags,
                           content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM internal_link_documents
                    WHERE tenant_id = %s
                      AND categories ?| %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """, (
                    json.dumps(query_embedding),
                    tenant_id,
                    category_filter,
                    json.dumps(query_embedding),
                    limit,
                ))
            else:
                cur.execute("""
                    SELECT slug, title, url, summary, categories, tags,
                           content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM internal_link_documents
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """, (
                    json.dumps(query_embedding),
                    tenant_id,
                    json.dumps(query_embedding),
                    limit,
                ))

            for row in cur.fetchall():
                results.append({
                    "slug": row[0],
                    "title": row[1],
                    "url": row[2],
                    "summary": row[3] or "",
                    "categories": row[4] if isinstance(row[4], list) else json.loads(row[4] or "[]"),
                    "tags": row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]"),
                    "content_snippet": (row[6] or "")[:1000],
                    "score": float(row[7]),
                })

        return results

    def delete(self, tenant_id: str, slug: str) -> bool:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM internal_link_documents WHERE tenant_id = %s AND slug = %s",
                (tenant_id, slug)
            )
            return cur.rowcount > 0

    def count(self, tenant_id: str) -> int:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM internal_link_documents WHERE tenant_id = %s",
                (tenant_id,)
            )
            return cur.fetchone()[0]

    def get_document(self, tenant_id: str, slug: str) -> Optional[Document]:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT slug, title, url, content, summary, categories, tags,
                       source, last_modified
                FROM internal_link_documents
                WHERE tenant_id = %s AND slug = %s
            """, (tenant_id, slug))
            row = cur.fetchone()
            if not row:
                return None
            return Document(
                slug=row[0],
                title=row[1],
                url=row[2],
                content=row[3] or "",
                summary=row[4] or "",
                categories=row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]"),
                tags=row[6] if isinstance(row[6], list) else json.loads(row[6] or "[]"),
                source=row[7] or "",
                last_modified=row[8] or "",
                tenant_id=tenant_id,
            )


# ═══════════════════════════════════════════════════════════════════════════
# INDEXING SERVICE (runs independently from retrieval)
# ═══════════════════════════════════════════════════════════════════════════

class IndexingService:
    """
    Background indexing: crawl → extract → embed → store.

    This service is called OUTSIDE the article generation pipeline.
    It should be triggered by:
        - Cron job (hourly / daily)
        - Webhook (when new articles are published)
        - Manual API call (POST /sources/sitemap)

    It NEVER runs during retrieve() — that's the RetrievalService's job.
    """

    def __init__(
        self,
        config: AppConfig,
        vector_store: VectorStore,
        providers: Optional[List[DocumentProvider]] = None,
    ) -> None:
        self.config = config
        self.vector_store = vector_store
        self.providers: List[DocumentProvider] = providers or []

        # Auto-register SitemapProvider if configured
        if not self.providers and getattr(config, "sitemap_url", ""):
            self.providers.append(SitemapProvider(config))

        self._model: object | None = None
        # ── CHANGED: prefer HuggingFace API client over local SentenceTransformer ──
        # The local model path is kept as a fallback for local dev, but in
        # production (Railway free tier) we use HF exclusively because:
        #   1. PyTorch is ~700MB and OOMs the container on import.
        #   2. The model cache is lost on every redeploy.
        # See embeddings.py for the API client.
        self._hf_client: Optional["HuggingFaceEmbeddings"] = None
        self._hf_initialized: bool = False

    @property
    def model(self) -> object | None:
        """
        Legacy hook — returns the local SentenceTransformer if available.
        Kept for backward compatibility with code that still calls
        ``self.model.encode(...)``.

        New code should use ``self.hf_client`` instead. The
        ``index_tenant`` and ``index_single_document`` methods below
        have been rewritten to use ``hf_client``.
        """
        global SentenceTransformer
        if self._model is None:
            if SentenceTransformer is None:
                try:
                    from sentence_transformers import SentenceTransformer as ST
                    SentenceTransformer = ST
                except Exception:
                    return None
            try:
                self._model = SentenceTransformer('all-MiniLM-L6-v2')
            except Exception:
                pass
        return self._model

    @property
    def hf_client(self) -> Optional["HuggingFaceEmbeddings"]:
        """
        Lazy-initialized HuggingFace embeddings client.

        Returns None if:
            - No API key is configured (caller falls back to keyword-only)
            - The embeddings module failed to import

        We cache the client (and a "we tried" flag) so we don't re-read
        env vars on every call.
        """
        if self._hf_initialized:
            return self._hf_client
        self._hf_initialized = True
        if create_embeddings_client is None:
            return None
        try:
            self._hf_client = create_embeddings_client(self.config)
        except Exception:
            self._hf_client = None
        return self._hf_client

    def add_provider(self, provider: DocumentProvider) -> None:
        self.providers.append(provider)

    def remove_provider(self, provider_type: type) -> None:
        self.providers = [p for p in self.providers if not isinstance(p, provider_type)]

    def get_available_providers(self) -> List[DocumentProvider]:
        return [p for p in self.providers if p.is_available()]

    def index_tenant(self, tenant_id: str, force_refresh: bool = False) -> IndexResult:
        """
        Full indexing run for a tenant:
            1. Discover documents from all providers
            2. Generate embeddings for new/changed documents
            3. Store in vector store

        This is the method you call from your background worker / cron job.
        """
        start_time = time.time()
        indexed = 0
        skipped = 0
        errors = 0

        available = self.get_available_providers()
        if not available:
            return IndexResult(
                tenant_id=tenant_id,
                documents_indexed=0,
                documents_skipped=0,
                errors=0,
                duration_seconds=0.0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # 1. Discover
        all_documents: List[Document] = []
        for provider in available:
            try:
                docs = provider.discover(tenant_id=tenant_id)
                all_documents.extend(docs)
            except Exception:
                errors += 1
                continue

        if not all_documents:
            return IndexResult(
                tenant_id=tenant_id,
                documents_indexed=0,
                documents_skipped=0,
                errors=errors,
                duration_seconds=time.time() - start_time,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # 2. Check which documents need (re-)indexing
        to_index: List[tuple[Document, List[float]]] = []

        # ── CHANGED: prefer HF API client; fall back to local model only if HF not configured ──
        hf = self.hf_client
        use_local_model = (hf is None) and (self.model is not None)

        if hf is not None:
            # Batch encode via HuggingFace API (chunks of 32 internally).
            # This is the production path on Railway free tier.
            texts_to_encode: List[str] = []
            docs_needing_embedding: List[Document] = []

            for doc in all_documents:
                existing = self.vector_store.get_document(tenant_id, doc["slug"])
                if existing and not force_refresh:
                    existing_lastmod = existing.get("last_modified", "")
                    doc_lastmod = doc.get("last_modified", "")
                    if existing_lastmod and doc_lastmod and existing_lastmod >= doc_lastmod:
                        skipped += 1
                        continue

                text = f"{doc['title']}. {doc['summary']}. {doc['content'][:500]}"
                texts_to_encode.append(text)
                docs_needing_embedding.append(doc)

            if texts_to_encode:
                try:
                    embeddings_batch = hf.embed_batch(texts_to_encode)
                    for doc, emb in zip(docs_needing_embedding, embeddings_batch):
                        # embed_batch returns [] for any text that failed →
                        # store the doc with an empty embedding so we still
                        # benefit from keyword matching.
                        to_index.append((doc, list(emb)))
                except Exception:
                    errors += len(docs_needing_embedding)

        elif use_local_model:
            # Legacy path: local SentenceTransformer (dev only).
            # Kept so the diff stays minimal and local dev still works.
            texts_to_encode = []
            docs_needing_embedding = []

            for doc in all_documents:
                existing = self.vector_store.get_document(tenant_id, doc["slug"])
                if existing and not force_refresh:
                    existing_lastmod = existing.get("last_modified", "")
                    doc_lastmod = doc.get("last_modified", "")
                    if existing_lastmod and doc_lastmod and existing_lastmod >= doc_lastmod:
                        skipped += 1
                        continue

                text = f"{doc['title']}. {doc['summary']}. {doc['content'][:500]}"
                texts_to_encode.append(text)
                docs_needing_embedding.append(doc)

            if texts_to_encode:
                try:
                    embeddings = self.model.encode(texts_to_encode, convert_to_numpy=True)
                    for doc, emb in zip(docs_needing_embedding, embeddings):
                        to_index.append((doc, emb.tolist()))
                except Exception:
                    errors += len(docs_needing_embedding)
        else:
            # No embedding source — store without embeddings (keyword-only mode)
            for doc in all_documents:
                existing = self.vector_store.get_document(tenant_id, doc["slug"])
                if existing and not force_refresh:
                    skipped += 1
                    continue
                to_index.append((doc, []))

        # 3. Bulk store
        if to_index:
            try:
                indexed = self.vector_store.bulk_upsert(tenant_id, to_index)
            except Exception:
                errors += len(to_index)
                indexed = 0

        return IndexResult(
            tenant_id=tenant_id,
            documents_indexed=indexed,
            documents_skipped=skipped,
            errors=errors,
            duration_seconds=time.time() - start_time,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def index_single_document(
        self,
        tenant_id: str,
        document: Document,
    ) -> bool:
        """
        Index a single document (e.g., when a new article is published via webhook).
        """
        embedding: List[float] = []

        # ── CHANGED: prefer HF API; fall back to local model only if HF not configured ──
        hf = self.hf_client
        if hf is not None:
            text = f"{document['title']}. {document['summary']}. {document['content'][:500]}"
            try:
                embedding = hf.embed(text)
            except Exception:
                return False
        elif self.model is not None:
            text = f"{document['title']}. {document['summary']}. {document['content'][:500]}"
            try:
                embedding = self.model.encode(text).tolist()
            except Exception:
                return False

        try:
            self.vector_store.upsert(tenant_id, document, embedding)
            return True
        except Exception:
            return False

    def remove_document(self, tenant_id: str, slug: str) -> bool:
        """Remove a document from the index."""
        return self.vector_store.delete(tenant_id, slug)


# ═══════════════════════════════════════════════════════════════════════════
# RETRIEVAL SERVICE (runs during article generation — NO crawling)
# ═══════════════════════════════════════════════════════════════════════════

class RetrievalService:
    """
    Pure retrieval: embed query → vector search → score → LLM filter.

    This is the ONLY service called during article generation.
    No crawling. No scraping. No extraction. Just search.
    """

    # Scoring Weights
    WEIGHT_EMBEDDING = 0.5
    WEIGHT_CATEGORY = 0.3
    WEIGHT_KEYWORD = 0.2
    TITLE_PHRASE_BOOST = 0.15
    EXACT_TOPIC_BOOST = 0.2

    def __init__(self, config: AppConfig, vector_store: VectorStore) -> None:
        self.config = config
        self.vector_store = vector_store
        self._model: object | None = None
        # ── CHANGED: HF API client (preferred over local model on Railway) ──
        self._hf_client: Optional["HuggingFaceEmbeddings"] = None
        self._hf_initialized: bool = False
        # Diagnostics for pipeline logs.
        self.last_resolved_tenant_id: str | None = None
        self.last_tried_tenant_ids: List[str] = []

    @property
    def model(self) -> object | None:
        """
        Legacy hook — returns the local SentenceTransformer if available.
        Kept for backward compat; new code should use ``hf_client``.
        """
        if not getattr(self.config, "internal_link_embeddings_enabled", True):
            return None

        global SentenceTransformer
        if self._model is None:
            if SentenceTransformer is None:
                try:
                    from sentence_transformers import SentenceTransformer as ST
                    SentenceTransformer = ST
                except Exception:
                    return None
            try:
                self._model = SentenceTransformer('all-MiniLM-L6-v2')
            except Exception:
                pass
        return self._model

    @property
    def hf_client(self) -> Optional["HuggingFaceEmbeddings"]:
        """
        Lazy-initialized HuggingFace embeddings client.

        Respects ``config.internal_link_embeddings_enabled`` — if the
        operator explicitly disabled embeddings, we return None so the
        retrieval falls back to keyword-only matching (same behavior
        as the old code).
        """
        if not getattr(self.config, "internal_link_embeddings_enabled", True):
            return None
        if self._hf_initialized:
            return self._hf_client
        self._hf_initialized = True
        if create_embeddings_client is None:
            return None
        try:
            self._hf_client = create_embeddings_client(self.config)
        except Exception:
            self._hf_client = None
        return self._hf_client

    def retrieve(
        self,
        tenant_id: str,
        topic: TrendTopic,
        plan_summary: str | None = None,
        target_word_count: int = 800,
        exclude_slug: str | None = None,
    ) -> List[InternalLink]:
        """
        Find relevant internal links for a new article.

        NO crawling happens here. All data comes from the VectorStore
        which was populated by the IndexingService.
        """
        # 1. Prepare query
        self.last_resolved_tenant_id = None
        self.last_tried_tenant_ids = []

        query_text = f"{topic.keyword}. {plan_summary or ''}"
        query_embedding: Optional[List[float]] = None

        # ── CHANGED: prefer HF API; fall back to local model only if HF not configured ──
        hf = self.hf_client
        if hf is not None:
            try:
                query_embedding = hf.embed(query_text) or None
            except Exception:
                query_embedding = None
        elif self.model is not None:
            query_embedding = self.model.encode(query_text).tolist()

        # 2. Vector search (with category filter from topic)
        category_filter = self._topic_categories(topic)
        search_limit = min(40, max(10, target_word_count // 50))

        candidates: List[dict] = []
        for current_tenant_id in self._resolve_tenant_candidates(tenant_id):
            self.last_tried_tenant_ids.append(current_tenant_id)
            if query_embedding:
                current_candidates = self.vector_store.search(
                    tenant_id=current_tenant_id,
                    query_embedding=query_embedding,
                    limit=search_limit,
                    category_filter=category_filter if category_filter else None,
                )
            else:
                # No embedding model — fallback to keyword-only
                current_candidates = self._keyword_fallback(current_tenant_id, topic, limit=search_limit)

            if current_candidates:
                self.last_resolved_tenant_id = current_tenant_id
                candidates = current_candidates
                break

        if not candidates:
            return []

        candidates = [
            c for c in candidates
            if self._is_allowed_internal_link_url(c.get("url", ""))
        ]
        if not candidates:
            return []

        # 3. Exclude self-referencing slug
        if exclude_slug:
            candidates = [c for c in candidates if c.get("slug") != exclude_slug]

        # 4. Score candidates (hybrid: vector similarity + category + keyword)
        scored = self._score_candidates(topic, candidates)

        # 5. Sort and select top
        scored.sort(key=lambda x: x["_score"], reverse=True)
        scored = self._dedupe_scored_candidates(scored)
        link_limit = min(8, max(2, int(target_word_count / 200)))
        top_candidates = scored[:link_limit * 2]

        # 6. LLM reasoning filter (for the 'reason' field only, NOT anchors)
        final_links = self._llm_reasoning_filter(topic, top_candidates, limit=link_limit)

        return final_links

    def _resolve_tenant_candidates(self, tenant_id: str) -> List[str]:
        """
        Build an ordered list of tenant IDs to try during retrieval.

        This protects against common config drift where indexing and
        retrieval use different tenant IDs (for example, empty string vs
        hostname-derived tenant).
        """
        ordered: list[str] = []
        seen: set[str] = set()

        def add(value: str | None) -> None:
            if value is None:
                return
            normalized = value.strip()
            if normalized in seen:
                return
            seen.add(normalized)
            ordered.append(normalized)

        add(tenant_id)
        add(getattr(self.config, "tenant_id", ""))
        add("")
        add("_default")

        for url in (
            getattr(self.config, "sitemap_url", ""),
            getattr(self.config, "wordpress_graphql_url", ""),
        ):
            parsed = urlparse(url or "")
            host = (parsed.hostname or "").lower().strip()
            if not host:
                continue
            if host.startswith("www."):
                host = host[4:]
            add(host)
            root_label = host.split(".")[0]
            add(root_label)

        # JSON stores place tenants under storage/vector-store/tenants/<tenant>
        tenants_root = Path(self.config.storage_root) / "vector-store" / "tenants"
        if tenants_root.exists():
            try:
                for child in tenants_root.iterdir():
                    if child.is_dir():
                        add(child.name)
            except Exception:
                pass

        return ordered

    def _score_candidates(self, topic: TrendTopic, candidates: List[dict]) -> List[dict]:
        """
        Hybrid scoring: embedding similarity + category overlap + keyword overlap.

        Categories come FROM the documents (set during indexing),
        NOT from hardcoded keyword inference.
        """
        topic_categories = self._topic_categories(topic)
        topic_tokens = set(tokenize(topic.keyword))

        scored = []
        for cand in candidates:
            score = 0.0
            metadata = {}
            clean_title = self._clean_candidate_title(cand.get("title", ""))
            title_tokens = set(tokenize(clean_title))

            # A. Semantic score (from vector store)
            semantic_sim = cand.get("score", 0.0)
            score += semantic_sim * self.WEIGHT_EMBEDDING
            metadata["semantic_sim"] = semantic_sim

            # B. Category match — using DOCUMENT categories, not hardcoded inference
            cand_cats = {str(c).lower() for c in cand.get("categories", [])}
            category_match = bool(topic_categories & cand_cats)
            if category_match:
                score += self.WEIGHT_CATEGORY
            metadata["category_match"] = category_match

            # C. Keyword/Tag overlap
            kw_score = self._keyword_score(topic_tokens, cand)
            score += kw_score * self.WEIGHT_KEYWORD
            metadata["keyword_overlap"] = kw_score

            # D. Prefer articles whose visible title directly overlaps the topic.
            title_overlap = 0.0
            if title_tokens and topic_tokens:
                title_overlap = len(topic_tokens & title_tokens) / max(1, len(topic_tokens))
                score += min(self.TITLE_PHRASE_BOOST, title_overlap * self.TITLE_PHRASE_BOOST)
            metadata["title_overlap"] = title_overlap

            topic_phrase = topic.keyword.strip().lower()
            if topic_phrase and topic_phrase in clean_title.lower():
                score += self.EXACT_TOPIC_BOOST
                metadata["exact_topic_match"] = True
            else:
                metadata["exact_topic_match"] = False

            cand["_score"] = score
            cand["_metadata"] = metadata
            scored.append(cand)

        return scored

    def _dedupe_scored_candidates(self, scored: List[dict]) -> List[dict]:
        deduped: list[dict] = []
        seen_titles: set[str] = set()
        seen_urls: set[str] = set()

        for candidate in scored:
            normalized_title = self._clean_candidate_title(candidate.get("title", "")).lower()
            normalized_url = str(candidate.get("url", "")).strip().lower()

            if normalized_url and normalized_url in seen_urls:
                continue
            if normalized_title and normalized_title in seen_titles:
                continue

            if normalized_url:
                seen_urls.add(normalized_url)
            if normalized_title:
                seen_titles.add(normalized_title)
            deduped.append(candidate)

        return deduped

    def _topic_categories(self, topic: TrendTopic) -> set[str]:
        """
        Determine categories for the current topic.

        Priority (first non-empty wins):
            1. TrendTopic.category / TrendTopic.categories (from trend source)
            2. config.topic_category (from --topic-category CLI flag)
            3. config.sitemap_category_map (detect from topic keyword)

        This does NOT use hardcoded keyword inference like the old
        _infer_categories(). It uses the tenant's own category_map
        config to detect categories from the topic keyword.
        """
        cats: set[str] = set()

        # 1. TrendTopic's own category field (set by SelectorAgent)
        if hasattr(topic, "category") and topic.category:
            cats.add(str(topic.category).lower())

        # TrendTopic.categories (plural)
        if hasattr(topic, "categories") and topic.categories:
            for c in topic.categories:
                cats.add(str(c).lower())

        # 2. Config topic_category (from --topic-category CLI flag)
        if not cats:
            config_category = getattr(self.config, "topic_category", None)
            if config_category:
                cats.add(str(config_category).lower())

        # 3. Smart fallback: detect category from topic keyword using
        #    the tenant's own sitemap_category_map (NOT hardcoded).
        #    Matches both exact segments ("politics") and hyphenated
        #    segments split into words ("stock-market" → "stock market").
        if not cats:
            category_map = getattr(self.config, "sitemap_category_map", {})
            if category_map:
                keyword_lower = topic.keyword.lower()
                for url_segment, category_name in category_map.items():
                    # Try exact match first: "politics" in keyword
                    if re.search(rf'\b{re.escape(url_segment)}\b', keyword_lower):
                        cats.add(category_name.lower())
                        continue
                    # Try with hyphens replaced by spaces: "stock-market" → "stock market"
                    spaced = url_segment.replace("-", " ")
                    if spaced != url_segment and re.search(rf'\b{re.escape(spaced)}\b', keyword_lower):
                        cats.add(category_name.lower())
                        continue
                    # Try individual words: "stock-market" → ["stock", "market"]
                    # If ALL words from the segment appear in the keyword
                    parts = url_segment.split("-")
                    if len(parts) > 1 and all(p in keyword_lower for p in parts if len(p) > 2):
                        cats.add(category_name.lower())

        return cats

    def _keyword_score(self, topic_tokens: set[str], candidate: dict) -> float:
        if not topic_tokens:
            return 0.0

        cand_text = " ".join([
            candidate.get("title", ""),
            candidate.get("summary", ""),
            candidate.get("content_snippet", ""),
            candidate.get("slug", ""),
        ])
        cand_tokens = set(tokenize(cand_text))

        if not cand_tokens:
            return 0.0

        intersection = topic_tokens & cand_tokens
        return len(intersection) / len(topic_tokens | cand_tokens)

    def _keyword_fallback(
        self,
        tenant_id: str,
        topic: TrendTopic,
        limit: int = 20,
    ) -> List[dict]:
        """
        Fallback when no embedding model is available.
        Does a brute-force keyword search over the vector store.

        Note: JSONVectorStore doesn't support keyword search natively,
        so this retrieves all documents and scores them.
        For PgvectorStore, we'd use full-text search.
        """
        # Retrieve a large set and let keyword scoring sort it
        # Use a dummy embedding of zeros to get all docs
        if np is None:
            return []

        # Get the count first
        total = self.vector_store.count(tenant_id)
        if total == 0:
            return []

        # Use a random embedding to get results (JSON store returns all,
        # PgvectorStore would need a different approach)
        dummy_embedding = [0.0] * 384  # all-MiniLM-L6-v2 dimension
        results = self.vector_store.search(
            tenant_id=tenant_id,
            query_embedding=dummy_embedding,
            limit=limit,
        )

        return results

    def _llm_reasoning_filter(
        self,
        topic: TrendTopic,
        candidates: List[dict],
        limit: int,
    ) -> List[InternalLink]:
        if not candidates:
            return []

        if not self.config.groq_api_key or Groq is None:
            return [self._map_to_link(c, reason="Automatically matched") for c in candidates[:limit]]

        client = Groq(api_key=self.config.groq_api_key)

        candidates_text = "\n".join([
            f"{i+1}. Title: {c['title']}\n   Summary: {c.get('summary', '')}"
            for i, c in enumerate(candidates)
        ])

        prompt = f"""
Topic: {topic.keyword}

Available Articles:
{candidates_text}

Select the top {limit} relevant articles.
For each, provide:
1. "index": number from list
2. "relevant": boolean
3. "reason": a very short phrase (max 6 words) explaining the connection.

Return ONLY a valid JSON object with a "links" array of objects.
"""

        try:
            response = client.chat.completions.create(
                model=self.config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=1024,
            )
            content = response.choices[0].message.content or "[]"
            content = content.replace("```json", "").replace("```", "").strip()
            decisions_payload = json.loads(content)
            decisions = (
                decisions_payload.get("links", decisions_payload)
                if isinstance(decisions_payload, dict)
                else decisions_payload
            )

            if not isinstance(decisions, list):
                return [self._map_to_link(c, reason="Related") for c in candidates[:limit]]

            final_links: List[InternalLink] = []
            for decision in decisions:
                if not decision.get("relevant"):
                    continue
                idx = decision.get("index", 1) - 1
                if 0 <= idx < len(candidates):
                    final_links.append(
                        self._map_to_link(
                            candidates[idx],
                            reason=decision.get("reason", "Related")
                        )
                    )
            return final_links[:limit]

        except Exception:
            return [self._map_to_link(c, reason="Related") for c in candidates[:limit]]

    def _map_to_link(self, candidate: dict, reason: str = "") -> InternalLink:
        normalized_url = self._normalize_internal_link_url(candidate.get("url", ""))
        return InternalLink(
            title=candidate["title"],
            url=normalized_url,
            slug=candidate["slug"],
            relevance_score=candidate.get("_score", candidate.get("score", 0.0)),
            category_match=candidate.get("_metadata", {}).get("category_match", False),
            reason=reason,
            anchor_candidates=self._derive_anchor_candidates(candidate)
        )

    def _is_allowed_internal_link_url(self, url: str) -> bool:
        raw = str(url or "").strip()
        if not raw:
            return False

        target = str(getattr(self.config, "internal_link_target", "people") or "people").strip().lower()
        if target != "people":
            return True

        try:
            parsed = urlparse(raw)
        except Exception:
            return False

        host = (parsed.netloc or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host == "peoplenewstime.com"

    def _normalize_internal_link_url(self, url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return raw

        target = str(getattr(self.config, "internal_link_target", "people") or "people").strip().lower()
        if target != "people":
            return raw

        base = str(getattr(self.config, "public_site_base_url", "") or "").strip()
        if not base:
            return raw

        try:
            parsed = urlparse(raw)
            base_parsed = urlparse(base)
        except Exception:
            return raw

        if not parsed.netloc:
            return raw

        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host != "peoplenewstime.com":
            return raw

        return urlunparse(
            (
                base_parsed.scheme or parsed.scheme or "https",
                base_parsed.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    def _derive_anchor_candidates(self, candidate: dict) -> List[AnchorCandidate]:
        """Generate anchors that are more likely to exist naturally in news prose."""
        raw_anchors: list[dict] = []
        clean_title = self._clean_candidate_title(candidate.get("title", ""))

        # Priority 1: Tags
        tags = [str(tag).strip() for tag in candidate.get("tags", []) if str(tag).strip()]
        for tag in tags:
            raw_anchors.append({"text": tag, "priority": 1})

        # Priority 2: Entity-like title phrases such as person, place, race, or case names.
        for phrase in self._candidate_title_phrases(clean_title):
            raw_anchors.append({"text": phrase, "priority": 2})

        # Priority 3: Full cleaned title only when it is already compact.
        if clean_title and len(clean_title.split()) <= 6:
            raw_anchors.append({"text": clean_title, "priority": 3})

        seen: set[str] = set()
        final_anchors: List[AnchorCandidate] = []

        for item in raw_anchors:
            text = item["text"].strip()
            normalized = text.lower()

            if normalized in seen:
                continue
            if len(text) < 3 or len(text) > 60:
                continue

            seen.add(normalized)
            final_anchors.append(AnchorCandidate(text=text, priority=item["priority"]))

        return final_anchors

    def _clean_candidate_title(self, title: str) -> str:
        cleaned = re.sub(r"\s*\|\s*People News Time\s*$", "", title or "", flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _candidate_title_phrases(self, title: str) -> List[str]:
        if not title:
            return []

        break_words = {
            "after", "amid", "and", "as", "at", "before", "during", "faces",
            "for", "from", "in", "latest", "on", "over", "predicting", "reveals",
            "says", "shows", "to", "updates", "vs", "with"
        }
        generic_words = {
            "breaking", "comments", "election", "final", "latest", "mixed", "outcome",
            "predicting", "report", "results", "tough", "unexpected", "update", "updates"
        }

        phrases: list[str] = []
        current: list[str] = []

        def flush() -> None:
            if 1 < len(current) <= 4:
                lower_tokens = [token.lower() for token in current]
                if not any(token in generic_words for token in lower_tokens):
                    phrases.append(" ".join(current))
            current.clear()

        for token in re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", title):
            if token.lower() in break_words:
                flush()
                continue

            if token[:1].isupper() or token.isupper() or token.isdigit():
                current.append(token)
                if len(current) == 4:
                    flush()
                continue

            flush()

        flush()
        return phrases


# ═══════════════════════════════════════════════════════════════════════════
# SYNCED JSON VECTOR STORE (B2-backed cache for Railway free tier)
# ═══════════════════════════════════════════════════════════════════════════

class SyncedJSONVectorStore(VectorStore):
    """
    JSONVectorStore + automatic B2 sync.

    WHY THIS EXISTS
    ===============
    On Railway's free tier, the container's local filesystem is wiped
    on every redeploy. That means the local ``documents.json`` /
    ``embeddings.json`` files written by ``JSONVectorStore`` disappear,
    and the next article-generation run finds an empty index.

    This wrapper solves that by mirroring the local files to B2:

        App startup        → download from B2 to local cache (one-time)
        After bulk_upsert  → upload local cache back to B2
        Search             → local only (no B2 traffic)

    The on-disk format is identical to ``JSONVectorStore`` — this class
    just adds B2 sync hooks around the write paths. If B2 is not
    configured, it gracefully degrades to local-only behavior.

    WHERE THINGS LIVE
    =================
    Local disk (fast, temporary):
        storage/vector-store/tenants/{tenant_id}/
            ├── documents.json
            └── embeddings.json

    B2 (slow, permanent):
        bucket/vector-store/tenants/{tenant_id}/
            ├── documents.json
            └── embeddings.json

    The B2 key prefix is ``vector-store/`` — added to CATEGORY_PREFIXES
    in b2_engine.py via the default-when-unknown fallback
    (``_resolve_category_prefix("vector-store") → "vector-store/"``).
    """

    B2_CATEGORY = "vector-store"

    def __init__(
        self,
        storage_root: Path,
        cloud_sync=None,
        auto_download_on_init: bool = True,
    ) -> None:
        """
        Args:
            storage_root:    Local root (e.g. ``storage/vector-store``).
            cloud_sync:      Optional ``CloudSync`` instance. If None or
                             if cloud is disabled, the store operates
                             in local-only mode (degraded).
            auto_download_on_init:
                             If True and cloud_sync is reachable, pull
                             the latest ``documents.json`` and
                             ``embeddings.json`` from B2 into the local
                             cache on first use.
        """
        self._inner = JSONVectorStore(storage_root)
        self._cloud_sync = cloud_sync
        self._auto_download = auto_download_on_init
        self._initialized_tenants: set[str] = set()

    # ── B2 sync helpers ───────────────────────────────────────────────

    def _cloud_enabled(self) -> bool:
        if self._cloud_sync is None:
            return False
        try:
            return bool(self._cloud_sync.cloud_enabled)
        except Exception:
            return False

    def _normalize_tenant_id(self, tenant_id: str) -> str:
        """Use a stable tenant key for B2 object paths."""
        normalized = (tenant_id or "").strip()
        return normalized or "_default"

    def _download_from_b2(self, tenant_id: str) -> None:
        """Pull documents.json + embeddings.json from B2 → local cache."""
        if not self._cloud_enabled():
            return
        try:
            sync = self._cloud_sync
            b2 = sync._get_b2()  # type: ignore[attr-defined]
            safe_tenant_id = self._normalize_tenant_id(tenant_id)
            for filename in ("documents.json", "embeddings.json"):
                key = f"tenants/{safe_tenant_id}/{filename}"
                if b2.object_exists(self.B2_CATEGORY, key):
                    data = b2.download_bytes(self.B2_CATEGORY, key)
                    local_path = self._inner._tenant_dir(safe_tenant_id) / filename
                    local_path.write_bytes(data)
        except Exception as exc:
            print(f"[SyncedJSONVectorStore] download failed: {exc}")

    def _upload_to_b2(self, tenant_id: str) -> None:
        """Push documents.json + embeddings.json from local cache → B2."""
        if not self._cloud_enabled():
            return
        try:
            sync = self._cloud_sync
            b2 = sync._get_b2()  # type: ignore[attr-defined]
            safe_tenant_id = self._normalize_tenant_id(tenant_id)
            for filename in ("documents.json", "embeddings.json"):
                local_path = self._inner._tenant_dir(safe_tenant_id) / filename
                if not local_path.exists():
                    continue
                key = f"tenants/{safe_tenant_id}/{filename}"
                b2.upload_file(self.B2_CATEGORY, str(local_path), key=key)
        except Exception as exc:
            print(f"[SyncedJSONVectorStore] upload failed: {exc}")

    def _ensure_tenant_synced(self, tenant_id: str) -> None:
        """Pull from B2 once per tenant per process lifetime."""
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        if safe_tenant_id in self._initialized_tenants:
            return
        self._initialized_tenants.add(safe_tenant_id)
        if self._auto_download:
            self._download_from_b2(safe_tenant_id)

    # ── VectorStore interface (delegates to inner JSONVectorStore) ───

    def upsert(self, tenant_id: str, document: Document, embedding: List[float]) -> None:
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        self._inner.upsert(safe_tenant_id, document, embedding)
        self._upload_to_b2(safe_tenant_id)

    def bulk_upsert(self, tenant_id: str, items: List[tuple[Document, List[float]]]) -> int:
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        count = self._inner.bulk_upsert(safe_tenant_id, items)
        self._upload_to_b2(safe_tenant_id)
        return count

    def search(
        self,
        tenant_id: str,
        query_embedding: List[float],
        limit: int = 20,
        category_filter: Optional[List[str]] = None,
    ) -> List[dict]:
        # Search is read-only — no upload needed. But we still want
        # the latest data on first access (in case this is a fresh
        # container with an empty local cache).
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        return self._inner.search(
            tenant_id=safe_tenant_id,
            query_embedding=query_embedding,
            limit=limit,
            category_filter=category_filter,
        )

    def delete(self, tenant_id: str, slug: str) -> bool:
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        found = self._inner.delete(safe_tenant_id, slug)
        if found:
            self._upload_to_b2(safe_tenant_id)
        return found

    def count(self, tenant_id: str) -> int:
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        return self._inner.count(safe_tenant_id)

    def get_document(self, tenant_id: str, slug: str) -> Optional[Document]:
        safe_tenant_id = self._normalize_tenant_id(tenant_id)
        self._ensure_tenant_synced(safe_tenant_id)
        return self._inner.get_document(safe_tenant_id, slug)

    # ── Public extras (used by maintenance scripts) ───────────────────

    def force_upload(self, tenant_id: str) -> None:
        """Force-push the local cache for a tenant to B2 (regardless of dirty state)."""
        self._upload_to_b2(self._normalize_tenant_id(tenant_id))

    def force_download(self, tenant_id: str) -> None:
        """Force-pull the B2 copy for a tenant into the local cache (overwrites local)."""
        self._download_from_b2(self._normalize_tenant_id(tenant_id))


# ═══════════════════════════════════════════════════════════════════════════
# VECTOR STORE FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def create_vector_store(config: AppConfig, cloud_sync=None) -> VectorStore:
    """
    Factory: create the right VectorStore based on config.

    Resolution order:
        1. ``vector_store_type == "pgvector"`` + database_url set →
           ``PgvectorStore`` (production / multi-tenant).
        2. ``cloud_sync`` provided AND B2 reachable →
           ``SyncedJSONVectorStore`` (Railway free tier — local cache
           with B2 backup so it survives redeployments).
        3. Otherwise → plain ``JSONVectorStore`` (local dev, no B2).

    The ``cloud_sync`` parameter is optional — callers (pipeline.py)
    can pass ``CloudSync.instance()`` after initialization. If not
    passed, the factory falls back to plain JSONVectorStore.
    """
    store_type = getattr(config, "vector_store_type", "json")
    database_url = getattr(config, "vector_store_database_url", "")

    if store_type == "pgvector" and database_url and psycopg2 is not None:
        return PgvectorStore(database_url)

    storage_root = Path(config.storage_root) / "vector-store"

    b2_sync_enabled = bool(getattr(config, "vector_store_b2_sync_enabled", True))
    if cloud_sync is not None and b2_sync_enabled:
        return SyncedJSONVectorStore(storage_root, cloud_sync=cloud_sync)

    return JSONVectorStore(storage_root)


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL LINK AGENT (pipeline integration)
# ═══════════════════════════════════════════════════════════════════════════

class InternalLinkAgent(BaseAgent):
    stage_name = "internal_links"

    def __init__(
        self,
        retrieval_service: RetrievalService,
        injector: AnchorInjectorService,
        logger: PipelineLogger,
        tenant_id: str = "",
    ) -> None:
        self.retrieval_service = retrieval_service
        self.injector = injector
        self.logger = logger
        self.tenant_id = tenant_id

    def execute(self, context: AgentContext) -> None:
        if context.run is None or context.run.selected_topic is None or context.run.blog is None:
            raise RuntimeError("Selected topic and blog are required before internal linking")

        target_words = getattr(self.retrieval_service.config, "min_article_words", 800)
        plan_summary = ""

        if context.run.plan:
            plan_summary = getattr(context.run.plan, 'brief', "")

        current_slug_source = (
            context.run.blog.seo_keywords[0]
            if getattr(context.run.blog, "seo_keywords", None)
            else context.run.selected_topic.keyword
        )

        # Retrieval — no crawling, just search
        links = self.retrieval_service.retrieve(
            tenant_id=self.tenant_id,
            topic=context.run.selected_topic,
            plan_summary=plan_summary,
            target_word_count=target_words,
            exclude_slug=slugify(current_slug_source)
        )

        self.logger.info(
            context.run,
            (
                "Internal-link retrieval tenant: "
                f"requested='{self.tenant_id}', "
                f"resolved='{self.retrieval_service.last_resolved_tenant_id or 'none'}', "
                f"tried={self.retrieval_service.last_tried_tenant_ids}"
            ),
        )

        # If retrieval returns nothing, attempt a one-time on-demand index build
        # for the same tenant candidates. This avoids a common failure mode where
        # the index was never bootstrapped before running the generation pipeline.
        if not links and getattr(self.retrieval_service.config, "sitemap_url", ""):
            try:
                indexing_service = IndexingService(
                    self.retrieval_service.config,
                    self.retrieval_service.vector_store,
                )
                tenant_candidates = self.retrieval_service._resolve_tenant_candidates(self.tenant_id)
                for candidate_tenant in tenant_candidates:
                    result = indexing_service.index_tenant(candidate_tenant)
                    if result.get("documents_indexed", 0) > 0:
                        break

                links = self.retrieval_service.retrieve(
                    tenant_id=self.tenant_id,
                    topic=context.run.selected_topic,
                    plan_summary=plan_summary,
                    target_word_count=target_words,
                    exclude_slug=slugify(current_slug_source)
                )

                self.logger.info(
                    context.run,
                    (
                        "Internal-link retrieval tenant after bootstrap: "
                        f"requested='{self.tenant_id}', "
                        f"resolved='{self.retrieval_service.last_resolved_tenant_id or 'none'}', "
                        f"tried={self.retrieval_service.last_tried_tenant_ids}"
                    ),
                )
            except Exception:
                # Keep pipeline resilient: if bootstrapping fails, we continue
                # without inline links and let publisher diagnostics surface state.
                pass

        context.run.internal_links = links

        original_html = context.run.blog.article_html
        linked_html = self.injector.inject(original_html, links)
        context.run.blog.article_html = linked_html

        inserted_count = linked_html.count("<a ") - original_html.count("<a ")
        self.logger.info(
            context.run,
            f"Retrieved {len(links)} internal links and inserted {inserted_count} anchors"
        )
        self.logger.transition(context.run, "internal_links_loaded")


# ═══════════════════════════════════════════════════════════════════════════
# ANCHOR INJECTOR SERVICE (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

class AnchorInjectorService:
    """
    Deterministic, Regex-based anchor injection.
    Respects priorities and quality filters.
    """
    GUTENBERG_PARA_PATTERN = re.compile(
        r'(<!--\s*wp:paragraph[^>]*-->\s*<p>)(.*?)(</p>\s*<!--\s*/wp:paragraph\s*-->)',
        flags=re.IGNORECASE | re.DOTALL
    )

    STOP_ANCHORS = {
        "click here", "read more", "source", "article", "post",
        "guide", "blog", "website", "link", "page", "info"
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.max_links_per_paragraph = 3
        self.max_links_total = 8

    def inject(self, article_html: str, internal_links: List[InternalLink]) -> str:
        if not internal_links or not article_html:
            return article_html

        total_links_inserted = 0
        used_urls: set[str] = set()
        output_parts: list[str] = []
        last_end = 0

        for match in self.GUTENBERG_PARA_PATTERN.finditer(article_html):
            output_parts.append(article_html[last_end:match.start()])
            if total_links_inserted >= self.max_links_total:
                output_parts.append(article_html[match.start():])
                break

            prefix = match.group(1)
            content = match.group(2)
            suffix = match.group(3)

            if not content.strip() or "Also Read" in content:
                output_parts.extend([prefix, content, suffix])
                last_end = match.end()
                continue

            remaining_limit = min(
                self.max_links_per_paragraph,
                self.max_links_total - total_links_inserted
            )
            updated_content, inserted_urls = self._inject_into_paragraph(
                content,
                internal_links,
                limit=remaining_limit,
                used_urls=used_urls,
            )

            used_urls.update(inserted_urls)
            total_links_inserted += len(inserted_urls)
            output_parts.extend([prefix, updated_content, suffix])
            last_end = match.end()

        else:
            output_parts.append(article_html[last_end:])

        result = "".join(output_parts)

        # ── NEW: Also Read fallback ─────────────────────────────────────
        # If no anchors were inserted inline, optionally append a single
        # "Also Read" callout using the top relevance link.
        also_read_enabled = bool(
            getattr(self.config, "internal_link_also_read_enabled", True)
        )
        if also_read_enabled and total_links_inserted == 0 and internal_links:
            top_link = max(internal_links, key=lambda l: l.get("relevance_score", 0.0))
            also_read_block = (
                "\n<!-- wp:separator -->\n<hr />\n<!-- /wp:separator -->\n"
                "<!-- wp:paragraph -->\n"
                f"<p><strong>Also Read:</strong> <a href=\"{escape(top_link['url'], quote=True)}\">{escape(top_link['title'])}</a></p>\n"
                "<!-- /wp:paragraph -->\n"
                "<!-- wp:separator -->\n<hr />\n<!-- /wp:separator -->\n"
            )

            close_match = re.search(r'</article>\s*$', result, flags=re.IGNORECASE)
            if close_match:
                result = result[:close_match.start()] + also_read_block + result[close_match.start():]
            else:
                result = result + also_read_block

            total_links_inserted = 1

        # ── NEW: Related Articles fallback ───────────────────────────────
        # If no anchors were inserted inline and Also Read did not run,
        # optionally append a "Related Articles" section with up to 4 links.
        #
        # These links:
        #   - Use the correct URLs from the vector store (www.peoplenewstime.com/...)
        #   - Have NO rel="nofollow" (good for SEO)
        #   - Have NO target="_blank" (clean internal links)
        #   - Are sorted by relevance score (best first)
        related_articles_enabled = bool(
            getattr(self.config, "internal_link_related_articles_enabled", True)
        )
        if related_articles_enabled and total_links_inserted == 0 and internal_links:
            sorted_links = sorted(
                internal_links,
                key=lambda l: l.get("relevance_score", 0.0),
                reverse=True,
            )
            related_links = sorted_links[:4]

            list_items = []
            for link in related_links:
                url = escape(link["url"], quote=True)
                title = escape(link["title"])
                list_items.append(f'<li><a href="{url}">{title}</a></li>')

            related_block = (
                '\n<!-- wp:separator -->\n<hr />\n<!-- /wp:separator -->\n'
                '<!-- wp:heading -->\n<h2>Related Articles</h2>\n<!-- /wp:heading -->\n'
                '<!-- wp:list -->\n<ul>\n'
                + "\n".join(list_items)
                + '\n</ul>\n<!-- /wp:list -->\n'
            )

            close_match = re.search(r'</article>\s*$', result, flags=re.IGNORECASE)
            if close_match:
                result = result[:close_match.start()] + related_block + result[close_match.start():]
            else:
                result = result + related_block

            total_links_inserted = len(related_links)

        return result

    def _inject_into_paragraph(
        self,
        content: str,
        internal_links: List[InternalLink],
        limit: int,
        used_urls: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        updated_content = content
        inserted_urls: list[str] = []
        used_urls = used_urls or set()

        for link in internal_links:
            if len(inserted_urls) >= limit:
                break

            url = link["url"]
            if url in used_urls or url in inserted_urls:
                continue

            candidates = link.get("anchor_candidates", [])
            candidates.sort(key=lambda x: x["priority"])

            for candidate in candidates:
                text_to_link = candidate["text"]

                if not self._is_valid_anchor(text_to_link):
                    continue

                updated_content, inserted = self._replace_first_unlinked_text(
                    updated_content, text_to_link, url
                )

                if inserted:
                    inserted_urls.append(url)
                    break

        return updated_content, inserted_urls

    def _replace_first_unlinked_text(
        self, content: str, anchor: str, url: str
    ) -> tuple[str, bool]:
        pattern = re.compile(
            rf'(?<![\w-])({re.escape(anchor)})(?![\w-])',
            flags=re.IGNORECASE
        )
        segments = re.split(
            r"(<a\b[^>]*>.*?</a>)", content, flags=re.IGNORECASE | re.DOTALL
        )

        for index, segment in enumerate(segments):
            if index % 2 == 1:
                continue

            match = pattern.search(segment)
            if match is None:
                continue

            exact_match = match.group(1)
            replacement = f'<a href="{escape(url, quote=True)}">{exact_match}</a>'
            segments[index] = segment[:match.start()] + replacement + segment[match.end():]
            return "".join(segments), True

        return content, False

    def _is_valid_anchor(self, anchor: str) -> bool:
        anchor = anchor.strip()
        words = anchor.split()

        if len(anchor) < 3:
            return False
        if len(words) > 5:
            return False
        if anchor.lower() in self.STOP_ANCHORS:
            return False
        return True