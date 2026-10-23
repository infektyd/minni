"""Offline campaign corpus builder and transcript/audit/DB parser (Rev 5 §1, §5.2 item 1, §8.1).

Extracts chronological events from parent and subagent transcripts, SessionStart
spill files, the vault markdown audit log, and an optional read-only SQLite DB
copy, writing sanitized JSONL rows strictly inside ``resolve_private_root()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from minni.afm_passes.harness_envelopes import (
    TRIM_CHARS,
    _from_units,
    _strip_blocks_detailed,
    _to_units,
    classify_prompt_envelope,
)
from scripts.recall_replay.private_root import assert_under

TEMPLATE_MIN_CHARS = 80
TEMPLATE_HASH_INPUT_CHARS = 2000
TEMPLATE_MIN_PRIOR = 3
TEMPLATE_MIN_SESSIONS = 2
TEMPLATE_MIN_SPAN_MS = 2 * 60 * 60 * 1000
TEMPLATE_RING_MAX = 16
TEMPLATE_SESSIONS_MAX = 8
TEMPLATE_ENTRIES_MAX = 500
TEMPLATE_IDLE_MAX_MS = 14 * 24 * 60 * 60 * 1000
TEMPLATE_RULE_ID = "scheduled.template"

_WHITESPACE_RUN_RE = re.compile(f"[{re.escape(TRIM_CHARS)}]+")
_DIGITS_RE = re.compile(r"\d+")

_ABS_PATH_RE = re.compile(
    r"(?:/[U]sers/[A-Za-z0-9._-]+(?:/[^\s\"'`<>|]+)*"
    r"|/[h]ome/[A-Za-z0-9._-]+(?:/[^\s\"'`<>|]+)*"
    r"|/private/(?:tmp|var|etc)(?:/[^\s\"'`<>|]+)+"
    r"|/var/folders(?:/[^\s\"'`<>|]+)+"
    r"|/tmp/[A-Za-z0-9._-]+(?:/[^\s\"'`<>|]+)*)"
)

_AUDIT_HEADER_RE = re.compile(
    r"^##\s+\[(?P<ts>[^\]]+)\]\s+(?P<tool>[^|\n]+?)(?:\s*\|\s*(?P<summary>.*))?$"
)
_AUDIT_JSON_RE = re.compile(r"`(\{.*\})`")
_MINNI_EXPLICIT_RE = re.compile(r"(?:^|__)minni_(recall|drill|thread_scar)$")


def redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with ``<path>``."""
    if not isinstance(text, str) or not text:
        return ""
    return _ABS_PATH_RE.sub("<path>", text)


def template_hash(text: str) -> str:
    """Compute the 12-hex SHA-1 template hash matching ``harness-envelope.ts:templateHash``."""
    stripped_units, _ = _strip_blocks_detailed(text if isinstance(text, str) else "")
    lowered = stripped_units.lower()
    no_digits = _DIGITS_RE.sub("N", lowered)
    collapsed = _WHITESPACE_RUN_RE.sub(" ", no_digits)
    clipped_units = (
        collapsed[:TEMPLATE_HASH_INPUT_CHARS]
        if len(collapsed) > TEMPLATE_HASH_INPUT_CHARS
        else collapsed
    )
    utf8_text = _from_units(clipped_units)
    return hashlib.sha1(utf8_text.encode("utf-8")).hexdigest()[:12]


def is_scheduled_template(
    entry: Mapping[str, Any] | None,
    at: int | float,
    pid: str,
) -> bool:
    """Evaluate the scheduled-template rule matching ``harness-envelope.ts:isScheduledTemplate``."""
    if entry is None or not isinstance(entry, Mapping):
        return False
    if not isinstance(at, (int, float)):
        return False

    occ_list = entry.get("occ", [])
    if not isinstance(occ_list, list):
        occ_list = []

    prior = 0
    own: dict[str, Any] | None = None
    for item in occ_list:
        if not isinstance(item, Mapping):
            continue
        o_pid = item.get("pid")
        o_ts = item.get("ts")
        if not isinstance(o_pid, str) or not isinstance(o_ts, (int, float)):
            continue
        if o_pid == pid:
            if own is None:
                own = {"ts": o_ts, "pid": o_pid}
            continue
        if o_ts < at:
            prior += 1

    if prior < TEMPLATE_MIN_PRIOR:
        return False

    first_seen = entry.get("first_seen")
    if isinstance(first_seen, (int, float)) and (at - first_seen) >= TEMPLATE_MIN_SPAN_MS:
        return True

    sessions = entry.get("sessions", {})
    if not isinstance(sessions, Mapping):
        return False

    before = 0
    own_excluded = False
    for first_ts in sessions.values():
        if not isinstance(first_ts, (int, float)) or first_ts >= at:
            continue
        if not own_excluded and own is not None and first_ts == own["ts"]:
            own_excluded = True
            continue
        before += 1

    return before >= TEMPLATE_MIN_SESSIONS


