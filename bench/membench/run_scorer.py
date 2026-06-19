"""End-to-end Layer-1 scorer CLI (slice s4).

Runs EVERY adapter over the synthetic fixture gold set, aggregates a per-adapter
scorecard, and emits canonical JSON (sorted keys) + a human-readable table. NO
LLM / network call anywhere — Layer 1 is fully offline and deterministic (§3.2).

The real ``minni`` adapter requires a live isolated daemon; when one cannot be
stood up (CI / sandbox) the spec sanctions running Minni AS A STUB so the
end-to-end scorer still exercises the full adapter roster (S1 fallback, §4). Pass
``--live-minni`` to attempt the real daemon.

Usage:
    python -m membench.run_scorer            # all adapters, table + JSON to stdout
    python -m membench.run_scorer --json     # canonical JSON only (for diffing)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .adapters.llm_wiki import LlmWikiAdapter
from .adapters.markdown_grep import MarkdownGrepAdapter
from .adapters.naive_rag import NaiveRagAdapter
from .adapters.native_platform import NativePlatformAdapter
from .adapters.sanity_random import SanityRandomAdapter
from .adapters.stub import StubAdapter
from .contract import TokenBudget
from .corpus import load_corpus
from .goldset import load_jsonl
from .runner_layer1 import (
    build_scorecards,
    canonical_json,
    render_table,
    run_layer1_gold,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "corpus_synthetic"
_GOLD_PATH = Path(__file__).resolve().parent / "fixtures" / "gold_synthetic.jsonl"


class _MinniStubAdapter(StubAdapter):
    """Minni-as-stub fallback (§4): same deterministic stub, named 'minni'.

    Used end-to-end when a live isolated daemon is unavailable, so the scorer
    still produces a full adapter roster. Reported under the 'minni' name with
    its stub provenance evident in config_hash.
    """

    name = "minni"

    def __init__(self) -> None:
        super().__init__()
        self.config_hash = "minni-as-stub-fallback"


def _build_adapters(live_minni: bool):
    adapters = [
        StubAdapter(),
        NaiveRagAdapter(),
        MarkdownGrepAdapter(),
        LlmWikiAdapter(),
        NativePlatformAdapter(),
        SanityRandomAdapter(),
    ]
    if live_minni:
        from .adapters.minni_adapter import MinniAdapter

        adapters.append(MinniAdapter())
    else:
        adapters.append(_MinniStubAdapter())
    return adapters


def run(live_minni: bool = False) -> dict:
    """Run every adapter over the fixture gold set; return the scorecard dict."""
    corpus = load_corpus(
        _FIXTURE_DIR, pinned_hash=config.FIXTURE_CORPUS_HASH, scrubbed=False
    )
    gold_items = load_jsonl(_GOLD_PATH)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)

    per_adapter = {}
    # Build every adapter up front, but guarantee teardown of ALL of them even if
    # one raises mid-loop: a bare per-iteration finally only tears down the adapter
    # currently in hand, leaking any later (un-entered) ones that allocated at
    # __init__. The pending list is the teardown ledger.
    pending = list(_build_adapters(live_minni))
    try:
        while pending:
            adapter = pending.pop(0)
            try:
                records = run_layer1_gold(adapter, corpus, gold_items, budget)
                per_adapter[adapter.name] = records
            finally:
                adapter.teardown()
    finally:
        for leftover in pending:
            leftover.teardown()
    return build_scorecards(per_adapter, config.K)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="membench Layer-1 scorer (s4)")
    parser.add_argument(
        "--json", action="store_true", help="emit canonical JSON only (for diff)"
    )
    parser.add_argument(
        "--live-minni",
        action="store_true",
        help="attempt the real isolated Minni daemon (default: minni-as-stub)",
    )
    args = parser.parse_args(argv)

    cards = run(live_minni=args.live_minni)
    if args.json:
        print(canonical_json(cards))
    else:
        print(render_table(cards))
        print()
        print(canonical_json(cards))
    return 0


if __name__ == "__main__":
    sys.exit(main())
