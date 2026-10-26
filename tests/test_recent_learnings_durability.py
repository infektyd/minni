"""S3b (§5.2.4, §5.3): durability-ranked boot ``recent_learnings``.

Pins:
1. 10 recent relay rows plus 1 older ``decision``: the ``decision`` is in
   ``## Learnings`` (and ``recent_learnings``) and no relay is.
2. When > 10 non-noise candidates exist, up to ``K`` (default 2) slots are
   reserved for the newest durable learnings (by category or content test),
   rendered first, with the remaining slots filled by recency.
3. ``MINNI_RECENT_LEARNINGS_RANK=off`` (via env or ``.runtime/flags.json``)
   restores the legacy recency-only query.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_issue_437_learnings_dedup import (  # noqa: E402,F401
    _make_db,
    setup_hermetic_principals,
)
from test_read_recent_learning_ids import (  # noqa: E402
    _insert,
    _read,
    _rendered_learning_lines,
)


def test_ten_recent_relays_plus_one_older_decision_keeps_decision_and_drops_relays(
    tmp_path: Path, monkeypatch
) -> None:
    """§5.3 RED test: 10 recent relay rows plus 1 older decision.
    The decision is in ``## Learnings`` and no relay is."""
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    relay_Payloads = [
        '<cross-session-message from="peer-1">Status ping 1</cross-session-message>',
        '<cross-session-message from="peer-2">Status ping 2</cross-session-message>',
        "Another Claude session sent a message:\nPlease check branch status 3",
        "Another Claude session sent a message:\nPlease check branch status 4",
        "[Workflow harness: step 5]\nContinue verification",
        "[Artifact comment: line 6]\nAddress nit",
        '<task-notification id="7">Background task 7 completed</task-notification>',
        "<side-sessions-event>Session 8 joined</side-sessions-event>",
        "<ci-monitor-event>Build 9 green</ci-monitor-event>",
        "<system-reminder>Only a reminder 10</system-reminder>",
    ]
    with db_obj.cursor() as c:
        decision_id = _insert(
            c,
            1,
            "decision",
            "Decision: keep SQLite WAL mode enabled on local disk.",
        )
        relay_ids = [
            _insert(c, 10 + idx, "general", payload)
            for idx, payload in enumerate(relay_Payloads)
        ]

    result = _read(minnid)

    assert "recent_learnings" in result
    recent = result["recent_learnings"]
    assert [r["learning_id"] for r in recent] == [decision_id]
    assert set(relay_ids).isdisjoint({r["learning_id"] for r in recent})

    context = result["context"]
    assert "## Learnings (main)" in context
    assert "Decision: keep SQLite WAL mode enabled on local disk." in context
    for payload in relay_Payloads:
        assert payload not in context
    assert [r["line"] for r in recent] == _rendered_learning_lines(context)


def test_k_reserved_durable_slots_prevent_ordinary_crowdout_when_over_ten_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    """§5.2.4: when > 10 non-noise candidates survive, up to K=2 durable rows
    are reserved and rendered first, followed by the newest remaining rows."""
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        # 2 older durable rows: one by category ('decision'), one by content test ('Root cause:')
        dur_older_id = _insert(
            c,
            1,
            "decision",
            "Use non-blocking O_NOFOLLOW descriptors for runtime state files.",
        )
        dur_newer_id = _insert(
            c,
            2,
            "general",
            "Root cause: missing URI flag on read-only sqlite connection.",
        )
        # 10 newer ordinary rows that would otherwise crowd out both durable rows
        ordinary_ids = [
            _insert(c, 10 + i, "fact", f"Ordinary recent observation number {i:02d}.")
            for i in range(10)
        ]

    result = _read(minnid)
    recent_ids = [r["learning_id"] for r in result["recent_learnings"]]

    # 10 total slots: 2 reserved durable (newest durable first) + 8 newest ordinary
    expected_ordinary = list(reversed(ordinary_ids))[:8]
    assert len(recent_ids) == 10
    assert recent_ids[:2] == [dur_newer_id, dur_older_id]
    assert recent_ids[2:] == expected_ordinary
    assert [r["line"] for r in result["recent_learnings"]] == _rendered_learning_lines(
        result["context"]
    )


def test_kill_switch_restores_legacy_recency_selection(
    tmp_path: Path, monkeypatch
) -> None:
    """When MINNI_RECENT_LEARNINGS_RANK is off (via env or .runtime/flags.json),
    handle_read falls back to the legacy recency query."""
    import minni.minnid as minnid

    db_obj, _wb, cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        dur_id = _insert(
            c,
            1,
            "decision",
            "Older decision that gets crowded out when ranking is off.",
        )
        ordinary_ids = [
            _insert(c, 10 + i, "fact", f"Ordinary observation {i:02d}.")
            for i in range(10)
        ]

    # 1. Via environment variable
    monkeypatch.setenv("MINNI_RECENT_LEARNINGS_RANK", "off")
    res_env_off = _read(minnid)
    assert [r["learning_id"] for r in res_env_off["recent_learnings"]] == list(
        reversed(ordinary_ids)
    )
    assert dur_id not in [r["learning_id"] for r in res_env_off["recent_learnings"]]

    # 2. Via <vault>/.runtime/flags.json when env var is unset
    monkeypatch.delenv("MINNI_RECENT_LEARNINGS_RANK", raising=False)
    runtime_dir = Path(cfg.vault_path) / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    flags_file = runtime_dir / "flags.json"
    flags_file.write_text(
        json.dumps({"MINNI_RECENT_LEARNINGS_RANK": "off"}), encoding="utf-8"
    )
    res_file_off = _read(minnid)
    assert [r["learning_id"] for r in res_file_off["recent_learnings"]] == list(
        reversed(ordinary_ids)
    )

    # 3. A symlinked flags.json is refused and defaults to ranking enabled ("on")
    outside_flags = tmp_path / "outside-flags.json"
    outside_flags.write_text(
        json.dumps({"MINNI_RECENT_LEARNINGS_RANK": "off"}), encoding="utf-8"
    )
    flags_file.unlink()
    flags_file.symlink_to(outside_flags)
    res_symlink = _read(minnid)
    assert res_symlink["recent_learnings"][0]["learning_id"] == dur_id

