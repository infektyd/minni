import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from minni.config import CANONICAL_SOVEREIGN_HOME

from .redaction import redact_value


logger = logging.getLogger("minnid")

HANDOFF_KINDS = frozenset({"handoff", "candidate_learning", "request", "answer"})
HANDOFF_ACK_STATUSES = {"accepted", "rejected_stale", "rejected_contradicts", "rejected_scope"}


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
    # Resolve at call time (matches config.py providers/secrets pattern) so
    # MINNI_HOME overrides are honored without hardcoding ~/.minni.
    root = Path(os.environ.get("MINNI_HOME", os.path.expanduser("~/.minni")))
    if root.exists():
        vaults.extend(root.glob("*-vault"))
    return list(dict.fromkeys(vaults))


# SEC-014: caps for audit-log fields. Mirrors the TS helper in
# plugins/minni/src/vault.ts so a forged summary written by one
# daemon cannot be parsed as multiple `## [...]` entries by either reader.
AUDIT_SUMMARY_MAX = 500
AUDIT_DETAIL_LINE_MAX = 1000
AUDIT_DETAIL_BLOCK_MAX = 4000


def escape_audit_field(value: str, *, mode: str = "inline", max_len: Optional[int] = None) -> str:
    """Escape an audit-log field so injected newlines or leading `#` cannot
    forge a new `## [...]` log entry.

    - mode="inline" (tool, summary): collapse \\ \\r \\n to literal escapes so
      the header line stays single-line. Prefix a leading `#` with `\\`.
    - mode="block" (details): keep real newlines but escape any per-line
      leading `#` so the parser can't mistake them for entry headers.
    """
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


@dataclass(frozen=True)
class HandoffContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    lazy_writeback: Callable[[], Any]
    increment_request_count: Callable[[], None] | None = None
    store_handoff_lease: Callable[[dict, Path, Path], bool] | None = None
    agent_vault: Callable[[str], tuple[Path, bool]] = agent_vault
    known_agent_vaults: Callable[[], list[Path]] = known_agent_vaults
    ensure_handoff_vault: Callable[[Path], None] = ensure_handoff_vault
    write_json: Callable[[Path, dict], None] = write_json
    append_handoff_audit: Callable[[Path, str, str, dict], None] = append_handoff_audit
    validate_handoff_packet: Callable[[str, str, Any], tuple[Optional[dict], Optional[str]]] = validate_handoff_packet
    compile_handoff_page: Callable[[Path, dict, str], Optional[Path]] = compile_handoff_page
    redact_value: Callable[[Any], tuple[Any, bool]] = redact_value
    slugify: Callable[[str], str] = slugify
    parse_iso_ts: Callable[[Optional[str]], Optional[float]] = parse_iso_ts
    iso_from_epoch: Callable[[float], str] = iso_from_epoch
    logger: logging.Logger = logger


