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
    text = re.sub(r"/(?:Users|Volumes|private|var|tmp|Library)/[^\s\"')]+", "[local-path]", str(error))
    text = re.sub(r"[^\s\"')]+\.fmadapter\b", "[adapter]", text)
    text = re.sub(r"[^\s\"')]+\.(?:db|sqlite|sqlite3|faiss|index|plist)\b", "[local-artifact]", text)
    return text[:240]


# --- Verified generation health (P1: honest health) -------------------------
#
# Mirrors plugins/minni/src/afm.ts getAfmProviderHealth/ProviderHealth.
# `ok`/`generation_verified` is only true when a real 1-token completion has
# been verified within the TTL — /health reachability alone never counts.

_GENERATION_PROBE_TTL_SECONDS = 300.0
_GENERATION_PROBE_TIMEOUT_SECONDS = 1.5
_generation_probe_cache: Dict[str, Dict[str, Any]] = {}


def reset_afm_generation_probe_cache() -> None:
    _generation_probe_cache.clear()


def note_afm_generation_failure(url: Optional[str] = None) -> None:
    """Invalidate cached generation probes after a live call failure."""
    if url is None:
        _generation_probe_cache.clear()
        return
    for key in list(_generation_probe_cache):
        if key.endswith(f"|{url}"):
            del _generation_probe_cache[key]


def _chat_completion_content(data: Dict[str, Any]) -> Optional[str]:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


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
    if result.provider == "native":
        verified = result.ok
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
    timeout: float = _GENERATION_PROBE_TIMEOUT_SECONDS,
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
    if cached is None or (now() - cached["probed_at"]) >= ttl_seconds:
        cached = _run_generation_probe(resolved, target, timeout, client, now)
        _generation_probe_cache[key] = cached
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


def _default_bridge_client(payload: Dict[str, Any], url: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local-only AFM bridge
        return json.loads(resp.read().decode("utf-8"))


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
        if native.ok or resolved == "native":
            return native

    bridge_client = client or _default_bridge_client
    try:
        data = bridge_client(payload, url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, timeout)
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
