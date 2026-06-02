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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from config import CANONICAL_SOVEREIGN_HOME

logger = logging.getLogger("minnid.principal")

PRINCIPALS_DIR: Path = Path(CANONICAL_SOVEREIGN_HOME) / "principals"

# Single source of truth for the canonical principal file names (G11 root-of-trust).
# "main" is included because it is the synthesized default and a reasonable operator choice
# for the primary local identity file. All probe, strict-detection, and legacy-alias logic
# must use this list so that an operator who only ships principals/main.json is handled
# uniformly (no synthesis override, aliases respected, etc.).
CANONICAL_PRINCIPAL_NAMES: tuple[str, ...] = ("local", "default", "operator", "main")


def _has_any_principal_files(d: Path) -> bool:
    """Detect strict mode: whether any CANONICAL principal file exists under the dir.
    Extracted for testability + hygiene (addresses re-review nit on repeated any() walk).
    """
    return any((d / f"{n}.json").is_file() for n in CANONICAL_PRINCIPAL_NAMES)


@dataclass(frozen=True)
class EffectivePrincipal:
    """Server-stamped identity. Immutable. Single source of truth for who is calling."""

    agent_id: str
    workspace_id: str = "default"
    session_id: Optional[str] = None
    transport: str = "uds"
    capabilities: List[str] = field(default_factory=lambda: ["*"])
    allowed_vault_roots: List[str] = field(default_factory=list)

    def can(self, capability: str) -> bool:
        """Return True if the principal is allowed the named operation."""
        return "*" in self.capabilities or capability in self.capabilities

    def allows_vault_root(self, path: str | Path) -> bool:
        """Return True if path is under one of the allowed vault roots (or no restriction)."""
        if not self.allowed_vault_roots:
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
    aid = str(raw.get("agent_id") or raw.get("id") or "main")
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
      2. synthesize a safe wide-open local default (agent_id="main") — used for fresh installs
         (RCM-003: only "main" accepted when no principal files; mismatches rejected upstream)
    """
    d = principals_dir or PRINCIPALS_DIR
    _ensure_principals_dir(d)
    for name in CANONICAL_PRINCIPAL_NAMES:
        p = from_operator_config(
            name, principals_dir=d, transport=transport, strict=strict
        )
        if p is not None:
            return p
    # No operator config present — synthesize trusted local default "main".
    # RCM-003: resolve_effective_principal now rejects non-"main" supplied in this mode.
    return EffectivePrincipal(
        agent_id="main",
        workspace_id="default",
        transport=transport,
        capabilities=["*"],
        allowed_vault_roots=[],
    )


def resolve_effective_principal(
    *,
    supplied_agent_id: Optional[str] = None,
    transport: str = "uds",
    principals_dir: Optional[Path] = None,
) -> EffectivePrincipal:
    """Resolve the single authoritative EffectivePrincipal for this request.

    Rules (G11, post-RCM-003):
    - If any principals/*.json exist, strict mode: use the canonical stamped one (first in CANONICAL list).
    - If NO principal config files exist (fresh install), synthesize ONLY the fixed local "main"
      and reject any wire-supplied agent_id that differs (no more wildcard synthesis).
    - If the caller supplied an agent_id that does not match the stamped (or legacy alias in strict),
      raise IdentityMismatchError.
    - Aliases only honored in strict mode from existing principal files.

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

    supplied = str(supplied_agent_id).strip()
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
            caps = platform_caps.get(supplied) if isinstance(platform_caps, dict) else None
            # An explicit empty list means "no capabilities" and MUST be honoured —
            # `caps or ...` would wrongly fall through to the broader principal caps,
            # silently escalating a deliberately-restricted platform agent. Only fall
            # back when caps is absent (None). Likewise treat explicit null
            # workspace_id/capabilities as absent rather than coercing to "None".
            if caps is not None:
                resolved_caps = caps
            else:
                raw_caps = raw.get("capabilities")
                resolved_caps = raw_caps if raw_caps is not None else stamped.capabilities
            # Guard against malformed principal files: a bare string would be
            # silently split into per-character caps by list(), and a scalar
            # (int/bool) would raise TypeError and crash the daemon.
            if isinstance(resolved_caps, str):
                resolved_caps = [resolved_caps]
            elif not isinstance(resolved_caps, (list, tuple, set)):
                resolved_caps = []
            raw_ws = raw.get("workspace_id")
            resolved_ws = raw_ws if raw_ws is not None else stamped.workspace_id
            return EffectivePrincipal(
                agent_id=supplied,
                workspace_id=str(resolved_ws),
                session_id=raw.get("session_id"),
                transport=transport,
                capabilities=list(resolved_caps),
                allowed_vault_roots=stamped.allowed_vault_roots,
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

    # Mismatch (in strict or in fresh/no-principals mode) → hard deny.
    # Fresh installs now ONLY accept the fixed "main"; other supplied ids are rejected.
    raise IdentityMismatchError(supplied, stamped.agent_id)


# Convenience for handlers that want a ready-made JSON-RPC error payload
def make_mismatch_error(supplied: str, stamped: str, request_id: Any = None) -> dict:
    """Return a JSON-RPC error dict for identity mismatch (code -32000)."""
    # Local import to avoid any potential cycle with minnid
    try:
        from minnid import _make_error  # type: ignore

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
    if p.agent_id in ("main", "operator"):
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

    # Workspace scoping (doc or call can use '*' for shared cross-ws)
    doc_ws = str(
        doc_metadata.get("workspace_id")
        or doc_metadata.get("workspace")
        or doc_metadata.get("ws")
        or "default"
    )
    call_ws = str(workspace or "default")
    if doc_ws != "*" and call_ws != "*" and doc_ws != call_ws:
        return False

    # Vault root containment (realpath, symlink-aware, via G12)
    path = (
        doc_metadata.get("path")
        or doc_metadata.get("source")
        or doc_metadata.get("filename")
    )
    if path:
        try:
            if not principal.allows_vault_root(path):
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

    # Same-agent or legacy "unknown" → visible (if vault/privacy passed)
    if agent == principal.agent_id or agent == "unknown":
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
    page_type = str(doc_metadata.get("page_type") or "").lower()
    if (
        page_type in {"wiki", "handoff", "synthesis", "decision", "session"}
        or agent.lower().startswith(("wiki:", "handoff"))
        or "wiki" in agent.lower()
        or "handoff" in agent.lower()
    ):
        return True

    # Default-deny any other cross-agent case
    return False
