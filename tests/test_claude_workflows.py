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
    assert re.search(r"github\.event\.(issue|pull_request)\.number", group), (
        f"concurrency group is not keyed on the issue/PR: {group!r}"
    )
    # `run_number`/`run_id` contain the substring "number" while giving every
    # run its own group — zero serialisation, the defect this pins.
    assert "run_number" not in group and "run_id" not in group


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


# --- a small GitHub-expressions evaluator ----------------------------------
# The author gate was previously checked STRUCTURALLY (paren-walk to prove the
# gate was ANDed with the mention match). That test was inverted in value: it
# rejected a semantically identical reorder of a commutative `&&`, and it
# accepted four real bypasses — `...gate) && true || (mentions)` among them,
# where `&&` binds tighter and the gate stops applying to anything.
#
# So evaluate the condition instead of parsing its shape. GitHub's semantics
# that matter here: `||` yields the first truthy operand (not a boolean),
# falsy is {false, 0, -0, "", null}, and property access on a null object
# yields null rather than erroring.

_FALSY = (False, 0, "", None)


def _truthy(value: object) -> bool:
    return value not in _FALSY


class _Expr:
    """Recursive-descent evaluator for the subset used in these `if:` gates."""

    def __init__(self, text: str, context: dict):
        self.tokens = re.findall(
            r"\(|\)|,|\|\||&&|==|!=|'[^']*'|[A-Za-z_][\w.\[\]]*", text
        )
        self.pos, self.context = 0, context

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def parse(self):
        value = self._or()
        assert self.peek() is None, f"trailing tokens at {self.peek()!r}"
        return value

    def _or(self):
        value = self._and()
        while self.peek() == "||":
            self.take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self):
        value = self._cmp()
        while self.peek() == "&&":
            self.take()
            right = self._cmp()
            value = right if _truthy(value) else value
        return value

    def _cmp(self):
        left = self._atom()
        while self.peek() in ("==", "!="):
            op = self.take()
            right = self._atom()
            equal = (left or "") == (right or "")
            left = equal if op == "==" else not equal
        return left

    def _atom(self):
        token = self.take()
        if token == "(":
            value = self._or()
            assert self.take() == ")"
            return value
        if token == "contains":
            assert self.take() == "("
            haystack = self._or()
            assert self.take() == ","
            needle = self._or()
            assert self.take() == ")"
            return needle in (haystack or [])
        if token == "fromJSON":
            assert self.take() == "("
            value = self._or()
            assert self.take() == ")"
            return json.loads(value)
        if token.startswith("'"):
            return token[1:-1]
        if token in ("true", "false"):
            return token == "true"
        if token == "null":
            return None
        node = self.context
        for part in token.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node


def _gate(event_name: str, association: str | None, mention: bool) -> bool:
    """Evaluate claude.yml's job condition for one concrete event."""
    body = "please @claude take a look" if mention else "unrelated text"
    event: dict = {}
    if event_name in ("issue_comment", "pull_request_review_comment"):
        event = {"comment": {"body": body, "author_association": association},
                 "issue": {"body": "", "title": "", "author_association": "NONE"}}
    elif event_name == "pull_request_review":
        event = {"review": {"body": body, "author_association": association}}
    elif event_name == "issues":
        event = {"issue": {"body": body, "title": "",
                           "author_association": association}}
    context = {"github": {"event_name": event_name, "event": event}}
    return _truthy(_Expr(_job(CLAUDE)["if"], context).parse())


TRIGGERS = ("issue_comment", "pull_request_review_comment",
            "pull_request_review", "issues")
OUTSIDERS = ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR")


@pytest.mark.parametrize("event", TRIGGERS)
@pytest.mark.parametrize("association", sorted(OUTSIDERS))
def test_outsiders_cannot_spend_the_metered_agent(event, association):
    """The job holds CLAUDE_CODE_OAUTH_TOKEN. Mention or not, an outsider must
    never start it — including an outsider commenting on an OWNER-authored
    issue, where a naive `||` chain would fall through to the wrong principal."""
    assert not _gate(event, association, mention=True)
    assert not _gate(event, association, mention=False)


@pytest.mark.parametrize("event", TRIGGERS)
@pytest.mark.parametrize("association", sorted(ALLOWED_ASSOCIATIONS))
def test_collaborators_can_still_reach_the_agent(event, association):
    """The gate must not be so tight that it disables the handler: narrowing it
    to one event's field silently kills the others for everyone."""
    assert _gate(event, association, mention=True)


@pytest.mark.parametrize("event", TRIGGERS)
def test_a_mention_is_still_required(event):
    """Without this, every collaborator comment spends a metered agent."""
    assert not _gate(event, "OWNER", mention=False)


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

    The permission theory (`pull-requests: read` cannot post) is REFUTED, not
    merely unconfirmed. My first attempt "measured" it with
    `commenter:claude`, which is a real unrelated HUMAN user and returns 0.
    The correct query returns 1:

        commenter:claude       -> 0     (a human account, not the bot)
        commenter:claude[bot]  -> 1     (PR #216, run 30700780377)

    That run is claude.yml, which carries `pull-requests: read` and posted a
    public comment anyway — claude-code-action posts through its own App
    installation, so job permissions never governed the publish. Granting
    write would have bought nothing while handing an agent that reads
    PR-authored content the ability to publish what it read, dismiss a blocking
    review, and retarget the PR base.

    So the true cause of the silent successes is still UNIDENTIFIED — most
    likely the plugin/slash-command path (`plugin_marketplaces`, `plugins`,
    `prompt: /code-review:...`), which is why the workflow was removed rather
    than "fixed": a repair aimed at the wrong cause would have re-shipped the
    same green-for-nothing check.
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
    existing = {
        _load(f).get("name") or f.name
        for pattern in ("*.yml", "*.yaml") for f in WORKFLOWS.glob(pattern)
    }
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


def test_the_operator_runbook_cannot_require_a_check_nothing_produces():
    """docs/ops/grok-reviewer-app.md carries branch-protection payloads meant
    to be run VERBATIM, and the doc itself notes that `checks` REPLACES the
    whole list. It listed `claude-review`, produced by the job in the workflow
    #240 removed — so following the runbook after that deletion would have
    required a check that can never report again, blocking every merge to main
    with no failure message. A landmine created by the deletion, not present
    before it."""
    runbook = (ROOT / "docs" / "ops" / "grok-reviewer-app.md").read_text(encoding="utf-8")
    contexts = set(re.findall(r'"context":\s*"([^"]+)"', runbook))
    assert contexts, "no protection payloads found — has the runbook moved?"

    produced = set()
    for pattern in ("*.yml", "*.yaml"):
        for path in WORKFLOWS.glob(pattern):
            for name, job in (_load(path).get("jobs") or {}).items():
                produced.add(job.get("name") or name)
    # Checks posted by an App rather than a job, named in the runbook's own prose.
    produced |= {"grok-mechanical-approve"}

    missing = contexts - produced
    assert not missing, (
        f"runbook would require checks nothing produces: {sorted(missing)}"
    )