class TemplateTracker:
    """In-memory mirror of ``.runtime/prompt-templates.json``."""

    def __init__(self) -> None:
        self.templates: dict[str, dict[str, Any]] = {}

    def get_entry(self, h: str) -> dict[str, Any] | None:
        return self.templates.get(h)

    @staticmethod
    def _last_seen(entry: Mapping[str, Any]) -> float:
        occ = entry.get("occ", [])
        first_seen = float(entry.get("first_seen", 0))
        if occ and isinstance(occ, list):
            newest = float(occ[-1].get("ts", first_seen))
            return max(first_seen, newest)
        return first_seen

    def record_occurrence(
        self,
        *,
        h: str,
        ts: int | float,
        pid: str,
        session_key_prefix: str,
        now: int | float | None = None,
    ) -> bool:
        s_key = (session_key_prefix or "unlabeled")[:8]
        eff_now = ts if now is None else now
        entry = self.templates.get(h)
        if entry is None:
            entry = {"first_seen": ts, "occ": [], "sessions": {}}
        if any(o.get("pid") == pid for o in entry["occ"]):
            return False

        entry["first_seen"] = min(entry["first_seen"], ts)
        entry["occ"].append({"ts": ts, "pid": pid})
        entry["occ"].sort(key=lambda item: item["ts"])
        if len(entry["occ"]) > TEMPLATE_RING_MAX:
            entry["occ"] = entry["occ"][-TEMPLATE_RING_MAX:]

        sessions: dict[str, float] = entry["sessions"]
        if s_key in sessions:
            sessions[s_key] = min(sessions[s_key], float(ts))
        else:
            sessions[s_key] = float(ts)

        if len(sessions) > TEMPLATE_SESSIONS_MAX:
            sorted_pairs = sorted(sessions.items(), key=lambda kv: (kv[1], kv[0]))
            entry["sessions"] = dict(sorted_pairs[:TEMPLATE_SESSIONS_MAX])

        self.templates[h] = entry
        self._prune(eff_now)
        return True

    def _prune(self, now: int | float) -> None:
        stale = [
            h
            for h, ent in self.templates.items()
            if (now - self._last_seen(ent)) > TEMPLATE_IDLE_MAX_MS
        ]
        for h in stale:
            self.templates.pop(h, None)

        if len(self.templates) > TEMPLATE_ENTRIES_MAX:
            ordered = sorted(
                self.templates.keys(),
                key=lambda h: (self._last_seen(self.templates[h]), h),
            )
            for h in ordered[: len(self.templates) - TEMPLATE_ENTRIES_MAX]:
                self.templates.pop(h, None)


def classify_prompt_with_templates(
    text: str,
    *,
    tracker: TemplateTracker | None = None,
    at: int | float | None = None,
    pid: str | None = None,
    session_key: str = "unlabeled",
    record: bool = True,
) -> dict[str, Any]:
    """Classify a prompt with both text rules and the scheduled-template rule."""
    base = classify_prompt_envelope(text)
    if tracker is None or at is None or pid is None:
        base["template_rule"] = "off"
        return base

    base["template_rule"] = "on"
    if base["kind"] != "human" or base["subkind"] != "typed":
        return base

    stripped_units = _to_units(base["body"])
    if len(stripped_units) < TEMPLATE_MIN_CHARS:
        return base

    h = template_hash(text)
    sched = is_scheduled_template(tracker.get_entry(h), at, pid)
    if record:
        tracker.record_occurrence(h=h, ts=at, pid=pid, session_key_prefix=session_key[:8], now=at)

    base["template"] = {"hash": h, "scheduled": sched}
    if sched:
        out: dict[str, Any] = {
            "kind": "scheduled",
            "subkind": "template",
            "body": base["body"],
            "template_rule": "on",
            "rule_id": TEMPLATE_RULE_ID,
            "template": {"hash": h, "scheduled": True},
        }
        if base.get("suspicious_prefix"):
            out["suspicious_prefix"] = True
        return out
    return base


