import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from config import CANONICAL_SOVEREIGN_HOME


logger = logging.getLogger("minnid")

HANDOFF_KINDS = frozenset({"handoff", "candidate_learning", "request", "answer"})


def agent_env_key(agent_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", agent_id.upper()).strip("_")


def default_agent_vault(agent_id: str) -> Path:
    aliases = {
        "claude-code": "claudecode",
        "claudecode": "claudecode",
        "codex": "codex",
        "hermes": "hermes",
        "openclaw": "openclaw",
        # Preserve hyphenated canonical slugs; the Grok overlay polls these vault names.
        "grok-build": "grok-build",
        "grok-beta": "grok-beta",
        "grok": "grok",
    }
    slug = aliases.get(agent_id, re.sub(r"[^a-z0-9]+", "", agent_id.lower()) or "agent")
    return Path(CANONICAL_SOVEREIGN_HOME) / f"{slug}-vault"


def agent_vault(agent_id: str) -> tuple[Path, bool]:
    mapping_raw = os.environ.get("MINNI_AGENT_VAULTS", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict) and agent_id in mapping:
                return Path(os.path.expanduser(str(mapping[agent_id]))), True
        except json.JSONDecodeError:
            logger.warning("Invalid MINNI_AGENT_VAULTS JSON; using defaults")

    env_key = f"MINNI_{agent_env_key(agent_id)}_VAULT_PATH"
    if os.environ.get(env_key):
        return Path(os.path.expanduser(os.environ[env_key])), True
    return default_agent_vault(agent_id), False


def ensure_handoff_vault(vault_path: Path) -> None:
    for rel in (
        "raw",
        "wiki",
        "wiki/handoffs",
        "schema",
        "logs",
        "inbox",
        "outbox",
    ):
        (vault_path / rel).mkdir(parents=True, exist_ok=True)
    log_path = vault_path / "log.md"
    if not log_path.exists():
        log_path.write_text("# Minni Log\n\n", encoding="utf-8")
    index_path = vault_path / "index.md"
    if not index_path.exists():
        index_path.write_text("# Minni Index\n\n", encoding="utf-8")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "handoff"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_iso_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def iso_from_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def known_agent_vaults() -> list[Path]:
    vaults = []
    mapping_raw = os.environ.get("MINNI_AGENT_VAULTS", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict):
                vaults.extend(Path(os.path.expanduser(str(v))) for v in mapping.values())
        except json.JSONDecodeError:
            pass
    # Resolve at call time so MINNI_HOME overrides are honored without hardcoding ~/.minni.
    root = Path(os.environ.get("MINNI_HOME", os.path.expanduser("~/.minni")))
    if root.exists():
        vaults.extend(root.glob("*-vault"))
    return list(dict.fromkeys(vaults))


# Caps mirror the TypeScript helper so forged summaries cannot parse as new audit entries.
AUDIT_SUMMARY_MAX = 500
AUDIT_DETAIL_LINE_MAX = 1000
AUDIT_DETAIL_BLOCK_MAX = 4000


def escape_audit_field(value: str, *, mode: str = "inline", max_len: Optional[int] = None) -> str:
    """Escape an audit-log field so injected newlines cannot forge entries."""
    v = "" if value is None else str(value)
    if mode == "inline":
        v = v.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        if v.startswith("#"):
            v = "\\" + v
    else:
        v = "\n".join(("\\" + ln) if ln.startswith("#") else ln for ln in v.split("\n"))
    if max_len is not None and len(v) > max_len:
        v = v[: max(0, max_len - 1)] + "…"
    return v


def escape_audit_details_block(raw: str) -> str:
    out_lines = []
    for ln in raw.split("\n"):
        escaped = ("\\" + ln) if ln.startswith("#") else ln
        if len(escaped) > AUDIT_DETAIL_LINE_MAX:
            escaped = escaped[: max(0, AUDIT_DETAIL_LINE_MAX - 1)] + "…"
        out_lines.append(escaped)
    block = "\n".join(out_lines)
    if len(block) > AUDIT_DETAIL_BLOCK_MAX:
        block = block[: max(0, AUDIT_DETAIL_BLOCK_MAX - 1)] + "…"
    return block


def append_handoff_audit(vault_path: Path, tool: str, summary: str, details: dict) -> None:
    ensure_handoff_vault(vault_path)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    safe_tool = escape_audit_field(tool, mode="inline", max_len=200)
    safe_summary = escape_audit_field(summary, mode="inline", max_len=AUDIT_SUMMARY_MAX)
    raw_details = json.dumps(details, indent=2, sort_keys=True)
    safe_details = escape_audit_details_block(raw_details)
    line = f"## [{ts}] {safe_tool} | {safe_summary}\n\n```json\n{safe_details}\n```\n\n"
    for path in (vault_path / "log.md", vault_path / "logs" / f"{ts[:10]}.md"):
        if not path.exists():
            path.write_text(f"# {ts[:10]} Minni Audit\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def validate_handoff_packet(from_agent: str, to_agent: str, packet: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(packet, dict):
        return None, "packet must be an object"
    normalized = dict(packet)
    normalized.setdefault("from_agent", from_agent)
    normalized.setdefault("to_agent", to_agent)
    normalized.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    normalized.setdefault("trace_id", str(uuid.uuid4()))
    normalized.setdefault("lease_id", f"handoff-{uuid.uuid4().hex[:16]}")
    normalized.setdefault("requires_ack", True)
    normalized.setdefault("expires_at", iso_from_epoch(time.time() + 24 * 3600))

    required = {
        "from_agent": str,
        "to_agent": str,
        "kind": str,
        "task": str,
        "envelope": str,
        "wikilink_refs": list,
        "trace_id": str,
        "created_at": str,
        "lease_id": str,
        "requires_ack": bool,
        "expires_at": str,
    }
    for key, typ in required.items():
        if key not in normalized:
            return None, f"{key} is required"
        if not isinstance(normalized[key], typ):
            return None, f"{key} must be {typ.__name__}"
        if typ is str and not normalized[key].strip():
            return None, f"{key} must not be empty"
    if normalized["from_agent"] != from_agent:
        return None, "from_agent must match params.from_agent"
    if normalized["to_agent"] != to_agent:
        return None, "to_agent must match params.to_agent"
    if normalized["kind"] not in HANDOFF_KINDS:
        return None, f"kind must be one of {sorted(HANDOFF_KINDS)}"
    if not all(isinstance(ref, str) and ref.strip() for ref in normalized["wikilink_refs"]):
        return None, "wikilink_refs must be a list of strings"
    if "expires_at" in normalized and normalized["expires_at"] is not None and not isinstance(normalized["expires_at"], str):
        return None, "expires_at must be a string"
    return normalized, None


def compile_handoff_page(sender_vault: Path, packet: dict, stamp: str) -> Optional[Path]:
    if packet.get("kind") != "handoff":
        return None
    title = packet["task"].strip()
    slug = slugify(f"{packet['from_agent']}-to-{packet['to_agent']}-{title}")
    page_path = sender_vault / "wiki" / "handoffs" / f"{stamp[:8]}-{slug}.md"
    refs = "\n".join(f"- [[{ref.removesuffix('.md')}]]" for ref in packet.get("wikilink_refs", []))
    page = (
        "---\n"
        f"title: {title}\n"
        "type: handoff\n"
        "status: accepted\n"
        "privacy: safe\n"
        f"from_agent: {packet['from_agent']}\n"
        f"to_agent: {packet['to_agent']}\n"
        f"trace_id: {packet['trace_id']}\n"
        f"created: {packet['created_at']}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"From: `{packet['from_agent']}`\n\n"
        f"To: `{packet['to_agent']}`\n\n"
        "## Envelope\n\n"
        f"```xml\n{packet['envelope']}\n```\n\n"
        "## Wikilink References\n\n"
        f"{refs or '- None'}\n"
    )
    page_path.write_text(page, encoding="utf-8")
    return page_path
