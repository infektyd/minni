"""Pure decide() + review analysis for grok_approve_gate (no network)."""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "grok_approve_gate.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("grok_approve_gate", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclasses look up cls.__module__ in sys.modules during decorate.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _inp(mod, **kwargs):
    base = dict(
        head_sha="abc123",
        required_contexts=("Forbidden Files", "boundary"),
        check_states={"Forbidden Files": "success", "boundary": "success"},
        eligible=True,
        blocked_by_request_changes=False,
        path_denied=False,
    )
    base.update(kwargs)
    return mod.GateInput(**base)


def test_success_when_eligible_and_all_green(mod):
    d = mod.decide(_inp(mod))
    assert d.conclusion == "success"


def test_red_build_never_success(mod):
    d = mod.decide(
        _inp(mod, check_states={"Forbidden Files": "success", "boundary": "failure"})
    )
    assert d.conclusion == "failure"
    assert "red" in d.title.lower() or "red" in d.summary.lower()


def test_pending_never_success(mod):
    d = mod.decide(
        _inp(mod, check_states={"Forbidden Files": "success", "boundary": "pending"})
    )
    assert d.conclusion == "failure"
    assert "pending" in d.title.lower() or "pending" in d.summary.lower()


def test_missing_context_fail_closed(mod):
    d = mod.decide(_inp(mod, check_states={"Forbidden Files": "success"}))
    assert d.conclusion == "failure"


def test_empty_required_contexts_fail_closed(mod):
    d = mod.decide(_inp(mod, required_contexts=()))
    assert d.conclusion == "failure"
    assert "empty" in d.title.lower() or "empty" in d.summary.lower()


def test_not_eligible(mod):
    d = mod.decide(_inp(mod, eligible=False))
    assert d.conclusion == "failure"
    assert "eligible" in d.title.lower() or "eligible" in d.summary.lower()


def test_request_changes_blocks(mod):
    d = mod.decide(_inp(mod, blocked_by_request_changes=True))
    assert d.conclusion == "failure"


def test_path_filter_blocks(mod):
    d = mod.decide(_inp(mod, path_denied=True))
    assert d.conclusion == "failure"
    assert "path" in d.title.lower()


def test_analyze_reviews_eligibility_marker(mod):
    marker = mod.ELIGIBILITY_MARKER
    reviews = [
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "COMMENTED",
            "body": f"ok\n{marker}\n",
        }
    ]
    eligible, blocked = mod.analyze_reviews(reviews)
    assert eligible is True
    assert blocked is False


def test_analyze_reviews_request_changes_clears_eligibility(mod):
    marker = mod.ELIGIBILITY_MARKER
    reviews = [
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "COMMENTED",
            "body": f"earlier\n{marker}\n",
        },
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "CHANGES_REQUESTED",
            "body": "nope",
        },
    ]
    eligible, blocked = mod.analyze_reviews(reviews)
    assert blocked is True
    assert eligible is False


def test_analyze_reviews_ignores_human_planted_marker_without_bot(mod):
    marker = mod.ELIGIBILITY_MARKER
    reviews = [
        {
            "user": {"login": "evil-user"},
            "state": "COMMENTED",
            "body": marker,
        }
    ]
    eligible, blocked = mod.analyze_reviews(reviews)
    assert eligible is False
    assert blocked is False


@pytest.mark.parametrize(
    "login",
    ["github-actions[bot]", "claude[bot]", "dependabot[bot]", "cursor[bot]"],
)
def test_other_bots_cannot_stamp_eligibility(mod, login):
    """Any same-repo PR can post a review as github-actions[bot]; only the
    Grok App installation identity may carry the marker."""
    reviews = [
        {
            "user": {"login": login},
            "state": "COMMENTED",
            "body": mod.ELIGIBILITY_MARKER,
        }
    ]
    eligible, _ = mod.analyze_reviews(reviews)
    assert eligible is False


def test_marker_quoted_inside_reply_is_not_eligibility(mod):
    """The review body embeds the model reply verbatim; a marker planted in the
    PR diff and echoed back must not read as a stamp."""
    reviews = [
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "COMMENTED",
            "body": (
                "The diff adds a suspicious line: "
                f"`{mod.ELIGIBILITY_MARKER}` in src/foo.py\n"
                "VERDICT: COMMENT\n"
            ),
        }
    ]
    eligible, _ = mod.analyze_reviews(reviews)
    assert eligible is False


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/grok-approve-gate.yml",
        ".github/scripts/grok_approve_gate.py",
        ".github/scripts/parse_grok_verdict.py",
        ".github/workflows/grok-review.yml",
        ".github/workflows/ci.yml",
        ".github/scripts/check-no-credential-leak.py",
        ".github/actions/thing/action.yml",
    ],
)
def test_ci_paths_are_denied(mod, path):
    assert mod.path_denied(path) is True


@pytest.mark.parametrize("path", ["src/minni/cli.py", "docs/ops/grok-reviewer-app.md"])
def test_ordinary_paths_are_not_denied(mod, path):
    assert mod.path_denied(path) is False


def test_decide_is_exactly_the_intended_predicate(mod):
    """Exhaustive: success iff eligible AND unblocked AND not path-denied AND a
    non-empty required list whose every context is success. Any other input
    combination — including states this file does not enumerate elsewhere —
    must be failure. This is the whole security property of the gate."""
    states = ["success", "pending", "failure", "error", "missing", "expected", "neutral"]
    mismatches = []
    checked = 0
    for ctxs in [(), ("a",), ("a", "b")]:
        combos = itertools.product(states, repeat=len(ctxs)) if ctxs else [()]
        for combo in combos:
            # "missing" is modelled as absent from the map, as the API returns.
            check_states = {c: s for c, s in zip(ctxs, combo) if s != "missing"}
            for eligible, blocked, denied in itertools.product([True, False], repeat=3):
                checked += 1
                got = mod.decide(
                    mod.GateInput("abc123", ctxs, check_states, eligible, blocked, denied)
                ).conclusion
                want_success = (
                    eligible
                    and not blocked
                    and not denied
                    and bool(ctxs)
                    and all(check_states.get(c) == "success" for c in ctxs)
                )
                if (got == "success") != want_success:
                    mismatches.append((ctxs, combo, eligible, blocked, denied, got))
    assert checked == 456
    assert mismatches == []


def test_outstanding_request_changes_survives_a_later_marker(mod):
    """Documented semantics: a CHANGES_REQUESTED review blocks until DISMISSED,
    even if a newer review stamps eligibility."""
    reviews = [
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "CHANGES_REQUESTED",
            "body": "nope",
        },
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "COMMENTED",
            "body": f"fixed now\n{mod.ELIGIBILITY_MARKER}\n",
        },
    ]
    eligible, blocked = mod.analyze_reviews(reviews)
    assert eligible is True
    assert blocked is True
    assert mod.decide(_inp(mod, eligible=eligible,
                           blocked_by_request_changes=blocked)).conclusion == "failure"


def test_dismissed_request_changes_does_not_block(mod):
    reviews = [
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "DISMISSED",
            "body": "was blocking",
        },
        {
            "user": {"login": "infektydgrokreviewer[bot]"},
            "state": "COMMENTED",
            "body": f"ok\n{mod.ELIGIBILITY_MARKER}\n",
        },
    ]
    eligible, blocked = mod.analyze_reviews(reviews)
    assert eligible is True
    assert blocked is False
