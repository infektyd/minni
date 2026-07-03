"""AFM provider selection and native helper boundary.

This module keeps AFM calls opt-in and local-only. Call sites choose an
operation-specific bridge fallback, while this boundary owns shared provider
mode handling and the native JSON helper contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
from typing import Any, Callable, Dict, Literal, Optional

AFMMode = Literal["off", "bridge", "native", "auto"]

_VALID_MODES = {"off", "bridge", "native", "auto"}
_DEFAULT_NATIVE_HELPER = Path(__file__).resolve().parent / "native_afm_helper"
DEFAULT_AFM_CHAT_COMPLETIONS_URL = "http://127.0.0.1:11437/v1/chat/completions"


@dataclass(frozen=True)
class AFMResult:
    ok: bool
    data: Dict[str, Any]
    provider: Literal["native", "bridge", "off"]
    status: str
    error: Optional[str] = None
    fallback_used: bool = False


def resolve_afm_mode(value: Optional[str] = None) -> AFMMode:
    raw = (
        value
        if value is not None
        else os.environ.get("MINNI_AFM_PROVIDER_MODE", os.environ.get("MINNI_AFM_MODE", "bridge"))
    ).strip().lower()
    return raw if raw in _VALID_MODES else "bridge"  # type: ignore[return-value]


def native_helper_path() -> Path:
    return Path(os.environ.get("MINNI_AFM_NATIVE_HELPER", os.fspath(_DEFAULT_NATIVE_HELPER))).expanduser()


def native_available(helper_path: Optional[Path] = None) -> bool:
    helper = helper_path or native_helper_path()
    return helper.is_file() and os.access(helper, os.X_OK)


def _safe_status_error(error: Optional[str]) -> Optional[str]:
    if not error:
        return None
    text = str(error)
    # SEC (P3): strip auth headers / bearer tokens / API keys before anything
    # can reach status, audit, or error output (mirror of afm.ts safeError).
    # Key names and values may be JSON-quoted (serialized header dumps), so
    # quotes around the name and separator are tolerated.
    text = re.sub(
        r"\b(authorization|proxy-authorization)\b[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|basic\s+)?[^\s\"',;)]+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "bearer [redacted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(x-api-key|api[-_]?key|apikey|access[-_]?token|secret[-_]?key)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;)]+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted-key]", text)
    text = re.sub(r"/(?:Users|Volumes|private|var|tmp|Library)/[^\s\"')]+", "[local-path]", text)
    # Windows drive-letter paths (C:\Users\..., D:/data/...) — the POSIX pattern
    # above only matches forward-slash unix paths, so Windows paths leaked raw.
    # \b avoids eating multi-letter URL schemes (http:// etc., where the letter
    # before ':' is not a word boundary).
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s\"')]+", "[local-path]", text)
    text = re.sub(r"[^\s\"')]+\.fmadapter\b", "[adapter]", text)
    text = re.sub(r"[^\s\"')]+\.(?:db|sqlite|sqlite3|faiss|index|plist)\b", "[local-artifact]", text)
    return text[:240]


# --- Verified generation health (P1: honest health) -------------------------
#
# Mirrors plugins/minni/src/afm.ts getAfmProviderHealth/ProviderHealth.
# `ok`/`generation_verified` is only true when a real 1-token completion has
# been verified within the TTL — /health reachability alone never counts.

_GENERATION_PROBE_TTL_SECONDS = 300.0
# Native mode spawns a fresh helper that cold-loads FoundationModels on the
# first call (~2-3s measured; warm calls ~0.5s). The 1.5s bridge-era default
# clipped that cold start. Generous default (env-overridable) so the cold probe
# succeeds and warms the model for every subsequent call; cached for the TTL.
def _generation_probe_timeout_seconds() -> float:
    raw = os.environ.get("MINNI_AFM_PROBE_TIMEOUT", "10.0")
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 10.0
    return timeout if timeout > 0 else 10.0


_GENERATION_PROBE_TIMEOUT_SECONDS = _generation_probe_timeout_seconds()
_generation_probe_cache: Dict[str, Dict[str, Any]] = {}

# --- cross-process persistent probe cache (L2) -------------------------------
#
# The in-memory dict above (L1) only benefits long-lived processes; SessionStart
# hooks and short CLI invocations start fresh and would re-probe on every boot.
# A small atomic-write JSON file under ~/.minni/run/ shares recent probes across
# processes — same versioned schema as plugins/minni/src/afm.ts (probed_at_ms is
# epoch milliseconds in the file; converted at this boundary). Any read/parse
# problem degrades to "no cache" (normal probe); writes are best-effort.

_PROBE_CACHE_FILE_VERSION = 1


def _probe_cache_file_path() -> str:
    """MINNI_AFM_PROBE_CACHE override exists so tests never touch live ~/.minni."""
    override = os.environ.get("MINNI_AFM_PROBE_CACHE")
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser(os.environ.get("MINNI_HOME", "~/.minni"))
    return os.path.join(home, "run", "afm-probe-cache.json")


def _read_persistent_probe_entries() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_probe_cache_file_path(), "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict) or parsed.get("version") != _PROBE_CACHE_FILE_VERSION:
        return {}
    entries = parsed.get("entries")
    if not isinstance(entries, dict):
        return {}
    valid: Dict[str, Dict[str, Any]] = {}
    for key, value in entries.items():
        if (
            isinstance(value, dict)
            and isinstance(value.get("reachable"), bool)
            and isinstance(value.get("generation_verified"), bool)
            and isinstance(value.get("probed_at_ms"), (int, float))
        ):
            valid[key] = value
    return valid


def _write_persistent_probe_entries(entries: Dict[str, Dict[str, Any]]) -> None:
    # Best-effort: persistence must never break the health path.
    temp_path = None
    try:
        file_path = _probe_cache_file_path()
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        temp_path = f"{file_path}.{os.getpid()}.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": _PROBE_CACHE_FILE_VERSION, "entries": entries}))
        os.replace(temp_path, file_path)
        temp_path = None  # renamed away; nothing to clean up
    except OSError:
        pass
    finally:
        # A failed write/rename must not leak .tmp files into the run dir.
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _persist_probe_mutation(mutate: Callable[[Dict[str, Dict[str, Any]]], None]) -> None:
    entries = _read_persistent_probe_entries()
    mutate(entries)
    _write_persistent_probe_entries(entries)


def _to_persisted_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reachable": bool(entry["reachable"]),
        "generation_verified": bool(entry["generation_verified"]),
        "detail": entry.get("detail"),
        "probed_at_ms": float(entry["probed_at"]) * 1000.0,
    }


def _load_persisted_probe_entry(key: str) -> Optional[Dict[str, Any]]:
    value = _read_persistent_probe_entries().get(key)
    if value is None:
        return None
    detail = value.get("detail")
    return {
        "reachable": value["reachable"],
        "generation_verified": value["generation_verified"],
        "detail": detail if isinstance(detail, str) else None,
        "probed_at": float(value["probed_at_ms"]) / 1000.0,
    }


def reset_afm_generation_probe_cache() -> None:
    _generation_probe_cache.clear()
    try:
        os.unlink(_probe_cache_file_path())
    except OSError:
        pass


def note_afm_generation_failure(url: Optional[str] = None) -> None:
    """Invalidate cached generation probes after a live call failure."""
    if url is None:
        _generation_probe_cache.clear()
        _persist_probe_mutation(lambda entries: entries.clear())
        return
    for key in list(_generation_probe_cache):
        if key.endswith(f"|{url}"):
            del _generation_probe_cache[key]

    def _drop(entries: Dict[str, Dict[str, Any]]) -> None:
        for key in list(entries):
            if key.endswith(f"|{url}"):
                del entries[key]

    _persist_probe_mutation(_drop)


def note_afm_generation_success(url: str, mode: str = "bridge", now: Callable[[], float] = time.time) -> None:
    """Upsert a verified probe entry after a successful live call.

    The call IS a generation proof; without this symmetric positive signal a
    transient bridge outage would keep afm_ok=false for the rest of the TTL
    even while real calls succeed (mirror of afm.ts noteAfmGenerationSuccess).
    """
    entry = {"reachable": True, "generation_verified": True, "detail": None, "probed_at": now()}
    _generation_probe_cache[f"{mode}|{url}"] = entry
    for key in list(_generation_probe_cache):
        if key.endswith(f"|{url}"):
            _generation_probe_cache[key] = dict(entry)

    def _upsert(entries: Dict[str, Dict[str, Any]]) -> None:
        entries[f"{mode}|{url}"] = _to_persisted_entry(entry)
        for key in list(entries):
            if key.endswith(f"|{url}"):
                entries[key] = _to_persisted_entry(entry)

    _persist_probe_mutation(_upsert)


def _chat_completion_content(data: Dict[str, Any]) -> Optional[str]:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _native_completion_content(data: Dict[str, Any]) -> Optional[str]:
    """Native helpers answer chat_completion probes with a chat-shaped body or
    a flat {answer|content|text} field; ok with neither proves nothing."""
    content = _chat_completion_content(data)
    if content:
        return content
    for key in ("answer", "content", "text"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _run_generation_probe(
    resolved: AFMMode,
    url: str,
    timeout: float,
    client: Optional["BridgeClient"],
    now: Callable[[], float],
) -> Dict[str, Any]:
    payload = {
        "model": "apple-foundation-models",
        "temperature": 0,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ok"}],
    }
    result = afm_chat_completion(payload, mode=resolved, url=url, timeout=timeout, client=client)
    # Native mode no longer auto-verifies on any ok response: both paths must
    # produce actual completion content before afm_ok may flip true.
    if result.provider == "native":
        verified = result.ok and _native_completion_content(result.data) is not None
    else:
        verified = result.ok and isinstance(_chat_completion_content(result.data), str)
    reachable = result.ok or bool(result.error and "HTTP" in str(result.error))
    detail = None if verified else _safe_status_error(result.error) or "generation probe failed"
    return {
        "reachable": reachable,
        "generation_verified": verified,
        "detail": detail,
        "probed_at": now(),
    }


def verify_afm_generation(
    mode: Optional[str] = None,
    *,
    url: Optional[str] = None,
    timeout: Optional[float] = None,
    ttl_seconds: float = _GENERATION_PROBE_TTL_SECONDS,
    client: Optional["BridgeClient"] = None,
    now: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Return ProviderHealth-shaped status for the configured AFM provider.

    {"ok", "reachable", "generation_verified", "probe_age_ms", "detail"} where
    ok requires a verified 1-token completion within the TTL. Stale entries are
    re-probed synchronously here (the probe is bounded by `timeout`); the TS
    mirror refreshes in the background because SessionStart latency matters there.
    """
    if timeout is None:
        # PR84-5: read the probe timeout at CALL time so a runtime/test override
        # of MINNI_AFM_PROBE_TIMEOUT takes effect — it was previously bound once
        # at import into the default argument.
        timeout = _generation_probe_timeout_seconds()
    resolved = resolve_afm_mode(mode)
    if resolved == "off":
        return {
            "ok": False,
            "reachable": False,
            "generation_verified": False,
            "probe_age_ms": 0,
            "detail": "AFM mode is off",
        }
    target = url or DEFAULT_AFM_CHAT_COMPLETIONS_URL
    key = f"{resolved}|{target}"
    cached = _generation_probe_cache.get(key)
    if cached is None:
        # L2: a fresh process (SessionStart hook / short CLI) reuses a recent
        # probe persisted by another process instead of paying a live
        # generation call every boot. Stale or missing/corrupt file entries
        # fall through to a normal probe.
        persisted = _load_persisted_probe_entry(key)
        if persisted is not None and (now() - persisted["probed_at"]) < ttl_seconds:
            _generation_probe_cache[key] = persisted
            cached = persisted
    if cached is None or (now() - cached["probed_at"]) >= ttl_seconds:
        cached = _run_generation_probe(resolved, target, timeout, client, now)
        _generation_probe_cache[key] = cached
        fresh = cached

        def _store(entries: Dict[str, Dict[str, Any]]) -> None:
            entries[key] = _to_persisted_entry(fresh)

        _persist_probe_mutation(_store)
    age_ms = int(max(0.0, now() - cached["probed_at"]) * 1000)
    return {
        "ok": cached["generation_verified"],
        "reachable": cached["reachable"],
        "generation_verified": cached["generation_verified"],
        "probe_age_ms": age_ms,
        "detail": cached["detail"],
    }


