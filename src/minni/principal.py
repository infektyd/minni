#!/usr/bin/env python3
"""EffectivePrincipal — server-stamped single source of identity (G11 / SEC-002).

Caller-supplied ``agent_id`` (or ``agentId``) on the wire is NEVER authoritative.
This module is the sole resolution point used by the daemon:

* Resolve from explicit operator-controlled principal files under
  ``$MINNI_HOME/principals/<id>.json`` (0600) when present, OR
* Fall back to a trusted local default synthesized from the transport
  (UDS / stdio from the same host = operator context).

The stamped ``EffectivePrincipal`` is then used for ALL downstream decisions
(retrieval filters, vault derivation, audit attribution, writeback, etc.).
Mismatched caller-supplied agent_id is rejected with a structured JSON-RPC
error before any engine work occurs.

This is the keystone for all later governance (read policy, candidate pipeline,
deletion, cross-agent consent). No other module may mint or trust a raw
agent_id string from the caller.

Backwards compatibility: only via explicit server-side principal config files
that declare ``legacy_agent_ids``. There is no silent fallback in the hot path.

G11 minimal scope (per design doc + user query): Only the traditional agent_id-bearing
JSON-RPC methods (search, learn, read, feedback, log_event, resolve_contradiction)
plus the handoff from_agent claim receive the EffectivePrincipal stamp + mismatch
rejection. daemon.handoff/to_agent and internal per-agent helpers (_agent_vault etc.)
remain on validated claim strings for this increment; full threading of the
EffectivePrincipal object and G12+ read-policy / candidate pipeline are out of scope.
See CANONICAL_PRINCIPAL_NAMES for the authoritative file-name list.

Fields (per spec):
    agent_id: str                 — canonical stamped identity ("main", "hermes", ...)
    workspace_id: str             — scoping domain (default "default")
    session_id: Optional[str]     — per-session nonce for correlation
    transport: str                — "uds" | "stdio" | "http" | ...
    capabilities: List[str]       — e.g. ["*"] or subset ["search","learn"]
    allowed_vault_roots: List[str]— absolute paths this principal may touch

Usage in handlers:
    from principal import resolve_effective_principal, IdentityMismatchError
    supplied = params.get("agent_id")
    try:
        p = resolve_effective_principal(supplied_agent_id=supplied, transport="uds")
    except IdentityMismatchError as e:
        return _make_error(-32000, str(e), request_id)
    agent_id = p.agent_id
    # ... pass p (or just agent_id) downstream
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from minni.config import CANONICAL_SOVEREIGN_HOME

logger = logging.getLogger("minnid.principal")

PRINCIPALS_DIR: Path = Path(CANONICAL_SOVEREIGN_HOME) / "principals"

# Single source of truth for the canonical operator principal file names (G11 root-of-trust).
# "main" is included because it is the synthesized default and a reasonable operator choice
# for the primary local identity file. All probe, strict-detection, and legacy-alias logic
# must use this list so that an operator who only ships principals/main.json is handled
# uniformly (no synthesis override, aliases respected, etc.).
CANONICAL_PRINCIPAL_NAMES: tuple[str, ...] = ("local", "default", "operator", "main")
OPERATOR_RESERVED_AGENT_IDS: tuple[str, ...] = ("operator", "main")


def _has_any_principal_files(d: Path) -> bool:
    """Detect strict mode: whether any principal JSON file exists under the dir."""
    return any(p.is_file() and p.suffix == ".json" for p in d.glob("*.json"))


@dataclass(frozen=True)
class EffectivePrincipal:
    """Server-stamped identity. Immutable. Single source of truth for who is calling."""

    agent_id: str
    workspace_id: str = "default"
    session_id: Optional[str] = None
    transport: str = "uds"
    capabilities: List[str] = field(default_factory=lambda: ["*"])
    allowed_vault_roots: List[str] = field(default_factory=list)
    # Why the resolver default-denied this stamp (None for capable principals):
    # "unknown_identity"  — supplied non-reserved id with no principals/<id>.json
    # "reserved_agent_id" — wire claim of an operator-reserved id ("main"/"operator")
    # Diagnostic only: it lets the dispatch capability gate report WHY the deny
    # happened without re-deriving; it never grants anything (issue #121).
    deny_reason: Optional[str] = None

    def can(self, capability: str) -> bool:
        """Return True if the principal is allowed the named operation."""
        return "*" in self.capabilities or capability in self.capabilities

    def allows_vault_root(self, path: str | Path) -> bool:
        """Return True if path is under one of the allowed vault roots (or no restriction)."""
        if not self.allowed_vault_roots:
            if not self.capabilities:
                return False
            return True
        try:
            p = Path(path).resolve()
            for root in self.allowed_vault_roots:
                if p.is_relative_to(Path(root).resolve()):
                    return True
        except Exception:
            return False
        return False


class IdentityMismatchError(Exception):
    """Caller-supplied agent_id does not match the server-stamped EffectivePrincipal."""

    def __init__(self, supplied: str, stamped: str, detail: str = ""):
        msg = f"identity_mismatch: supplied '{supplied}' != stamped EffectivePrincipal '{stamped}'"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.supplied = supplied
        self.stamped = stamped


def _ensure_principals_dir(principals_dir: Optional[Path] = None) -> Path:
    """Ensure the principals/ directory exists with 0700 (like G04 SECURE_RUN_DIR).

    Called on every resolution path so the operator root-of-trust for identity is
    always present with safe permissions. Best-effort on non-POSIX.
    """
    d = principals_dir or PRINCIPALS_DIR
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
    except Exception:
        # Non-fatal on platforms without full POSIX chmod or when running as non-root
        # in a container; the subsequent file loads will still warn on bad file perms.
        pass
    return d


def _load_raw_principal_file(
    principal_name: str, principals_dir: Path, *, strict: bool = False
) -> Optional[dict]:
    """Best-effort load of a principal JSON file.

    When strict=True (we are about to trust this file for the authoritative stamp),
    a world-readable/writable file causes a hard error (security root-of-trust).
    Otherwise we warn + best-effort chmod the file to 0600 and continue (non-strict
    rollout safety). The directory is always ensured 0700 via _ensure_principals_dir.
    """
    _ensure_principals_dir(principals_dir)
    f = principals_dir / f"{principal_name}.json"
    if not f.is_file():
        return None
    try:
        try:
            mode = f.stat().st_mode
        except Exception as exc:
            if strict:
                raise RuntimeError(f"failed to stat principal file {f}: {exc}") from exc
            mode = 0o600
        if (mode & 0o077) != 0:
            if strict:
                raise RuntimeError(
                    f"principal file {f} must be 0600 (world-readable principal "
                    "config is a security risk for the identity root-of-trust)"
                )
            logger.warning(
                "principal file %s has overly permissive mode %o (expected 0600); "
                "best-effort chmod to 0600",
                f,
                mode,
            )
            try:
                os.chmod(f, 0o600)
            except Exception:
                pass
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except RuntimeError:
        if strict:
            raise
        logger.warning("failed to validate principal file %s", f)
    except Exception as exc:
        logger.warning("failed to parse principal file %s: %s", f, exc)
    return None


def _principal_from_raw(
    raw: dict, *, transport: str, principals_dir: Path
) -> EffectivePrincipal:
    """Construct EffectivePrincipal from a raw JSON dict (with sane defaults)."""
    aid = validate_agent_id(str(raw.get("agent_id") or raw.get("id") or "main"))
    raw_roots = raw.get("allowed_vault_roots", []) or []
    norm_roots = []
    for x in raw_roots:
        p = str(x)
        try:
            rp = Path(p).resolve()
            if not Path(p).is_absolute():
                logger.warning(
                    "principal %s allowed_vault_root %r was relative; resolved against cwd to %s. "
                    "Operator configs should use absolute paths.",
                    aid, p, rp,
                )
            norm_roots.append(str(rp))
        except Exception:
            norm_roots.append(p)
    return EffectivePrincipal(
        agent_id=aid,
        workspace_id=str(raw.get("workspace_id", "default")),
        session_id=raw.get("session_id"),
        transport=transport,
        capabilities=list(raw.get("capabilities", ["*"])),
        allowed_vault_roots=norm_roots,
    )


def from_operator_config(
    principal_name: str,
    *,
    principals_dir: Optional[Path] = None,
    transport: str = "uds",
    strict: bool = False,
) -> Optional[EffectivePrincipal]:
    """Load a specific principal by name from the operator config dir, or None."""
    d = principals_dir or PRINCIPALS_DIR
    _ensure_principals_dir(d)
    raw = _load_raw_principal_file(principal_name, d, strict=strict)
    if raw is None:
        return None
    return _principal_from_raw(raw, transport=transport, principals_dir=d)


def from_local_transport(
    transport: str = "uds",
    *,
    principals_dir: Optional[Path] = None,
    strict: bool = False,
) -> EffectivePrincipal:
    """Return a trusted local principal.

    Preference order:
      1. explicit principals/<name>.json for any name in CANONICAL_PRINCIPAL_NAMES
         (local, default, operator, main — "main" last so explicit operator files win)
      2. synthesize a local default (agent_id="main"):
         - Fresh install (non-strict: NO principal files at all) → wide-open ``["*"]``
           operator, preserving the single-user zero-config UX.
         - Strict install (P3): principal files EXIST but none is a canonical
           operator/main file. The operator has clearly set up an identity model
           and did NOT author an operator principal, so a no-agent-id caller must
           NOT be silently elevated to a wide-open operator ``main`` — that let any
           local process on a shared multi-agent daemon claim full governance.
           Synthesize a default-deny ``main`` instead, unless the operator asserts
           the authenticated local-operator signal ``MINNI_LOCAL_OPERATOR`` (an
           operator-/env-controlled channel, never wire-controlled).
    """
    d = principals_dir or PRINCIPALS_DIR
    _ensure_principals_dir(d)
    for name in CANONICAL_PRINCIPAL_NAMES:
        p = from_operator_config(
            name, principals_dir=d, transport=transport, strict=strict
        )
        if p is not None:
            return p
    # No canonical operator config present.
    # RCM-003: resolve_effective_principal now rejects non-"main" supplied in this mode.
    if strict and not _local_operator_asserted():
        # P3: strict install without an authored operator/main file → default-deny.
        return EffectivePrincipal(
            agent_id="main",
            workspace_id="default",
            transport=transport,
            capabilities=[],
            allowed_vault_roots=[],
        )
    # Fresh install (or operator-asserted local operator) → trusted wide-open main.
    return EffectivePrincipal(
        agent_id="main",
        workspace_id="default",
        transport=transport,
        capabilities=["*"],
        allowed_vault_roots=[],
    )


def _local_operator_asserted() -> bool:
    """True when the operator has asserted the authenticated local-operator signal.

    ``MINNI_LOCAL_OPERATOR`` is set in the daemon's own environment (operator-
    controlled), never derived from wire input, so it is a trustworthy channel
    for re-enabling a wide-open synthesized ``main`` in a strict install that
    deliberately relies on the zero-config operator (single-user who added a
    per-agent file but wants the no-agent-id caller to remain operator)."""
    return os.environ.get("MINNI_LOCAL_OPERATOR", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_AGENT_ID_RE = _re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')


def validate_agent_id(agent_id: str) -> str:
    """Validate an agent_id string against the canonical format.

    Accepts: lowercase alphanumeric, dots, underscores, hyphens. Must start
    with [a-z0-9]. Maximum 64 characters.

    Returns the agent_id unchanged if valid.
    Raises ValueError with a descriptive message if invalid.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError(f"agent_id must be a non-empty string, got {agent_id!r}")
    if not _AGENT_ID_RE.match(agent_id):
        raise ValueError(
            f"invalid agent_id {agent_id!r}: must match ^[a-z0-9][a-z0-9._-]{{0,63}}$ "
            f"(lowercase alphanumeric/dots/underscores/hyphens, max 64 chars)"
        )
    return agent_id


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = [value]
    else:
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _principal_aliases(raw: dict) -> list[str]:
    # Support both spellings; legacy_agent_ids wins in resolve_effective_principal
    # for auth, but recall scope can safely include the union declared by the
    # same operator-owned file.
    aliases: list[str] = []
    aliases.extend(_string_list(raw.get("legacy_agent_ids")))
    aliases.extend(_string_list(raw.get("aliases")))
    return aliases


