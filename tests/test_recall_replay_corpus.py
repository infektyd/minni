"""Tests for scripts.recall_replay.build_corpus (Rev 5 §1, §5.2 item 1, §8.1)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from scripts.recall_replay.build_corpus import (
    TemplateTracker,
    build_corpus,
    canonical_agent_key,
    canonical_session_key,
    classify_prompt_with_templates,
    classify_tool_result_delivery,
    clip_tool_result_text,
    parse_audit_log_markdown,
    template_hash,
)
from scripts.recall_replay.private_root import PrivateRootError


def test_delivery_class_tagging() -> None:
    assert (
        classify_tool_result_delivery(
            "📓 Minni recall guard: UNCONSULTED memory hit", is_error=True
        )
        == "minni_authored"
    )
    assert (
        classify_tool_result_delivery(
            "PreToolUse:Bash hook error: Blocked: git show is not allowed", is_error=True
        )
        == "hook_deny"
    )
    assert (
        classify_tool_result_delivery(
            "Permission to use Write has been denied by the user.", is_error=True
        )
        == "permission_denied"
    )
    assert (
        classify_tool_result_delivery("Command exited with code 1: syntax error", is_error=True)
        == "tool_error"
    )
    assert classify_tool_result_delivery(" normal stdout output ", is_error=False) == "ok"


def test_scheduled_template_rule_cadence_and_bounds() -> None:
    tracker = TemplateTracker()
    long_checkin = (
        "Scheduled autonomous monitor check-in #1: inspect active worker lanes, "
        "verify test gates, and summarize open blockers."
    )
    base_ts = 1_700_000_000_000

    # 20 occurrences in 1 session within 1 hour stay human
    for i in range(20):
        res = classify_prompt_with_templates(
            long_checkin.replace("#1", f"#{i + 1}"),
            tracker=tracker,
            at=base_ts + i * 120_000,  # every 2 minutes (< 1h total)
            pid=f"p-{i}",
            session_key="sess0001",
            record=True,
        )
        assert res["kind"] == "human"
        assert res["subkind"] == "typed"

    # At >= 2 hours after first_seen in the same session, it becomes scheduled
    res_2h = classify_prompt_with_templates(
        long_checkin.replace("#1", "#21"),
        tracker=tracker,
        at=base_ts + 2 * 3600 * 1000 + 1000,
        pid="p-21",
        session_key="sess0001",
        record=True,
    )
    assert res_2h["kind"] == "scheduled"
    assert res_2h["subkind"] == "template"
    assert res_2h["rule_id"] == "scheduled.template"

    # Continue every 10 minutes up to 72 hours: never flips back to human after ring rollover
    for step in range(13, 72 * 6 + 1):
        res_later = classify_prompt_with_templates(
            long_checkin.replace("#1", f"#{step}"),
            tracker=tracker,
            at=base_ts + step * 600_000,
            pid=f"p-step-{step}",
            session_key="sess0001",
            record=True,
        )
        assert res_later["kind"] == "scheduled"

    # Multi-session rule: 3 prior occurrences across >= 2 distinct sessions within 10 minutes
    tracker2 = TemplateTracker()
    for idx, sess in enumerate(["sessa111", "sessb222", "sessa111"]):
        r = classify_prompt_with_templates(
            long_checkin,
            tracker=tracker2,
            at=base_ts + idx * 60_000,
            pid=f"ms-{idx}",
            session_key=sess,
            record=True,
        )
        assert r["kind"] == "human"

    r4 = classify_prompt_with_templates(
        long_checkin,
        tracker=tracker2,
        at=base_ts + 4 * 60_000,
        pid="ms-3",
        session_key="sessc333",
        record=True,
    )
    assert r4["kind"] == "scheduled"

    # Harvest re-evaluating ms-3 by its own pid gets the same class (scheduled)
    r4_harvest = classify_prompt_with_templates(
        long_checkin,
        tracker=tracker2,
        at=base_ts + 4 * 60_000,
        pid="ms-3",
        session_key="sessc333",
        record=False,
    )
    assert r4_harvest["kind"] == "scheduled"

    # Short prompt (< 80 chars) is never scheduled
    short_prompt = "check status of workers now"
    assert len(short_prompt) < 80
    for i in range(10):
        rs = classify_prompt_with_templates(
            short_prompt,
            tracker=tracker2,
            at=base_ts + i * 3600 * 1000,
            pid=f"short-{i}",
            session_key=f"sess{i:04d}",
            record=True,
        )
        assert rs["kind"] == "human"


def test_canonical_keys_and_subagent_matching() -> None:
    k_from_id = canonical_agent_key(agent_id="agent-7f8e9a0b")
    k_from_raw = canonical_agent_key(agent_id="7f8e9a0b")
    k_from_path = canonical_agent_key(
        transcript_path="/synthetic/projects/sess1/subagents/agent-7f8e9a0b.jsonl"
    )
    assert k_from_id == k_from_raw == k_from_path
    assert k_from_id.startswith("a-")
    assert len(k_from_id) == 14

    assert canonical_agent_key(agent_id=None, transcript_path="/synthetic/sess1.jsonl") == "main"
    s_key = canonical_session_key(session_id="sess-uuid-1234")
    assert len(s_key) == 16
    assert s_key == s_key.lower()


def test_clip_tool_result_text_and_path_redaction() -> None:
    prefix = "Header line at /Users/alice/secret_repo/src/main.py\n"
    middle = "x" * 40_000
    suffix = "\nFooter line at /private/tmp/worktree/out.txt"
    raw = prefix + middle + suffix

    clipped = clip_tool_result_text(raw)
    assert clipped["clipped"] is True
    assert clipped["orig_len"] == len(raw)
    assert len(clipped["head"]) <= 8192
    assert len(clipped["tail"]) <= 24576
    assert "alice" not in clipped["head"]
    assert "/private/tmp/" not in clipped["tail"]
    assert "<path>" in clipped["head"]
    assert "<path>" in clipped["tail"]


def test_parse_audit_log_markdown() -> None:
    md = (
        "# Audit Log\n\n"
        "## [2026-10-20T10:00:00Z] hook_session_start | Boot completed\n"
        'Details: `{"tokens": 4200, "budget": 4000}`\n\n'
        "## [2026-10-20T10:01:00Z] hook_pre_tool_use | Guard deny\n"
        "- hits: 1\n"
    )
    entries = parse_audit_log_markdown(md)
    assert len(entries) == 2
    assert entries[0]["tool"] == "hook_session_start"
    assert entries[0]["details"]["tokens"] == 4200
    assert entries[1]["tool"] == "hook_pre_tool_use"


def test_build_corpus_end_to_end_and_private_root_guard(tmp_path: Path) -> None:
    priv = tmp_path / "priv"
    priv.mkdir()
    tx_dir = tmp_path / "transcripts"
    tx_dir.mkdir()

    # Parent session transcript
    sess_file = tx_dir / "sess-alpha.jsonl"
    parent_events = [
        {
            "type": "user",
            "timestamp": "2026-10-20T10:00:00Z",
            "sessionId": "sess-alpha",
            "promptId": "p-1",
            "message": {"content": "<task-notification id=\"1\">done</task-notification>"},
        },
        {
            "type": "user",
            "timestamp": "2026-10-20T10:01:00Z",
            "sessionId": "sess-alpha",
            "promptId": "p-2",
            "message": {"content": "Please inspect the recall state logic in /Users/test/repo/file.ts"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:01:05Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "Read",
                        "input": {"file_path": "/Users/test/repo/file.ts"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-10-20T10:01:06Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-1",
                        "is_error": True,
                        "content": "📓 Minni recall guard: UNCONSULTED memory hit [[wiki/decisions/d1]]",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:01:08Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-2",
                        "name": "mcp__minni__minni_recall",
                        "input": {"query": "d1"},
                    }
                ]
            },
        },
    ]
    sess_file.write_text("\n".join(json.dumps(e) for e in parent_events) + "\n", encoding="utf-8")

    # Subagent transcript
    sub_dir = tx_dir / "sess-alpha" / "subagents"
    sub_dir.mkdir(parents=True)
    sub_file = sub_dir / "agent-sub99.jsonl"
    sub_events = [
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:02:00Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-sub-1",
                        "name": "Read",
                        "input": {"file_path": "/Users/test/repo/other.ts"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-10-20T10:02:01Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-sub-1",
                        "is_error": True,
                        "content": "📓 Minni recall guard: UNCONSULTED memory hit [[wiki/decisions/d1]]",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:02:02Z",
            "sessionId": "sess-alpha",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-sub-2",
                        "name": "Read",
                        "input": {"file_path": "/Users/test/repo/other.ts"},
                    }
                ]
            },
        },
    ]
    sub_file.write_text("\n".join(json.dumps(e) for e in sub_events) + "\n", encoding="utf-8")

    # Optional DB copy
    db_path = tmp_path / "minni_copy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE learnings (id INTEGER PRIMARY KEY, category TEXT, content TEXT, status TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO learnings VALUES (1, 'decision', 'Decision: always verify', 'active', '2026-10-20T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO learnings VALUES (2, 'preference', '<task-notification>done</task-notification>', 'active', '2026-10-20T01:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Refuses out_path outside private root
    with pytest.raises(PrivateRootError):
        build_corpus(
            transcripts_dir=tx_dir,
            out_path=tmp_path / "outside_corpus.jsonl",
            private_root=priv,
        )

    out_jsonl = priv / "corpus" / "events.jsonl"
    summary = build_corpus(
        transcripts_dir=tx_dir,
        out_path=out_jsonl,
        db_copy_path=db_path,
        private_root=priv,
    )

    assert out_jsonl.is_file()
    raw_lines = out_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) >= 6
    for line in raw_lines:
        assert "/Users/test" not in line
        assert str(tmp_path) not in line

    assert summary["prompts_total"] == 2
    assert summary["prompts_by_kind"]["skip"] == 1
    assert summary["prompts_by_kind"]["human"] == 1
    assert summary["guard_denies_total"] == 2
    assert summary["guard_denies_subagent"] == 1
    assert summary["guard_next_action"]["memory_consult"] == 1
    assert summary["guard_next_action"]["identical_reissue"] == 1
    assert summary["db_metrics"]["B7_chatter_active"] == 1