def _with_generation_health(report: Dict[str, Any], health: Dict[str, Any]) -> Dict[str, Any]:
    report["ok"] = health["generation_verified"]
    report["generation_verified"] = health["generation_verified"]
    report["probe_age_ms"] = health["probe_age_ms"]
    report["reachable"] = health["reachable"]
    if health["detail"] and "error" not in report:
        report["error"] = health["detail"]
    return report


def afm_runtime_status(
    mode: Optional[str] = None,
    *,
    probe: Optional[Callable[..., Dict[str, Any]]] = None,
    probe_client: Optional["BridgeClient"] = None,
) -> Dict[str, Any]:
    resolved = resolve_afm_mode(mode)
    helper = native_helper_path()
    helper_available = native_available(helper)
    adapter_configured = bool(os.environ.get("MINNI_AFM_ADAPTER_PATH") or os.environ.get("MINNI_AFM_ADAPTER_ID"))
    verify = probe or verify_afm_generation
    if resolved == "off":
        return {
            "mode": resolved,
            "status": "off",
            "ok": False,
            "generation_verified": False,
            "probe_age_ms": 0,
            "native_available": False,
            "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
            "adapter_configured": adapter_configured,
        }
    if resolved == "bridge":
        return _with_generation_health(
            {
                "mode": resolved,
                "status": "bridge",
                "native_available": False,
                "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
                "adapter_configured": adapter_configured,
            },
            verify(resolved, client=probe_client),
        )
    if not helper_available:
        report = {
            "mode": resolved,
            "status": "native_helper_missing" if resolved == "native" else "fallback_used",
            "native_health": "helper_missing",
            "native_available": False,
            "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
            "adapter_configured": adapter_configured,
        }
        if resolved == "auto":
            # Auto mode can still verify generation through the bridge fallback.
            return _with_generation_health(report, verify(resolved, client=probe_client))
        report.update({"ok": False, "generation_verified": False, "probe_age_ms": 0})
        return report

    health = invoke_native_afm("health", {}, timeout=1.0)
    backend = health.data.get("backend")
    availability = health.data.get("availability")
    native_is_available = bool(health.ok and backend == "apple-foundation-models" and availability == "available")
    native_health = "available" if native_is_available else "foundation_models_unavailable"
    status = "native_available" if native_is_available else ("native_unavailable" if resolved == "native" else "fallback_used")
    report = {
        "mode": resolved,
        "status": status,
        "native_health": native_health,
        "native_available": native_is_available,
        "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
        "adapter_configured": adapter_configured,
    }
    if isinstance(backend, str):
        report["backend"] = backend
    if isinstance(availability, str):
        report["availability"] = availability
    safe_error = _safe_status_error(health.error)
    if safe_error:
        report["error"] = safe_error
    return _with_generation_health(report, verify(resolved, client=probe_client))