def _add_valid_agent(scope: list[str], seen: set[str], agent_id: str) -> None:
    try:
        agent_id = validate_agent_id(agent_id)
    except ValueError:
        logger.warning("Ignoring invalid principal alias in recall scope: %r", agent_id)
        return
    if agent_id not in seen:
        scope.append(agent_id)
        seen.add(agent_id)


@lru_cache(maxsize=128)
def _agent_scope_for_cached(agent_id: str, principals_dir: str) -> tuple[str, ...]:
    scope: list[str] = []
    seen: set[str] = set()
    _add_valid_agent(scope, seen, agent_id)

    d = Path(principals_dir)
    _ensure_principals_dir(d)
    for name in CANONICAL_PRINCIPAL_NAMES:
        raw = _load_raw_principal_file(name, d, strict=False)
        if not raw:
            continue
        canonical = str(raw.get("agent_id") or raw.get("id") or "main").strip()
        aliases = _principal_aliases(raw)
        platform_ids = _string_list(raw.get("platform_agent_ids"))

        if agent_id == canonical or agent_id in platform_ids:
            for alias in aliases:
                _add_valid_agent(scope, seen, alias)
        elif agent_id in aliases:
            _add_valid_agent(scope, seen, canonical)
            for alias in aliases:
                _add_valid_agent(scope, seen, alias)

    return tuple(scope)


