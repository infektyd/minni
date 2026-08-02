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


def test_gate_does_not_wake_for_ci_completing_on_main(gate):
    """workflow_run branch filters match the TRIGGERING run's head branch. CI
    completing on main starts a gate run that resolves main's SHA, finds no open
    PR and skips — pure waste, and it got worse when the workflows list was
    widened. PR-branch CI must still fire, since that is the only case that can
    produce a decision."""
    wr = _triggers(gate)["workflow_run"]
    assert wr.get("branches-ignore") == ["main"]
    # A `branches` allowlist would be the wrong shape here: it would silently
    # drop every future PR branch that does not match the pattern.
    assert "branches" not in wr


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
    defang = run.index("/usr/bin/sed -i 's/grok-mechanical-eligibility")
    stamp = run.index('echo "<!-- grok-mechanical-eligibility: APPROVE -->"')
    assert defang < stamp, "defang must run before the workflow stamps its own marker"


def test_leak_gate_steps_pin_path_binaries_and_pipe_isolated(review):
    """#252: after GITHUB_PATH prepends $HOME/.grok/bin, gate/parser steps must
    not resolve git/python3 via PATH, and the leak gate must not re-open a
    /tmp file after hashing (pipe + python3 -I)."""
    grok_yml = (ROOT / ".github" / "workflows" / "grok.yml").read_text(encoding="utf-8")
    review_yml = REVIEW_YML.read_text(encoding="utf-8")
    for name, text in (("grok.yml", grok_yml), ("grok-review.yml", review_yml)):
        assert "/usr/bin/git fetch" in text, f"{name}: git fetch must be path-pinned"
        assert "/usr/bin/git show" in text, f"{name}: git show must be path-pinned"
        assert "/usr/bin/python3 -I -" in text, (
            f"{name}: leak gate must run as isolated stdin script"
        )
        # Unpinned forms that #246 closed on boundary-test and #252 extends here.
        assert "python3 /tmp/leak-gate.py" not in text, (
            f"{name}: must not exec a /tmp gate file via unpinned python3"
        )
    # Parser path in grok-review: pinned interpreter, no PATH `cut`/`sed`,
    # default parser via pipe (no /tmp re-open after git show).
    post = next(s for s in review["jobs"]["grok-review"]["steps"] if s.get("name") == "Post review")
    run = post["run"]
    assert "/usr/bin/python3 -I" in run
    assert "| cut " not in run and " | cut" not in run
    assert "/usr/bin/sed -i" in run, "defang sed must be path-pinned"
    # Bare `sed` (not /usr/bin/sed) must not appear as a command — PATH plant.
    assert not any(
        line.lstrip().startswith("sed ") for line in run.splitlines()
    ), "Post review must not resolve bare sed on PATH"
    assert "PARSER_BYTES=$(/usr/bin/git show" in run or 'PARSER_BYTES=$(/usr/bin/git show' in run
    assert "printf '%s' \"$PARSER_BYTES\" | /usr/bin/python3 -I -" in run
    assert "/tmp/parse_grok_verdict.py" not in run, (
        "default parser must not re-open trusted bytes from /tmp"
    )


def test_app_tokens_are_minted_least_privilege(gate, review):
    """create-github-app-token with no permission-* input mints a token
    carrying EVERY installation permission — including checks:write for the
    reviewer, which is the trust root this whole design rests on."""
    gate_step = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert gate_step["with"].get("permission-checks") == "write"
    assert gate_step["with"].get("permission-administration") == "read"
    # PR write is now required — the gate also submits the mechanical APPROVE
    # and dismisses its own stale ones. Everything else stays unrequested.
    assert set(gate_step["with"]) <= {
        "app-id", "private-key", "owner", "repositories",
        "permission-checks", "permission-administration",
        "permission-pull-requests",
    }, "the gate token must not quietly grow new scopes"

    review_step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert review_step["with"].get("permission-pull-requests") == "write"
    assert "permission-checks" not in review_step["with"], (
        "the reviewer must not be able to post the mechanical check"
    )


# --- /grok-review comment re-trigger ---------------------------------------
# Eligibility is bound to the head SHA, so after agents push fixes the previous
# stamp is worthless. This command is how a review gets re-earned without
# close/reopen or dismissing a review that was never superseded.


def _resolve_step(review: dict) -> dict:
    return next(
        s for s in review["jobs"]["grok-review"]["steps"] if s.get("id") == "resolve"
    )


def test_comment_command_is_a_trigger(review):
    assert _triggers(review)["issue_comment"]["types"] == ["created"]


def test_command_is_restricted_to_collaborators(review):
    """This is a public repo and each review burns metered subscription time,
    so a drive-by commenter must not be able to spend it."""
    cond = review["jobs"]["grok-review"]["if"]
    assert "author_association" in cond
    for role in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert role in cond
    assert "github.event.issue.pull_request" in cond, "must be a PR comment"


def test_command_match_is_exact_not_substring(review):
    """Prose merely mentioning the command must not spend a metered review."""
    run = _resolve_step(review)["run"]
    assert '"$BODY" != "/grok-review"' in run, "needs an exact trimmed comparison"


def test_review_runs_against_the_current_head_sha(review):
    """issue_comment carries no PR head. Reviewing a stale SHA would stamp
    eligibility the gate then rejects for being bound to the wrong commit."""
    assert ".head.sha" in _resolve_step(review)["run"]
    checkout = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ steps.resolve.outputs.sha }}"


