"""Platform specs and expansion for minni wire."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

PLATFORM_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "kilo": "kilocode",
    "grok-build": "grok",
    "grok_build": "grok",
    "grok_tui": "grok",
    "grok-beta": "grok",
    "grok_beta": "grok",
    "agy": "antigravity",
    "antigravity-cli": "antigravity",
    "antigravity-ide": "antigravity",
    "antigravity_cli": "antigravity",
    "antigravity_ide": "antigravity",
}

VALID_PLATFORMS = frozenset({
    "codex", "claude-code", "kilocode", "gemini", "antigravity",
    "grok", "generic", "all",
})

ALL_EXPANSION_V03 = ("codex", "claude-code", "kilocode", "grok")
GEMINI_SKIP_WARNING = (
    "gemini wiring is provisional; run `minni wire gemini` explicitly to attempt it"
)
GEMINI_PROVISIONAL_REASON = (
    "gemini extension-manifest wiring is not yet implemented (open question 8)"
)

def config_root_candidates() -> dict[str, tuple[Path, ...]]:
    """Ordered config-root candidates for preflight probes (§6.4).

    Computed per call from the current HOME — never at import time — so a
    caller (or sandboxed test) that sets HOME after import probes the right
    home, matching platform_spec()'s late binding.
    """
    home = Path(os.environ.get("HOME") or Path.home())
    return {
        "codex": (home / ".codex",),
        "claude-code": (home,),
        "kilocode": (home / ".config/kilo",),
        "grok": (home / ".grok",),
        "gemini": (home / ".gemini",),
        "antigravity": (home / ".gemini",),
    }

HOOK_ENTRYPOINTS: dict[str, str] = {
    "claude-code": "dist/hook.js",
    "codex": "dist/codex-hook.js",
    "kilocode": "dist/kilocode-hook.js",
    "grok": "dist/grok-hook.js",
    "gemini": "dist/gemini-hook.js",
    "antigravity": "dist/gemini-hook.js",
}


def canonical_platform(platform: str) -> str:
    normalized = platform.strip().lower().replace("_", "-")
    return PLATFORM_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    agent: str
    config_path: Path | None
    config_kind: str
    hook_entry: str | None = None


def expand_platforms(platform: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (platforms_to_wire, warnings) for the given platform arg."""
    platform = canonical_platform(platform)
    if platform == "all":
        return list(ALL_EXPANSION_V03), [("gemini", GEMINI_SKIP_WARNING)]
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"unknown platform {platform!r}; use codex, claude-code, kilocode, "
            "gemini, antigravity, grok, generic, or all",
        )
    return [platform], []


def platform_spec(
    platform: str,
    *,
    install_root: str | None = None,
    agent: str | None = None,
) -> PlatformSpec:
    platform = canonical_platform(platform)
    if agent is not None and not AGENT_ID_RE.match(agent):
        raise ValueError(
            f"invalid --agent {agent!r}; must match {AGENT_ID_RE.pattern}",
        )
    home = Path(os.environ.get("HOME") or Path.home())
    if platform == "generic":
        if not install_root:
            raise ValueError("generic wire requires --install-root")
        if not agent:
            raise ValueError(
                "generic wire requires --agent so it cannot inherit another agent's vault",
            )
        return PlatformSpec(
            platform="generic",
            agent=agent,
            config_path=Path(install_root).expanduser() / ".mcp.json",
            config_kind="mcp-json-only",
        )

    specs: dict[str, PlatformSpec] = {
        "codex": PlatformSpec(
            "codex", "codex", home / ".codex/config.toml", "toml", "dist/codex-hook.js",
        ),
        "claude-code": PlatformSpec(
            "claude-code", "claude-code", home / ".claude.json", "claude-json",
            "dist/hook.js",
        ),
        "kilocode": PlatformSpec(
            "kilocode", "kilocode", home / ".config/kilo/kilo.json", "kilo-json",
            "dist/kilocode-hook.js",
        ),
        "gemini": PlatformSpec(
            "gemini", "gemini", None, "gemini-provisional", "dist/gemini-hook.js",
        ),
        "antigravity": PlatformSpec(
            "antigravity", "gemini", None, "antigravity", "dist/gemini-hook.js",
        ),
        "grok": PlatformSpec(
            "grok", "grok-build", home / ".grok/config.toml", "toml", "dist/grok-hook.js",
        ),
    }
    if platform not in specs:
        raise ValueError(f"unknown platform {platform!r}")
    base = specs[platform]
    resolved_agent = agent or base.agent
    return PlatformSpec(
        platform=base.platform,
        agent=resolved_agent,
        config_path=base.config_path,
        config_kind=base.config_kind,
        hook_entry=base.hook_entry,
    )


def default_config_scan_paths() -> dict[str, Path]:
    """Known per-platform default config locations for GC belt-and-braces scan."""
    return {
        "codex": Path("~/.codex/config.toml").expanduser(),
        "claude-code": Path("~/.claude.json").expanduser(),
        "kilocode": Path("~/.config/kilo/kilo.json").expanduser(),
        "grok": Path("~/.grok/config.toml").expanduser(),
    }


def config_root_exists(platform: str) -> tuple[bool, list[str]]:
    candidates = config_root_candidates().get(canonical_platform(platform), ())
    probed = [str(p) for p in candidates]
    ok = any(p.exists() for p in candidates) if candidates else True
    return ok, probed