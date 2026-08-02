"""D7 (#232): `--all` must mean ONE fleet, shared by wire and propagate.

wire and propagate each expand `all` themselves (propagate ships standalone in
the plugin payload and cannot import the minni package), so each carries a copy
of the canonical fleet. These tests pin the copies equal and require every
fleet member either to be expanded by `all` or to be excluded with an explicit
named reason — never silently absent from both.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from minni.wire import platform as wire_platform

REPO = Path(__file__).resolve().parent.parent


def _load_propagate():
    spec = importlib.util.spec_from_file_location(
        "_minni_propagate",
        REPO / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
        / "propagate.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


propagate = _load_propagate()


def test_canonical_fleet_copies_are_identical():
    assert tuple(wire_platform.CANONICAL_FLEET) == tuple(propagate.CANONICAL_FLEET)


def test_wire_all_accounts_for_every_fleet_member():
    expanded = set(wire_platform.ALL_EXPANSION_V03)
    skipped = set(wire_platform.ALL_SKIPS)
    assert expanded | skipped == set(wire_platform.CANONICAL_FLEET)
    assert not expanded & skipped


def test_propagate_all_accounts_for_every_fleet_member():
    expanded = set(propagate.ALL_PLATFORMS)
    skipped = set(propagate.ALL_SKIPS)
    assert expanded | skipped == set(propagate.CANONICAL_FLEET)
    assert not expanded & skipped


def test_wire_expand_platforms_names_every_exclusion():
    platforms, warnings = wire_platform.expand_platforms("all")
    assert platforms == list(wire_platform.ALL_EXPANSION_V03)
    assert {plat for plat, _reason in warnings} == set(wire_platform.ALL_SKIPS)
    assert all(reason for _plat, reason in warnings)


def test_antigravity_is_inside_at_least_one_all_expansion():
    """The live corroboration behind D7: agy surfaces were stale because
    antigravity sat outside BOTH expansions. It must be executed by at least
    one command's `all`."""
    assert (
        "antigravity" in propagate.ALL_PLATFORMS
        or "antigravity" in wire_platform.ALL_EXPANSION_V03
    )


def test_wire_all_skips_antigravity_does_not_recommend_propagate_platform_all():
    """Round-2 Med: skip text must point at explicit antigravity/cursor or
    make sync-root — not claim that ``propagate --platform all`` rewrites
    wire MCP onto legacy trees (D7: all expands only antigravity+cursor)."""
    reason = wire_platform.ALL_SKIPS["antigravity"]
    assert "--platform antigravity" in reason
    assert "make sync-root" in reason or "--platform cursor" in reason
    # Must not recommend bulk all as a synonym for the full fleet, and must
    # not assert the pre-D7 "rewrites legacy" footgun for --platform all.
    assert "or `propagate.py update-plugin --platform all`" not in reason
    assert "rewrites" not in reason.lower()
    assert "legacy cache" not in reason.lower()