def test_repeat_command_is_deduped_by_head_sha(review):
    """Without this a repeated command double-bills for an identical diff."""
    run = _resolve_step(review)["run"]
    assert 'infektydgrokreviewer[bot]' in run
    assert "select(.commit_id==$sha)" in run
    # Piped to jq: `gh api --jq` rejects --arg, which would leave the result
    # empty and silently disable the dedup.
    assert "| jq --arg sha" in run
    # An unparseable result must skip, not proceed.
    assert "''|*[!0-9]*)" in run


def test_draft_and_fork_prs_are_skipped(review):
    run = _resolve_step(review)["run"]
    assert "is a draft" in run
    assert "fork PRs are out of scope" in run


def test_the_command_is_acknowledged(review):
    """A command that silently does nothing is indistinguishable from a broken
    workflow — the requester must learn it was accepted or skipped."""
    ack = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if s.get("name") == "Acknowledge the command"
    )
    run = ack["run"]
    # Accepted -> a reaction on the command comment.
    assert "/reactions" in run and "content=eyes" in run
    # Rejected -> an explicit reply carrying the reason.
    assert "skipped" in run and "steps.resolve.outputs.reason" in run
    # Must report either way, including when Resolve itself failed.
    assert ack["if"].startswith("always()")


def test_app_token_mint_is_gated_on_the_resolver(review):
    """Otherwise a skipped command still mints an installation token."""
    step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert "steps.resolve.outputs.skip" in step["if"]
    assert "vars.GROK_APP_ID" in step["if"]


def test_metered_steps_never_run_on_a_skip(review):
    """The whole point of the guards is that a rejected command costs nothing."""
    steps = review["jobs"]["grok-review"]["steps"]
    for name in ("Run Grok review", "Post review", "Build review prompt"):
        step = next(s for s in steps if s.get("name") == name)
        assert step["if"] == "steps.resolve.outputs.skip == 'false'", name


def _code(run: str) -> str:
    """Shell body with comment lines stripped.

    Absence assertions must not be satisfied or defeated by prose: several of
    these comments legitimately name the very construct being banned.
    """
    return "\n".join(
        ln for ln in run.splitlines() if not ln.lstrip().startswith("#")
    )


def _step(review: dict, name: str) -> dict:
    return next(
        s for s in review["jobs"]["grok-review"]["steps"] if s.get("name") == name
    )


def test_review_is_submitted_bound_to_the_reviewed_sha(review):
    """`gh pr review` has no commit flag and the REST default is "most recent
    commit AT SUBMISSION TIME", so a push landing during the review would bind
    the review — and its eligibility marker — to code the model never saw.
    Pinning the CHECKOUT is not enough; this pins the review RECORD."""
    run = _code(_step(review, "Post review")["run"])
    assert "gh pr review" not in run, "gh pr review cannot set commit_id"
    assert "commit_id: $c" in run
    assert "/reviews" in run and "gh api -X POST" in run


def test_submission_aborts_if_head_moved_during_the_review(review):
    """Belt to the commit_id brace: a review bound to a stale SHA is useless,
    so say so visibly rather than posting one nobody can act on."""
    run = _step(review, "Post review")["run"]
    assert '"$LIVE" != "$SHA"' in run
    assert "refusing to submit" in run


def test_resolve_step_does_not_reference_its_own_outputs(review):
    """The steps context only holds COMPLETED steps. Referencing
    steps.resolve.outputs.* from inside `resolve` expands empty, which under
    `set -euo pipefail` kills the step and silently disables auto-review."""
    resolve = _code(_resolve_step(review)["run"])
    assert "steps.resolve.outputs" not in resolve, (
        "resolve cannot read its own outputs"
    )
    assert "github.event.pull_request.number" in resolve, (
        "the pull_request path must take the PR number from the payload"
    )


def test_dedup_counts_dismissed_reviews_too(review):
    """Agents can dismiss reviews. Keying dedup on non-dismissed ones would let
    dismiss + /grok-review re-roll the model on identical code until it agrees."""
    run = _code(_resolve_step(review)["run"])
    assert 'select(.state!="DISMISSED")' not in run
    assert "select(.commit_id==$sha)" in run


def test_gate_app_token_no_longer_carries_pr_write(gate, review):
    """The mechanical APPROVE moved to the relay USER token, because an App's
    approval does not satisfy required_approving_review_count (measured on
    #243). So the App token drops PR write entirely: it posts the check and
    reads protection, nothing else. The reviewer keeps PR write and still must
    not gain checks:write — separate capabilities, separate tokens."""
    gate_step = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert gate_step["with"].get("permission-checks") == "write"
    assert gate_step["with"].get("permission-administration") == "read"
    assert "permission-pull-requests" not in gate_step["with"]

    review_step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert review_step["with"].get("permission-pull-requests") == "write"
    assert "permission-checks" not in review_step["with"]


def test_relay_approve_token_is_passed_to_the_gate(gate):
    """A user with write access is the only identity whose approval counts."""
    runner = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if s.get("name") == "Run mechanical gate"
    )
    assert runner["env"]["RELAY_APPROVE_TOKEN"] == "${{ secrets.RELAY_APPROVE_TOKEN }}"
    # It must be a distinct secret, not reused from the App token.
    assert runner["env"]["RELAY_APPROVE_TOKEN"] != runner["env"]["APP_TOKEN"]


def test_workflow_github_token_stays_read_only_on_prs(gate):
    """The mechanical approval must come from the App, never GITHUB_TOKEN."""
    assert gate["permissions"]["pull-requests"] == "read"
