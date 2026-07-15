#!/usr/bin/env python3
"""minni watch — live, read-only tail of memory activity.

Answers the operator question "is my agent actually using minni?" by
streaming, in one pane, every observable memory event:

- the per-vault Markdown audit trail (``<MINNI_HOME>/<agent>-vault/log.md``),
  which the plugin appends for every hook firing and MCP tool call, and
- the daemon's ``episodic_events`` table (``<MINNI_HOME>/minni.db``), which
  carries daemon-native events, including the durable recall trace.

Packaging-only surface (PACKAGING_PLAN.md §3): stdlib-only, never imports
engine internals, and strictly read-only — the audit files are tailed by
size/offset and the database is opened with SQLite's read-only URI mode, so
watching can never change how memory is stored, recalled, scored, or
governed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Mirrors plugins/minni/src/vault.ts recordAudit: one entry per
# `## [<ISO-8601>] <tool> | <summary>` header (fields are injection-escaped
# to stay single-line at write time), optionally followed by a ```json block.
AUDIT_HEADER = re.compile(r"^## \[([^\]]+)\]\s+([^|]+)\|\s?(.*)$")

POLL_INTERVAL_SECONDS = 1.0
BACKLOG_ENTRIES = 10
# Detail keys worth a one-line summary, in display order (the audit `details`
# blocks are free-form JSON; these are the high-signal fields the hooks and
# MCP tools actually record).
DETAIL_KEYS = ("query", "hits", "top_score", "recall_strong", "denied",
               "tool", "candidates", "session_id", "task_signature")

# Audit summaries and episodic content derive from prompts and vault text —
# attacker-influenceable. Write-time escaping protects the markdown structure,
# not the operator's terminal: strip C0/C1 control characters (ESC, BEL, CSI…)
# before printing so recalled content cannot inject terminal escapes.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _term_safe(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


def _term_safe_value(value):
    """Recursively strip control chars from strings in a details payload.
    json.dumps escapes C0 (< 0x20) but passes raw C1 bytes (0x7f–0x9f)
    through with ensure_ascii=False, so --json output needs this too."""
    if isinstance(value, str):
        return _term_safe(value)
    if isinstance(value, dict):
        return {_term_safe(str(k)): _term_safe_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_term_safe_value(v) for v in value]
    return value


def minni_home() -> Path:
    return Path(os.environ.get("MINNI_HOME", Path.home() / ".minni"))


def episodic_db_path(home: Path) -> Path:
    """The daemon honors MINNI_DB_PATH (config.py db_path); watch must tail
    the same database or daemon-side events silently vanish from the view."""
    override = os.environ.get("MINNI_DB_PATH")
    return Path(override) if override else home / "minni.db"


def agent_id_for_vault(vault_dir: Path) -> str:
    """Vault dir basename -> agent id (mirrors vault.ts
    getAgentIdFromVaultPath basename fallback, including the claude aliases)."""
    base = vault_dir.name
    if base.endswith("-vault"):
        base = base[: -len("-vault")]
        if base in ("claudecode", "claude"):
            return "claude-code"
    return base or "agent"


def discover_vault_logs(home: Path) -> dict[Path, str]:
    """Map each vault's rolling audit log to its agent id.

    MINNI_AGENT_VAULTS (a JSON object of agent id -> vault path, the same
    override the plugin's getAgentIdFromVaultPath honors) wins over basename
    inference, so `--agent` filtering matches the plugin's own attribution."""
    logs: dict[Path, str] = {}
    try:
        entries = sorted(home.iterdir())
    except OSError:
        entries = []
    try:
        home_real = home.resolve()
    except OSError:
        home_real = home
    for entry in entries:
        if not (entry.is_dir() and entry.name.endswith("-vault")):
            continue
        # Containment (mirrors path_safety.path_within_root): a symlinked
        # *-vault must not redirect the tailer outside MINNI_HOME. Operator-
        # authored MINNI_AGENT_VAULTS mappings below may point anywhere.
        # Keys are canonicalized so the same on-disk vault reachable via two
        # spellings never yields two tailers (duplicate events).
        try:
            resolved = entry.resolve()
            if not resolved.is_relative_to(home_real):
                continue
        except OSError:
            continue
        logs[resolved / "log.md"] = agent_id_for_vault(entry)

    mapping_raw = os.environ.get("MINNI_AGENT_VAULTS")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
        except ValueError:
            mapping = None
        if isinstance(mapping, dict):
            for agent_id, vault_path in mapping.items():
                if isinstance(vault_path, str) and vault_path:
                    try:
                        resolved = Path(vault_path).expanduser().resolve()
                    except OSError:
                        continue
                    logs[resolved / "log.md"] = str(agent_id)
    return logs


@dataclass
class WatchEvent:
    ts: str            # ISO-8601 as recorded
    agent: str
    tool: str
    summary: str
    source: str        # "plugin" (vault audit) or "daemon" (episodic_events)
    details: dict | None = None


def parse_audit_entries(text: str, agent: str) -> list[WatchEvent]:
    """Parse audit-log markdown into events. Tolerates a leading partial
    entry (text may start mid-file after an offset seek)."""
    events: list[WatchEvent] = []
    current: WatchEvent | None = None
    detail_lines: list[str] = []
    in_details = False

    def flush() -> None:
        nonlocal current, detail_lines, in_details
        if current is not None and detail_lines:
            try:
                current.details = json.loads("\n".join(detail_lines))
            except ValueError:
                current.details = None
        if current is not None:
            events.append(current)
        current, detail_lines, in_details = None, [], False

    for line in text.splitlines():
        header = AUDIT_HEADER.match(line)
        if header:
            flush()
            current = WatchEvent(ts=header.group(1),
                                 agent=agent,
                                 tool=header.group(2).strip(),
                                 summary=header.group(3).strip(),
                                 source="plugin")
        elif current is not None:
            if line.startswith("```json"):
                in_details = True
            elif line.startswith("```"):
                in_details = False
            elif in_details:
                detail_lines.append(line)
    flush()
    return events


class AuditTailer:
    """Offset-based tailer for one vault's rolling log.md. Rotation (the
    plugin truncates log.md back to its header at 5 MB) shows up as the file
    shrinking; we then restart from offset 0."""

    def __init__(self, path: Path, agent: str):
        self.path = path
        self.agent = agent
        self.offset = 0
        self._ino: int | None = None

    def seed(self) -> list[WatchEvent]:
        """Read the whole current file once (for backlog) and set the
        offset to its end."""
        text = self._read_from(0)
        return parse_audit_entries(text, self.agent)

    def poll(self) -> list[WatchEvent]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        events: list[WatchEvent] = []
        # Rotation is a rename (log.md -> log.1.md, then a fresh log.md), so
        # the reliable signal is the inode changing — the new file can be
        # larger than our old offset, which defeats a size-only check.
        # size < offset stays as a fallback for in-place truncation.
        rotated = (self._ino is not None and st.st_ino != self._ino)
        if rotated:
            # Entries appended between our last poll and the rotation live at
            # the tail of the rotated file — drain them before restarting.
            try:
                with open(self.path.with_name("log.1.md"), "rb") as fh:
                    fh.seek(self.offset)
                    tail = fh.read().decode("utf-8", errors="replace")
                events.extend(parse_audit_entries(tail, self.agent))
            except OSError:
                pass
            self.offset = 0
        elif st.st_size < self.offset:
            self.offset = 0
        self._ino = st.st_ino
        if st.st_size != self.offset:
            text = self._read_from(self.offset)
            events.extend(parse_audit_entries(text, self.agent))
        return events

    def _read_from(self, start: int) -> str:
        try:
            with open(self.path, "rb") as fh:
                data_ino = os.fstat(fh.fileno()).st_ino
                fh.seek(start)
                data = fh.read()
        except OSError:
            return ""
        self.offset = start + len(data)
        self._ino = data_ino
        return data.decode("utf-8", errors="replace")


class EpisodicPoller:
    """Read-only poller of the daemon's episodic_events table. Opens the
    SQLite file in read-only URI mode so a watcher can never write."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.last_event_id = 0

    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def seed(self, backlog: int,
             since_epoch: float | None = None) -> list[WatchEvent]:
        """Backlog for startup. With since_epoch, return the whole requested
        window (not just the last `backlog` rows) so --since is honest."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            # Snapshot ceiling FIRST, on the same connection, then bound the
            # window query by it — a row landing between two separate queries
            # could otherwise sit above the returned batch yet below a
            # later-computed cursor, skipped forever.
            max_id = conn.execute(
                "SELECT MAX(event_id) AS m FROM episodic_events"
            ).fetchone()["m"] or 0
            if since_epoch is not None:
                rows = conn.execute(
                    "SELECT event_id, agent_id, event_type, content,"
                    " created_at FROM episodic_events"
                    " WHERE created_at >= ? AND event_id <= ?"
                    " ORDER BY event_id ASC",
                    (since_epoch, max_id)).fetchall()
            else:
                rows = list(reversed(conn.execute(
                    "SELECT event_id, agent_id, event_type, content,"
                    " created_at FROM episodic_events WHERE event_id <= ?"
                    " ORDER BY event_id DESC LIMIT ?",
                    (max_id, backlog)).fetchall()))
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        # The cursor advances to the snapshot ceiling, window or not, so the
        # follow loop never re-emits seeded rows and never skips later ones.
        self.last_event_id = max(self.last_event_id, max_id)
        return [self._to_event(row) for row in rows]

    def poll(self) -> list[WatchEvent]:
        """Drain everything pending, in bounded batches — a burst larger than
        one batch (or the final Ctrl-C drain) must not truncate."""
        events: list[WatchEvent] = []
        while True:
            conn = self._connect()
            if conn is None:
                return events
            try:
                rows = conn.execute(
                    "SELECT event_id, agent_id, event_type, content,"
                    " created_at FROM episodic_events WHERE event_id > ?"
                    " ORDER BY event_id ASC LIMIT 200",
                    (self.last_event_id,)).fetchall()
            except sqlite3.Error:
                return events
            finally:
                conn.close()
            if rows:
                self.last_event_id = rows[-1]["event_id"]
                events.extend(self._to_event(row) for row in rows)
            if len(rows) < 200:
                return events

    @staticmethod
    def _to_event(row: sqlite3.Row) -> WatchEvent:
        ts = datetime.fromtimestamp(
            row["created_at"], tz=timezone.utc).isoformat()
        return WatchEvent(ts=ts,
                          agent=row["agent_id"] or "?",
                          tool=row["event_type"] or "event",
                          summary=(row["content"] or "").strip(),
                          source="daemon")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_sort_key(event: WatchEvent) -> tuple:
    """Chronological sort key. Plugin timestamps are millisecond 'Z'-suffixed,
    daemon ones microsecond '+00:00' — lexicographic comparison inverts them,
    so sort on the parsed instant (unparseable timestamps sort last)."""
    parsed = _parse_ts(event.ts)
    if parsed is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc), event.ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (0, parsed, event.ts)


