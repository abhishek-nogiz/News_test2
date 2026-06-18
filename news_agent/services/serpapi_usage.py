"""Per-agent SerpAPI usage tracking via contextvars.

How it works
------------
1. `pipeline.py` calls `bind(run, stage_name)` right before each agent's
   `execute()` method runs, and `unbind(token)` right after (in a `finally`
   block). This binds the current (run, stage) onto a ContextVar.

2. Any code that runs inside that `execute()` (including deep helper calls
   like `ResearchService._search_context_reference`) can use
   `InstrumentedGoogleSearch` as a drop-in replacement for
   `serpapi.GoogleSearch`. The wrapper reads the bound (run, stage) and
   increments the counters on `PipelineRun.serpapi_by_stage`.

3. If no (run, stage) is bound — e.g. someone constructs
   `InstrumentedGoogleSearch` outside the pipeline — calls are silently
   no-op for accounting. The actual SerpAPI request still happens.

4. Errors are counted separately (`stats.errors += 1`) and the original
   exception is re-raised so your existing error handling is unchanged.

What gets counted
-----------------
* `stats.calls`         — total successful SerpAPI calls in this stage
* `stats.errors`        — total SerpAPI calls that raised an exception
* `stats.by_engine`     — per-engine breakdown, e.g.
                          `{"google_trends_trending_now": 1, "google_news": 1, "google": 3}`

Zero changes to existing agent code are required — both
`trends/service.py` and `research/service.py` only need a one-line import
swap from `from serpapi import GoogleSearch` to
`from ..serpapi_usage import InstrumentedGoogleSearch as GoogleSearch`.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import PipelineRun


@dataclass(slots=True)
class _CurrentCall:
    run: "PipelineRun"
    stage: str


# ContextVar propagates through the entire synchronous call stack
# (including into deeply-nested helper methods) automatically.
_current: ContextVar[_CurrentCall | None] = ContextVar(
    "news_agent_serpapi_current", default=None
)


def bind(run: "PipelineRun", stage: str):
    """Bind (run, stage) as the current attribution target.

    Returns a token that must be passed to `unbind()` in a `finally` block.
    """
    return _current.set(_CurrentCall(run=run, stage=stage))


def unbind(token) -> None:
    """Reset the ContextVar to its previous value."""
    _current.reset(token)


def _record(engine: str, *, error: bool = False) -> None:
    """Record one SerpAPI call against the currently-bound (run, stage).

    Silently no-ops if nothing is bound (e.g. calls made outside the
    pipeline) so utility scripts can still construct
    `InstrumentedGoogleSearch` without breaking.
    """
    cur = _current.get()
    if cur is None:
        return

    # Lazy import to avoid the circular import:
    #   models.py -> (nothing) -> serpapi_usage.py -> models.py
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


class InstrumentedGoogleSearch:
    """Drop-in replacement for `serpapi.GoogleSearch`.

    Same constructor signature, same `.get_dict()` / `.get_json()` /
    `.get_response()` API — but every successful or failed call is
    auto-attributed to the currently running agent stage via the
    contextvars binding set up in `pipeline.py`.

    Usage:
        # Before (in trends/service.py and research/service.py):
        from serpapi import GoogleSearch
        response = GoogleSearch(params).get_dict()

        # After (one-line change):
        from ..serpapi_usage import InstrumentedGoogleSearch as GoogleSearch
        response = GoogleSearch(params).get_dict()
    """

    def __init__(self, params: dict | None = None, **kwargs: Any) -> None:
        from serpapi import GoogleSearch as _Real

        # Construct the real client first. If the params are bad, we let
        # the real exception propagate WITHOUT recording — nothing was
        # actually sent to SerpAPI.
        self._inner = _Real(params, **kwargs)

        # Pull out the engine name for accounting. SerpAPI's params dict
        # always carries an "engine" key (e.g. "google", "google_news",
        # "google_trends_trending_now"). If it's missing for any reason
        # we fall back to "unknown" so we never silently lose a count.
        if isinstance(params, dict):
            self._engine = params.get("engine", "unknown")
        else:
            self._engine = kwargs.get("engine", "unknown")

    def get_dict(self) -> dict:
        try:
            result = self._inner.get_dict()
        except Exception:
            _record(self._engine, error=True)
            raise
        _record(self._engine)
        return result

    def get_json(self) -> str:
        try:
            result = self._inner.get_json()
        except Exception:
            _record(self._engine, error=True)
            raise
        _record(self._engine)
        return result

    def get_response(self, *args: Any, **kwargs: Any):
        try:
            result = self._inner.get_response(*args, **kwargs)
        except Exception:
            _record(self._engine, error=True)
            raise
        _record(self._engine)
        return result

    def __getattr__(self, name: str) -> Any:
        # Forward any attribute we didn't explicitly override
        # (e.g. `.params`, `.backend`, `.serpapi_client`) to the real client.
        return getattr(self._inner, name)
