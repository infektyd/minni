"""§6.7 token-efficiency composite — task-success-per-1k-context-tokens.

The HEADLINE composite is **task-success-per-1k-tokens-of-retrieved-context**
(§6.7). Both terms are means over the IDENTICAL Layer-2 population (episode
turns), so the ratio never mixes the Layer-1 labeled-query set with the Layer-2
episode set:

    mean_ctx_tokens(adapter)   = mean over Layer-2 turns of token_cost(context)
    mean_task_success(adapter) = mean over Layer-2 turns of success ∈ {0,1}
    efficiency(adapter)        = mean_task_success / (max(mean_ctx_tokens, 1)/1000)

Both means are TURN-LEVEL (every (episode × trial) observation weighted equally),
matching the spec's "averaged across all turns in all N trials and all episodes"
— this is the ``flattened_observations`` aggregation the Layer-2 runner already
emits, NOT the per-episode-weighted ``point`` used for the §6.9 CIs.

ZERO-DENOMINATOR HANDLING (§6.7): an adapter that returns empty context on every
turn yields ``mean_ctx_tokens == 0``; the ``max(.,1)`` floor keeps the formula
defined, AND such an adapter is FLAGGED (``no_context=True``) and its composite
reported separately as "not meaningful" — never silently handed a huge score off
a tiny denominator. Pure / deterministic / offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from .runner_layer2 import AdapterLayer2Result


# Below this turn-mean context-token count we treat the adapter as having
# returned NO context (the §6.7 "rounds to 0" flag). A refuse-everything adapter
# already scores 0.0 task success, so its flagged composite is informative.
_NO_CONTEXT_EPSILON = 0.5


@dataclass(frozen=True)
class EfficiencyComposite:
    """One adapter's §6.7 token-efficiency composite (turn-level means)."""

    adapter: str
    mean_task_success: float  # turn-level mean success ∈ [0,1]
    mean_ctx_tokens: float  # turn-level mean context-only token cost
    efficiency: float  # task-success per 1k context tokens (floored denom)
    no_context: bool  # True iff mean_ctx_tokens rounds to ~0 (not meaningful)
    n_turns: int

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "mean_task_success": self.mean_task_success,
            "mean_ctx_tokens": self.mean_ctx_tokens,
            "efficiency_per_1k_ctx_tokens": self.efficiency,
            "no_context_flag": self.no_context,
            "n_turns": self.n_turns,
        }


def adapter_efficiency(result: AdapterLayer2Result) -> EfficiencyComposite:
    """Compute the §6.7 composite for one adapter from its Layer-2 trials."""
    trials = result.trials
    n = len(trials)
    if n == 0:
        return EfficiencyComposite(
            adapter=result.adapter,
            mean_task_success=0.0,
            mean_ctx_tokens=0.0,
            efficiency=0.0,
            no_context=True,
            n_turns=0,
        )
    mean_success = sum(t.success for t in trials) / n
    mean_ctx = sum(t.ctx_tokens for t in trials) / n
    no_context = mean_ctx < _NO_CONTEXT_EPSILON
    # max(mean_ctx, 1) floor -> denominator in units of 1k tokens.
    efficiency = mean_success / (max(mean_ctx, 1.0) / 1000.0)
    return EfficiencyComposite(
        adapter=result.adapter,
        mean_task_success=mean_success,
        mean_ctx_tokens=mean_ctx,
        efficiency=efficiency,
        no_context=no_context,
        n_turns=n,
    )


def efficiency_block(results: dict[str, AdapterLayer2Result]) -> dict:
    """Canonical, sorted §6.7 efficiency block for the results JSON / report."""
    return {
        "unit": "task-success per 1k context-only tokens (Layer-2 turns)",
        "note": (
            "denominator is context-only token cost (§6.6), not tokens_to_model; "
            "adapters flagged no_context returned ~0 context — composite not "
            "meaningful, reported separately (§6.7)."
        ),
        "per_adapter": {
            name: adapter_efficiency(res).to_dict()
            for name, res in sorted(results.items())
        },
    }
