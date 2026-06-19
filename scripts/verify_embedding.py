"""
HuggingFace feature-extraction (embeddings) client.

NOTE: HuggingFace decommissioned the old `api-inference.huggingface.co`
domain. All requests now go through the unified router:

    https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction

Using the old domain will fail with a DNS resolution error (NXDOMAIN),
not an auth error, since the host no longer exists.
"""

from __future__ import annotations

import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter, Retry

DEFAULT_EMBEDDING_DIM = 384
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
ROUTER_BASE_URL = "https://router.huggingface.co/hf-inference/models"

# Tune as needed
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 1.5


class HuggingFaceEmbeddings:
    """Thin client around the HF Inference router's feature-extraction pipeline."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        if not api_key:
            raise ValueError("HuggingFaceEmbeddings requires a non-empty api_key")

        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.last_error: Optional[str] = None

        self.url = f"{ROUTER_BASE_URL}/{model}/pipeline/feature-extraction"

        self.session = requests.Session()
        retries = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def _post(self, inputs) -> Optional[list]:
        try:
            resp = self.session.post(
                self.url,
                json={"inputs": inputs, "options": {"wait_for_model": True}},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            self.last_error = f"all retries exhausted: {exc}"
            return None

        if resp.status_code == 410 or "is no longer supported" in resp.text:
            self.last_error = (
                "HF endpoint returned 410/decommission notice. "
                "Double-check ROUTER_BASE_URL is router.huggingface.co, not api-inference.huggingface.co."
            )
            return None

        if resp.status_code != 200:
            self.last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            return None

        try:
            data = resp.json()
        except ValueError:
            self.last_error = f"Non-JSON response: {resp.text[:300]}"
            return None

        if isinstance(data, dict) and "error" in data:
            self.last_error = data["error"]
            return None

        return data

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns [] on failure (check .last_error)."""
        result = self._post(text)
        if result is None:
            return []

        # API may return [dim] for single inputs, or [1][dim] depending on model.
        vec = result
        while isinstance(vec, list) and vec and isinstance(vec[0], list):
            vec = vec[0]

        if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
            self.last_error = f"Unexpected response shape: {str(result)[:300]}"
            return []

        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Returns [] (or partial list with empties) on failure."""
        result = self._post(texts)
        if result is None:
            return [[] for _ in texts]

        if not isinstance(result, list) or len(result) != len(texts):
            self.last_error = f"Unexpected batch response shape: {str(result)[:300]}"
            return [[] for _ in texts]

        vectors = []
        for item in result:
            vec = item
            while isinstance(vec, list) and vec and isinstance(vec[0], list):
                vec = vec[0]
            vectors.append(vec if isinstance(vec, list) else [])

        return vectors