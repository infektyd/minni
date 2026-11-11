"""Plain-English human digest generator (<minni_home>/TODAY.md).

Stdlib-only module (PACKAGING_PLAN.md §3): imported eagerly by ``minni_cli.py``
and called safely from daemon governance, repo-map, and watch loops. Must never
import third-party ML/YAML libraries (numpy, torch, faiss, sentence_transformers,
yaml) or heavy engine modules.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("minni.today_digest")

_CONTROL_CHARS_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_EVIDENCE_TAG_RE = re.compile(r"</?(?:EVIDENCE|minni:context)[^>]*>", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s\"'`:)]+")
_GIT_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_HEX_VAULT_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})-vault$"
)
_WRAPPER_NOISE_RE = re.compile(
    r"(?im)(?:<SYSTEM_MESSAGE>|<MINNI_ENVELOPE>|<task-notification>|<background-task>"
    r"|\[Message\]\s+timestamp=|^Created At:\s+\d{4}-\d{2}-\d{2})"
)

_TERMINAL_THREAD_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "done",
        "cancelled",
        "canceled",
        "abandoned",
        "archived",
        "accepted",
        "superseded",
        "rejected",
    }
)

_SCAR_CATEGORIES = frozenset(
    {
        "scar",
        "gotcha",
        "fix",
        "bugfix",
        "correction",
        "error",
        "incident",
        "bug",
        "pitfall",
        "thread_scar",
    }
)

_EXCLUDED_REL_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        "DerivedData",
    }
)

_CLOSED_PAGE_STATUSES = frozenset(
    {
        "draft",
        "expired",
        "rejected",
        "superseded",
        "retired",
        "archived",
        "deleted",
    }
)
_BLOCKED_PRIVACY_LEVELS = frozenset({"blocked", "secret"})
_FRONTMATTER_PRIVACY_KEY_RE = re.compile(
    r"^\s*['\"]?(privacy|privacy_level)['\"]?\s*:\s*(.*)$", re.IGNORECASE
)

_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH_BY_HOME: dict[str, float] = {}

_MAP_FRESHNESS_LOCK = threading.Lock()
_MAP_FRESHNESS_CACHE: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
_MAP_FRESHNESS_TTL_SEC = 5.0
_MAP_FRESHNESS_MAX_ENTRIES = 64


def _term_safe(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


def _redact_home_paths(text: str) -> str:
    return _HOME_PATH_RE.sub("~", text)


def _strip_frontmatter_and_wrappers(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _EVIDENCE_TAG_RE.sub("", raw)
    lines = cleaned.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip() == "---":
        end = idx + 1
        while end < len(lines):
            if lines[end].strip() in ("---", "..."):
                idx = end + 1
                break
            end += 1
    kept: list[str] = []
    for line in lines[idx:]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            if not s:
                continue
        kept.append(s)
    joined = " ".join(kept)
    joined = re.sub(r"\s+", " ", _term_safe(_redact_home_paths(joined))).strip()
    return joined


def _clean_yaml_privacy_tokens(raw_val: str) -> list[str]:
    in_single = False
    in_double = False
    cut = len(raw_val)
    for i, ch in enumerate(raw_val):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or raw_val[i - 1].isspace():
                cut = i
                break
    s = raw_val[:cut].strip()
    if not s:
        return []
    items = s[1:-1].split(",") if (s.startswith("[") and s.endswith("]")) else [s]
    out: list[str] = []
    for item in items:
        tok = item.strip().strip("'\"").strip().lower()
        if tok:
            out.append(tok)
    return out


def _frontmatter_declares_blocked(raw: str) -> bool:
    if not raw:
        return False
    cleaned = _EVIDENCE_TAG_RE.sub("", raw)
    lines = cleaned.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return False
    end = idx + 1
    while end < len(lines):
        if lines[end].strip() in ("---", "..."):
            fm_lines = lines[idx + 1 : end]
            break
        end += 1
    else:
        fm_lines = lines[idx + 1 : min(len(lines), idx + 64)]

    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        m = _FRONTMATTER_PRIVACY_KEY_RE.match(line)
        if m:
            tokens = _clean_yaml_privacy_tokens(m.group(2))
            if any(t in _BLOCKED_PRIVACY_LEVELS for t in tokens):
                return True
            if not tokens or tokens[0] in ("|", ">", "|-", ">-", "|+", ">+"):
                base_indent = len(line) - len(line.lstrip())
                j = i + 1
                while j < len(fm_lines):
                    sub = fm_lines[j]
                    if not sub.strip():
                        j += 1
                        continue
                    sub_indent = len(sub) - len(sub.lstrip())
                    if sub_indent <= base_indent and not sub.lstrip().startswith("-"):
                        break
                    sub_val = sub.lstrip()
                    if sub_val.startswith("-"):
                        sub_val = sub_val[1:].strip()
                    if any(t in _BLOCKED_PRIVACY_LEVELS for t in _clean_yaml_privacy_tokens(sub_val)):
                        return True
                    j += 1
        i += 1
    return False


def _is_wrapper_or_blocked(raw_text: str, privacy: object = None) -> bool:
    if privacy is not None and str(privacy).strip().lower() in _BLOCKED_PRIVACY_LEVELS:
        return True
    if not raw_text or not raw_text.strip():
        return True
    if _frontmatter_declares_blocked(raw_text):
        return True
    if _WRAPPER_NOISE_RE.search(raw_text):
        return True
    return False


def _short_summary(raw_text: str, max_chars: int = 160) -> str:
    clean = _strip_frontmatter_and_wrappers(raw_text)
    if len(clean) > max_chars:
        return clean[: max_chars - 3].rstrip() + "..."
    return clean


def _is_memory_db_path(db_path: Path | str | None) -> bool:
    if db_path is None:
        return False
    db_str = str(db_path).strip()
    return db_str == ":memory:" or "mode=memory" in db_str


def resolve_minni_home(
    minni_home: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Path:
    """Resolve Minni home without dereferencing a leaf symlink."""
    if minni_home is not None and str(minni_home).strip():
        return Path(os.path.expanduser(str(minni_home).strip()))
    env_home = (os.environ.get("MINNI_HOME") or "").strip()
    if env_home:
        return Path(os.path.expanduser(env_home))
    if db_path is not None and str(db_path).strip() and not _is_memory_db_path(db_path):
        expanded = os.path.expanduser(str(db_path).strip())
        parent = Path(expanded).parent
        if str(parent) in ("", "."):
            return Path(os.path.abspath(expanded)).parent
        return parent
    return Path(os.path.expanduser("~/.minni"))


def _is_safe_contained_file(
    target: Path,
    *,
    home: Path,
    vault_root: Path | None = None,
) -> bool:
    """Verify target is a regular non-symlink file strictly inside home (and vault_root)
    with no '..' segments or intermediate symlink components beneath home."""
    try:
        if ".." in target.parts:
            return False
        if home.is_symlink() or target.is_symlink():
            return False
        home_real = home.resolve()
        v_real: Path | None = None
        if vault_root is not None:
            if vault_root.is_symlink():
                return False
            v_real = vault_root.resolve()
            if v_real != home_real and home_real not in v_real.parents:
                return False

        reached_home = False
        reached_vault = v_real is None
        cur = target
        while True:
            if cur.is_symlink():
                return False
            cur_real = cur.resolve()
            if cur != target:
                if v_real is not None and cur_real == v_real:
                    reached_vault = True
                if cur_real == home_real:
                    reached_home = True
                    break
            parent = cur.parent
            if parent == cur:
                break
            cur = parent

        if not reached_home or not reached_vault:
            return False
        if not target.is_file() or target.is_symlink():
            return False
        target_real = target.resolve()
        if home_real not in target_real.parents:
            return False
        if v_real is not None and v_real not in target_real.parents:
            return False
        return True
    except OSError:
        return False


def normalize_workspace_id(raw: object) -> str:
    if raw is None:
        return "default"
    s = str(raw).strip()
    if not s:
        return "default"
    low = s.lower()
    if low in ("default", "workspace-default", "*", "workspace-unknown", "unknown"):
        return "default"
    if low in ("global", "workspace-global"):
        return "global"
    if low.startswith("workspace-"):
        slug = re.sub(r"[^a-z0-9._-]+", "-", low[len("workspace-") :]).strip("-")
        return f"workspace-{slug}" if slug else "default"
    slug = re.sub(r"[^a-z0-9._-]+", "-", low).strip("-")
    return slug or "default"


def human_project_label(workspace_id: object, fallback_name: str | None = None) -> str:
    """Convert internal workspace_id ('workspace-alpha', 'global') to a plain human title."""
    norm = normalize_workspace_id(workspace_id)
    if norm in ("default", "global"):
        if fallback_name and fallback_name.strip():
            raw_base = fallback_name.strip()
        else:
            return "General / Cross-Project"
    elif norm.startswith("workspace-"):
        raw_base = norm[len("workspace-") :]
    else:
        raw_base = norm

    parts = [p for p in re.split(r"[-_./]+", raw_base) if p]
    if not parts:
        return "General / Cross-Project"
    titled = [p.upper() if len(p) <= 2 and p.isalpha() else p.capitalize() for p in parts]
    return _term_safe(" ".join(titled))


def _pid_is_live(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _parse_epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)
        return fval / 1000.0 if fval > 1e12 else fval
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        fval = float(raw)
        return fval / 1000.0 if fval > 1e12 else fval
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _format_age(seconds: float) -> str:
    sec = max(0, int(seconds))
    if sec < 60:
        return "just now"
    mins = sec // 60
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    rem_m = mins % 60
    return f"{hrs}h {rem_m}m ago" if rem_m else f"{hrs}h ago"


def _probe_daemon_running(home: Path) -> bool:
    sock_override = (os.environ.get("MINNI_SOCKET_PATH") or "").strip()
    sock_path = Path(os.path.expanduser(sock_override)) if sock_override else (home / "run" / "minnid.sock")
    if sock_path.exists() and not sock_path.is_symlink():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                s.connect(str(sock_path))
                return True
        except OSError:
            pass
    pid_file = home / "run" / "minnid.pid"
    if pid_file.is_file() and not pid_file.is_symlink():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            if _pid_is_live(pid) and sock_path.exists():
                return True
        except (OSError, ValueError):
            pass
    return False


def _agent_label_for_vault(vault_dir: Path) -> str:
    name = vault_dir.name
    if name.endswith("-vault"):
        slug = name[: -len("-vault")]
        if slug in ("claudecode", "claude"):
            return "claude-code"
        return _term_safe(slug or "agent")
    return _term_safe(name or "agent")


def _discover_vault_dirs(home: Path) -> list[Path]:
    vaults: list[Path] = []
    seen: set[Path] = set()
    try:
        entries = sorted(home.iterdir())
    except OSError:
        entries = []
    for child in entries:
        if child.is_symlink() or not child.is_dir():
            continue
        if not child.name.endswith("-vault"):
            continue
        if _HEX_VAULT_RE.fullmatch(child.name):
            continue
        try:
            real = child.resolve()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            vaults.append(child)
    return vaults


def _parse_plan_note(note_path: Path) -> dict[str, object]:
    info: dict[str, object] = {}
    if note_path.is_symlink() or not note_path.is_file():
        return info
    try:
        raw = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    lines = raw.splitlines()
    idx = 0
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines):
            s = lines[idx].strip()
            if s in ("---", "..."):
                idx += 1
                break
            if ":" in s:
                k, _, v = s.partition(":")
                key = k.strip().lower()
                val = v.strip().strip("'\"")
                if key in ("title", "status", "workspace_id", "plan_id") and val:
                    info[key] = val
            idx += 1
    for line in lines[idx:]:
        stripped = line.strip()
        if stripped.startswith("# ") and "title" not in info:
            info["title"] = stripped[2:].strip()
            break
    scars: list[dict[str, object]] = []
    for block in re.findall(r"```json\s*(\{.*?\})\s*```", raw, flags=re.DOTALL):
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key in ("title", "status", "workspace_id"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip() and key not in info:
                info[key] = val.strip()
        raw_scars = parsed.get("scars")
        if isinstance(raw_scars, list):
            for s in raw_scars:
                if isinstance(s, dict):
                    scars.append(s)
                elif isinstance(s, str) and s.strip():
                    scars.append({"summary": s.strip()})
    if scars:
        info["scars"] = scars
    return info


def _collect_runtime_work(
    home: Path,
    *,
    now_ts: float,
    cutoff_24h: float,
) -> dict[str, object]:
    vault_dirs = _discover_vault_dirs(home)
    sessions: list[dict[str, object]] = []
    seen_sessions: set[tuple[str, str]] = set()
    active_plans: list[dict[str, object]] = []
    seen_plans: set[tuple[str, str]] = set()
    thread_scars: list[dict[str, object]] = []
    inbox_handoffs: list[dict[str, object]] = []
    seen_handoff_keys: set[str] = set()

    for vdir in vault_dirs:
        default_agent = _agent_label_for_vault(vdir)
        rt_dir = vdir / ".runtime"
        if rt_dir.is_dir() and not rt_dir.is_symlink():
            try:
                rt_files = sorted(rt_dir.iterdir())
            except OSError:
                rt_files = []
            for entry in rt_files:
                if entry.is_symlink() or not entry.is_file():
                    continue
                if not (entry.name.startswith("active-session-") and entry.name.endswith(".json")):
                    continue
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                agent = _term_safe(str(data.get("agent_id") or default_agent).strip() or default_agent)
                if re.fullmatch(r"[0-9a-fA-F]{32,64}", agent):
                    continue
                host_pid = data.get("host_pid") if data.get("host_pid") is not None else data.get("pid")
                has_valid_pid = not isinstance(host_pid, bool) and isinstance(host_pid, int) and host_pid > 1
                pid_live = _pid_is_live(host_pid) if has_valid_pid else False
                ts = _parse_epoch(data.get("updated_at") or data.get("started_at") or data.get("created_at"))
                if ts is None:
                    try:
                        ts = entry.stat().st_mtime
                    except OSError:
                        ts = None
                age_s = (now_ts - ts) if ts is not None else None
                recent_2h = age_s is not None and age_s <= 7200.0
                if not pid_live and not recent_2h:
                    continue
                ws_id = normalize_workspace_id(data.get("workspace_id") or "default")
                dedup = (agent, ws_id)
                if dedup in seen_sessions:
                    if pid_live:
                        for existing in sessions:
                            if existing.get("agent_id") == agent and existing.get("workspace_id") == ws_id:
                                existing["live"] = True
                                existing["age_seconds"] = age_s
                    continue
                seen_sessions.add(dedup)
                sessions.append(
                    {
                        "agent_id": agent,
                        "workspace_id": ws_id,
                        "project": human_project_label(ws_id),
                        "live": pid_live,
                        "age_seconds": age_s,
                    }
                )

        # Active plan pointers
        def _inspect_plan_ptr(ptr_path: Path, v_root: Path | None) -> None:
            if not _is_safe_contained_file(ptr_path, home=home, vault_root=v_root):
                return
            try:
                pdata = json.loads(ptr_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(pdata, dict):
                return
            plan_id = str(pdata.get("plan_id") or pdata.get("thread_id") or "").strip()
            if not plan_id:
                return
            host_pid = pdata.get("host_pid")
            if not isinstance(host_pid, bool) and isinstance(host_pid, int) and host_pid > 1:
                if not _pid_is_live(host_pid):
                    return
            agent = _term_safe(
                str(pdata.get("agent_id") or (_agent_label_for_vault(v_root) if v_root else "main")).strip()
                or "main"
            )
            note_meta: dict[str, object] = {}
            note_raw = pdata.get("notePath") or pdata.get("note_path")
            if isinstance(note_raw, str) and note_raw.strip():
                np = Path(note_raw)
                if not np.is_absolute():
                    np = (v_root / np) if v_root is not None else (home / np)
                if _is_safe_contained_file(np, home=home, vault_root=v_root):
                    note_meta = _parse_plan_note(np)
            status = str(note_meta.get("status") or pdata.get("status") or "active").strip().lower()
            if status in _TERMINAL_THREAD_STATUSES:
                return
            ws_id = normalize_workspace_id(pdata.get("workspace_id") or note_meta.get("workspace_id") or "default")
            title_raw = str(note_meta.get("title") or pdata.get("title") or "").strip()
            title = _short_summary(title_raw, 120) if title_raw else "Active work plan"
            key = (agent, plan_id)
            if key not in seen_plans:
                seen_plans.add(key)
                active_plans.append(
                    {
                        "title": title,
                        "agent_id": agent,
                        "workspace_id": ws_id,
                        "project": human_project_label(ws_id),
                        "status": status.replace("_", " "),
                    }
                )
            raw_scars = note_meta.get("scars")
            if isinstance(raw_scars, list):
                for sc in raw_scars:
                    if not isinstance(sc, dict):
                        continue
                    sc_ts = _parse_epoch(sc.get("created_at") or sc.get("ts") or sc.get("timestamp"))
                    if sc_ts is not None and sc_ts < cutoff_24h:
                        continue
                    raw_s = str(
                        sc.get("lesson")
                        or sc.get("summary")
                        or sc.get("signal")
                        or sc.get("description")
                        or sc.get("what_failed")
                        or ""
                    )
                    if _is_wrapper_or_blocked(raw_s, sc.get("privacy_level") or sc.get("privacy")):
                        continue
                    summary = _short_summary(raw_s)
                    if summary:
                        thread_scars.append({"workspace_id": ws_id, "summary": summary})

        active_ptr = vdir / "wiki" / "artifacts" / "_active_plan.json"
        _inspect_plan_ptr(active_ptr, vdir)

        td_dir = vdir / ".runtime" / "active-thread.d"
        if td_dir.is_dir() and not td_dir.is_symlink():
            try:
                td_files = sorted(td_dir.iterdir())
            except OSError:
                td_files = []
            for tf in td_files:
                if tf.suffix == ".json":
                    _inspect_plan_ptr(tf, vdir)

        # Unread handoff files in <vault>/inbox/
        inbox_dir = vdir / "inbox"
        if inbox_dir.is_dir() and not inbox_dir.is_symlink():
            try:
                in_files = sorted(inbox_dir.iterdir())
            except OSError:
                in_files = []
            for inf in in_files:
                if inf.suffix != ".json" or not _is_safe_contained_file(inf, home=home, vault_root=vdir):
                    continue
                try:
                    pkt = json.loads(inf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(pkt, dict) or str(pkt.get("kind") or "").lower() != "handoff":
                    continue
                status_val = str(pkt.get("status") or "pending").lower()
                if status_val not in ("pending", "unread", "open"):
                    continue
                exp = _parse_epoch(pkt.get("expires_at"))
                if exp is not None and exp <= now_ts:
                    continue
                task_raw = str(pkt.get("task") or pkt.get("summary") or "")
                if _is_wrapper_or_blocked(task_raw, pkt.get("privacy_level") or pkt.get("privacy")):
                    continue
                task_clean = _short_summary(task_raw, 140)
                if not task_clean:
                    continue
                lease_key = str(pkt.get("lease_id") or inf.name)
                if lease_key in seen_handoff_keys:
                    continue
                seen_handoff_keys.add(lease_key)
                inbox_handoffs.append(
                    {
                        "lease_key": lease_key,
                        "from_agent": _term_safe(str(pkt.get("from_agent") or "agent")),
                        "to_agent": _term_safe(str(pkt.get("to_agent") or default_agent)),
                        "task": task_clean,
                    }
                )

    shared_td = home / ".runtime" / "active-thread.d"
    if shared_td.is_dir() and not shared_td.is_symlink():
        try:
            s_files = sorted(shared_td.iterdir())
        except OSError:
            s_files = []
        for sf in s_files:
            if sf.suffix != ".json" or not _is_safe_contained_file(sf, home=home):
                continue
            try:
                pdata = json.loads(sf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(pdata, dict):
                continue
            plan_id = str(pdata.get("plan_id") or pdata.get("thread_id") or "").strip()
            if not plan_id:
                continue
            host_pid = pdata.get("host_pid")
            if not isinstance(host_pid, bool) and isinstance(host_pid, int) and host_pid > 1:
                if not _pid_is_live(host_pid):
                    continue
            agent = _term_safe(str(pdata.get("agent_id") or "main").strip() or "main")
            note_meta: dict[str, object] = {}
            note_raw = pdata.get("notePath") or pdata.get("note_path")
            if isinstance(note_raw, str) and note_raw.strip():
                np = Path(note_raw)
                if not np.is_absolute():
                    np = home / np
                if _is_safe_contained_file(np, home=home):
                    note_meta = _parse_plan_note(np)
            status = str(note_meta.get("status") or pdata.get("status") or "active").strip().lower()
            if status in _TERMINAL_THREAD_STATUSES:
                continue
            ws_id = normalize_workspace_id(
                pdata.get("workspace_id") or note_meta.get("workspace_id") or "default"
            )
            title_raw = str(note_meta.get("title") or pdata.get("title") or "").strip()
            title = _short_summary(title_raw, 120) if title_raw else "Active work plan"
            key = (agent, plan_id)
            if key not in seen_plans:
                seen_plans.add(key)
                active_plans.append(
                    {
                        "title": title,
                        "agent_id": agent,
                        "workspace_id": ws_id,
                        "project": human_project_label(ws_id),
                        "status": status.replace("_", " "),
                    }
                )

    return {
        "sessions": sessions,
        "active_plans": active_plans,
        "thread_scars": thread_scars,
        "inbox_handoffs": inbox_handoffs,
    }


def _bad_document_sql_cond(doc_alias: str, doc_cols: set[str]) -> str:
    """SQL predicate that is TRUE when a documents row is blocked, closed, or superseded."""
    disjuncts: list[str] = []
    if "privacy_level" in doc_cols:
        disjuncts.append(
            f"LOWER(TRIM(COALESCE({doc_alias}.privacy_level, 'safe'))) IN ('blocked', 'secret')"
        )
    if "superseded_by" in doc_cols:
        disjuncts.append(f"{doc_alias}.superseded_by IS NOT NULL")
    if "page_status" in doc_cols:
        disjuncts.append(
            f"LOWER(TRIM(COALESCE({doc_alias}.page_status, 'accepted'))) IN "
            "('draft', 'expired', 'rejected', 'superseded', 'retired', 'archived', 'deleted')"
        )
    return "(" + " OR ".join(disjuncts) + ")" if disjuncts else "0"


def _learning_active_and_unblocked_sql(
    learn_alias: str,
    tables: set[str],
    doc_cols: set[str],
    learn_cols: set[str],
) -> str:
    """SQL conjunction (starting with AND ...) enforcing active, non-superseded, unblocked learnings."""
    clauses: list[str] = []
    if "superseded_by" in learn_cols:
        clauses.append(f"{learn_alias}.superseded_by IS NULL")
    if "status" in learn_cols:
        clauses.append(f"LOWER(TRIM(COALESCE({learn_alias}.status, 'active'))) = 'active'")
    if "privacy_level" in learn_cols:
        clauses.append(
            f"LOWER(TRIM(COALESCE({learn_alias}.privacy_level, 'safe'))) NOT IN ('blocked', 'secret')"
        )
    if "documents" in tables:
        bad_doc = _bad_document_sql_cond("d", doc_cols)
        if bad_doc != "0":
            if "learning_documents" in tables:
                clauses.append(
                    f"NOT EXISTS ("
                    f"SELECT 1 FROM learning_documents ld "
                    f"JOIN documents d ON d.doc_id = ld.doc_id "
                    f"WHERE ld.learning_id = {learn_alias}.learning_id AND {bad_doc}"
                    f")"
                )
            if "memory_uri" in doc_cols:
                clauses.append(
                    f"NOT EXISTS ("
                    f"SELECT 1 FROM documents d "
                    f"WHERE d.memory_uri = ('learning://' || {learn_alias}.learning_id) AND {bad_doc}"
                    f")"
                )
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _resolve_doc_plain_info(
    conn: sqlite3.Connection,
    doc_id: int,
    tables: set[str],
    doc_cols: set[str],
    learn_cols: set[str],
    home: Path,
) -> dict[str, object] | None:
    """Resolve plain-English summary + workspace_id for a document, or None if blocked/retired."""
    ws_expr = "workspace_id" if "workspace_id" in doc_cols else "''"
    priv_expr = "privacy_level" if "privacy_level" in doc_cols else "'safe'"
    status_expr = "page_status" if "page_status" in doc_cols else "'accepted'"
    sup_expr = "superseded_by" if "superseded_by" in doc_cols else "NULL"
    uri_expr = "memory_uri" if "memory_uri" in doc_cols else "NULL"
    row = conn.execute(
        f"""SELECT doc_id, path, {ws_expr} AS ws, {priv_expr} AS priv,
                   {status_expr} AS pstatus, {sup_expr} AS sup_by, {uri_expr} AS mem_uri
            FROM documents WHERE doc_id = ?""",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    priv = str(row["priv"] or "safe").strip().lower()
    if priv in _BLOCKED_PRIVACY_LEVELS:
        return None
    pstatus = str(row["pstatus"] or "accepted").strip().lower()
    if pstatus in _CLOSED_PAGE_STATUSES or row["sup_by"] is not None:
        return None

    ws_id = normalize_workspace_id(row["ws"])
    learning_id: int | None = None

    if "learnings" in tables:
        uri_lid: int | None = None
        mem_uri_raw = str(row["mem_uri"] or "").strip()
        if mem_uri_raw.startswith("learning://"):
            try:
                uri_lid = int(mem_uri_raw[len("learning://") :])
            except ValueError:
                uri_lid = None

        has_ld = (
            conn.execute(
                "SELECT 1 FROM learning_documents WHERE doc_id = ? LIMIT 1",
                (doc_id,),
            ).fetchone()
            is not None
            if "learning_documents" in tables
            else False
        )

        if has_ld or uri_lid is not None:
            l_ws_expr = "l.workspace_id" if "workspace_id" in learn_cols else "'default'"
            l_priv_expr = "l.privacy_level" if "privacy_level" in learn_cols else "'safe'"
            l_filter = _learning_active_and_unblocked_sql("l", tables, doc_cols, learn_cols)
            l_row = None
            if has_ld:
                l_row = conn.execute(
                    f"""SELECT l.learning_id, l.content, {l_ws_expr} AS l_ws, {l_priv_expr} AS l_priv
                        FROM learning_documents ld
                        JOIN learnings l ON l.learning_id = ld.learning_id
                        WHERE ld.doc_id = ? {l_filter}
                        ORDER BY l.learning_id DESC
                        LIMIT 1""",
                    (doc_id,),
                ).fetchone()
            if l_row is None and uri_lid is not None:
                l_row = conn.execute(
                    f"""SELECT l.learning_id, l.content, {l_ws_expr} AS l_ws, {l_priv_expr} AS l_priv
                        FROM learnings l
                        WHERE l.learning_id = ? {l_filter}
                        LIMIT 1""",
                    (uri_lid,),
                ).fetchone()
            if l_row is None:
                return None
            raw_c = str(l_row["content"] or "")
            if _is_wrapper_or_blocked(raw_c, l_row["l_priv"]):
                return None
            summary = _short_summary(raw_c, 140)
            if not summary:
                return None
            l_ws = normalize_workspace_id(l_row["l_ws"])
            if ws_id == "default" and l_ws != "default":
                ws_id = l_ws
            learning_id = int(l_row["learning_id"])
            return {"summary": summary, "workspace_id": ws_id, "learning_id": learning_id}

    text_candidate = ""
    if "chunk_embeddings" in tables:
        ch_row = conn.execute(
            "SELECT chunk_text FROM chunk_embeddings WHERE doc_id = ? ORDER BY chunk_index ASC LIMIT 1",
            (doc_id,),
        ).fetchone()
        if ch_row and ch_row["chunk_text"]:
            text_candidate = str(ch_row["chunk_text"])
    if not text_candidate and "vault_fts" in tables:
        try:
            fts_row = conn.execute(
                "SELECT content FROM vault_fts WHERE CAST(doc_id AS INTEGER) = ? LIMIT 1",
                (doc_id,),
            ).fetchone()
            if fts_row and fts_row["content"]:
                text_candidate = str(fts_row["content"])
        except sqlite3.Error:
            pass
    if not text_candidate and row["path"]:
        p = Path(str(row["path"]))
        try:
            if _is_safe_contained_file(p, home=home):
                text_candidate = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            pass

    if _is_wrapper_or_blocked(text_candidate, priv):
        return None
    summary = _short_summary(text_candidate, 140)
    if not summary:
        return None
    return {"summary": summary, "workspace_id": ws_id, "learning_id": learning_id}


def _resolve_learning_plain_info(
    conn: sqlite3.Connection,
    learning_id: int,
    tables: set[str],
    doc_cols: set[str],
    learn_cols: set[str],
) -> dict[str, object] | None:
    ws_expr = "l.workspace_id" if "workspace_id" in learn_cols else "'default'"
    priv_expr = "l.privacy_level" if "privacy_level" in learn_cols else "'safe'"
    l_filter = _learning_active_and_unblocked_sql("l", tables, doc_cols, learn_cols)
    row = conn.execute(
        f"""SELECT l.learning_id, l.content, {ws_expr} AS ws, {priv_expr} AS priv
            FROM learnings l
            WHERE l.learning_id = ? {l_filter}""",
        (learning_id,),
    ).fetchone()
    if row is None:
        return None
    raw_c = str(row["content"] or "")
    if _is_wrapper_or_blocked(raw_c, row["priv"]):
        return None
    summary = _short_summary(raw_c, 140)
    if not summary:
        return None
    ws_norm = normalize_workspace_id(row["ws"])
    if ws_norm == "default" and "documents" in tables and "workspace_id" in doc_cols:
        if "learning_documents" in tables:
            d_ws_row = conn.execute(
                """SELECT d.workspace_id AS d_ws
                   FROM learning_documents ld
                   JOIN documents d ON d.doc_id = ld.doc_id
                   WHERE ld.learning_id = ? AND TRIM(COALESCE(d.workspace_id, '')) != ''
                   ORDER BY d.doc_id DESC LIMIT 1""",
                (learning_id,),
            ).fetchone()
            if d_ws_row and d_ws_row["d_ws"]:
                ws_norm = normalize_workspace_id(d_ws_row["d_ws"])
    return {
        "summary": summary,
        "workspace_id": ws_norm,
        "learning_id": int(row["learning_id"]),
    }


def _collect_sqlite_digest(
    db_file: Path,
    *,
    home: Path,
    now_ts: float,
    cutoff_24h: float,
) -> dict[str, object]:
    result: dict[str, object] = {
        "db_available": False,
        "conflicts": [],
        "learnings_24h": [],
        "scars_24h": [],
        "db_handoffs": [],
        "db_repo_maps": {},
    }
    if db_file.is_symlink() or not db_file.is_file():
        return result

    uri = f"file:{quote(str(db_file.resolve()))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return result

    try:
        result["db_available"] = True
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        doc_cols = (
            {str(r["name"]) for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
            if "documents" in tables
            else set()
        )
        learn_cols = (
            {str(r["name"]) for r in conn.execute("PRAGMA table_info(learnings)").fetchall()}
            if "learnings" in tables
            else set()
        )

        # 1. Genuine unresolved rule conflicts
        conflicts: list[dict[str, str]] = []
        seen_doc_pairs: set[tuple[int, int]] = set()
        seen_learning_pairs: set[tuple[int, int]] = set()
        seen_summary_pairs: set[tuple[str, str]] = set()

        def _record_conflict(info_a: dict[str, object], info_b: dict[str, object]) -> None:
            lid_a = info_a.get("learning_id")
            lid_b = info_b.get("learning_id")
            if isinstance(lid_a, int) and isinstance(lid_b, int):
                if lid_a == lid_b:
                    return
                lpair = (min(lid_a, lid_b), max(lid_a, lid_b))
                if lpair in seen_learning_pairs:
                    return
                seen_learning_pairs.add(lpair)
            sum_a = str(info_a["summary"])
            sum_b = str(info_b["summary"])
            if sum_a == sum_b:
                return
            spair = (min(sum_a, sum_b), max(sum_a, sum_b))
            if spair in seen_summary_pairs:
                return
            seen_summary_pairs.add(spair)
            ws_a = str(info_a.get("workspace_id") or "default")
            ws_b = str(info_b.get("workspace_id") or "default")
            chosen_ws = ws_a if ws_a not in ("default", "global") else ws_b
            conflicts.append(
                {
                    "workspace_id": chosen_ws,
                    "project": human_project_label(chosen_ws),
                    "side_a": sum_a,
                    "side_b": sum_b,
                }
            )

        if "memory_links" in tables and "documents" in tables:
            ml_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(memory_links)").fetchall()}
            edge_clause = "AND COALESCE(edge_status, 'active') = 'active'" if "edge_status" in ml_cols else ""
            ml_rows = conn.execute(
                f"""SELECT source_doc_id, target_doc_id
                    FROM memory_links
                    WHERE link_type = 'contradicts' {edge_clause}
                    ORDER BY created_at DESC
                    LIMIT 50"""
            ).fetchall()
            for r in ml_rows:
                try:
                    d1 = int(r["source_doc_id"])
                    d2 = int(r["target_doc_id"])
                except (TypeError, ValueError):
                    continue
                if d1 == d2:
                    continue
                dpair = (min(d1, d2), max(d1, d2))
                if dpair in seen_doc_pairs:
                    continue
                seen_doc_pairs.add(dpair)
                info_a = _resolve_doc_plain_info(conn, d1, tables, doc_cols, learn_cols, home)
                info_b = _resolve_doc_plain_info(conn, d2, tables, doc_cols, learn_cols, home)
                if info_a and info_b:
                    _record_conflict(info_a, info_b)

        if "contradiction_log" in tables:
            cl_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(contradiction_log)").fetchall()}
            clauses: list[str] = []
            if "resolution_id" in cl_cols:
                clauses.append("resolution_id IS NULL")
            if "resolution" in cl_cols:
                clauses.append("(resolution IS NULL OR TRIM(resolution) = '')")
            if "resolution_status" in cl_cols:
                clauses.append(
                    "(resolution_status IS NULL OR TRIM(resolution_status) = '' OR resolution_status = 'unresolved')"
                )
            if "classification" in cl_cols:
                clauses.append("COALESCE(classification, '') != 'legacy_unclassified'")
            where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            select_cols = [
                c
                for c in (
                    "id",
                    "memory_a_id",
                    "memory_b_id",
                    "source_doc_id",
                    "target_doc_id",
                    "detection_method",
                )
                if c in cl_cols
            ]
            if select_cols:
                order_col = "detected_at DESC" if "detected_at" in cl_cols else "ROWID DESC"
                cl_rows = conn.execute(
                    f"SELECT {', '.join(select_cols)} FROM contradiction_log {where_sql} ORDER BY {order_col} LIMIT 50"
                ).fetchall()
                for r in cl_rows:
                    rk = set(r.keys())
                    s_doc = r["source_doc_id"] if "source_doc_id" in rk else None
                    t_doc = r["target_doc_id"] if "target_doc_id" in rk else None
                    if s_doc is not None and t_doc is not None and "documents" in tables:
                        try:
                            d1, d2 = int(s_doc), int(t_doc)
                        except (TypeError, ValueError):
                            continue
                        if d1 == d2:
                            continue
                        dpair = (min(d1, d2), max(d1, d2))
                        if dpair in seen_doc_pairs:
                            continue
                        seen_doc_pairs.add(dpair)
                        info_a = _resolve_doc_plain_info(conn, d1, tables, doc_cols, learn_cols, home)
                        info_b = _resolve_doc_plain_info(conn, d2, tables, doc_cols, learn_cols, home)
                        if info_a and info_b:
                            _record_conflict(info_a, info_b)
                        continue

                    method = str(r["detection_method"] or "").strip().lower() if "detection_method" in rk else ""
                    if method == "cosine":
                        continue
                    mem_a = r["memory_a_id"] if "memory_a_id" in rk else None
                    mem_b = r["memory_b_id"] if "memory_b_id" in rk else None
                    if mem_a is not None and mem_b is not None and "learnings" in tables:
                        try:
                            l1, l2 = int(mem_a), int(mem_b)
                        except (TypeError, ValueError):
                            continue
                        info_a = _resolve_learning_plain_info(conn, l1, tables, doc_cols, learn_cols)
                        info_b = _resolve_learning_plain_info(conn, l2, tables, doc_cols, learn_cols)
                        if info_a and info_b:
                            _record_conflict(info_a, info_b)

        if "learnings" in tables and "contradicts_id" in learn_cols:
            l_filter = _learning_active_and_unblocked_sql("l", tables, doc_cols, learn_cols)
            c_rows = conn.execute(
                f"""SELECT l.learning_id, l.contradicts_id
                    FROM learnings l
                    WHERE l.contradicts_id IS NOT NULL {l_filter}
                    ORDER BY l.created_at DESC
                    LIMIT 30"""
            ).fetchall()
            for r in c_rows:
                try:
                    l1, l2 = int(r["learning_id"]), int(r["contradicts_id"])
                except (TypeError, ValueError):
                    continue
                info_a = _resolve_learning_plain_info(conn, l1, tables, doc_cols, learn_cols)
                info_b = _resolve_learning_plain_info(conn, l2, tables, doc_cols, learn_cols)
                if info_a and info_b:
                    _record_conflict(info_a, info_b)

        result["conflicts"] = conflicts

        # 2. Today's learnings & scars (last 24h)
        learnings_24h: list[dict[str, str]] = []
        scars_24h: list[dict[str, str]] = []
        if "learnings" in tables:
            ws_expr = "COALESCE(l.workspace_id, 'default')" if "workspace_id" in learn_cols else "'default'"
            priv_expr = "l.privacy_level" if "privacy_level" in learn_cols else "NULL"
            l_filter = _learning_active_and_unblocked_sql("l", tables, doc_cols, learn_cols)
            l_rows = conn.execute(
                f"""SELECT l.category, l.content, {ws_expr} AS ws, {priv_expr} AS priv
                    FROM learnings l
                    WHERE l.created_at >= ? {l_filter}
                    ORDER BY l.created_at DESC
                    LIMIT 100""",
                (cutoff_24h,),
            ).fetchall()
            for r in l_rows:
                raw_c = str(r["content"] or "")
                if _is_wrapper_or_blocked(raw_c, r["priv"]):
                    continue
                summary = _short_summary(raw_c, 160)
                if not summary:
                    continue
                ws_norm = normalize_workspace_id(r["ws"])
                cat = str(r["category"] or "general").strip().lower()
                entry = {"workspace_id": ws_norm, "category": cat, "summary": summary}
                if cat in _SCAR_CATEGORIES:
                    scars_24h.append(entry)
                else:
                    learnings_24h.append(entry)

        if "episodic_events" in tables:
            ep_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(episodic_events)").fetchall()}
            ts_col = "created_at" if "created_at" in ep_cols else ("timestamp" if "timestamp" in ep_cols else None)
            if ts_col:
                ep_rows = conn.execute(
                    f"""SELECT event_type, content, metadata
                        FROM episodic_events
                        WHERE {ts_col} >= ?
                          AND event_type IN ('scar', 'thread_scar', 'correction', 'error')
                        ORDER BY {ts_col} DESC
                        LIMIT 50""",
                    (cutoff_24h,),
                ).fetchall()
                for r in ep_rows:
                    raw_c = str(r["content"] or "")
                    ev_ws = "default"
                    priv_val = None
                    raw_meta = r["metadata"]
                    if isinstance(raw_meta, str) and raw_meta.strip():
                        try:
                            meta = json.loads(raw_meta)
                            if isinstance(meta, dict):
                                ev_ws = normalize_workspace_id(meta.get("workspace_id") or "default")
                                priv_val = meta.get("privacy_level") or meta.get("privacy")
                        except ValueError:
                            pass
                    if _is_wrapper_or_blocked(raw_c, priv_val):
                        continue
                    summary = _short_summary(raw_c, 160)
                    if summary:
                        scars_24h.append(
                            {
                                "workspace_id": ev_ws,
                                "category": str(r["event_type"] or "scar").lower(),
                                "summary": summary,
                            }
                        )

        result["learnings_24h"] = learnings_24h
        result["scars_24h"] = scars_24h

        # 3. Pending handoffs from SQLite (handoff_leases / handoffs)
        db_handoffs: list[dict[str, str]] = []
        for tbl_name in ("handoff_leases", "handoffs"):
            if tbl_name not in tables:
                continue
            h_cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({tbl_name})").fetchall()}
            if not {"from_agent", "to_agent", "status"} <= h_cols:
                continue
            lease_col = "lease_id" if "lease_id" in h_cols else "ROWID"
            task_col = "task" if "task" in h_cols else ("summary" if "summary" in h_cols else "''")
            exp_clause = "AND (expires_at IS NULL OR expires_at > ?)" if "expires_at" in h_cols else ""
            params: tuple[object, ...] = (now_ts,) if exp_clause else ()
            h_rows = conn.execute(
                f"""SELECT {lease_col} AS lid, from_agent, to_agent, {task_col} AS task
                    FROM {tbl_name}
                    WHERE status = 'pending' {exp_clause}
                    ORDER BY ROWID DESC
                    LIMIT 25""",
                params,
            ).fetchall()
            for r in h_rows:
                raw_t = str(r["task"] or "")
                if _is_wrapper_or_blocked(raw_t):
                    continue
                summary = _short_summary(raw_t, 140)
                if summary:
                    db_handoffs.append(
                        {
                            "lease_key": str(r["lid"]),
                            "from_agent": _term_safe(str(r["from_agent"] or "agent")),
                            "to_agent": _term_safe(str(r["to_agent"] or "agent")),
                            "task": summary,
                        }
                    )
        result["db_handoffs"] = db_handoffs

        # 4. Repo map cards from SQLite
        db_repo_maps: dict[str, dict[str, object]] = {}
        if "repo_map_cards" in tables:
            rm_rows = conn.execute(
                """SELECT workspace_id, repo_root, subsystem_slug, owned_files_json, git_sha, updated_at
                   FROM repo_map_cards
                   ORDER BY workspace_id ASC, subsystem_slug ASC"""
            ).fetchall()
            for r in rm_rows:
                ws_norm = normalize_workspace_id(r["workspace_id"])
                entry = db_repo_maps.setdefault(
                    ws_norm,
                    {
                        "workspace_id": ws_norm,
                        "repo_root": str(r["repo_root"] or "").strip(),
                        "git_sha": str(r["git_sha"] or "").strip(),
                        "subsystems": [],
                    },
                )
                slug = str(r["subsystem_slug"] or "").strip()
                if slug and slug != "overview":
                    try:
                        owned = json.loads(r["owned_files_json"] or "[]")
                        owned_list = [str(x) for x in owned] if isinstance(owned, list) else []
                    except ValueError:
                        owned_list = []
                    subs = entry["subsystems"]
                    if isinstance(subs, list):
                        subs.append({"slug": slug, "owned_files": owned_list})
        result["db_repo_maps"] = db_repo_maps
    except sqlite3.Error:
        logger.debug("today_digest SQLite query failed", exc_info=True)
    finally:
        conn.close()

    return result


def _is_excluded_rel_path(rel_path: str) -> bool:
    parts = [p for p in rel_path.split("/") if p]
    return any(p in _EXCLUDED_REL_PARTS for p in parts)


def _compute_unique_dir_prefixes(subsystems: list[dict[str, object]]) -> dict[str, list[str]]:
    prefix_owners: dict[str, set[str]] = {}
    for sub in subsystems:
        slug = str(sub.get("slug") or "")
        for f in sub.get("owned_files") or []:  # type: ignore[union-attr]
            rel = str(f).replace("\\", "/")
            if "/" in rel:
                top_dir = rel.rsplit("/", 1)[0] + "/"
                prefix_owners.setdefault(top_dir, set()).add(slug)
    unique: dict[str, list[str]] = {}
    for prefix, owners in prefix_owners.items():
        if len(owners) == 1:
            owner = next(iter(owners))
            unique.setdefault(owner, []).append(prefix)
    return unique


def _run_git_300ms(repo_root: Path, args: list[str], timeout_sec: float = 0.3) -> tuple[int, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
            env=env,
        )
        return proc.returncode, proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 124, ""


def _match_changed_subsystems(
    changed_files: list[str],
    subsystems: list[dict[str, object]],
) -> list[str]:
    if not changed_files:
        return []
    unique_prefixes = _compute_unique_dir_prefixes(subsystems)
    changed_subsystems: list[str] = []
    for sub in subsystems:
        slug = _term_safe(str(sub.get("slug") or "").strip())
        if not slug or slug == "overview":
            continue
        owned_set = {str(x) for x in (sub.get("owned_files") or [])}  # type: ignore[union-attr]
        prefixes = list(sub.get("dir_prefixes") or unique_prefixes.get(slug, []))  # type: ignore[arg-type]
        matched = False
        for cf in changed_files:
            if cf == "<commits diverged>":
                matched = True
                break
            if cf in owned_set or any(cf.startswith(str(p)) for p in prefixes) or Path(cf).stem.lower() == slug:
                matched = True
                break
        if matched:
            changed_subsystems.append(slug)
    return changed_subsystems


def _store_map_freshness_cache(
    cache_key: tuple[str, str],
    now_mono: float,
    entry: dict[str, object],
) -> None:
    stored = {
        "short_sha": str(entry.get("short_sha") or ""),
        "stale": bool(entry.get("stale")),
        "changed_subsystems": list(entry.get("changed_subsystems") or []),  # type: ignore[arg-type]
        "changed_files": list(entry.get("changed_files") or []),  # type: ignore[arg-type]
    }
    with _MAP_FRESHNESS_LOCK:
        expired = [
            k
            for k, (ts, _) in _MAP_FRESHNESS_CACHE.items()
            if (now_mono - ts) >= _MAP_FRESHNESS_TTL_SEC
        ]
        for k in expired:
            _MAP_FRESHNESS_CACHE.pop(k, None)
        if cache_key not in _MAP_FRESHNESS_CACHE and len(_MAP_FRESHNESS_CACHE) >= _MAP_FRESHNESS_MAX_ENTRIES:
            oldest_key = min(_MAP_FRESHNESS_CACHE, key=lambda k: _MAP_FRESHNESS_CACHE[k][0])
            _MAP_FRESHNESS_CACHE.pop(oldest_key, None)
        _MAP_FRESHNESS_CACHE[cache_key] = (now_mono, stored)


def _check_map_freshness(
    repo_root_str: str,
    built_sha: str,
    subsystems: list[dict[str, object]],
    *,
    bypass_cache: bool = False,
) -> dict[str, object]:
    """Strictly hardened git staleness check (BUG-39 invariants + 300ms timeout + 5s TTL cache)."""
    clean_sha = built_sha.strip()
    sha_valid = bool(clean_sha and _GIT_HEX_SHA_RE.fullmatch(clean_sha))
    short_sha = clean_sha[:7] if sha_valid else "uncommitted"

    if (
        not sha_valid
        or not repo_root_str
        or repo_root_str.startswith("-")
        or not os.path.isabs(repo_root_str)
    ):
        return {"short_sha": short_sha, "stale": False, "changed_subsystems": [], "changed_files": []}

    repo_root = Path(repo_root_str)
    if repo_root.is_symlink() or not repo_root.is_dir() or not (repo_root / ".git").exists():
        return {"short_sha": short_sha, "stale": False, "changed_subsystems": [], "changed_files": []}

    cache_key = (repo_root_str, clean_sha)
    now_mono = time.monotonic()
    if not bypass_cache:
        with _MAP_FRESHNESS_LOCK:
            cached_pair = _MAP_FRESHNESS_CACHE.get(cache_key)
            if cached_pair is not None and (now_mono - cached_pair[0]) < _MAP_FRESHNESS_TTL_SEC:
                cached_entry = cached_pair[1]
                cached_files = [str(x) for x in (cached_entry.get("changed_files") or [])]  # type: ignore[union-attr]
                stale = bool(cached_entry.get("stale"))
                return {
                    "short_sha": str(cached_entry.get("short_sha") or short_sha),
                    "stale": stale,
                    "changed_subsystems": _match_changed_subsystems(cached_files, subsystems) if stale else [],
                    "changed_files": cached_files if stale else [],
                }

    rc_head, out_head = _run_git_300ms(repo_root, ["rev-parse", "HEAD"], timeout_sec=0.3)
    if rc_head != 0 or not out_head.strip():
        return {"short_sha": short_sha, "stale": False, "changed_subsystems": [], "changed_files": []}

    head_sha = out_head.strip()
    if not _GIT_HEX_SHA_RE.fullmatch(head_sha) or head_sha == clean_sha:
        fresh_res: dict[str, object] = {
            "short_sha": short_sha,
            "stale": False,
            "changed_subsystems": [],
            "changed_files": [],
        }
        _store_map_freshness_cache(cache_key, time.monotonic(), fresh_res)
        return fresh_res

    rc_diff, out_diff = _run_git_300ms(
        repo_root,
        ["diff", "--name-only", f"{clean_sha}..{head_sha}", "--"],
        timeout_sec=0.3,
    )
    if rc_diff == 0:
        changed_files = [
            ln.strip().replace("\\", "/")
            for ln in out_diff.splitlines()
            if ln.strip() and not _is_excluded_rel_path(ln.strip().replace("\\", "/"))
        ]
    else:
        changed_files = ["<commits diverged>"]

    if not changed_files:
        clean_res: dict[str, object] = {
            "short_sha": short_sha,
            "stale": False,
            "changed_subsystems": [],
            "changed_files": [],
        }
        _store_map_freshness_cache(cache_key, time.monotonic(), clean_res)
        return clean_res

    changed_subsystems = _match_changed_subsystems(changed_files, subsystems)
    stale_res: dict[str, object] = {
        "short_sha": short_sha,
        "stale": True,
        "changed_subsystems": changed_subsystems,
        "changed_files": changed_files,
    }
    _store_map_freshness_cache(cache_key, time.monotonic(), stale_res)
    return stale_res


def _collect_project_maps(
    home: Path,
    db_repo_maps: dict[str, dict[str, object]],
    *,
    bypass_cache: bool = False,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}

    maps_root = home / "vault" / "maps"
    if maps_root.is_dir() and not maps_root.is_symlink():
        try:
            ws_dirs = sorted(maps_root.iterdir())
        except OSError:
            ws_dirs = []
        for ws_dir in ws_dirs:
            if ws_dir.is_symlink() or not ws_dir.is_dir():
                continue
            idx_file = ws_dir / "_index.json"
            if not _is_safe_contained_file(idx_file, home=home):
                continue
            try:
                idx_data = json.loads(idx_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(idx_data, dict):
                continue
            ws_norm = normalize_workspace_id(idx_data.get("workspace_id") or ws_dir.name)
            raw_subs = idx_data.get("subsystems")
            subs_list: list[dict[str, object]] = []
            if isinstance(raw_subs, list):
                for item in raw_subs:
                    if isinstance(item, dict) and item.get("slug"):
                        subs_list.append(item)
            merged[ws_norm] = {
                "workspace_id": ws_norm,
                "repo_root": str(idx_data.get("repo_root") or "").strip(),
                "git_sha": str(idx_data.get("git_sha") or "").strip(),
                "subsystems": subs_list,
            }

    for ws_norm, db_entry in db_repo_maps.items():
        if ws_norm not in merged:
            merged[ws_norm] = db_entry
        else:
            existing = merged[ws_norm]
            if not existing.get("git_sha") and db_entry.get("git_sha"):
                existing["git_sha"] = db_entry["git_sha"]
            if not existing.get("repo_root") and db_entry.get("repo_root"):
                existing["repo_root"] = db_entry["repo_root"]
            if not existing.get("subsystems") and db_entry.get("subsystems"):
                existing["subsystems"] = db_entry["subsystems"]

    projects: list[dict[str, object]] = []
    for ws_norm in sorted(merged.keys()):
        item = merged[ws_norm]
        repo_root_str = str(item.get("repo_root") or "")
        fallback_name = Path(repo_root_str).name if repo_root_str and not repo_root_str.startswith("-") else None
        project_label = human_project_label(ws_norm, fallback_name=fallback_name)
        subs = list(item.get("subsystems") or [])  # type: ignore[arg-type]
        freshness = _check_map_freshness(
            repo_root_str=repo_root_str,
            built_sha=str(item.get("git_sha") or ""),
            subsystems=subs,
            bypass_cache=bypass_cache,
        )
        projects.append(
            {
                "workspace_id": ws_norm,
                "project": project_label,
                "subsystem_count": len(subs),
                **freshness,
            }
        )
    projects.sort(key=lambda p: str(p.get("project") or "").lower())
    return projects


def collect_today_digest_data(
    minni_home: Path | str | None = None,
    *,
    daemon_running: bool | None = None,
    db_path: Path | str | None = None,
    now: float | None = None,
    bypass_cache: bool = False,
) -> dict[str, object]:
    """Gather all data for TODAY.md and CLI status summaries without writing."""
    home = resolve_minni_home(minni_home, db_path=db_path)
    now_ts = float(now) if now is not None else time.time()
    cutoff_24h = now_ts - 86400.0

    effective_db = Path(os.path.expanduser(str(db_path))) if db_path is not None else (home / "minni.db")
    is_running = _probe_daemon_running(home) if daemon_running is None else bool(daemon_running)

    rt_data = _collect_runtime_work(home, now_ts=now_ts, cutoff_24h=cutoff_24h)
    sql_data = _collect_sqlite_digest(effective_db, home=home, now_ts=now_ts, cutoff_24h=cutoff_24h)

    # Deduplicate handoffs across SQLite and inbox files
    handoffs: list[dict[str, str]] = []
    seen_leases: set[str] = set()
    for h in [
        *(sql_data.get("db_handoffs") or []),  # type: ignore[list-item]
        *(rt_data.get("inbox_handoffs") or []),  # type: ignore[list-item]
    ]:
        if not isinstance(h, dict):
            continue
        lk = str(h.get("lease_key") or "")
        if lk and lk in seen_leases:
            continue
        if lk:
            seen_leases.add(lk)
        handoffs.append(h)

    # Group 24h scars and learnings by human project label
    grouped_24h: dict[str, dict[str, list[str]]] = {}
    seen_scars_by_proj: dict[str, set[str]] = {}
    seen_rules_by_proj: dict[str, set[str]] = {}

    total_new_items = 0
    all_scars = [
        *(sql_data.get("scars_24h") or []),  # type: ignore[list-item]
        *(rt_data.get("thread_scars") or []),  # type: ignore[list-item]
    ]
    for sc in all_scars:
        if not isinstance(sc, dict):
            continue
        proj = human_project_label(sc.get("workspace_id"))
        summary = str(sc.get("summary") or "").strip()
        if not summary:
            continue
        norm_key = summary.lower()
        if norm_key in seen_scars_by_proj.setdefault(proj, set()):
            continue
        seen_scars_by_proj[proj].add(norm_key)
        grouped_24h.setdefault(proj, {"scars": [], "rules": []})["scars"].append(summary)
        total_new_items += 1

    for lr in sql_data.get("learnings_24h") or []:  # type: ignore[union-attr]
        if not isinstance(lr, dict):
            continue
        proj = human_project_label(lr.get("workspace_id"))
        summary = str(lr.get("summary") or "").strip()
        if not summary:
            continue
        norm_key = summary.lower()
        if (
            norm_key in seen_rules_by_proj.setdefault(proj, set())
            or norm_key in seen_scars_by_proj.get(proj, set())
        ):
            continue
        seen_rules_by_proj[proj].add(norm_key)
        grouped_24h.setdefault(proj, {"scars": [], "rules": []})["rules"].append(summary)
        total_new_items += 1

    project_maps = _collect_project_maps(
        home,
        sql_data.get("db_repo_maps") or {},  # type: ignore[arg-type]
        bypass_cache=bypass_cache,
    )

    active_plans = list(rt_data.get("active_plans") or [])  # type: ignore[arg-type]
    conflicts = list(sql_data.get("conflicts") or [])  # type: ignore[arg-type]

    return {
        "minni_home": str(home),
        "today_path": str(home / "TODAY.md"),
        "generated_at": now_ts,
        "daemon_running": is_running,
        "active_plans_count": len(active_plans),
        "new_learnings_24h_count": total_new_items,
        "mapped_projects_count": len(project_maps),
        "conflicts_count": len(conflicts),
        "conflicts": conflicts,
        "sessions": rt_data.get("sessions") or [],
        "active_plans": active_plans,
        "handoffs": handoffs,
        "grouped_24h": grouped_24h,
        "project_maps": project_maps,
    }


def render_today_digest(
    minni_home: Path | str | None = None,
    *,
    daemon_running: bool | None = None,
    db_path: Path | str | None = None,
    now: float | None = None,
    bypass_cache: bool = False,
) -> tuple[str, dict[str, object]]:
    """Render plain-spoken TODAY.md Markdown and return (markdown, summary_data)."""
    data = collect_today_digest_data(
        minni_home,
        daemon_running=daemon_running,
        db_path=db_path,
        now=now,
        bypass_cache=bypass_cache,
    )
    ts_str = datetime.fromtimestamp(float(data["generated_at"])).strftime("%Y-%m-%d %H:%M")  # type: ignore[arg-type]
    sys_state = "running" if data["daemon_running"] else "offline — showing saved state"

    lines: list[str] = [
        f"# Minni — Today's Digest ({ts_str})",
        "",
        (
            f"System: {sys_state} · "
            f"Active plans: {data['active_plans_count']} · "
            f"New learnings (24h): {data['new_learnings_24h_count']} · "
            f"Mapped projects: {data['mapped_projects_count']}"
        ),
        "",
        "## Needs Your Call",
        "",
    ]

    conflicts: list[dict[str, str]] = data["conflicts"]  # type: ignore[assignment]
    if not conflicts:
        lines.append("Nothing waiting on you.")
    else:
        for c in conflicts:
            lines.append(
                f"- **{c['project']}:** \"{c['side_a']}\" conflicts with \"{c['side_b']}\""
            )

    lines.extend(["", "## Live & Recent Agent Work", ""])
    sessions: list[dict[str, object]] = data["sessions"]  # type: ignore[assignment]
    plans: list[dict[str, object]] = data["active_plans"]  # type: ignore[assignment]
    handoffs: list[dict[str, str]] = data["handoffs"]  # type: ignore[assignment]

    if not sessions and not plans and not handoffs:
        lines.append("No active agent sessions, plans, or waiting handoffs right now.")
    else:
        for s in sessions:
            state_lbl = (
                "live now"
                if s.get("live")
                else f"active {_format_age(float(s.get('age_seconds') or 0.0))}"
            )
            lines.append(f"- **Session ({s['agent_id']}):** {state_lbl} in **{s['project']}**")
        for p in plans:
            lines.append(
                f"- **Plan:** {p['title']} (*{p['agent_id']}*, **{p['project']}**, {p['status']})"
            )
        for h in handoffs:
            lines.append(
                f"- **Handoff waiting** for *{h['to_agent']}* (from *{h['from_agent']}*): {h['task']}"
            )

    lines.extend(["", "## Today's Learnings & Scars (Last 24h)", ""])
    grouped_24h: dict[str, dict[str, list[str]]] = data["grouped_24h"]  # type: ignore[assignment]
    if not grouped_24h:
        lines.append("No new learnings or scars recorded in the last 24 hours.")
    else:
        ordered_projects = sorted(
            grouped_24h.keys(),
            key=lambda p: (1 if p == "General / Cross-Project" else 0, p.lower()),
        )
        for proj in ordered_projects:
            bucket = grouped_24h[proj]
            lines.append(f"### {proj}")
            if bucket["scars"]:
                lines.append("- **Scars & Gotchas**")
                for item in bucket["scars"][:15]:
                    lines.append(f"  - {item}")
            if bucket["rules"]:
                lines.append("- **Rules & Decisions**")
                for item in bucket["rules"][:20]:
                    lines.append(f"  - {item}")
            lines.append("")
        if lines[-1] == "":
            lines.pop()

    lines.extend(["", "## Project Map Freshness", ""])
    project_maps: list[dict[str, object]] = data["project_maps"]  # type: ignore[assignment]
    if not project_maps:
        lines.append("No projects mapped yet. Run `minni map <repo>` to index a project.")
    else:
        for pm in project_maps:
            proj = pm["project"]
            sha = pm["short_sha"]
            sub_cnt = int(pm["subsystem_count"])  # type: ignore[arg-type]
            sub_word = "subsystem" if sub_cnt == 1 else "subsystems"
            if not pm.get("stale"):
                lines.append(f"- **{proj}** (`{sha}`) — up to date ({sub_cnt} {sub_word})")
            else:
                changed_subs: list[str] = pm.get("changed_subsystems") or []  # type: ignore[assignment]
                if changed_subs:
                    c_cnt = len(changed_subs)
                    c_word = "subsystem" if c_cnt == 1 else "subsystems"
                    shown = ", ".join(f"`{s}`" for s in changed_subs[:6])
                    if len(changed_subs) > 6:
                        shown += f", +{len(changed_subs) - 6} more"
                    lines.append(
                        f"- **{proj}** (`{sha}`) — {c_cnt} {c_word} changed since last map ({shown}); "
                        "refreshes automatically on recall or via `minni map`"
                    )
                else:
                    lines.append(
                        f"- **{proj}** (`{sha}`) — files changed since last map; "
                        "refreshes automatically on recall or via `minni map`"
                    )

    lines.append("")
    return "\n".join(lines), data


def write_today_digest(
    minni_home: Path | str | None = None,
    *,
    daemon_running: bool | None = None,
    db_path: Path | str | None = None,
    now: float | None = None,
    bypass_cache: bool = True,
) -> tuple[Path, dict[str, object]]:
    """Atomically write <minni_home>/TODAY.md (mode 0600), refusing symlinks."""
    home = resolve_minni_home(minni_home, db_path=db_path)
    if home.is_symlink():
        raise ValueError(f"Refusing to write TODAY.md: minni_home is a symlink ({home})")
    home.mkdir(parents=True, exist_ok=True)

    today_path = home / "TODAY.md"
    if today_path.is_symlink():
        raise ValueError(f"Refusing to write TODAY.md: target is a symlink ({today_path})")

    markdown, data = render_today_digest(
        home,
        daemon_running=daemon_running,
        db_path=db_path,
        now=now,
        bypass_cache=bypass_cache,
    )

    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".TODAY.md.",
        suffix=".tmp",
        dir=str(home),
        delete=False,
    )
    tmp_name = tmp_file.name
    try:
        try:
            os.chmod(tmp_name, 0o600)
            tmp_file.write(markdown)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        finally:
            tmp_file.close()

        if home.is_symlink() or today_path.is_symlink():
            raise ValueError(f"Refusing to replace symlinked TODAY.md ({today_path})")
        os.replace(tmp_name, str(today_path))
        os.chmod(str(today_path), 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return today_path, data


def refresh_today_digest_safe(
    minni_home: Path | str | None = None,
    *,
    daemon_running: bool | None = None,
    db_path: Path | str | None = None,
    min_interval_sec: float = 0.0,
    bypass_cache: bool = False,
) -> Path | None:
    """Non-raising, optionally throttled TODAY.md refresh helper."""
    try:
        if (
            (minni_home is None or not str(minni_home).strip())
            and not (os.environ.get("MINNI_HOME") or "").strip()
            and _is_memory_db_path(db_path)
        ):
            return None
        home = resolve_minni_home(minni_home, db_path=db_path)
        if min_interval_sec > 0.0:
            key = str(home)
            now_mono = time.monotonic()
            with _REFRESH_LOCK:
                last = _LAST_REFRESH_BY_HOME.get(key)
                if last is not None and (now_mono - last) < min_interval_sec:
                    return home / "TODAY.md"
                _LAST_REFRESH_BY_HOME[key] = now_mono
        path, _ = write_today_digest(
            home,
            daemon_running=daemon_running,
            db_path=db_path,
            bypass_cache=bypass_cache,
        )
        return path
    except Exception:
        logger.debug("refresh_today_digest_safe skipped", exc_info=True)
        return None