def format_event(event: WatchEvent) -> str:
    parsed = _parse_ts(event.ts)
    clock = parsed.astimezone().strftime("%H:%M:%S") if parsed else event.ts
    line = (f"{clock}  {event.agent:<12} {event.tool:<32} "
            f"{event.summary}").rstrip()
    if event.details:
        extras = [f"{key}={event.details[key]}" for key in DETAIL_KEYS
                  if key in event.details]
        if extras:
            line += f"  ({', '.join(extras)})"
    return _term_safe(line)


def event_to_json(event: WatchEvent) -> str:
    payload = {"ts": event.ts, "agent": _term_safe(event.agent),
               "tool": _term_safe(event.tool),
               "summary": _term_safe(event.summary), "source": event.source}
    if event.details:
        payload["details"] = _term_safe_value(event.details)
    return json.dumps(payload, ensure_ascii=False)


def _emit(events: list[WatchEvent], args) -> None:
    for event in events:
        if args.agent and event.agent != args.agent:
            continue
        if args.since:
            parsed = _parse_ts(event.ts)
            if parsed is not None and parsed < args.since:
                continue
        print(event_to_json(event) if args.json else format_event(event),
              flush=True)


def refresh_tailers(tailers: dict, home: Path) -> list[AuditTailer]:
    """Reconcile the tailer map against current vault discovery, returning
    tailers created this call. Vaults can appear while watch runs (a hook's
    ensureVault on first audit); one-shot discovery would miss them forever.
    A newly discovered vault is tailed from offset 0 — its whole content is
    new to this watcher."""
    fresh: list[AuditTailer] = []
    for path, agent in discover_vault_logs(home).items():
        existing = tailers.get(path)
        if existing is None:
            tailer = AuditTailer(path, agent)
            tailers[path] = tailer
            fresh.append(tailer)
        elif existing.agent != agent:
            # Discovery relabeled the path (mapping override): adopt the new
            # id in place so --agent filtering stays accurate, offsets kept.
            existing.agent = agent
    return fresh