def agent_scope_for(agent_id: str, principals_dir: Optional[Path] = None) -> list[str]:
    """Return the learning agent-id scope for a stamped caller.

    The scope is the caller id plus any legacy aliases declared in the same
    operator principal files that resolve_effective_principal reads. This is
    intentionally for learning rows only; document agents are a page taxonomy.
    """
    agent_id = validate_agent_id(str(agent_id))
    d = principals_dir or PRINCIPALS_DIR
    return list(_agent_scope_for_cached(agent_id, str(d)))


agent_scope_for.cache_clear = _agent_scope_for_cached.cache_clear  # type: ignore[attr-defined]


def resolve_effective_principal(
    *,
    supplied_agent_id: Optional[str] = None,
    transport: str = "uds",
    principals_dir: Optional[Path] = None,
    operator_context: bool = False,
) -> EffectivePrincipal:
    """Resolve the single authoritative EffectivePrincipal for this request.

    Rules:
    - No supplied id means explicit local/operator context and resolves through
      the canonical operator principal order.
    - A supplied non-operator agent id first resolves principals/<agent_id>.json.
    - Unknown/fileless valid agent ids resolve to a default-deny principal.
    - Reserved operator ids ("main", "operator") require operator_context=True.
    - Per-agent file id mismatches still raise IdentityMismatchError.

    The returned principal (or the raised error) is the ONLY identity that
    downstream code is allowed to use.
    """
    d = principals_dir or PRINCIPALS_DIR
    _ensure_principals_dir(d)

    # Decide whether we are in "strict" mode (any principal file present)
    # Uses the single CANONICAL list so "main.json" (or any other) is detected uniformly.
    strict = _has_any_principal_files(d)

    if transport in ("uds", "stdio", "local", "test"):
        stamped = from_local_transport(transport, principals_dir=d, strict=strict)
    else:
        # Non-local transports would normally require authenticated principal id.
        # For G11 minimal we still give a local-style default (future work: mTLS etc.)
        stamped = from_local_transport("http", principals_dir=d, strict=strict)

    if supplied_agent_id is None:
        return stamped

    supplied_raw = str(supplied_agent_id).strip()
    if not supplied_raw:
        return stamped
    try:
        supplied = validate_agent_id(supplied_raw)
    except ValueError as exc:
        raise IdentityMismatchError(supplied_raw, stamped.agent_id, str(exc)) from exc

    if supplied in OPERATOR_RESERVED_AGENT_IDS and not operator_context:
        # RCM-003 anti-spoofing: a wire claim of a reserved operator id is never
        # honored on its own. MINNI_LOCAL_OPERATOR is the trusted escape hatch:
        # set in the daemon's own environment (operator-controlled, never
        # wire-controlled), it asserts operator context exactly like the
        # explicit operator_context flag (issue #121, Root B).
        if _local_operator_asserted():
            operator_context = True
        else:
            return _default_deny_principal(
                supplied, transport=transport, deny_reason="reserved_agent_id"
            )

    if operator_context:
        if supplied == stamped.agent_id:
            return stamped
        # The reserved operator ids are aliases of ONE local operator: an
        # operator-context claim of "operator" must normalize to the stamped
        # operator (synthesized/authored as e.g. "main"), not mismatch against
        # it (#132 review, P2). Only fires under trusted operator context —
        # wire callers without it were already denied above.
        if (
            supplied in OPERATOR_RESERVED_AGENT_IDS
            and stamped.agent_id in OPERATOR_RESERVED_AGENT_IDS
        ):
            return stamped
        if strict:
            aliases: list[str] = []
            for name in CANONICAL_PRINCIPAL_NAMES:
                raw = _load_raw_principal_file(name, d, strict=True)
                if raw:
                    aliases.extend(_principal_aliases(raw))
            if supplied in aliases:
                return stamped
        raise IdentityMismatchError(supplied, stamped.agent_id)

    agent_file_principal = from_operator_config(
        supplied, principals_dir=d, transport=transport, strict=True
    )
    if agent_file_principal is not None:
        if agent_file_principal.agent_id != supplied:
            raise IdentityMismatchError(supplied, agent_file_principal.agent_id)
        return agent_file_principal

    if not supplied:
        return stamped

    if supplied == stamped.agent_id:
        return stamped

    # Platform-hosted agents are not legacy aliases: they need their own stamped
    # principal so reads/writes are attributed to the actual surface (e.g. codex)
    # while the operator-controlled local principal file remains the trust root.
    if strict:
        for name in CANONICAL_PRINCIPAL_NAMES:
            raw = _load_raw_principal_file(name, d, strict=True)
            if not raw:
                continue
            raw_ids = raw.get("platform_agent_ids")
            platform_ids = [str(a) for a in raw_ids] if isinstance(raw_ids, list) else []
            if supplied not in platform_ids:
                continue
            platform_caps = raw.get("platform_agent_capabilities") or {}
            platform_caps_map = (
                platform_caps if isinstance(platform_caps, dict) else {}
            )
            # Platform agents must NOT inherit the operator wildcard when no per-agent
            # entry exists — listed ids without an explicit cap map entry default-deny.
            if supplied in platform_caps_map:
                resolved_caps = platform_caps_map[supplied]
            else:
                resolved_caps = []
            # Finding 6 residual: never copy the operator stamp's vault roots onto a
            # platform agent. Optional per-id map; a present-but-empty/malformed entry
            # → fail-closed sentinel (empty+caps would otherwise vacuous-allow every
            # path via allows_vault_root; a string value must not be iterated as chars).
            # A MISSING entry is not a lockout: existing installs using only
            # platform_agent_ids + platform_agent_capabilities predate the roots map,
            # and their platform principals must keep pathed access to their own
            # ~/.minni/<agent>-vault (both the literal id and the dashless alias the
            # installer uses, e.g. claude-code → claudecode-vault). Never the
            # operator's roots.
            _PLATFORM_NO_ROOTS = ["/.minni-platform-no-vault-roots"]

            def _default_platform_roots(agent_id: str) -> list[str]:
                minni_home = Path(
                    os.environ.get("MINNI_HOME") or (Path.home() / ".minni")
                ).expanduser()
                names = {f"{agent_id}-vault", f"{agent_id.replace('-', '')}-vault"}
                roots = [str((minni_home / n).resolve()) for n in sorted(names)]
                if agent_id == "gemini":
                    # The installer's vault_for("gemini") deliberately falls
                    # back to the legacy ~/.gemini/minni-vault when it has data
                    # and the canonical vault is missing (propagate.py). A
                    # strict install upgraded without platform_agent_vault_roots
                    # would otherwise stamp MCP with the legacy path while the
                    # daemon principal rejects it via allows_vault_root.
                    roots.append(
                        str(Path("~/.gemini/minni-vault").expanduser().resolve())
                    )
                return roots

            platform_roots = raw.get("platform_agent_vault_roots") or {}
            platform_roots_map = (
                platform_roots if isinstance(platform_roots, dict) else {}
            )
            if supplied in platform_roots_map:
                raw_roots = platform_roots_map[supplied]
                if not isinstance(raw_roots, list) or not raw_roots:
                    resolved_roots = list(_PLATFORM_NO_ROOTS)
                else:
                    resolved_roots = [
                        str(Path(r).expanduser().resolve())
                        for r in raw_roots
                        if isinstance(r, str) and r.strip()
                    ]
                    if not resolved_roots:
                        resolved_roots = list(_PLATFORM_NO_ROOTS)
            else:
                resolved_roots = _default_platform_roots(supplied)
            raw_ws = raw.get("workspace_id")
            resolved_ws = raw_ws if raw_ws is not None else stamped.workspace_id
            return EffectivePrincipal(
                agent_id=supplied,
                workspace_id=str(resolved_ws),
                session_id=raw.get("session_id"),
                transport=transport,
                capabilities=list(resolved_caps),
                allowed_vault_roots=resolved_roots,
            )

    # Check legacy aliases declared across ALL principal files that exist (union).
    # No early break: an alias declared in secondary file (e.g. default.json) must be
    # honoured even if the primary (local.json) exists with an empty list.
    if strict:
        aliases: list[str] = []
        for name in CANONICAL_PRINCIPAL_NAMES:
            raw = _load_raw_principal_file(name, d, strict=True)
            if raw:
                aliases.extend(
                    str(a)
                    for a in (raw.get("legacy_agent_ids") or raw.get("aliases") or [])
                )
        if supplied in aliases:
            # Accepted via migration alias from any principal file; return the stamped one.
            return stamped

    if strict and stamped.agent_id not in OPERATOR_RESERVED_AGENT_IDS:
        raise IdentityMismatchError(supplied, stamped.agent_id)

    return _default_deny_principal(
        supplied, transport=transport, deny_reason="unknown_identity"
    )


