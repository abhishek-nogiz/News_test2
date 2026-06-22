"""SerpAPI key rotation with quota-exhaustion tracking and disk persistence.

Why this exists
---------------
SerpAPI's free tier is 250 searches/month per key. When that runs out,
SerpAPI returns HTTP 429. With multiple keys, you can rotate to the next
key automatically and keep the pipeline running.

How it works
------------
1. `SerpApiKeyRotator` holds a list of keys + per-key state
   (exhausted-until timestamp, call/error counters).
2. `next_key()` returns the next available key (round-robin) or `None`
   if all are exhausted.
3. When a 429/quota error occurs, `mark_exhausted(key, hours=24)`
   excludes that key for 24 hours. After 24h the key is retried — this
   matches SerpAPI's monthly reset cycle close enough for most setups.
4. State is persisted to `storage/serpapi_keys_state.json` so the next
   scheduled run (fresh Python process) knows which keys are dead.
   Without persistence, every run would waste 1 call per dead key
   rediscovering the 429.

Backward compatibility
----------------------
If only one key is configured (no `SERPAPI_KEYS` env var), the rotator
still works — it just has a single key and `next_key()` returns it until
it's marked exhausted. Existing single-key setups behave identically.

Thread safety
-------------
All state mutations are guarded by a lock. Reads are not (they're
atomic enough for our purposes — stats() is informational only).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _KeyState:
    key: str
    exhausted_until: float = 0.0  # epoch seconds; 0 = available
    call_count: int = 0
    error_count: int = 0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.exhausted_until


class SerpApiKeyRotator:
    """Round-robin SerpAPI key rotation with quota-exhaustion exclusion.

    Usage:
        rotator = SerpApiKeyRotator(
            keys=["key1", "key2", "key3"],
            state_path=Path("storage/serpapi_keys_state.json"),
        )
        key = rotator.next_key()                  # str | None
        rotator.mark_exhausted(key, hours=24)     # exclude for 24h
        rotator.record_call(key)                  # increment counter
        stats = rotator.stats()                   # dict for JSON output
    """

    def __init__(
        self,
        keys: list[str] | None,
        state_path: Path | str | None = None,
        exhaustion_hours: float = 24.0,
    ) -> None:
        # Dedupe + drop empties + strip whitespace
        seen: set[str] = set()
        clean: list[str] = []
        for k in keys or []:
            k = (k or "").strip()
            if k and k not in seen:
                seen.add(k)
                clean.append(k)

        self._keys = clean
        self._states: dict[str, _KeyState] = {k: _KeyState(key=k) for k in clean}
        self._exhaustion_hours = exhaustion_hours
        self._state_path = Path(state_path) if state_path else None
        self._lock = threading.Lock()
        self._next_index = 0  # round-robin cursor

        self._load_state()

    @property
    def total_keys(self) -> int:
        return len(self._keys)

    def next_key(self) -> str | None:
        """Return the next available key (round-robin), or None if all exhausted."""
        with self._lock:
            if not self._keys:
                return None
            n = len(self._keys)
            for offset in range(n):
                idx = (self._next_index + offset) % n
                key = self._keys[idx]
                if self._states[key].is_available:
                    self._next_index = (idx + 1) % n
                    return key
            return None

    def mark_exhausted(self, key: str, hours: float | None = None) -> None:
        """Exclude `key` from rotation for `hours` (default: 24)."""
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            h = hours if hours is not None else self._exhaustion_hours
            state.exhausted_until = time.time() + (h * 3600)
            self._save_state()

    def record_call(self, key: str) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                state.call_count += 1

    def record_error(self, key: str) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                state.error_count += 1

    def stats(self) -> dict[str, Any]:
        """JSON-friendly snapshot for logging / run-summary output.

        Keys are masked (first 4 + last 4 chars) so they're safe to log.
        """
        with self._lock:
            now = time.time()
            return {
                "total_keys": len(self._keys),
                "available": sum(1 for s in self._states.values() if s.is_available),
                "exhausted": [
                    self._mask(k)
                    for k, s in self._states.items()
                    if not s.is_available
                ],
                "calls_per_key": {
                    self._mask(k): s.call_count for k, s in self._states.items()
                },
                "errors_per_key": {
                    self._mask(k): s.error_count for k, s in self._states.items()
                },
                "seconds_until_reset": {
                    self._mask(k): int(s.exhausted_until - now)
                    if s.exhausted_until > now else 0
                    for k, s in self._states.items()
                },
            }

    @staticmethod
    def _mask(key: str) -> str:
        """Mask a key for logging: show only first 4 + last 4 chars."""
        if not key:
            return ""
        if len(key) <= 8:
            return "***" + key[-4:]
        return key[:4] + "..." + key[-4:]

    # ───────────────────────────────────────────────────────────────
    # Disk persistence
    # ───────────────────────────────────────────────────────────────
    def _load_state(self) -> None:
        """Load exhaustion state + counters from the JSON state file.

        Silently ignores missing/corrupted files — falls back to fresh state.
        """
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            states_data = data.get("states", {})
            for k, s in states_data.items():
                if k in self._states:
                    self._states[k].exhausted_until = float(s.get("exhausted_until", 0))
                    self._states[k].call_count = int(s.get("call_count", 0))
                    self._states[k].error_count = int(s.get("error_count", 0))
            next_idx = data.get("next_index", 0)
            if 0 <= next_idx < len(self._keys):
                self._next_index = next_idx
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass  # corrupted — start fresh

    def _save_state(self) -> None:
        """Persist current state to JSON. Silently fails on write errors."""
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "states": {
                    k: {
                        "exhausted_until": s.exhausted_until,
                        "call_count": s.call_count,
                        "error_count": s.error_count,
                    }
                    for k, s in self._states.items()
                },
                "next_index": self._next_index,
                "updated_at": time.time(),
            }
            self._state_path.write_text(json.dumps(data, indent=2))
        except OSError:
            pass  # can't persist (read-only fs?) — continue in-memory


# ─────────────────────────────────────────────────────────────────────────
# Module-level singleton (set by pipeline.py during ContentPipeline init)
# ─────────────────────────────────────────────────────────────────────────
_rotator: SerpApiKeyRotator | None = None


def set_rotator(rotator: SerpApiKeyRotator | None) -> None:
    global _rotator
    _rotator = rotator


def get_rotator() -> SerpApiKeyRotator | None:
    return _rotator


# ─────────────────────────────────────────────────────────────────────────
# Quota-error detection
# ─────────────────────────────────────────────────────────────────────────
def is_quota_error(exc: Exception) -> bool:
    """Heuristic: does this exception look like a SerpAPI quota/rate-limit error?

    SerpAPI's Python SDK raises different exception types depending on the
    version — sometimes `urllib.error.HTTPError` (with `.code == 429`),
    sometimes a generic `Exception` with a message. We check both.

    Indicators (case-insensitive substring match on the error message):
      - "429"
      - "rate limit" / "too many requests"
      - "quota" / "exceeded"
      - "searches per month" (SerpAPI's specific quota-exhausted message)
      - "billing limit" / "monthly limit"
    """
    # Direct status-code attribute (HTTPError, requests.Response.raise_for_status, etc.)
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if code == 429:
        return True

    # Message-string heuristic
    msg = str(exc).lower()
    indicators = (
        "429",
        "rate limit",
        "too many requests",
        "quota",
        "exceeded",
        "searches per month",
        "billing limit",
        "monthly limit",
    )
    return any(ind in msg for ind in indicators)