def handle_daemon_handoff(params: dict, request_id: Any, context: HandoffContext) -> dict:
    """Validate, redact, and mediate an agent-to-agent inbox handoff."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    from_agent = str(params.get("from_agent", "")).strip()
    to_agent = str(params.get("to_agent", "")).strip()
    if not from_agent or not to_agent:
        return context.make_error(-32602, "from_agent and to_agent are required", request_id)

    # G11: EffectivePrincipal stamp for the handoff path (from_agent is the identity
    # claim for the packet originator). Mismatch is rejected before any vault creation,
    # redaction, or cross-agent delivery. This closes the last public RPC that carried
    # an unauthenticated agent identity claim. (to_agent is a destination, not a
    # self-claim; it is validated by the consent contract in later G items.)
    principal, err = context.handler_principal(params, request_id, claim_key="from_agent")
    if err:
        return err
    # Use the stamped value for any attribution inside this handler (future G items
    # will thread the full EffectivePrincipal object).
    from_agent = principal.agent_id

    packet, error = context.validate_handoff_packet(from_agent, to_agent, params.get("packet"))
    if error:
        return context.make_error(-32602, error, request_id)

    redacted_packet, redacted = context.redact_value(packet)
    sender_vault, sender_explicit = context.agent_vault(from_agent)
    recipient_vault, recipient_explicit = context.agent_vault(to_agent)

    # G12: after G11 principal stamp, verify both derived vaults are inside allowed_vault_roots
    # (covers handoff across agents; realpath denies .. and symlink escapes to outside roots)
    for vlabel, vpath in (("sender", sender_vault), ("recipient", recipient_vault)):
        if not principal.allows_vault_root(vpath):
            context.logger.warning(
                "vault_root_denied handoff label=%s principal=%s", vlabel, principal.agent_id
            )
            return context.make_error(
                -32003,
                f"vault_root_denied: {vlabel} vault outside principal.allowed_vault_roots",
                request_id,
            )

    # G23: harden wikilink_refs containment — no traversal out of sender's allowed vault/wiki
    # (realpath + is_relative_to + allows_vault_root; symlink-aware)
    wiki_root = sender_vault / "wiki"
    for ref in packet.get("wikilink_refs", []) or []:
        try:
            ref_str = str(ref).strip()
            if not ref_str:
                continue
            # candidate target inside wiki/ (or vault root); reject ../ or absolute escapes
            cand = (wiki_root / ref_str.lstrip("/")).resolve()
            if not cand.is_relative_to(wiki_root.resolve()) or not principal.allows_vault_root(cand):
                context.logger.warning(
                    "wikilink_traversal_denied ref=%s principal=%s",
                    ref_str,
                    principal.agent_id,
                )
                return context.make_error(
                    -32003,
                    "wikilink_traversal_denied: wikilink escapes allowed vault roots (G23)",
                    request_id,
                )
        except Exception:
            # treat resolution failure as deny
            return context.make_error(-32003, "wikilink_traversal_denied: unresolvable ref", request_id)

    create_missing = os.environ.get("MINNI_HANDOFF_CREATE_MISSING_VAULTS", "1").lower() not in {
        "0",
        "false",
        "off",
    }

    if not recipient_explicit and not recipient_vault.exists() and not create_missing:
        if create_missing or sender_explicit:
            context.ensure_handoff_vault(sender_vault)
        return context.make_response({
            "status": "degraded",
            "delivered": False,
            "reason": f"destination vault not found for {to_agent}",
            "to_agent": to_agent,
            "recipient_vault": str(recipient_vault),
        }, request_id)

    try:
        context.ensure_handoff_vault(sender_vault)
        context.ensure_handoff_vault(recipient_vault)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = f"{stamp}-{context.slugify(redacted_packet['task'])}-{redacted_packet['trace_id'][:8]}.json"
        outbox_path = sender_vault / "outbox" / suffix
        inbox_path = recipient_vault / "inbox" / suffix
        context.write_json(outbox_path, redacted_packet)
        context.write_json(inbox_path, redacted_packet)
        if context.store_handoff_lease is not None:
            lease_persisted = context.store_handoff_lease(redacted_packet, inbox_path, outbox_path)
        else:
            lease_persisted = store_handoff_lease(redacted_packet, inbox_path, outbox_path, context)
        page_path = context.compile_handoff_page(sender_vault, redacted_packet, stamp)
        audit_details = {
            "trace_id": redacted_packet["trace_id"],
            "from_agent": from_agent,
            "to_agent": to_agent,
            "kind": redacted_packet["kind"],
            "task": redacted_packet["task"],
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "redacted": redacted,
            "lease_id": redacted_packet["lease_id"],
            "expires_at": redacted_packet.get("expires_at"),
            "lease_persisted": lease_persisted,
        }
        context.append_handoff_audit(sender_vault, "handoff_sent", redacted_packet["task"], audit_details)
        context.append_handoff_audit(recipient_vault, "handoff_received", redacted_packet["task"], audit_details)
        status = "ok" if lease_persisted else "degraded"
        return context.make_response({
            "status": status,
            "delivered": True,
            "lease_persisted": lease_persisted,
            "reason": None if lease_persisted else "handoff delivered to JSON packets but SQLite lease persistence failed",
            "redacted": redacted,
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "trace_id": redacted_packet["trace_id"],
            "lease_id": redacted_packet["lease_id"],
            "expires_at": redacted_packet.get("expires_at"),
        }, request_id)
    except Exception as exc:
        context.logger.exception("daemon.handoff failed")
        return context.make_response({
            "status": "degraded",
            "delivered": False,
            "reason": str(exc),
            "to_agent": to_agent,
        }, request_id)


def iter_handoff_files(agent_id: Optional[str] = None, *, context: HandoffContext | None = None):
    agent_vault_fn = context.agent_vault if context is not None else agent_vault
    known_agent_vaults_fn = context.known_agent_vaults if context is not None else known_agent_vaults
    if agent_id:
        vault, _ = agent_vault_fn(agent_id)
        vaults = [vault]
    else:
        vaults = known_agent_vaults_fn()
    for vault in vaults:
        for rel in ("inbox", "outbox"):
            directory = vault / rel
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    packet = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(packet, dict) and packet.get("lease_id"):
                    yield vault, path, packet


def write_matching_lease_packets(
    lease_id: str,
    updates: dict,
    *,
    context: HandoffContext | None = None,
) -> list[str]:
    writer = context.write_json if context is not None else write_json
    changed = []
    for _vault, path, packet in iter_handoff_files(context=context):
        if packet.get("lease_id") != lease_id:
            continue
        packet.update(updates)
        writer(path, packet)
        changed.append(str(path))
    return changed


def store_handoff_lease(packet: dict, inbox_path: Path, outbox_path: Path, context: HandoffContext) -> bool:
    """Persist the authoritative handoff lease row while keeping JSON packets."""
    try:
        created_at = context.parse_iso_ts(packet.get("created_at")) or time.time()
        expires_at = context.parse_iso_ts(packet.get("expires_at"))
        status = "pending" if packet.get("requires_ack", True) else "not_required"
        db = context.lazy_writeback().db
        with db.cursor() as c:
            c.execute(
                """INSERT INTO handoff_leases
                   (lease_id, from_agent, to_agent, task, status, contradicts_id,
                    inbox_path, outbox_path, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lease_id) DO UPDATE SET
                     from_agent = excluded.from_agent,
                     to_agent = excluded.to_agent,
                     task = excluded.task,
                     inbox_path = excluded.inbox_path,
                     outbox_path = excluded.outbox_path,
                     expires_at = excluded.expires_at""",
                (
                    packet["lease_id"],
                    packet["from_agent"],
                    packet["to_agent"],
                    packet.get("task"),
                    status,
                    packet.get("contradicts_id"),
                    str(inbox_path),
                    str(outbox_path),
                    created_at,
                    expires_at,
                ),
            )
        return True
    except Exception as exc:
        context.logger.warning("Could not persist handoff lease %r: %s", packet.get("lease_id"), exc)
        return False


def update_handoff_lease_status(
    lease_id: str,
    status: str,
    contradicts_id: Any = None,
    *,
    context: HandoffContext,
) -> bool:
    try:
        db = context.lazy_writeback().db
        with db.cursor() as c:
            c.execute(
                """UPDATE handoff_leases
                   SET status = ?,
                       contradicts_id = COALESCE(?, contradicts_id)
                   WHERE lease_id = ?""",
                (status, contradicts_id, lease_id),
            )
            return c.rowcount > 0
    except Exception as exc:
        context.logger.warning("Could not update handoff lease %r: %s", lease_id, exc)
        return False


def pending_handoff_leases(agent_id: str, *, context: HandoffContext) -> list[dict]:
    try:
        now = time.time()
        db = context.lazy_writeback().db
        with db.cursor() as c:
            rows = c.execute(
                """SELECT lease_id, from_agent, to_agent, task, expires_at, inbox_path
                   FROM handoff_leases
                   WHERE to_agent = ?
                     AND status = 'pending'
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at ASC, lease_id ASC""",
                (agent_id, now),
            ).fetchall()
        return [
            {
                "lease_id": row["lease_id"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "task": row["task"],
                "expires_at": context.iso_from_epoch(row["expires_at"]) if row["expires_at"] is not None else None,
                "path": row["inbox_path"],
            }
            for row in rows
        ]
    except Exception as exc:
        context.logger.warning("Could not list SQLite handoff leases for %r: %s", agent_id, exc)
        return []


def handoff_lease_status(lease_id: str, *, context: HandoffContext) -> Optional[dict]:
    try:
        db = context.lazy_writeback().db
        with db.cursor() as c:
            row = c.execute(
                "SELECT lease_id, status FROM handoff_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        if row is None:
            return None
        return {"lease_id": row["lease_id"], "status": row["status"]}
    except Exception as exc:
        context.logger.warning("Could not read handoff lease %r: %s", lease_id, exc)
        return None


def lease_to_agent(lease_id: str, *, context: HandoffContext) -> Optional[str]:
    """Recipient (``to_agent``) of a handoff lease: authoritative SQLite row
    first, JSON packets as fallback (leases can exist file-only when SQLite
    persistence degraded). None when the lease is unknown on both channels."""
    try:
        db = context.lazy_writeback().db
        with db.cursor() as c:
            row = c.execute(
                "SELECT to_agent FROM handoff_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        if row is not None and row["to_agent"]:
            return str(row["to_agent"])
    except Exception as exc:
        context.logger.warning("Could not read lease %r for authz: %s", lease_id, exc)
    for _vault, _path, packet in iter_handoff_files(context=context):
        if packet.get("lease_id") == lease_id and packet.get("to_agent"):
            return str(packet.get("to_agent"))
    return None


def handle_ack_handoff(params: dict, request_id: Any, context: HandoffContext) -> dict:
    lease_id = str(params.get("lease_id", "")).strip()
    status = str(params.get("status", "")).strip()
    if not lease_id:
        return context.make_error(-32602, "lease_id is required", request_id)
    if status not in HANDOFF_ACK_STATUSES:
        return context.make_error(-32602, f"status must be one of {sorted(HANDOFF_ACK_STATUSES)}", request_id)
    # A3 authz (fixes a live incident: co-located session subagents acked and
    # archived other agents' live handoffs): only the lease's RECIPIENT may ack
    # it. The principal is server-stamped (G11) — caller-supplied agent_id is
    # only honoured through the platform_agent_ids / alias machinery, never raw.
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    to_agent = lease_to_agent(lease_id, context=context)
    if to_agent is None:
        return context.make_error(-32000, f"No handoff lease found for {lease_id}", request_id)
    if to_agent != principal.agent_id:
        return context.make_error(
            -32004,
            f"principal_mismatch: handoff lease {lease_id} is addressed to "
            f"'{to_agent}'; caller principal '{principal.agent_id}' may not ack it",
            request_id,
        )
    contradicts_id = params.get("contradicts_id")
    updates = {
        "ack_status": status,
        "acked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if contradicts_id is not None:
        updates["contradicts_id"] = contradicts_id
    changed = write_matching_lease_packets(lease_id, updates, context=context)
    db_changed = update_handoff_lease_status(lease_id, status, contradicts_id, context=context)
    if not changed and not db_changed:
        return context.make_error(-32000, f"No handoff lease found for {lease_id}", request_id)
    # B1: an explicit ack is terminal for the file channel — archive the
    # recipient-side inbox copies so they stop re-surfacing at SessionStart.
    # The outbox copy (and the authoritative lease row) keep the ack_status
    # for await_handoff. Rename only, never unlink.
    archived = []
    for changed_path in changed:
        p = Path(changed_path)
        if p.parent.name != "inbox":
            continue
        try:
            from minni.afm_passes.inbox_archive import archive_inbox_file

            new_path = archive_inbox_file(p)
            if new_path:
                archived.append(new_path)
        except Exception as exc:
            context.logger.warning("ack_handoff: archive of %s failed: %s", p, exc)
    return context.make_response({
        "lease_id": lease_id,
        "status": status,
        "updated_paths": changed,
        "archived_paths": archived,
    }, request_id)


def handle_list_pending_handoffs(params: dict, request_id: Any, context: HandoffContext) -> dict:
    # G11 / RCM-003/009: EffectivePrincipal stamp + mismatch guard (closes last model-facing agent_id surfaces)
    # Model-supplied agent_id is NEVER trusted; use stamped principal.agent_id for leases/queries.
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    if not agent_id:
        return context.make_error(-32602, "agent_id is required", request_id)
    sqlite_handoffs = pending_handoff_leases(agent_id, context=context)
    if sqlite_handoffs:
        return context.make_response({"agent_id": agent_id, "handoffs": sqlite_handoffs}, request_id)

    now = time.time()
    handoffs = []
    for _vault, path, packet in iter_handoff_files(agent_id, context=context):
        if packet.get("to_agent") != agent_id:
            continue
        if not packet.get("requires_ack", False):
            continue
        if packet.get("ack_status"):
            continue
        expires_at = context.parse_iso_ts(packet.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        handoffs.append({
            "lease_id": packet.get("lease_id"),
            "from_agent": packet.get("from_agent"),
            "to_agent": packet.get("to_agent"),
            "task": packet.get("task"),
            "expires_at": packet.get("expires_at"),
            "path": str(path),
        })
    return context.make_response({"agent_id": agent_id, "handoffs": handoffs}, request_id)


async def handle_await_handoff(params: dict, request_id: Any, context: HandoffContext) -> dict:
    lease_id = str(params.get("lease_id", "")).strip()
    if not lease_id:
        return context.make_error(-32602, "lease_id is required", request_id)
    timeout_ms = max(0, min(int(params.get("timeout_ms", 30000)), 300000))
    deadline = time.time() + (timeout_ms / 1000.0)
    while True:
        lease = handoff_lease_status(lease_id, context=context)
        if lease is not None and lease.get("status") not in {None, "pending"}:
            return context.make_response({
                "lease_id": lease_id,
                "status": lease.get("status"),
            }, request_id)
        for _vault, _path, packet in iter_handoff_files(context=context):
            if packet.get("lease_id") == lease_id and packet.get("ack_status"):
                return context.make_response({
                    "lease_id": lease_id,
                    "status": packet.get("ack_status"),
                    "acked_at": packet.get("acked_at"),
                }, request_id)
        if time.time() >= deadline:
            return context.make_response({"lease_id": lease_id, "status": "timeout"}, request_id)
        await asyncio.sleep(0.05)