def _default_deny_principal(
    agent_id: str, *, transport: str, deny_reason: Optional[str] = None
) -> EffectivePrincipal:
    """Return a valid caller stamp with no capabilities and no vault roots."""
    return EffectivePrincipal(
        agent_id=validate_agent_id(agent_id),
        workspace_id="default",
        transport=transport,
        capabilities=[],
        allowed_vault_roots=[],
        deny_reason=deny_reason,
    )


# Convenience for handlers that want a ready-made JSON-RPC error payload
def make_mismatch_error(supplied: str, stamped: str, request_id: Any = None) -> dict:
    """Return a JSON-RPC error dict for identity mismatch (code -32000)."""
    # Local import to avoid any potential cycle with minnid
    try:
        from minni.minnid import _make_error  # type: ignore

        return _make_error(
            -32000,
            f"identity_mismatch: supplied agent_id '{supplied}' does not match server-stamped EffectivePrincipal '{stamped}'",
            request_id,
        )
    except Exception:
        # Fallback if imported standalone
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": f"identity_mismatch: supplied agent_id '{supplied}' does not match server-stamped EffectivePrincipal '{stamped}'",
            },
        }


def is_operator_principal(p: EffectivePrincipal) -> bool:
    """Return True if principal may perform governance actions (G14-G18).

    - resolve_candidate, force=true durable learn, etc.
    - The local "main" (synthesized on fresh installs or from principal file) is operator.
    - In strict mode, explicit capability "resolve_candidate"|"govern" or
      agent_id in the trusted operator set wins.
    - Never trusts caller-supplied strings; the p is already the stamped one.
    """
    caps = p.capabilities or []
    if "*" in caps or "resolve_candidate" in caps or "govern" in caps:
        return True
    # Only the explicit operator identities (not all canonical names) get governance by default.
    # "local"/"default" can be operator if they carry the cap or are the chosen main.
    if p.agent_id in ("main", "operator") and bool(caps):
        return True
    return False


