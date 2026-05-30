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


def afm_runtime_status(mode: Optional[str] = None) -> Dict[str, Any]:
    resolved = resolve_afm_mode(mode)
    helper = native_helper_path()
    helper_available = native_available(helper)
    adapter_configured = bool(os.environ.get("MINNI_AFM_ADAPTER_PATH") or os.environ.get("MINNI_AFM_ADAPTER_ID"))
    if resolved == "off":
        return {
            "mode": resolved,
            "status": "off",
            "native_available": False,
            "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
            "adapter_configured": adapter_configured,
        }
    if resolved == "bridge":
        return {
            "mode": resolved,
            "status": "bridge",
            "native_available": False,
            "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
            "adapter_configured": adapter_configured,
        }
    if not helper_available:
        return {
            "mode": resolved,
            "status": "native_helper_missing" if resolved == "native" else "fallback_used",
            "native_health": "helper_missing",
            "native_available": False,
            "native_helper_configured": bool(os.environ.get("MINNI_AFM_NATIVE_HELPER")),
            "adapter_configured": adapter_configured,
        }

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
    return report


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
        return AFMResult(
            ok=False,
            data={},
            provider="bridge",
            status="bridge_unavailable",
            error=str(exc),
            fallback_used=resolved == "auto",
        )
