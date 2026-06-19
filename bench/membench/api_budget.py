"""ONE process-global, cumulative LLM API-call budget across ALL roles (§7.15).

Review fix 4. ``config.MAX_API_CALLS`` is documented as a hard-abort ceiling on
CUMULATIVE LLM calls. Before this module, the agent, the judge, and the
llm_wiki curator each carried their OWN module-level counter, each capped at
``MAX_API_CALLS`` — so a real run could make up to ``3 * MAX_API_CALLS`` calls
(agent + judge + curation) before any single counter aborted, defeating the
intended combined-cost safeguard.

This module owns the SINGLE shared counter that all three roles reserve against,
so the ceiling truly limits cumulative spend across every LLM role. It is
process-global and lock-guarded (atomic check-then-increment) so constructing
multiple consumers — of any role — cannot bypass the gate or race it.

Tests reset the shared counter via :func:`reset` so the global does not leak
budget across cases. The cap is read INSIDE the lock so a test that
monkeypatches ``config.MAX_API_CALLS`` is honoured.
"""

from __future__ import annotations

import threading

from . import config

# The ONE shared counter. All LLM roles (agent, judge, llm_wiki curation) reserve
# against this single global so MAX_API_CALLS is a true CUMULATIVE ceiling.
_LOCK = threading.Lock()
_CALLS = 0


def reserve(max_api_calls: int | None = None, *, role: str = "LLM") -> None:
    """Atomically reserve one call on the shared cumulative budget or raise.

    Checks the count of calls already reserved across ALL roles against the
    effective ceiling (``config.MAX_API_CALLS`` unless ``max_api_calls`` is
    given, e.g. a test override) and increments under a lock so the
    check-then-increment cannot be raced by concurrent consumers. ``role`` only
    flavours the error message.
    """
    global _CALLS
    with _LOCK:
        cap = config.MAX_API_CALLS if max_api_calls is None else max_api_calls
        if _CALLS >= cap:
            raise RuntimeError(
                f"MAX_API_CALLS={cap} reached (cumulative across all LLM roles) "
                f"— aborting run on the {role} call (API-cost guard, §7.15)."
            )
        _CALLS += 1


def reset() -> None:
    """Reset the shared cumulative call counter (test-only helper)."""
    global _CALLS
    with _LOCK:
        _CALLS = 0


def calls_made() -> int:
    """Current cumulative reserved-call count (test/diagnostic helper)."""
    with _LOCK:
        return _CALLS
