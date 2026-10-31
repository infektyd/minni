"""Class G reproduction tests for use-ledger outcome aggregation in decay.py."""

from __future__ import annotations

import json
from pathlib import Path

from minni.decay import aggregate_use_ledger


def test_class_g_decay_aggregate_use_ledger_counts_cited_cited_handle_applied_and_opened(
    tmp_path: Path,
) -> None:
    """Class G: exposure rows in `.runtime/use-ledger` with outcome in
    {'cited', 'cited_handle', 'applied', 'opened'} must count as positive uses
    even when `credits` is empty (e.g. `cited_handle` and `opened` never populate
    `credits`, which is `used_ii` only).
    """
    ledger_dir = tmp_path / ".runtime" / "use-ledger" / "deadbeef01234567"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = ledger_dir / "2026-10-31.jsonl"

    rows = [
        {
            "v": 1,
            "kind": "exposure",
            "ts": "2026-10-31T10:00:00.000Z",
            "session_key": "deadbeef01234567",
            "agent_key": "main",
            "wire": "claude-code",
            "principal": "claude-code",
            "exposure_id": "1111111111111111",
            "reset_id": "initial",
            "mkey": "doc:personal:42",
            "first_sid": "aaaa1111aaaa1111",
            "channel": "prompt_pointer",
            "visible": True,
            "outcome": "cited_handle",
            "credits": [],
            "final": True,
        },
        {
            "v": 1,
            "kind": "exposure",
            "ts": "2026-10-31T10:05:00.000Z",
            "session_key": "deadbeef01234567",
            "agent_key": "main",
            "wire": "claude-code",
            "principal": "claude-code",
            "exposure_id": "2222222222222222",
            "reset_id": "initial",
            "mkey": "doc:personal:43",
            "first_sid": "bbbb2222bbbb2222",
            "channel": "prompt_pointer",
            "visible": True,
            "outcome": "opened",
            "credits": [],
            "final": True,
        },
        {
            "v": 1,
            "kind": "exposure",
            "ts": "2026-10-31T10:10:00.000Z",
            "session_key": "deadbeef01234567",
            "agent_key": "main",
            "wire": "claude-code",
            "principal": "claude-code",
            "exposure_id": "3333333333333333",
            "reset_id": "initial",
            "mkey": "doc:personal:44",
            "first_sid": "cccc3333cccc3333",
            "channel": "prompt_pointer",
            "visible": True,
            "outcome": "ignored",
            "credits": [],
            "final": True,
        },
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    stats = aggregate_use_ledger(tmp_path)
    assert stats["doc:personal:42"].surfaces == 1
    assert stats["doc:personal:42"].uses == 1, "cited_handle must count as a positive use in decay"
    assert stats["doc:personal:43"].surfaces == 1
    assert stats["doc:personal:43"].uses == 1, "opened must count as a positive use in decay"
    assert stats["doc:personal:44"].surfaces == 1
    assert stats["doc:personal:44"].uses == 0, "ignored must not count as a use"
