"""Tests for scripts.recall_replay.{labelling,calibration,replay} (Rev 5 §4.7, §5.2, §5.4, §6.5, §8.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.recall_replay.calibration import (
    calibrate_k,
    calibrate_rho,
    durability_of,
    evaluate_b7_stored_chatter,
    evaluate_content_test_precision,
    run_noise_free_reference,
    score_ups_gate,
)
from scripts.recall_replay.labelling import (
    build_labels_from_audit,
    evaluate_detector_validity,
    write_labels,
)
from scripts.recall_replay.private_root import PrivateRootError
from scripts.recall_replay.replay import (
    run_handler_replay,
    run_pure_replay,
    write_replay_report,
)


def _make_campaign_audit_fixture() -> dict[str, Any]:
    """Synthetic fixture mirroring the 18-learning, 52-surfacing (29 visible, 23 invisible) audit."""
    learnings: list[dict[str, Any]] = []
    # 2 helped learnings, 1 visible surfacing each
    for idx in (1, 2):
        learnings.append(
            {
                "id": idx,
                "category": "fix",
                "class": "helped",
                "summary": f"Helped rule {idx}",
                "surfacings": [
                    {
                        "sid": f"sid-helped-{idx}",
                        "channel": "UserPromptSubmit pointer",
                        "session": f"sess-{idx}",
                        "ts": f"2026-10-20T10:0{idx}:00Z",
                        "visible": True,
                    }
                ],
                "use_quote": {
                    "session": f"sess-{idx}",
                    "ts": f"2026-10-20T10:0{idx}:05Z",
                    "quote": f"Applied helped rule {idx}",
                },
                "use_strength": "strong — applied directly",
            }
        )

    # 2 recalled-not-used with explicit dismissal (1 visible surfacing each)
    for idx in (3, 4):
        learnings.append(
            {
                "id": idx,
                "category": "general",
                "class": "recalled-not-used",
                "summary": f"Dismissed rule {idx}",
                "notes": f"Explicitly dismissed at 10:1{idx}:05Z as unrelated",
                "surfacings": [
                    {
                        "sid": f"sid-dismissed-{idx}",
                        "channel": "UserPromptSubmit pointer",
                        "session": f"sess-{idx}",
                        "ts": f"2026-10-20T10:1{idx}:00Z",
                        "visible": True,
                        "dismissed": True,
                    }
                ],
            }
        )

    # 11 recalled-not-used learnings holding 25 visible + 23 invisible surfacings = 48 surfacings
    vis_left = 25
    invis_left = 23
    for idx in range(5, 16):
        surfs: list[dict[str, Any]] = []
        v_take = 3 if vis_left >= 3 else vis_left
        vis_left -= v_take
        for v_i in range(v_take):
            surfs.append(
                {
                    "sid": f"sid-vis-{idx}-{v_i}",
                    "channel": "PreToolUse guard" if v_i % 2 == 0 else "SessionStart injection: recent_learnings",
                    "session": f"sess-{idx}",
                    "ts": f"2026-10-20T11:{idx:02d}:{v_i:02d}Z",
                    "visible": True,
                }
            )
        inv_take = 3 if invis_left >= 3 else invis_left
        invis_left -= inv_take
        for inv_i in range(inv_take):
            surfs.append(
                {
                    "sid": f"sid-invis-{idx}-{inv_i}",
                    "channel": "SessionStart injection: recent_learnings (truncated snippet)",
                    "session": f"sess-{idx}",
                    "ts": f"2026-10-20T12:{idx:02d}:{inv_i:02d}Z",
                    "visible": False,
                }
            )
        learnings.append(
            {
                "id": idx,
                "category": "preference",
                "class": "recalled-not-used",
                "summary": f"Unused rule {idx}",
                "surfacings": surfs,
            }
        )

    # 3 never-recalled learnings (the 3 Phase 1 addressable need moments)
    for idx in (16, 17, 18):
        learnings.append(
            {
                "id": idx,
                "category": "decision",
                "class": "never-recalled",
                "summary": f"Never recalled rule {idx}",
                "surfacings": [],
                "need_moment": {
                    "session": f"sess-need-{idx}",
                    "ts": f"2026-10-21T09:0{idx - 15}:00Z",
                },
            }
        )

    scars = [
        {"id": "scar-1", "lesson": "Use git cat-file instead of git show", "recurred": True, "occurrences": 64},
        {"id": "scar-2", "lesson": "Register new test file in CHECK_PYTEST", "recurred": True, "occurrences": 2},
    ]
    return {"learnings": learnings, "scars": scars}


def test_labelling_and_detector_validity_bar(tmp_path: Path) -> None:
    priv = tmp_path / "priv_labels"
    priv.mkdir()

    fixture = _make_campaign_audit_fixture()
    labels = build_labels_from_audit(fixture)
    assert labels["summary"]["total_surfacings"] == 52
    assert labels["summary"]["visible_surfacings"] == 29
    assert labels["summary"]["invisible_surfacings"] == 23
    assert labels["summary"]["need_moments_total"] == 5
    assert labels["summary"]["need_moments_addressable_phase1"] == 3
    assert labels["summary"]["need_moments_phase1b_recurrences"] == 2

    written = write_labels(labels, private_root=priv, timestamp_tag="20261023T120000Z")
    assert Path(written["labels_path"]).is_file()
    assert Path(written["backup_path"]).is_file()
    assert Path(written["pinned_path"]).is_file()

    # Build passing detector outcomes: 2/2 helped -> applied/cited, dismissals -> dismissed, 2 FPs
    outcomes: dict[str, str] = {}
    for row in labels["surfacings"]:
        sid = row["sid"]
        if row["cause"] == "used":
            outcomes[sid] = "applied"
        elif row["label_outcome"] == "dismissed":
            outcomes[sid] = "dismissed"
        else:
            outcomes[sid] = "ignored"

    non_used_vis = [
        r["sid"]
        for r in labels["surfacings"]
        if r["visible"] and r["cause"] != "used" and r["label_outcome"] != "dismissed"
    ]
    outcomes[non_used_vis[0]] = "cited"
    outcomes[non_used_vis[1]] = "applied"

    val_pass = evaluate_detector_validity(labels, outcomes)
    assert val_pass["passed"] is True
    assert val_pass["helped_total"] == 2
    assert val_pass["helped_detected"] == 2
    assert val_pass["dismissal_violations"] == 0
    assert val_pass["false_positives"] == 2
    assert "sanity bar" in val_pass["sanity_bar_note"]

    # Fail condition 1: 1 of 2 helped missed
    bad_helped = dict(outcomes)
    bad_helped["sid-helped-1"] = "ignored"
    assert evaluate_detector_validity(labels, bad_helped)["passed"] is False

    # Fail condition 2: dismissal comes out cited
    bad_dismiss = dict(outcomes)
    bad_dismiss["sid-dismissed-3"] = "cited"
    assert evaluate_detector_validity(labels, bad_dismiss)["passed"] is False

    # Fail condition 3: 3 false positives (> 2)
    bad_fp = dict(outcomes)
    bad_fp[non_used_vis[2]] = "cited"
    res_fp = evaluate_detector_validity(labels, bad_fp)
    assert res_fp["false_positives"] == 3
    assert res_fp["passed"] is False


def test_durability_of_predicate() -> None:
    gate = "2026-10-23T00:00:00Z"

    # Noise classes
    assert durability_of({"content": "<task-notification>ok</task-notification>"}) == "noise"
    assert (
        durability_of(
            {"content": "<cross-session-message from=\"a\">hi</cross-session-message>"}
        )
        == "noise"
    )
    assert (
        durability_of({"content": "<system-reminder>only reminder</system-reminder>"})
        == "noise"
    )
    assert (
        durability_of({"content": "Summary", "source": "compact_distillation:123"}) == "noise"
    )
    assert (
        durability_of(
            {"content": "Summary", "path": "wiki/sessions/20261020-compact-abc.md"}
        )
        == "noise"
    )
    assert (
        durability_of({"content": "Log", "path": "wiki/artifacts/plan-123.log.md"})
        == "noise"
    )
    assert (
        durability_of(
            {"content": "Plan", "path": "wiki/artifacts/plan-123.md", "status": "complete"}
        )
        == "noise"
    )
    assert (
        durability_of(
            {
                "content": "- [x] slice 1\n- [x] slice 2",
                "path": "wiki/artifacts/plan-cand.md",
                "status": "candidate",
            }
        )
        == "noise"
    )
    # Accepted plan is never noise
    assert (
        durability_of(
            {"content": "- [ ] slice 1", "path": "wiki/artifacts/plan-acc.md", "status": "accepted"}
        )
        == "ordinary"
    )

    # Durable categories and synonyms
    for cat in ("decision", "fix", "procedure", "architecture", "bugfix", "scar", "adr"):
        assert durability_of({"category": cat, "content": " plain body "}) == "durable"

    # Content test prefixes and sections
    for body in (
        "Decision: use strict containment",
        "Fix: guard against null",
        "Root cause: shared file state",
        "Resolution: scope per session",
        "Invariant: never mutate stableStringify",
        "Never loosen a gate",
        "Always check realpath",
        "# Note Title\n\nSome intro\n\n## Resolution\nFixed it.",
        "# Note Title\n\n## Fix\nPatched.",
    ):
        assert (
            durability_of({"category": "general", "content": body}, content_test_enabled=True)
            == "durable"
        )
        assert (
            durability_of({"category": "general", "content": body}, content_test_enabled=False)
            == "ordinary"
        )

    # Preference before vs after gate & origin check
    assert (
        durability_of(
            {
                "category": "preference",
                "content": "Prefer concise tables",
                "origin": "human",
                "created_at": "2026-10-24T00:00:00Z",
            },
            provenance_gate_since=gate,
        )
        == "durable"
    )
    assert (
        durability_of(
            {
                "category": "preference",
                "content": "Prefer concise tables",
                "origin": "human",
                "created_at": "2026-10-20T00:00:00Z",
            },
            provenance_gate_since=gate,
        )
        == "ordinary"
    )
    assert (
        durability_of(
            {
                "category": "preference",
                "content": "Prefer concise tables",
                "created_at": "2026-10-24T00:00:00Z",
            },
            provenance_gate_since=gate,
        )
        == "ordinary"
    )


def test_content_test_precision_and_b7_fallback() -> None:
    good_sample = [
        {"content": "Root cause: race condition", "is_durable": True},
        {"content": "Decision: pin Python 3.14", "is_durable": True},
        {"content": "Never bypass hooks", "is_durable": True},
        {"content": "Always verify realpath", "is_durable": True},
        {"content": "Random observation", "is_durable": False},
    ]
    res_good = evaluate_content_test_precision(good_sample)
    assert res_good["precision"] == 1.0
    assert res_good["ships"] is True

    bad_sample = [
        {"content": "Always check this casual chat", "is_durable": False},
        {"content": "Never mind that", "is_durable": False},
        {"content": "Decision: real one", "is_durable": True},
    ]
    res_bad = evaluate_content_test_precision(bad_sample)
    assert res_bad["precision"] < 0.8
    assert res_bad["ships"] is False

    # B7 >= 80% noise -> all_slots
    b7_high = [
        {"id": i, "content": "<task-notification>done</task-notification>"}
        for i in range(9)
    ] + [{"id": 99, "category": "preference", "content": "plain pre-gate directive"}]
    b7_res_high = evaluate_b7_stored_chatter(b7_high, provenance_gate_since="2026-10-23T00:00:00Z")
    assert b7_res_high["noise_share"] >= 0.80
    assert b7_res_high["target_applies_to"] == "all_slots"

    # B7 < 80% noise -> post_gate_rows_only + residual ids
    b7_low = [
        {"id": 1, "content": "<task-notification>done</task-notification>"},
        {"id": 2, "category": "preference", "content": "untagged reminder directive 1"},
        {"id": 3, "category": "preference", "content": "untagged reminder directive 2"},
    ]
    b7_res_low = evaluate_b7_stored_chatter(b7_low, provenance_gate_since="2026-10-23T00:00:00Z")
    assert b7_res_low["noise_share"] < 0.80
    assert b7_res_low["target_applies_to"] == "post_gate_rows_only"
    assert b7_res_low["residual_row_ids"] == [2, 3]


def test_calibrate_k_and_ups_gate_crowdout_and_calibrate_rho() -> None:
    assert calibrate_k([])["K"] == 2
    assert calibrate_k([[], []]) == {
        "K": 0,
        "median_durable_pool": 0.0,
        "reservation_enabled": False,
    }
    pool_6 = [[{"category": "decision", "content": f"d-{i}"} for i in range(6)]]
    assert calibrate_k(pool_6)["K"] == 3
    pool_12 = [[{"category": "decision", "content": f"d-{i}"} for i in range(12)]]
    assert calibrate_k(pool_12)["K"] == 4

    # 22 plan journals echoing 14 prompt terms vs 1 decision note with 3 distinctive terms (1 in title)
    query = (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa "
        "lambda mu nu xi provenance gate"
    )
    notes: list[dict[str, Any]] = []
    for i in range(22):
        notes.append(
            {
                "path": f"wiki/artifacts/plan-{i:02d}.log.md",
                "title": f"Plan log {i}",
                "content": "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu",
            }
        )
    # Add 60 Filler non-noise notes so DF of distinctive terms (xi, provenance, gate) is <= 5%
    for i in range(60):
        notes.append(
            {
                "path": f"wiki/notes/filler-{i:02d}.md",
                "title": f"Filler note {i}",
                "category": "general",
                "content": f"unrelated filler topic number {i}",
            }
        )
    notes.append(
        {
            "path": "wiki/decisions/provenance-gate.md",
            "title": "Provenance gate decision",
            "category": "decision",
            "content": "Decision: enforce xi provenance gate on harvest.",
        }
    )

    gated = score_ups_gate(notes, query, rho=0.2, limit=18)
    assert gated["gate_open"] is True
    assert gated["results"][0]["path"] == "wiki/decisions/provenance-gate.md"
    assert gated["results"][0]["route"] == "b"

    ref = run_noise_free_reference(notes, query, limit=6)
    assert ref[0]["path"] == "wiki/decisions/provenance-gate.md"

    # Term-count-only note without route a or b does not open strong gate
    common_notes = [
        {
            "path": f"wiki/notes/common-{i}.md",
            "title": "General note",
            "category": "general",
            "content": "ten nine eight seven six five four three two one",
        }
        for i in range(10)
    ]
    no_gate = score_ups_gate(
        common_notes,
        "one two three four five six seven eight nine ten",
        rho=0.2,
    )
    assert no_gate["gate_open"] is False

    # calibrate_rho over synthetic turns
    turns = [
        {"prompt": query, "kind": "human"},
        {"prompt": "unrelated short query", "kind": "human"},
    ]
    rho_cal = calibrate_rho(turns, notes, candidates=(0.1, 0.2, 0.3))
    assert rho_cal["selected_rho"] in (0.1, 0.2, 0.3)
    assert 0.2 in rho_cal["candidates"]


def test_pure_replay_and_handler_replay(tmp_path: Path) -> None:
    priv = tmp_path / "priv_replay"
    priv.mkdir()

    replay_res = run_pure_replay({}, allow_synthetic_fixture=True)
    assert replay_res["today"]["all_targets_passed"] is False
    assert replay_res["target"]["all_targets_passed"] is True
    assert replay_res["target"]["metrics"]["machine_output_turns_recalled"] == 0
    assert replay_res["target"]["metrics"]["boot_bodies_within_4000_tokens_ratio"] == 1.0
    assert replay_res["target"]["metrics"]["guard_denies_claude_code"] == 0
    assert replay_res["target"]["metrics"]["pinned_helped_retained"] == 2

    with pytest.raises(PrivateRootError):
        write_replay_report(replay_res, "out.json", private_root=priv, env={"MINNI_PRIVATE_ROOT": str(tmp_path / "other")})

    report_path = write_replay_report(replay_res, "replay-summary.json", private_root=priv)
    assert report_path.is_file()

    # Handler replay driving real compiled plugins/minni/dist modules
    handler_res = run_handler_replay(
        events=[
            {
                "type": "turn_and_surface",
                "session_id": "sess-handler-1",
                "prompt_id": "pid-1",
                "prompt": "How should we handle stableStringify in envelope ordering?",
                "note_rel_path": "wiki/decisions/envelope-order.md",
                "note_title": "Envelope Priority Serializer",
                "note_body": (
                    "Decision: never mutate stableStringify because plan digests and thread "
                    "claims hash through it; use a top-level priority serializer instead."
                ),
                "assistant_reply": (
                    "We must never mutate stableStringify because plan digests and thread "
                    "claims hash through it; we will use a top-level priority serializer."
                ),
            }
        ],
        private_root=priv,
    )
    assert handler_res["status"] == "ok"
    assert len(handler_res["judged"]) == 1
    assert handler_res["judged"][0]["outcome"] in ("applied", "cited")


def test_b1_pure_replay_rejects_empty_corpus_without_synthetic_flag_and_computes_real_metrics(
    tmp_path: Path,
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    empty_priv = tmp_path / "empty_priv"
    empty_priv.mkdir()

    with pytest.raises(ValueError, match="No replay corpus found"):
        run_pure_replay(None, minni_home=empty_home, private_root=empty_priv)

    with pytest.raises(ValueError, match="No replay corpus found"):
        run_pure_replay({}, minni_home=empty_home, private_root=empty_priv)

    notes = [
        {
            "path": "wiki/decisions/realpath-guard.md",
            "title": "Strict Realpath Guard",
            "category": "decision",
            "content": "Decision: enforce strict realpath containment on private root.",
        },
        {
            "path": "wiki/decisions/sqlite-wal.md",
            "title": "SQLite WAL Busy Timeout",
            "category": "fix",
            "content": "Fix: configure busy_timeout = 5000 on SQLite connections.",
        },
    ] + [
        {
            "path": f"wiki/notes/filler-{i:02d}.md",
            "title": f"Filler Note {i}",
            "category": "general",
            "content": f"ordinary background context note number {i}",
        }
        for i in range(40)
    ]

    corpus_a = {
        "notes": notes,
        "turns": [
            {"prompt": "enforce strict realpath containment on private root", "kind": "human"},
            {"prompt": "unrelated casual greeting", "kind": "human"},
            {"prompt": "another unrelated short question", "kind": "human"},
            {"prompt": "yet another unrelated prompt", "kind": "human"},
        ],
        "surfacings": [
            {"sid": "s1", "visible": True, "outcome": "applied", "rank": 1, "durability": "durable"},
            {"sid": "s2", "visible": True, "outcome": "ignored", "rank": 2, "durability": "durable"},
            {"sid": "s3", "visible": True, "outcome": "ignored", "rank": 3, "durability": "noise"},
        ],
        "boot_tokens": [310, 350, 390],
    }
    corpus_b = {
        "notes": notes,
        "turns": [
            {"prompt": "enforce strict realpath containment on private root", "kind": "human"},
            {"prompt": "configure busy_timeout = 5000 on SQLite connections", "kind": "human"},
        ],
        "surfacings": [
            {"sid": "s1", "visible": True, "outcome": "applied", "rank": 1, "durability": "durable"},
            {"sid": "s2", "visible": True, "outcome": "cited", "rank": 1, "durability": "durable"},
        ],
        "boot_tokens": [510, 530, 550],
    }

    res_a = run_pure_replay(corpus_a)
    res_b = run_pure_replay(corpus_b)

    assert "recall_to_use_rate" in res_a
    assert "p_at_3" in res_a
    assert "p50_boot_tokens" in res_a
    assert res_a["recall_to_use_rate"] == pytest.approx(1 / 3.0)
    assert res_b["recall_to_use_rate"] == pytest.approx(1.0)
    assert res_a["p50_boot_tokens"] == 350
    assert res_b["p50_boot_tokens"] == 530
    assert res_a["target"]["metrics"]["median_boot_recall_tokens"] == 350
    assert res_b["target"]["metrics"]["median_boot_recall_tokens"] == 530
    assert res_a["p_at_3"] != res_b["p_at_3"]

    # When boot_tokens is omitted and allow_synthetic_fixture=False, p50_boot_tokens must be computed from corpus notes/boot items, NOT hardcoded 420
    corpus_no_boot_list = {
        "notes": notes,
        "turns": corpus_a["turns"],
        "surfacings": corpus_a["surfacings"],
    }
    res_no_boot = run_pure_replay(corpus_no_boot_list)
    assert res_no_boot["p50_boot_tokens"] != 420
    assert res_no_boot["p50_boot_tokens"] > 0

    # Live/private corpus loader must ingest <minni_home>/*-vault/.runtime/use-outcomes.jsonl, nested use-ledger.d/**/*.jsonl, and minni.db (learnings, documents, episodic_events)
    import json
    import sqlite3

    live_home = tmp_path / "live_home"
    live_vault = live_home / "claudecode-vault"
    (live_vault / ".runtime" / "use-ledger.d" / "nested").mkdir(parents=True)
    (live_vault / "layer1").mkdir(parents=True)
    (live_vault / "layer1" / "core.md").write_text("# Core\n\n" + ("core guidance token " * 40), encoding="utf-8")
    (live_vault / ".runtime" / "use-outcomes.jsonl").write_text(
        json.dumps({"sid": "out-1", "visible": True, "outcome": "applied", "rank": 1, "channel": "prompt_pointer", "durability": "durable"}) + "\n",
        encoding="utf-8",
    )
    (live_vault / ".runtime" / "use-ledger.d" / "nested" / "sess1.jsonl").write_text(
        json.dumps({"kind": "turn", "prompt": "enforce strict realpath containment on private root"}) + "\n",
        encoding="utf-8",
    )
    db_file = live_home / "minni.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE learnings (id INTEGER PRIMARY KEY, category TEXT, content TEXT, confidence REAL, source TEXT)")
    conn.execute("INSERT INTO learnings VALUES (1, 'decision', 'Decision: enforce strict realpath containment on private root.', 0.95, 'claude-code')")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, path TEXT, title TEXT, content TEXT)")
    conn.execute("INSERT INTO documents VALUES (10, 'wiki/decisions/doc-table.md', 'Doc Table Decision', 'Decision: loaded from documents table.')")
    conn.execute("CREATE TABLE episodic_events (id INTEGER PRIMARY KEY, session_id TEXT, summary TEXT, content TEXT)")
    conn.execute("INSERT INTO episodic_events VALUES (20, 'sess-ep-1', ' investigated realpath containment ', 'prompt turn from episodic_events')")
    conn.commit()
    conn.close()

    res_live = run_pure_replay(None, minni_home=live_home, private_root=empty_priv)
    assert res_live["recall_to_use_rate"] == pytest.approx(1.0)
    assert res_live["p50_boot_tokens"] > 0 and res_live["p50_boot_tokens"] != 420


def test_b1_labelling_reads_per_note_unannotated_boot_seen_from_recall_state(
    tmp_path: Path,
) -> None:
    import json

    recall_state_file = tmp_path / "recall-state.json"
    recall_state_file.write_text(
        json.dumps(
            {
                "noteStats": {
                    "101": {"unannotatedBootSeen": 3},
                    "102": {"unannotatedBootSeen": 1},
                    "103": {"unannotatedBootSeen": 2},
                }
            }
        ),
        encoding="utf-8",
    )

    fixture = {
        "learnings": [
            {
                "id": 101,
                "category": "decision",
                "class": "recalled-not-used",
                "surfacings": [
                    {
                        "sid": "sid-101-boot-1",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-1",
                        "ts": "2026-10-20T10:00:00Z",
                    }
                ],
            },
            {
                "id": 102,
                "category": "decision",
                "class": "recalled-not-used",
                "surfacings": [
                    {
                        "sid": "sid-102-boot-1",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-1",
                        "ts": "2026-10-20T10:00:00Z",
                    },
                    {
                        "sid": "sid-102-boot-2",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-2",
                        "ts": "2026-10-20T11:00:00Z",
                    },
                    {
                        "sid": "sid-102-boot-3",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-3",
                        "ts": "2026-10-20T12:00:00Z",
                    },
                ],
            },
            {
                "id": 103,
                "category": "decision",
                "class": "recalled-not-used",
                "surfacings": [
                    {
                        "sid": "sid-103-boot-1",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-1",
                        "ts": "2026-10-20T10:00:00Z",
                    },
                    {
                        "sid": "sid-103-boot-2",
                        "channel": "SessionStart injection: recent_learnings",
                        "session": "sess-2",
                        "ts": "2026-10-20T11:00:00Z",
                    },
                ],
            },
        ],
        "scars": [],
    }

    labels = build_labels_from_audit(fixture, recall_state=recall_state_file)
    by_sid = {s["sid"]: s for s in labels["surfacings"]}
    assert by_sid["sid-101-boot-1"]["label_outcome"] == "ignored"
    assert by_sid["sid-101-boot-1"]["confidence"] == "medium"
    assert by_sid["sid-102-boot-1"]["label_outcome"] == "abstain"
    assert by_sid["sid-102-boot-1"]["confidence"] == "low"
    assert by_sid["sid-102-boot-2"]["label_outcome"] == "abstain"
    assert by_sid["sid-102-boot-2"]["confidence"] == "low"
    assert by_sid["sid-102-boot-3"]["label_outcome"] == "ignored"
    assert by_sid["sid-102-boot-3"]["confidence"] == "medium"
    assert by_sid["sid-103-boot-1"]["label_outcome"] == "abstain"
    assert by_sid["sid-103-boot-2"]["label_outcome"] == "ignored"


def test_b1_handler_replay_never_falls_back_to_note_body_as_assistant_reply(
    tmp_path: Path,
) -> None:
    priv = tmp_path / "priv_handler_b1"
    priv.mkdir()

    handler_res = run_handler_replay(
        events=[
            {
                "type": "turn_and_surface",
                "session_id": "sess-handler-no-reply",
                "prompt_id": "pid-no-reply",
                "prompt": "How should we handle stableStringify in envelope ordering?",
                "note_rel_path": "wiki/decisions/envelope-order.md",
                "note_title": "Envelope Priority Serializer",
                "note_body": (
                    "Decision: never mutate stableStringify because plan digests and thread "
                    "claims hash through it; use a top-level priority serializer instead."
                ),
                # Intentionally omit assistant_reply: must NOT self-match note_body!
            },
            {
                "type": "turn_and_surface",
                "session_id": "sess-handler-reply-text",
                "prompt_id": "pid-reply-text",
                "prompt": "How should we handle stableStringify in envelope ordering?",
                "note_rel_path": "wiki/decisions/envelope-order.md",
                "note_title": "Envelope Priority Serializer",
                "note_body": (
                    "Decision: never mutate stableStringify because plan digests and thread "
                    "claims hash through it; use a top-level priority serializer instead."
                ),
                "reply_text": (
                    "We must never mutate stableStringify because plan digests and thread "
                    "claims hash through it per [[wiki/decisions/envelope-order]]."
                ),
            },
        ],
        private_root=priv,
    )
    assert handler_res["status"] == "ok"
    assert len(handler_res["judged"]) == 2
    by_session = {r["session_key"]: r["outcome"] for r in handler_res["judged"]}
    assert "ignored" in by_session.values()
    assert any(v in ("applied", "cited") for v in by_session.values())