def invoke_native_afm(operation: str, payload: Dict[str, Any], timeout: float = 2.0) -> AFMResult:
    helper = native_helper_path()
    if not native_available(helper):
        return AFMResult(
            ok=False,
            data={},
            provider="native",
            status="native_unavailable",
            error="native AFM helper unavailable",
        )

    request = {
        "schema_version": 1,
        "operation": operation,
        "input": payload,
    }
    try:
        completed = subprocess.run(
            [os.fspath(helper)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AFMResult(
            ok=False,
            data={},
            provider="native",
            status="native_unavailable",
            error=str(exc),
        )

    if completed.returncode != 0:
        return AFMResult(
            ok=False,
            data={},
            provider="native",
            status="native_unavailable",
            error="native AFM helper failed",
        )

    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return AFMResult(
            ok=False,
            data={},
            provider="native",
            status="native_unavailable",
            error=f"native AFM helper returned invalid JSON: {exc.msg}",
        )

    if not isinstance(parsed, dict):
        return AFMResult(
            ok=False,
            data={},
            provider="native",
            status="native_unavailable",
            error="native AFM helper returned a non-object response",
        )

    parsed_data = parsed.get("data")
    data = dict(parsed_data) if isinstance(parsed_data, dict) else {}
    for key in ("provider", "backend", "availability", "status"):
        if key in parsed and key not in data:
            data[key] = parsed[key]
    if not data and isinstance(parsed, dict):
        data = {key: value for key, value in parsed.items() if key not in {"ok", "error"}}
    if parsed.get("ok") is False:
        error = parsed.get("error")
        return AFMResult(
            ok=False,
            data=data,
            provider="native",
            status="native_unavailable",
            error=str(error) if error else "native AFM helper rejected request",
        )
    return AFMResult(ok=True, data=data, provider="native", status="native_available")


BridgeClient = Callable[[Dict[str, Any], str, float], Dict[str, Any]]


# R7: cap the AFM bridge response so an unauthenticated/port-squatting loopback
# endpoint cannot stream an unbounded body and exhaust memory. 4 MiB is far above
# any legitimate two-sentence HyDE / chat-completion payload.
_AFM_BRIDGE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _default_bridge_client(payload: Dict[str, Any], url: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local-only AFM bridge
        # Read one byte past the cap so we can detect (and reject) an oversized
        # body instead of silently truncating it into invalid JSON.
        raw = resp.read(_AFM_BRIDGE_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _AFM_BRIDGE_MAX_RESPONSE_BYTES:
            raise ValueError(
                "AFM bridge response exceeded "
                f"{_AFM_BRIDGE_MAX_RESPONSE_BYTES} bytes; refusing to buffer"
            )
        return json.loads(raw.decode("utf-8"))


def afm_chat_completion(
    payload: Dict[str, Any],
    *,
    mode: Optional[str] = None,
    url: Optional[str] = None,
    timeout: float = 2.0,
    client: Optional[BridgeClient] = None,
) -> AFMResult:
    """Run a chat-completion-shaped AFM call through the configured provider.

    Native mode delegates to the local JSON helper. Auto mode prefers native
    when available, then falls back to the existing OpenAI-compatible bridge.
    All failures are returned as data, never raised, so callers can downgrade.
    """
    resolved = resolve_afm_mode(mode)
    if resolved == "off":
        return AFMResult(ok=False, data={}, provider="off", status="off", error="AFM mode is off")

    if resolved in {"native", "auto"}:
        native = invoke_native_afm("chat_completion", {"payload": payload}, timeout=timeout)
        # Cache-poisoning guard: an ok response only counts as a generation
        # proof when it carries actual completion content — the same check the
        # probe applies. A contentless ok is neutral (neither success nor
        # failure); a failed call still invalidates the cached probe (mirror
        # of afm.ts callAfmJson); in auto mode the bridge fall-through below
        # re-verifies on success.
        if native.ok and _native_completion_content(native.data) is not None:
            note_afm_generation_success(url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, resolved)
        elif not native.ok:
            note_afm_generation_failure(url or DEFAULT_AFM_CHAT_COMPLETIONS_URL)
        if native.ok or resolved == "native":
            return native

    # G13 (SEC-004): bridge targets must be loopback or explicitly allowlisted
    # (MINNI_AFM_ALLOWED_TARGETS / MINNI_MODEL_ALLOWED_TARGETS); non-loopback
    # additionally requires HTTPS. Enforced at the transport boundary exactly
    # like afm.ts callAfmJson, so every bridge caller is gated (model_provider
    # keeps its own check as defense in depth). Structured denial, no URL echo.
    from minni.config import check_model_target

    decision = check_model_target(url or DEFAULT_AFM_CHAT_COMPLETIONS_URL)
    if not decision["allowed"]:
        if decision["reason"] == "https_required":
            error = "afm_target_denied: non-loopback model targets require https"
        else:
            error = "afm_target_denied: target is not loopback-only and not explicitly allowlisted by operator config"
        return AFMResult(
            ok=False,
            data={},
            provider="bridge",
            status="target_denied",
            error=error,
            fallback_used=resolved == "auto",
        )

    bridge_client = client or _default_bridge_client
    try:
        data = bridge_client(payload, url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, timeout)
        # Honest health: a successful live call refreshes the cached probe
        # (symmetric counterpart of the failure path) but ONLY when the body
        # carries real completion content. A hollow 2xx ({"status": "ok"} with
        # no choices) proves nothing about generation and must not poison the
        # probe cache.
        if _chat_completion_content(data) is not None:
            note_afm_generation_success(url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, resolved)
        return AFMResult(
            ok=True,
            data=data,
            provider="bridge",
            status="ok",
            fallback_used=resolved == "auto",
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary must downgrade
        # Honest health: failed live calls invalidate the cached generation probe.
        note_afm_generation_failure(url or DEFAULT_AFM_CHAT_COMPLETIONS_URL)
        return AFMResult(
            ok=False,
            data={},
            provider="bridge",
            status="bridge_unavailable",
            error=str(exc),
            fallback_used=resolved == "auto",
        )
