"""Workflow-shape invariants for the Claude agent path.

SEC-G9 (#236): the Claude workflows were added in #214 and never received the
guards the Grok workflows got in #215 — author-association gate, `concurrency`,
`timeout-minutes`. The ASYMMETRY is a direct config read and is not in doubt;
whether the missing author gate was EXPLOITABLE is unverified, so these tests
pin the guards, not an exploit claim.

#240: `Claude Code Review` reported success 12+ times while posting nothing,
billing ~$0.29 a run. It was removed rather than repaired — see
`test_the_review_workflow_that_never_reviewed_is_gone` for why the permission
theory was not acted on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CLAUDE = WORKFLOWS / "claude.yml"
GROK = WORKFLOWS / "grok.yml"

ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job(path: Path) -> dict:
    return next(iter(_load(path)["jobs"].values()))


def test_agent_job_is_bounded():
    """An agent job with no timeout sits at the 6h default while metered.
    Bounded ABOVE too: raising it to 360 is the same defect restated."""
    timeout = _job(CLAUDE).get("timeout-minutes")
    assert timeout, "claude.yml: no timeout-minutes"
    assert 0 < int(timeout) <= 30, f"claude.yml: timeout of {timeout} is not a bound"


def test_agent_workflow_declares_concurrency():
    """Two rapid @claude mentions otherwise run two metered agents over the
    same context. A constant group would serialise every issue in the repo."""
    group = (_load(CLAUDE).get("concurrency") or {}).get("group")
    assert group, "claude.yml: no concurrency group"
    assert "${{" in group and "number" in group, (
        f"concurrency group is not keyed on the issue/PR: {group!r}"
    )


def test_mention_handler_gates_on_author_association():
    """Mirrors grok.yml. The job holds CLAUDE_CODE_OAUTH_TOKEN; without this a
    drive-by commenter spends it."""
    condition = _job(CLAUDE)["if"]
    match = re.search(r"fromJSON\('(\[.*?\])'\)", condition, re.S)
    assert match, "claude.yml: no fromJSON role list in the author gate"
    # The exact SET, not a substring scan: appending "CONTRIBUTOR" or "NONE"
    # opens the gate to anyone while every substring assertion still passes.
    assert set(json.loads(match.group(1))) == ALLOWED_ASSOCIATIONS
    assert "author_association" in condition


def test_the_author_gate_is_ANDed_with_the_mention_match():
    """Structural, not `"&&" in condition` — `&&` already appears inside every
    inner clause, so a substring check cannot tell AND from OR. Flipping the
    join to `||` disables the gate entirely while reading almost identically."""
    condition = _job(CLAUDE)["if"]
    start = condition.index("contains(fromJSON(")
    # Walk to the matching paren that closes the author-gate `contains(...)`,
    # then read the operator that joins it to the rest.
    depth, i = 0, start + len("contains")
    while i < len(condition):
        if condition[i] == "(":
            depth += 1
        elif condition[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    join = condition[i + 1:].lstrip()
    assert join.startswith("&&"), (
        f"author gate is not ANDed with the trigger match; joined by: "
        f"{join[:20]!r}"
    )
    assert "@claude" in condition, "claude.yml: mention match lost"


def test_author_gate_matches_the_grok_counterpart():
    """Divergence between the two agent paths is the bug #236 filed."""
    grok = _load(GROK)["jobs"]["decide"]["if"]
    grok_roles = set(json.loads(re.search(r"fromJSON\('(\[.*?\])'\)", grok, re.S).group(1)))
    assert grok_roles == ALLOWED_ASSOCIATIONS


def test_no_dead_trigger_that_the_gate_can_never_admit():
    """`issues: assigned` reads the ISSUE AUTHOR's association, never the
    assigner's, so with the gate it could only fire for collaborator-authored
    issues — which `opened` already covers. A trigger that can never do
    anything reads as a working path."""
    triggers = _load(CLAUDE).get("on") or _load(CLAUDE)[True]
    assert "assigned" not in (triggers["issues"]["types"] or [])


def test_the_review_workflow_that_never_reviewed_is_gone():
    """#240 offers two endings: make it post, or remove it. Removal was chosen.

    The permission theory (`pull-requests: read` cannot post) was never
    confirmed, and two measurements argue against acting on it: `claude[bot]`
    has posted ZERO comments repo-wide, and claude.yml carries the SAME
    `pull-requests: read` — so `read` does not distinguish the two. Granting
    write on an unconfirmed cause would have handed an agent that reads
    PR-authored content the ability to publish anything it read, dismiss a
    blocking review, and retarget the PR base — on the one agent path with no
    leak gate, which grok.yml has and this never did.
    """
    assert not (WORKFLOWS / "claude-code-review.yml").exists(), (
        "the review workflow is back; if it is revived it needs the fail-closed "
        "leak gate ported first, not just a permission change"
    )


def test_the_gate_does_not_wait_on_a_workflow_that_no_longer_exists():
    """grok-approve-gate re-evaluates on workflow_run completions. Naming a
    deleted workflow there is stale config that reads as coverage."""
    gate = _load(WORKFLOWS / "grok-approve-gate.yml")
    listed = set((gate.get("on") or gate[True])["workflow_run"]["workflows"])
    existing = {_load(p).get("name") for p in WORKFLOWS.glob("*.yml")}
    assert listed <= existing, f"gate waits on missing workflows: {listed - existing}"


def test_no_commented_out_guard_masquerading_as_one():
    """A commented-out condition with no explanation reads as a guard that
    exists. #236 asks for a real gate or a stated reason — never this."""
    for line in CLAUDE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and re.match(r"#\s*if:", stripped):
            raise AssertionError(f"claude.yml: commented-out condition: {stripped}")


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")))
def test_every_workflow_still_parses(path):
    assert _load(path)["jobs"], f"{path.name}: no jobs"
