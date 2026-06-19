"""Per-agent SerpAPI usage tracking + automatic key rotation on quota errors.

Two concerns, one module:

1. **Usage tracking** (per-stage counters on PipelineRun via contextvars)
2. **Key rotation** (round-robin through multiple SerpAPI keys, with
   automatic failover on 429/quota errors)

Both are transparent to the calling code — `trends/service.py` and
`research/service.py` still call `GoogleSearch(params).get_dict()` and
don't know or care which key was used or which stage they're running in.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import PipelineRun

from .serpapi_keys import get_rotator, is_quota_error


# ─────────────────────────────────────────────────────────────────────────
# Per-stage usage tracking (unchanged from v1)
# ─────────────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class _CurrentCall:
    run: "PipelineRun"
    stage: str


_current: ContextVar[_CurrentCall | None] = ContextVar(
    "news_agent_serpapi_current", default=None
)


def bind(run: "PipelineRun", stage: str):
    """Bind (run, stage) as the current attribution target."""
    return _current.set(_CurrentCall(run=run, stage=stage))


def unbind(token) -> None:
    _current.reset(token)


def _record(engine: str, *, error: bool = False) -> None:
    """Record one SerpAPI call against the currently-bound (run, stage)."""
    cur = _current.get()
    if cur is None:
        return
    from ..models import SerpApiStats
    stats = cur.run.serpapi_by_stage.get(cur.stage)
    if stats is None:
        stats = SerpApiStats()
        cur.run.serpapi_by_stage[cur.stage] = stats
    if error:
        stats.errors += 1
    else:
        stats.calls += 1
    stats.by_engine[engine] = stats.by_engine.get(engine, 0) + 1


# ─────────────────────────────────────────────────────────────────────────
# InstrumentedGoogleSearch — drop-in replacement for serpapi.GoogleSearch
# ─────────────────────────────────────────────────────────────────────────
class InstrumentedGoogleSearch:
    """Drop-in replacement for `serpapi.GoogleSearch`.

    Same constructor signature, same `.get_dict()` / `.get_json()` /
    `.get_response()` API.

    Adds two things on top of the real client:

    1. **Usage tracking** — every successful or failed call is
       auto-attributed to the currently running agent stage via the
       contextvars binding set up in `pipeline.py`.

    2. **Key rotation** — if a `SerpApiKeyRotator` has been bound via
       `serpapi_keys.set_rotator()`, the wrapper uses the rotator's
       current key instead of whatever `api_key` is in `params`. On a
       429/quota error, it marks that key exhausted and retries with
       the next available key (up to N attempts = number of keys).
       If all keys are exhausted, raises a clear `RuntimeError` with
       the rotator state for debugging.

    If no rotator is bound, falls back to legacy single-key behavior
    (uses `params["api_key"]` as-is, no retry).
    """

    def __init__(self, params: dict | None = None, **kwargs: Any) -> None:
        from serpapi import GoogleSearch as _Real

        # Stash originals so we can rebuild the inner client with a
        # different api_key on rotation
        self._original_params = params
        self._original_kwargs = kwargs
        self._engine = (
            (params or {}).get("engine", "unknown")
            if isinstance(params, dict)
            else kwargs.get("engine", "unknown")
        )

        rotator = get_rotator()

        if rotator is not None and rotator.total_keys > 0:
            # Rotation mode — pick a key from the rotator
            key = rotator.next_key()
            if key is None:
                raise RuntimeError(
                    "All SerpAPI keys are exhausted. "
                    f"Rotator state: {rotator.stats()}"
                )
            self._key_used = key
            # Override api_key in params (drop whatever the caller passed)
            if isinstance(params, dict):
                params = {**params, "api_key": key}
            else:
                kwargs = {**kwargs, "api_key": key}
        else:
            # Legacy single-key mode — use whatever api_key is in params
            self._key_used = (
                params.get("api_key") if isinstance(params, dict)
                else kwargs.get("api_key")
            )

        self._inner = _Real(params, **kwargs)

    def _rebuild_with_new_key(self, new_key: str) -> None:
        """Rebuild the inner serpapi client with a different api_key.

        Called when we need to retry the same call with a different key
        after a 429/quota error.
        """
        from serpapi import GoogleSearch as _Real
        if isinstance(self._original_params, dict):
            new_params = {**self._original_params, "api_key": new_key}
            self._inner = _Real(new_params)
        else:
            new_kwargs = {**self._original_kwargs, "api_key": new_key}
            self._inner = _Real(**new_kwargs)
        self._key_used = new_key

    def _call_with_rotation(self, method_name: str, *args: Any, **kwargs: Any):
        """Call a method on the inner client, rotating keys on quota errors.

        Tries up to N times (where N = number of keys in the rotator).
        On a non-quota error, raises immediately without retrying.
        """
        rotator = get_rotator()
        max_attempts = rotator.total_keys if rotator is not None else 1

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                method = getattr(self._inner, method_name)
                result = method(*args, **kwargs)
                # Success — record call on rotator + per-stage counter
                if rotator is not None and self._key_used:
                    rotator.record_call(self._key_used)
                _record(self._engine)
                return result

            except Exception as exc:
                last_exc = exc

                # Non-quota error → record + re-raise immediately
                if rotator is None or not is_quota_error(exc):
                    if rotator is not None and self._key_used:
                        rotator.record_error(self._key_used)
                    _record(self._engine, error=True)
                    raise

                # Quota error → mark this key exhausted + try the next
                if rotator is not None and self._key_used:
                    rotator.record_error(self._key_used)
                    rotator.mark_exhausted(self._key_used)

                next_key = rotator.next_key() if rotator is not None else None
                if next_key is None:
                    # All keys exhausted — give up with a clear message
                    _record(self._engine, error=True)
                    raise RuntimeError(
                        f"All SerpAPI keys exhausted after {attempt + 1} "
                        f"attempt(s). Last error: {exc}. "
                        f"Rotator state: {rotator.stats() if rotator else 'no rotator'}"
                    ) from exc

                # Rebuild the inner client with the new key and retry
                self._rebuild_with_new_key(next_key)

        # All attempts exhausted (shouldn't normally reach here)
        _record(self._engine, error=True)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("SerpAPI call failed for unknown reasons")

    # ───────────────────────────────────────────────────────────────
    # Public API — same as serpapi.GoogleSearch
    # ───────────────────────────────────────────────────────────────
    def get_dict(self) -> dict:
        return self._call_with_rotation("get_dict")

    def get_json(self) -> str:
        return self._call_with_rotation("get_json")

    def get_response(self, *args: Any, **kwargs: Any):
        return self._call_with_rotation("get_response", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Forward anything we didn't explicitly override
        return getattr(self._inner, name)