def classify_tool_result_delivery(text: str, is_error: bool = False) -> str:
    """Classify a tool_result's delivery channel/status."""
    raw = text if isinstance(text, str) else ""
    if (
        "📓 Minni" in raw
        or "Minni recall guard" in raw
        or "UNCONSULTED" in raw
        or "Consult minni_recall" in raw
    ):
        return "minni_authored"
    if raw.startswith("PreToolUse:") and (
        "hook error" in raw or "Blocked:" in raw or "Blocked by" in raw
    ):
        return "hook_deny"
    lower = raw.lower()
    if (
        "permission to use" in lower
        or "the user doesn't want to proceed" in lower
        or "user rejected tool use" in lower
        or "permission denied by" in lower
    ):
        return "permission_denied"
    if is_error:
        return "tool_error"
    return "ok"


def clip_tool_result_text(
    text: str,
    head_chars: int = 8192,
    tail_chars: int = 24576,
) -> dict[str, Any]:
    """Redact filesystem paths and clip tool output to 8 KB head + 24 KB tail."""
    raw = text if isinstance(text, str) else ""
    redacted = redact_paths(raw)
    if len(redacted) <= head_chars + tail_chars:
        return {
            "head": redacted,
            "tail": "",
            "clipped": False,
            "orig_len": len(raw),
        }
    return {
        "head": redacted[:head_chars],
        "tail": redacted[-tail_chars:],
        "clipped": True,
        "orig_len": len(raw),
    }


