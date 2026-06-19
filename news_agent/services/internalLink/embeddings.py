"""
HuggingFace Inference API client for embeddings.

WHY THIS FILE EXISTS
====================
The original ``internalLink/service.py`` loaded ``SentenceTransformer``
(``all-MiniLM-L6-v2``) locally. That pulls in PyTorch (~700MB) and
downloads the model (~90MB) on first use. On Railway's free tier
(512MB RAM, ephemeral disk) that:

    1. OOMs the container on import.
    2. Loses the model cache on every redeploy.

This module replaces local inference with a single HTTPS call to
HuggingFace's hosted Inference API. The model lives on HF's servers;
we just send text and get back a 384-dim vector. No PyTorch, no
model download, no OOM.

COST
====
HuggingFace Inference API (free tier):
    - 1,000 requests/day on the free plan (as of 2024-12).
    - First full index of a ~250-article site = ~250 requests.
    - Daily refresh (only new articles) = ~5 requests/day.
    - Article generation (every 4h) = 1 request per generation
      (the topic vector) — well within the daily budget.

USAGE
=====
    from .embeddings import HuggingFaceEmbeddings

    emb = HuggingFaceEmbeddings(
        api_key="hf_...",
        model="sentence-transformers/all-MiniLM-L6-v2",
    )

    # Single text
    vec = emb.embed("James Rodriguez signs for Boca Juniors")
    # -> List[float] of length 384

    # Batch (still one HTTPS call per chunk of 32 texts)
    vecs = emb.embed_batch([
        "James Rodriguez signs for Boca Juniors",
        "Messi wins Ballon d'Or 2023",
        ...
    ])

FALLBACK
========
If ``api_key`` is empty OR the API call fails, ``embed()`` returns an
empty list. Callers (IndexingService / RetrievalService) handle the
empty case by falling back to keyword-only matching — same behavior
as the old code when ``SentenceTransformer`` failed to load.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

# Optional dependency — graceful if missing (same pattern as service.py)
try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension
HF_INFERENCE_URL_TEMPLATE = (
    "https://api-inference.huggingface.co/pipeline/feature-extraction/{model}"
)
DEFAULT_TIMEOUT = 30          # seconds per request
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5      # 1.5s, 2.25s, 3.375s …
MAX_BATCH_SIZE = 32           # HF recommends <=32 inputs per request

try:
    from huggingface_hub import InferenceClient
except ImportError:
    InferenceClient = None

class HuggingFaceEmbeddings:
    """
    Thin client for the HuggingFace Inference feature-extraction API.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = model
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._dim = DEFAULT_EMBEDDING_DIM
        self._last_error: Optional[str] = None
        self._client = (
            InferenceClient(provider="hf-inference", api_key=self.api_key, timeout=timeout)
            if (InferenceClient is not None and self.api_key)
            else None
        )

    @property
    def is_available(self) -> bool:
        return self._client is not None
    
    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def embedding_dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> List[float]:
        text = (text or "").strip()
        if not self._client or not text:
            return []
        try:
            result = self._client.feature_extraction(text, model=self.model)
            return self._pool_sample(result.tolist() if hasattr(result, "tolist") else result)
        except Exception as exc:
            self._last_error = str(exc)
            return []

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]  # InferenceClient handles retries/routing internally

    def _post_with_retry(self, payload: dict):
        if requests is None:
            return None

        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_exc = exc
                self._last_error = f"network error: {exc}"
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                self._last_error = None
                return resp

            if resp.status_code in (429, 503):
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self._sleep_backoff(attempt)
                continue

            self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return None

        if last_exc is not None:
            self._last_error = f"all retries exhausted: {last_exc}"
        return None

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(10.0, RETRY_BACKOFF_BASE ** (attempt + 1))
        time.sleep(delay)

    def _embed_chunk(self, chunk: List[str]) -> List[List[float]]:
        payload = {"inputs": chunk, "options": {"wait_for_model": True}}
        response = self._post_with_retry(payload)
        if response is None:
            return [[] for _ in chunk]

        try:
            data = response.json()
        except Exception as exc:
            self._last_error = f"JSON decode failed: {exc}"
            return [[] for _ in chunk]

        return self._extract_batch_vectors(data, expected_len=len(chunk))

    def _extract_single_vector(self, data) -> List[float]:
        vectors = self._extract_batch_vectors(data, expected_len=1)
        return vectors[0] if vectors else []

    def _extract_batch_vectors(self, data, expected_len: int) -> List[List[float]]:
        if not isinstance(data, list) or not data:
            return [[] for _ in range(expected_len)]

        result: List[List[float]] = []
        for sample in data:
            vec = self._pool_sample(sample)
            result.append(vec)

        while len(result) < expected_len:
            result.append([])
        return result[:expected_len]

    def _pool_sample(self, sample) -> List[float]:
        if (
            isinstance(sample, list)
            and sample
            and isinstance(sample[0], (int, float))
        ):
            return [float(x) for x in sample]

        if (
            isinstance(sample, list)
            and sample
            and isinstance(sample[0], list)
        ):
            return self._mean_pool(sample)

        return []

    @staticmethod
    def _mean_pool(token_vectors: List[List[float]]) -> List[float]:
        if not token_vectors:
            return []

        dim = max(len(v) for v in token_vectors)
        sums = [0.0] * dim
        counts = [0] * dim

        for vec in token_vectors:
            for i, x in enumerate(vec):
                if isinstance(x, (int, float)):
                    sums[i] += float(x)
                    counts[i] += 1

        return [
            (sums[i] / counts[i]) if counts[i] > 0 else 0.0
            for i in range(dim)
        ]


def create_embeddings_client(config) -> "HuggingFaceEmbeddings | None":
    """
    Build a HuggingFaceEmbeddings client from the AppConfig.

    Reads these config fields (set in news_agent/core/config/__init__.py):
        - config.hf_api_key           (env: HUGGINGFACE_API_KEY)
        - config.hf_embedding_model   (env: HUGGINGFACE_EMBEDDING_MODEL)

    Returns ``None`` if no API key is configured.
    """
    api_key = getattr(config, "hf_api_key", "") or os.getenv("HUGGINGFACE_API_KEY", "")
    model = (
        getattr(config, "hf_embedding_model", "")
        or os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "")
        or DEFAULT_MODEL
    )

    if not api_key:
        return None

    return HuggingFaceEmbeddings(api_key=api_key, model=model)