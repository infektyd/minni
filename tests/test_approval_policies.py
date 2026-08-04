"""Tests for the Cursor approval-agent policies and Bugbot rules.

The failure these exist to prevent is not a wrong policy — it is an ABSENT
one that still reads as protection. `.cursor/` was gitignored four times over,
so the first attempt at this change committed three of six files and the
routing map that binds boundaries to policies never left the author's disk.
Cursor discovers policies from repository contents at the PR head, so an
ignored policy file is an inert policy file.

Per cursor.com/docs/approval-agents: discovery is by EXACT basename
(POLICY.md, approval_policy.md and *.bak are ignored), the closest policy wins
with ancestors applying unless they conflict, and policy prompts override
dashboard-level criteria.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROUTING = ROOT / ".cursor" / "approval-policies" / "ROUTING.md"

# Directories whose contents decide what CI executes, what the reviewing agent
# executes, and what the approver believes. Each must be human-only.
TRUST_DIRS = (".github", ".grok", ".cursor")


def _tracked(path: Path) -> bool:
    """True if git tracks `path` — the only thing that reaches the PR head."""
    rel = path.relative_to(ROOT).as_posix()
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", rel],
        capture_output=True, text=True,
    )
    return out.returncode == 0


def _routing() -> list[dict]:
    doc = yaml.safe_load(ROUTING.read_text(encoding="utf-8"))
    assert isinstance(doc, list), "ROUTING.md must be a YAML list of products"
    return doc


def test_routing_file_is_tracked_and_parses():
    assert _tracked(ROUTING), "ROUTING.md is not tracked — Cursor will never see it"
    assert _routing(), "ROUTING.md is empty"


@pytest.mark.parametrize("directory", TRUST_DIRS)
def test_every_trust_directory_has_a_tracked_policy(directory):
    policy = ROOT / directory / "APPROVAL_POLICY.md"
    assert policy.is_file(), f"{directory}/APPROVAL_POLICY.md is missing"
    assert _tracked(policy), (
        f"{directory}/APPROVAL_POLICY.md exists but is not tracked — it will "
        "not reach the PR head, so it protects nothing"
    )


def test_root_policy_is_tracked():
    root = ROOT / "APPROVAL_POLICY.md"
    assert root.is_file() and _tracked(root)


def test_bugbot_rules_are_tracked():
    rules = ROOT / ".cursor" / "BUGBOT.md"
    assert rules.is_file() and _tracked(rules)


def test_every_policy_named_by_routing_exists_and_is_tracked():
    """A pointer to a file that is absent or ignored routes to nothing."""
    for entry in _routing():
        for pointer in entry["policies"]:
            target = ROOT / pointer
            assert target.is_file(), f"{entry['product']} -> missing {pointer}"
            assert _tracked(target), f"{entry['product']} -> untracked {pointer}"


def test_routing_covers_every_trust_directory():
    boundaries = {e["boundary"] for e in _routing()}
    for directory in TRUST_DIRS:
        assert f"{directory}/**" in boundaries, (
            f"{directory}/** has no routing entry, so it falls through to the "
            "permissive repository-default policy"
        )


@pytest.mark.parametrize("directory", TRUST_DIRS)
def test_trust_policies_forbid_auto_approval(directory):
    """The whole point: these must refuse, not merely advise caution."""
    text = (ROOT / directory / "APPROVAL_POLICY.md").read_text(encoding="utf-8")
    assert "NEVER auto-approve" in text
    assert "do not approve" in text
    assert "request human reviewers" in text


def test_every_policy_is_path_denied_by_the_mechanical_gate():
    """The two lists must not drift. A policy file that Cursor obeys but the
    mechanical gate treats as ordinary code is a merge gate a PR can rewrite
    and then be auto-approved under — the approval path and the merge path
    have to agree about what counts as trust surface."""
    spec = importlib.util.spec_from_file_location(
        "grok_approve_gate", ROOT / ".github" / "scripts" / "grok_approve_gate.py"
    )
    gate = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules, so the module has to
    # be registered before it executes.
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)

    paths = [ROOT / "APPROVAL_POLICY.md", ROOT / ".cursor" / "BUGBOT.md", ROUTING]
    paths += [ROOT / d / "APPROVAL_POLICY.md" for d in TRUST_DIRS]
    paths.append(Path(__file__))
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        assert gate.path_denied(rel), f"{rel} is not in PATH_DENY_PREFIXES"


def test_policy_basenames_are_exact():
    """Cursor ignores POLICY.md, approval_policy.md, *.bak and team_* during
    directory policy discovery, so a near-miss name is silently inert."""
    for found in ROOT.rglob("*APPROVAL_POLICY*"):
        if ".git/" in found.as_posix():
            continue
        assert found.name == "APPROVAL_POLICY.md", (
            f"{found} would be ignored by policy discovery"
        )
