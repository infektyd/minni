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
    "grok", "cursor", "generic", "all",
})

# D7 (#232): ONE canonical fleet, shared with propagate.py (which carries its
# own copy — it ships standalone inside the plugin payload and cannot import
# this package); tests/test_all_fleet_parity.py pins the two copies equal so
# the commands can never silently disagree about what "all" means. Each
# command expands `all` to the members it owns and names every excluded
# member explicitly in its output.
CANONICAL_FLEET = (
    "codex", "claude-code", "kilocode", "gemini", "antigravity", "grok", "cursor",
)

ALL_EXPANSION_V03 = ("codex", "claude-code", "kilocode", "grok")
GEMINI_SKIP_WARNING = (
    "gemini wiring is provisional; run `minni wire gemini` explicitly to attempt it"
)
GEMINI_PROVISIONAL_REASON = (
    "gemini extension-manifest wiring is not yet implemented (open question 8)"
)
# Every CANONICAL_FLEET member absent from ALL_EXPANSION_V03 must appear here
# with the reason, so `wire all` accounts for the whole fleet in its output.
ALL_SKIPS = {
    "gemini": GEMINI_SKIP_WARNING,
    "antigravity": (
        "run `minni wire antigravity` or `propagate.py update-plugin "
        "--platform antigravity` (or `--platform cursor`) explicitly — "
        "shared ~/.gemini tree; excluded from `wire all` so bulk wire and "
        "propagate do not fight. Prefer `make sync-root` for the full D7 "
        "fleet (wire-primary hosts + antigravity/cursor)"
    ),
    "cursor": (
        "propagate-managed: run `propagate.py update-plugin --platform cursor`"
    ),
}

def config_root_candidates(home: Path | None = None) -> dict[str, tuple[Path, ...]]:
    """Ordered config-root candidates for preflight probes (§6.4).

    Computed per call from ``home`` (or ambient HOME) — never at import time —
    so sandboxed tests / MINNI_CHECK_*_HOME snapshots probe the right tree.
    """
    base = Path(home) if home is not None else Path(
        os.environ.get("HOME") or Path.home()
    )
    return {
        "codex": (base / ".codex",),
        "claude-code": (base,),
        "kilocode": (base / ".config/kilo",),
        "grok": (base / ".grok",),
        "gemini": (base / ".gemini",),
        "antigravity": (base / ".gemini",),
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
        return list(ALL_EXPANSION_V03), list(ALL_SKIPS.items())
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"unknown platform {platform!r}; use codex, claude-code, kilocode, "
            "gemini, antigravity, grok, cursor, generic, or all",
        )
    # cursor is fleet-known but propagate-managed only — same skip reason as
    # `wire all`, not "unknown platform". gemini/antigravity stay wireable.
    if platform == "cursor":
        return [], [(platform, ALL_SKIPS["cursor"])]
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
    """Known per-platform default config locations for GC belt-and-braces scan.

    `claude-code-plugins` is the plugin registry, not an MCP config: it is what
    Claude Code reads hooks/skills/commands from. It must be scanned or GC can
    collect the tree a live registration points at — wired.json alone would miss
    a registration written out of band, or one left behind by a wire run that
    failed verification before it recorded anything.

    `claude-desktop` is a separate product with its own config tree, and wire
    never writes it outside the one-time adoption cutover. It is scanned for the
    same reason: it holds a literal versioned path into the tree wire manages,
    and GC retaining that version is what keeps Desktop launchable.
    """
    return {
        "codex": Path("~/.codex/config.toml").expanduser(),
        "claude-code": Path("~/.claude.json").expanduser(),
        "claude-code-plugins": Path(
            "~/.claude/plugins/installed_plugins.json",
        ).expanduser(),
        "claude-desktop": Path(
            "~/Library/Application Support/Claude/claude_desktop_config.json",
        ).expanduser(),
        "kilocode": Path("~/.config/kilo/kilo.json").expanduser(),
        "grok": Path("~/.grok/config.toml").expanduser(),
        # Gemini/antigravity MCP views hold versioned install paths. D11
        # hard-fail can leave views rewritten without a wired.json row;
        # scan them so GC cannot orphan a live MCP pointer.
        "gemini-mcp": Path("~/.gemini/config/mcp_config.json").expanduser(),
        "antigravity-mcp": Path(
            "~/.gemini/antigravity/mcp_config.json",
        ).expanduser(),
        "antigravity-ide-mcp": Path(
            "~/.gemini/antigravity-ide/mcp_config.json",
        ).expanduser(),
        "antigravity-cli-mcp": Path(
            "~/.gemini/antigravity-cli/plugins/minni/mcp_config.json",
        ).expanduser(),
    }


def config_root_exists(
    platform: str, home: Path | None = None,
) -> tuple[bool, list[str]]:
    candidates = config_root_candidates(home).get(
        canonical_platform(platform), (),
    )
    probed = [str(p) for p in candidates]
    ok = any(p.exists() for p in candidates) if candidates else True
    return ok, probed

