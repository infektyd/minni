"""Workflow-shape invariants for the mechanical gate.

These are properties of the YAML that no unit test on grok_approve_gate.py can
see, and each one corresponds to a defect that minted a false success.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE_YML = ROOT / ".github" / "workflows" / "grok-approve-gate.yml"
REVIEW_YML = ROOT / ".github" / "workflows" / "grok-review.yml"


def _load(path: Path) -> dict:
    # PyYAML parses the `on:` key as the boolean True.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on") or doc[True]


@pytest.fixture(scope="module")
def gate() -> dict:
    return _load(GATE_YML)


@pytest.fixture(scope="module")
def review() -> dict:
    return _load(REVIEW_YML)


def test_concurrency_group_is_keyed_on_head_sha_for_every_trigger(gate):
    """Keying pull_request on PR number and check_suite on SHA put concurrent
    runs in different groups, so they never cancelled each other and a stale
    snapshot could publish success over a veto that had already landed."""
    group = gate["concurrency"]["group"]
    assert "pull_request.number" not in group, (
        "PR-number keying reintroduces the split-group race"
    )
    for expr in ("pull_request.head.sha", "workflow_run.head_sha"):
        assert expr in group, f"{expr} missing from concurrency group: {group}"


def test_ci_completion_uses_workflow_run_not_check_suite(gate):
    """check_suite does NOT trigger a workflow when the suite was created by
    GitHub Actions, and every CI suite here is Actions-created. Measured on this
    branch: 40 runs, 0 check_suite. A check_suite trigger is dead code that
    reads like a working re-evaluation path."""
    triggers = _triggers(gate)
    assert "check_suite" not in triggers
    listed = set(triggers["workflow_run"]["workflows"])
    # Every workflow producing a required context must re-trigger the gate.
    assert {"Public CI", "PR Hygiene", "Claude Code Review"} <= listed
    assert "Grok Code Review" in listed


def test_gate_timeout_covers_the_api_budget(gate):
    """Three observation rounds of ~8 calls at a 15s API timeout is ~360s; at
    timeout-minutes: 5 the job could be killed mid-decision."""
    assert gate["jobs"]["gate"]["timeout-minutes"] >= 10


def test_gate_reevaluates_on_review_submission(gate):
    """Without this the veto is advisory: once a success check run exists for a
    SHA, a later REQUEST_CHANGES fires nothing and the green stays visible."""
    types = _triggers(gate)["pull_request_review"]["types"]
    assert "submitted" in types
    assert "dismissed" in types


def test_review_trigger_resolves_a_head_sha(gate):
    """Adding the trigger without widening the resolver's case statement makes
    the job start and then skip: no SHA, no re-evaluation, veto still advisory."""
    steps = gate["jobs"]["gate"]["steps"]
    resolver = next(s for s in steps if s.get("id") == "meta")
    assert "pull_request|pull_request_review)" in resolver["run"]


def test_check_run_is_posted_under_the_app_not_github_token(gate):
    """A required context is matched by NAME. Posting with GITHUB_TOKEN puts the
    check under the GitHub Actions integration, which ANY same-repo workflow can
    also post as — so protection cannot tell the real gate from a spoof."""
    steps = gate["jobs"]["gate"]["steps"]
    minter = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert minter["with"]["app-id"] == "${{ vars.GROK_APP_ID }}"

    runner = next(s for s in steps if s.get("name") == "Run mechanical gate")
    assert runner["env"]["APP_TOKEN"] == "${{ steps.app-token.outputs.token }}", (
        "the gate must receive the App token, or it can only post red"
    )


def test_gate_loads_its_script_from_the_default_branch_only(gate):
    """A PR-head fallback here would let any PR ship its own approver."""
    steps = gate["jobs"]["gate"]["steps"]
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ steps.meta.outputs.default_branch }}"


def test_review_workflow_defangs_the_marker_before_embedding_the_reply(review):
    """Load-bearing, not belt-and-braces: _has_marker() still accepts a BARE
    marker line, so this sed is the only thing closing that echo case."""
    steps = review["jobs"]["grok-review"]["steps"]
    post = next(s for s in steps if s.get("name") == "Post review")
    run = post["run"]
    defang = run.index("sed -i 's/grok-mechanical-eligibility")
    stamp = run.index('echo "<!-- grok-mechanical-eligibility: APPROVE -->"')
    assert defang < stamp, "defang must run before the workflow stamps its own marker"


def test_app_tokens_are_minted_least_privilege(gate, review):
    """create-github-app-token with no permission-* input mints a token
    carrying EVERY installation permission — including checks:write for the
    reviewer, which is the trust root this whole design rests on."""
    gate_step = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert gate_step["with"].get("permission-checks") == "write"
    assert "permission-pull-requests" not in gate_step["with"], (
        "the gate only posts a check run; it must not carry PR write"
    )

    review_step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert review_step["with"].get("permission-pull-requests") == "write"
    assert "permission-checks" not in review_step["with"], (
        "the reviewer must not be able to post the mechanical check"
    )