def run_watch(args) -> int:
    """Entry point for `minni watch`. args carries: agent (str|None),
    since (aware datetime|None), json (bool), once (bool), interval (float)."""
    home = minni_home()
    tailers = [AuditTailer(path, agent)
               for path, agent in discover_vault_logs(home).items()]
    poller = EpisodicPoller(episodic_db_path(home))

    if not tailers and not poller.db_path.exists():
        print(f"Nothing to watch under {home} — no *-vault directories and "
              "no minni.db. Is anything wired? Try: minni wire <platform>",
              file=sys.stderr)
        return 1

    # With --since the seed is the whole requested window (the _emit filter
    # trims by timestamp); without it, cap to a short recent backlog.
    since_epoch = args.since.timestamp() if args.since else None
    backlog: list[WatchEvent] = []
    for tailer in tailers:
        seeded = tailer.seed()
        backlog.extend(seeded if since_epoch is not None
                       else seeded[-BACKLOG_ENTRIES:])
    backlog.extend(poller.seed(BACKLOG_ENTRIES, since_epoch=since_epoch))
    backlog.sort(key=event_sort_key)
    _emit(backlog, args)

    if args.once:
        return 0

    if not args.json:
        watched = ", ".join(sorted({t.agent for t in tailers})) or "none"
        print(f"-- watching {len(tailers)} vault log(s) [{watched}] + "
              f"daemon events; Ctrl-C stops --", flush=True)

    tailer_map = {tailer.path: tailer for tailer in tailers}

    def poll_round() -> None:
        # Vaults can appear while we run (a hook's first ensureVault);
        # re-discover each round so late vaults are tailed, not lost.
        refresh_tailers(tailer_map, home)
        fresh: list[WatchEvent] = []
        for tailer in tailer_map.values():
            fresh.extend(tailer.poll())
        fresh.extend(poller.poll())
        fresh.sort(key=event_sort_key)
        _emit(fresh, args)

    try:
        while True:
            time.sleep(args.interval)
            poll_round()
    except KeyboardInterrupt:
        # Ctrl-C usually lands mid-sleep: drain once so operations from the
        # final interval are shown, not silently dropped.
        poll_round()
        return 0