def can_read_document(
    principal: Optional[EffectivePrincipal],
    workspace: str,
    doc_metadata: Optional[dict],
) -> bool:
    """G19: Single centralized read authorization gate (SEC-005).

    ALL read surfaces (minnid _handle_search/_handle_read/_handle_expand + handoff wikilinks,
    retrieval.retrieve/expand_result/search_*, agent_api.recall, any console/DB read path)
    MUST call this before surfacing doc/chunk content, provenance, or wikilink targets.

    - Respects EffectivePrincipal (never caller strings)
    - Reuses allows_vault_root (G12) + is_operator_principal (G14)
    - Workspace scoping (prep for G28; '*' = cross-ws shared)
    - Shared wiki/handoff/synthesis pages (agent=wiki:* or page_type) visible across agents when in roots
    - Private/local-only/foreign docs denied unless operator or same-agent
    - Blocked always denied
    - Minimal P2: no SQL ws predicate yet; path+privacy+agent+type based.
    """
    if principal is None or doc_metadata is None:
        return False
    if not isinstance(doc_metadata, dict):
        return False

    # Ownership is computed up front because the workspace and path gates
    # below both need it: legacy indexer rows (empty workspace_id, relative
    # path) are owner-readable but stay fail-closed for everyone else.
    _agent_early = str(
        doc_metadata.get("agent") or doc_metadata.get("sigil") or "unknown"
    ).strip()
    _page_type_early = str(doc_metadata.get("page_type") or "").lower()
    same_agent = _agent_early == principal.agent_id and not (
        _page_type_early == "session" and _agent_early.lower() == "unknown"
    )

    # Workspace scoping (doc or call can use '*' for shared cross-ws).
    # An EMPTY/missing doc workspace is a legacy unstamped row — treat it as
    # owner-wildcard: the writing agent can still recall it from any named
    # workspace, but it is NOT '*' for foreign readers.
    doc_ws_raw = (
        doc_metadata.get("workspace_id")
        or doc_metadata.get("workspace")
        or doc_metadata.get("ws")
    )
    doc_ws = str(doc_ws_raw or "default")
    call_ws = str(workspace or "default")
    if doc_ws != "*" and call_ws != "*" and doc_ws != call_ws:
        if not (same_agent and not doc_ws_raw):
            return False

    # Vault root containment (realpath, symlink-aware, via G12).
    # Absolute paths are root-checked as before (same-agent escape via an
    # absolute path outside the allowed roots stays denied). Relative paths
    # cannot be meaningfully resolved here (they would resolve against the
    # daemon cwd): a same-agent relative path is trusted as within the agent's
    # own vault (the path was stamped by the indexer, not the caller); a
    # foreign relative path is denied fail-closed.
    path = (
        doc_metadata.get("path")
        or doc_metadata.get("source")
        or doc_metadata.get("filename")
    )
    if path:
        try:
            if Path(path).expanduser().is_absolute():
                if not principal.allows_vault_root(path):
                    return False
            elif not principal.allowed_vault_roots:
                # Mirror allows_vault_root's unrestricted-principal semantics:
                # empty roots + capabilities = no path restriction; empty roots
                # + no capabilities = fail-closed.
                if not principal.capabilities:
                    return False
            elif not same_agent:
                return False
        except Exception:
            return False

    privacy = str(
        doc_metadata.get("privacy_level")
        or doc_metadata.get("privacy")
        or "safe"
    ).lower().strip()
    if privacy == "blocked":
        return False

    agent = str(
        doc_metadata.get("agent") or doc_metadata.get("sigil") or "unknown"
    ).strip()
    agent_l = agent.lower()
    page_type = str(doc_metadata.get("page_type") or "").lower()

    # Same-agent → visible (if vault/privacy passed). Exception: the legacy
    # "unknown" sentinel must not count as a real owner for session pages — a
    # principal literally named "unknown" must not inherit every unattributed
    # session via the same-agent shortcut.
    if agent == principal.agent_id and not (
        page_type == "session" and agent_l == "unknown"
    ):
        return True

    # Finding 10: session notes are agent-scoped. Deny foreign sessions BEFORE
    # the legacy agent="unknown" grant — otherwise unattributed sessions with
    # missing agent metadata become cross-agent readable.
    if (
        page_type == "session"
        or agent_l == "wiki:session"
        or agent_l.startswith("session:")
    ):
        # Operators may still audit (governance) below.
        if not is_operator_principal(principal):
            return False

    # Legacy "unknown"-attributed docs are visible to any *capable* principal,
    # but never to a default-deny stamp (no capabilities AND no vault roots = an
    # unknown/unauthorized identity). allows_vault_root already gates pathed docs
    # for default-deny; this also closes pathless agent="unknown" docs. (B2.)
    # Session pages already handled above — unknown+session never reaches here
    # for non-operators.
    if agent == "unknown" and (principal.capabilities or principal.allowed_vault_roots):
        return True

    # Operator/governance principals may read non-blocked foreign content within their roots
    # (for audit, resolve, oversight). Still vault-gated.
    if is_operator_principal(principal):
        return True

    # Foreign agent + private or local-only → explicit deny (G20 core)
    if privacy in {"private", "local-only"}:
        return False

    # Shared wiki / handoff / synthesis / decision pages are intentionally cross-visible
    # when the principal's vault roots allow the path and the page is not private.
    # (session deliberately excluded — see above.)
    if (
        page_type in {"wiki", "handoff", "synthesis", "decision"}
        or agent_l.startswith(("wiki:", "handoff"))
        or "wiki" in agent_l
        or "handoff" in agent_l
    ):
        return True

    # Default-deny any other cross-agent case
    return False


def allows_cross_agent_recall(principal: EffectivePrincipal) -> bool:
    """Return True if principal may opt into cross-agent learning recall (search.cross_agent)."""
    caps = principal.capabilities or []
    if "*" in caps or "cross_agent" in caps or "govern" in caps:
        return True
    return is_operator_principal(principal)


def make_capability_denied_error(
    capability: str,
    method: str,
    request_id: Any = None,
    *,
    principal_id: Optional[str] = None,
) -> dict:
    """Return JSON-RPC -32004 for a missing stamped-principal capability."""
    msg = f"capability_denied: {capability!r} required for {method!r}"
    if principal_id:
        msg += f" (principal={principal_id!r})"
    try:
        from minni.minnid import _make_error  # type: ignore

        return _make_error(-32004, msg, request_id)
    except Exception:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32004, "message": msg},
        }