def canonical_session_key(
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> str:
    """Compute canonical 16-hex session key matching ``use-ledger.ts:sessionKeyFrom``."""
    if isinstance(session_id, str) and session_id.strip():
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    if transcript_path is not None:
        raw_p = str(transcript_path)
        if raw_p and "\x00" not in raw_p:
            real_p = os.path.realpath(raw_p)
            return "tx-" + hashlib.sha256(real_p.encode("utf-8")).hexdigest()[:16]
    return "unlabeled"


def _canonical_agent_id(raw: str) -> str:
    val = re.sub(r"\.jsonl$", "", raw)
    val = re.sub(r"^agent-", "", val)
    return val or raw


def canonical_agent_key(
    agent_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> str:
    """Compute canonical agent key (``main`` or ``a-<12hex>``) matching ``use-ledger.ts:agentKeyFrom``."""
    if isinstance(agent_id, str) and agent_id.strip():
        cid = _canonical_agent_id(agent_id.strip())
        return "a-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:12]
    if transcript_path is not None:
        parts = [p for p in re.split(r"[\\/]+", str(transcript_path)) if p]
        if parts and "subagents" in parts[:-1]:
            cid = _canonical_agent_id(parts[-1])
            return "a-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:12]
    return "main"


def parse_audit_log_markdown(text: str) -> list[dict[str, Any]]:
    """Parse ``audit-log.md`` sections into structured records."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        m = _AUDIT_HEADER_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            current = {
                "ts": m.group("ts").strip(),
                "tool": m.group("tool").strip(),
                "summary": (m.group("summary") or "").strip(),
                "details": {},
                "lines": [],
            }
            continue
        if current is not None and line:
            current["lines"].append(redact_paths(line))
            jm = _AUDIT_JSON_RE.search(line)
            if jm:
                try:
                    parsed = json.loads(jm.group(1))
                    if isinstance(parsed, dict):
                        current["details"].update(parsed)
                except json.JSONDecodeError:
                    pass
    if current is not None:
        entries.append(current)
    return entries


def _ts_to_ms(ts: Any, fallback: int) -> int:
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str) and ts.strip():
        try:
            clean = ts.strip().replace("Z", "+00:00")
            return int(datetime.fromisoformat(clean).timestamp() * 1000)
        except ValueError:
            pass
    return fallback


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item.get("content"), str):
                    chunks.append(item["content"])
        return "\n".join(chunks)
    return ""


def _inspect_db_copy(db_copy_path: Path) -> dict[str, Any]:
    uri = f"file:{os.path.realpath(db_copy_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, category, content, status, created_at FROM learnings WHERE status = 'active'"
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return {"active_total": 0, "B7_chatter_active": 0, "B25_durable_category": 0, "B25_preference": 0}
    conn.close()

    durable_cats = {"decision", "fix", "procedure", "architecture", "preference", "bugfix", "scar", "adr"}
    chatter_count = 0
    durable_count = 0
    pref_count = 0
    for r in rows:
        cat = (r["category"] or "").strip().lower()
        body = r["content"] or ""
        if cat in durable_cats:
            durable_count += 1
        if cat == "preference":
            pref_count += 1
        env = classify_prompt_envelope(body)
        if env["kind"] in ("skip", "wrapped") or (
            env["kind"] == "human" and env["subkind"] == "empty" and env.get("rule_id") == "block.system_reminder"
        ):
            chatter_count += 1

    return {
        "active_total": len(rows),
        "B7_chatter_active": chatter_count,
        "B25_durable_category": durable_count,
        "B25_preference": pref_count,
    }


def build_corpus(
    *,
    transcripts_dir: str | Path,
    out_path: str | Path,
    audit_log_path: str | Path | None = None,
    spills_dir: str | Path | None = None,
    db_copy_path: str | Path | None = None,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Scan transcripts, audit log, spills, and optional DB copy into a sanitized JSONL corpus."""
    out_file = assert_under(out_path, root=private_root, env=env)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    # Re-verify after mkdir so no symlink parent was traversed
    out_file = assert_under(out_path, root=private_root, env=env)

    tx_root = Path(transcripts_dir)
    tx_files = sorted(tx_root.rglob("*.jsonl")) if tx_root.is_dir() else []

    raw_records: list[tuple[int, int, Path, dict[str, Any], int]] = []
    seq = 0
    for tf in tx_files:
        for raw_line in tf.read_text(encoding="utf-8", errors="replace").splitlines():
            line_bytes = len(raw_line.encode("utf-8"))
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            seq += 1
            ts_ms = _ts_to_ms(obj.get("timestamp") or obj.get("ts"), 1_700_000_000_000 + seq * 1000)
            raw_records.append((ts_ms, seq, tf, obj, line_bytes))

    raw_records.sort(key=lambda item: (item[0], item[1]))

    tracker = TemplateTracker()
    corpus_rows: list[dict[str, Any]] = []
    prompts_by_kind: dict[str, int] = {"human": 0, "skip": 0, "wrapped": 0, "scheduled": 0}
    prompts_by_subkind: dict[str, int] = {}
    tool_calls_total = 0
    tool_calls_subagent = 0
    guard_denies_total = 0
    guard_denies_subagent = 0
    guard_cross_session = 0
    guard_next_action = {"identical_reissue": 0, "memory_consult": 0, "other": 0}

    last_armed_session: str | None = None
    pending_deny_by_agent: dict[tuple[str, str], str] = {}
    tool_id_to_name: dict[str, str] = {}

    for ts_ms, _, tf, obj, line_bytes in raw_records:
        sess_id = obj.get("sessionId") or obj.get("session_id") or tf.stem
        s_key = canonical_session_key(session_id=str(sess_id), transcript_path=tf)
        a_key = canonical_agent_key(agent_id=obj.get("agentId") or obj.get("agent_id"), transcript_path=tf)
        ts_iso = (
            obj.get("timestamp")
            if isinstance(obj.get("timestamp"), str)
            else datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()
        )
        msg = obj.get("message") if isinstance(obj.get("message"), Mapping) else obj
        content = msg.get("content") if isinstance(msg, Mapping) else obj.get("content")
        entry_type = obj.get("type")

        if entry_type == "user":
            # Check if tool_result blocks exist inside content
            tool_results = (
                [b for b in content if isinstance(b, Mapping) and b.get("type") == "tool_result"]
                if isinstance(content, list)
                else []
            )
            if tool_results:
                for tr in tool_results:
                    tr_text = _extract_text_from_content(tr.get("content", ""))
                    is_err = bool(tr.get("is_error"))
                    d_class = classify_tool_result_delivery(tr_text, is_error=is_err)
                    clipped = clip_tool_result_text(tr_text)
                    tu_id = str(tr.get("tool_use_id") or "")
                    t_name = tool_id_to_name.get(tu_id, "unknown")
                    if d_class == "minni_authored" and is_err:
                        guard_denies_total += 1
                        if a_key != "main":
                            guard_denies_subagent += 1
                        if last_armed_session is not None and last_armed_session != s_key:
                            guard_cross_session += 1
                        pending_deny_by_agent[(s_key, a_key)] = t_name
                    corpus_rows.append(
                        {
                            "event": "tool_result",
                            "ts": ts_iso,
                            "session_key": s_key,
                            "agent_key": a_key,
                            "tool_use_id": tu_id,
                            "tool_name": t_name,
                            "is_error": is_err,
                            "delivery_class": d_class,
                            "text": clipped,
                            "tx_growth_bytes": line_bytes,
                        }
                    )
                continue

            prompt_text = _extract_text_from_content(content)
            pid = str(obj.get("promptId") or obj.get("prompt_id") or f"pid-{ts_ms}")
            cls = classify_prompt_with_templates(
                prompt_text,
                tracker=tracker,
                at=ts_ms,
                pid=pid,
                session_key=s_key,
                record=True,
            )
            kind = cls["kind"]
            subkind = cls["subkind"]
            prompts_by_kind[kind] = prompts_by_kind.get(kind, 0) + 1
            prompts_by_subkind[subkind] = prompts_by_subkind.get(subkind, 0) + 1
            if kind in ("human", "wrapped") and cls["body"]:
                last_armed_session = s_key

            corpus_rows.append(
                {
                    "event": "prompt",
                    "ts": ts_iso,
                    "session_key": s_key,
                    "agent_key": a_key,
                    "prompt_id": pid,
                    "envelope_kind": kind,
                    "subkind": subkind,
                    "rule_id": cls.get("rule_id"),
                    "body": redact_paths(cls["body"]),
                    "tx_growth_bytes": line_bytes,
                }
            )

        elif entry_type == "assistant":
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if not isinstance(b, Mapping) or b.get("type") != "tool_use":
                    continue
                t_name = str(b.get("name") or "")
                tu_id = str(b.get("id") or "")
                tool_id_to_name[tu_id] = t_name
                tool_calls_total += 1
                if a_key != "main":
                    tool_calls_subagent += 1

                agent_pair = (s_key, a_key)
                if agent_pair in pending_deny_by_agent:
                    denied_tool = pending_deny_by_agent.pop(agent_pair)
                    if _MINNI_EXPLICIT_RE.search(t_name):
                        guard_next_action["memory_consult"] += 1
                    elif t_name == denied_tool:
                        guard_next_action["identical_reissue"] += 1
                    else:
                        guard_next_action["other"] += 1

                explicit_m = _MINNI_EXPLICIT_RE.search(t_name)
                corpus_rows.append(
                    {
                        "event": "minni_call" if explicit_m else "tool_call",
                        "ts": ts_iso,
                        "session_key": s_key,
                        "agent_key": a_key,
                        "tool_use_id": tu_id,
                        "tool_name": t_name,
                        "minni_op": explicit_m.group(1) if explicit_m else None,
                        "input_summary": redact_paths(json.dumps(b.get("input", {}), sort_keys=True)),
                        "tx_growth_bytes": line_bytes,
                    }
                )

    session_start_lengths: list[int] = []
    if spills_dir is not None and Path(spills_dir).is_dir():
        for sf in sorted(Path(spills_dir).glob("*")):
            if sf.is_file():
                body_txt = sf.read_text(encoding="utf-8", errors="replace")
                session_start_lengths.append(len(body_txt))
                corpus_rows.append(
                    {
                        "event": "session_start",
                        "spill_id": hashlib.sha1(sf.name.encode("utf-8")).hexdigest()[:12],
                        "char_len": len(body_txt),
                        "over_host_cap": len(body_txt) > 10_000,
                    }
                )

    audit_entries: list[dict[str, Any]] = []
    if audit_log_path is not None and Path(audit_log_path).is_file():
        audit_entries = parse_audit_log_markdown(
            Path(audit_log_path).read_text(encoding="utf-8", errors="replace")
        )

    db_metrics = (
        _inspect_db_copy(Path(db_copy_path))
        if db_copy_path is not None and Path(db_copy_path).is_file()
        else {
            "active_total": 0,
            "B7_chatter_active": 0,
            "B25_durable_category": 0,
            "B25_preference": 0,
        }
    )

    with out_file.open("w", encoding="utf-8") as fh:
        for row in corpus_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    return {
        "out_path": str(out_file),
        "rows_written": len(corpus_rows),
        "prompts_total": sum(prompts_by_kind.values()),
        "prompts_by_kind": prompts_by_kind,
        "prompts_by_subkind": prompts_by_subkind,
        "tool_calls_total": tool_calls_total,
        "tool_calls_subagent": tool_calls_subagent,
        "guard_denies_total": guard_denies_total,
        "guard_denies_subagent": guard_denies_subagent,
        "guard_cross_session": guard_cross_session,
        "guard_next_action": guard_next_action,
        "session_start_count": len(session_start_lengths),
        "session_start_over_cap": sum(1 for ln in session_start_lengths if ln > 10_000),
        "audit_entries_parsed": len(audit_entries),
        "db_metrics": db_metrics,
    }
