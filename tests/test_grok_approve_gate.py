"""Pure decide() + review analysis for grok_approve_gate (no network)."""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

# Any head sha; analyze_reviews now REQUIRES it and binds unconditionally.
HEAD_SHA = "deadbeefcafe"

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
            "commit_id": HEAD_SHA,
            "body": f"ok\n{marker}\n",
        }
    ]
    eligible, blocked = mod.analyze_reviews(reviews, HEAD_SHA)
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
    eligible, blocked = mod.analyze_reviews(reviews, HEAD_SHA)
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
    eligible, blocked = mod.analyze_reviews(reviews, HEAD_SHA)
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
    eligible, _ = mod.analyze_reviews(reviews, HEAD_SHA)
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
    eligible, _ = mod.analyze_reviews(reviews, HEAD_SHA)
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
            "commit_id": HEAD_SHA,
            "body": f"fixed now\n{mod.ELIGIBILITY_MARKER}\n",
        },
    ]
    eligible, blocked = mod.analyze_reviews(reviews, HEAD_SHA)
    assert eligible is True
    assert blocked is True
    assert mod.decide(_inp(mod, eligible=eligible,
                           blocked_by_request_changes=blocked)).conclusion == "failure"


def _rev(mod, state="COMMENTED", marker=True, sha="abc123",
         login="infektydgrokreviewer[bot]"):
    return {
        "user": {"login": login},
        "state": state,
        "commit_id": sha,
        "body": f"text\n{mod.ELIGIBILITY_MARKER}\n" if marker else "text only",
    }


def test_newer_unmarked_app_review_revokes_an_older_marker(mod):
    """Newest App review wins. Falling through to an older stamped review makes
    a later 'I no longer approve' COMMENT a no-op."""
    reviews = [_rev(mod, marker=True), _rev(mod, marker=False)]
    eligible, blocked = mod.analyze_reviews(reviews, "abc123")
    assert eligible is False
    assert blocked is False


def test_eligibility_does_not_survive_a_push(mod):
    """Approve clean code at sha1, push bad code as sha2: the stamp from sha1
    must not grant merge trust for sha2."""
    reviews = [_rev(mod, marker=True, sha="sha1aaa")]
    assert mod.analyze_reviews(reviews, "sha1aaa")[0] is True
    assert mod.analyze_reviews(reviews, "sha2bbb")[0] is False


def test_marker_without_commit_id_is_not_eligible(mod):
    reviews = [{
        "user": {"login": "infektydgrokreviewer[bot]"},
        "state": "COMMENTED",
        "body": mod.ELIGIBILITY_MARKER,
    }]
    assert mod.analyze_reviews(reviews, "abc123")[0] is False


def test_pending_rerun_never_masked_by_an_older_success(mod):
    """A completed success plus an in-flight re-run of the same name is pending,
    not green — otherwise the gate greens a check that is still running."""
    assert mod._worse("success", "pending") == "pending"
    assert mod._worse("pending", "success") == "pending"
    assert mod._worse("success", "failure") == "failure"
    assert mod._worse("pending", "failure") == "failure"
    assert mod._worse("success", "success") == "success"


@pytest.mark.parametrize(
    "run,expected",
    [
        ({"status": "in_progress"}, "pending"),
        ({"status": "queued"}, "pending"),
        ({"status": "completed", "conclusion": "success"}, "success"),
        ({"status": "completed", "conclusion": "failure"}, "failure"),
        ({"status": "completed", "conclusion": "timed_out"}, "failure"),
        ({"status": "completed", "conclusion": "cancelled"}, "failure"),
        ({"status": "completed", "conclusion": "action_required"}, "failure"),
        ({"status": "completed", "conclusion": "neutral"}, "failure"),
        ({"status": "completed", "conclusion": "skipped"}, "failure"),
        ({"status": "completed", "conclusion": None}, "failure"),
    ],
)
def test_check_run_state_mapping(mod, run, expected):
    assert mod._check_run_state(run) == expected


def test_missing_app_token_is_a_hard_failure_not_a_github_token_post(mod, monkeypatch):
    """Falling back to GITHUB_TOKEN is worse than not posting: an app-bound
    context ignores that check, so it can neither grant nor revoke, and the
    check's identity would depend on whether a secret happened to be set."""
    posted = []
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "_gather_and_decide",
        lambda *a, **k: mod.GateDecision("success", "ok", "all green"),
    )
    monkeypatch.setattr(mod, "post_check_run", lambda *a, **k: posted.append(a[2]))

    with pytest.raises(mod.MissingAppToken):
        mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="ghs_actions",
                     base_branch="main", post_token="")
    assert posted == [], "nothing may be posted without the App token"

    mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="ghs_actions",
                 base_branch="main", post_token="app_tok")
    assert posted == ["app_tok"], "must post under the App token only"


def test_green_is_never_published_then_retracted(mod, monkeypatch):
    """Publish-then-retract is inherently racy: if the retraction POST fails or
    the job is killed first, the green stands and the merge channel never learns
    otherwise. Success must only be posted after every observation agrees."""
    seen = iter([
        mod.GateDecision("success", "ok", "green"),
        mod.GateDecision("success", "ok", "green"),
        mod.GateDecision("failure", "REQUEST_CHANGES outstanding", "vetoed"),
    ])
    posted = []
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_gather_and_decide", lambda *a, **k: next(seen))
    monkeypatch.setattr(
        mod, "post_check_run", lambda o, r, t, s_, d: posted.append(d.conclusion)
    )
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "failure"
    assert posted == ["failure"], "a green must never be published at all here"


def test_unreadable_protection_posts_a_reason_not_a_stack_trace(mod, monkeypatch):
    """Reading protection needs Administration:read. If that 403s and the job
    just dies, nothing is posted — and once the context is required, every PR
    wedges with no visible explanation."""
    posted = []
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)

    def boom(*a, **k):
        raise mod.ProtectionUnreadable("needs Administration:read")

    monkeypatch.setattr(mod, "_gather_and_decide", boom)
    monkeypatch.setattr(
        mod, "post_check_run", lambda o, r, t, s_, d: posted.append((d.conclusion, d.title))
    )
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "failure"
    assert d.title == "cannot read protection"
    assert posted == [("failure", "cannot read protection")]


def test_protection_403_is_not_read_as_no_requirements(mod, monkeypatch):
    """A 403 means the required set is UNKNOWN. Treating it like a 404 (empty)
    is the difference between fail-closed and vacuously green."""
    def api(method, url, token, body=None):
        raise RuntimeError(f"GitHub API GET {url} -> 403: Resource not accessible")
    monkeypatch.setattr(mod, "_api", api)
    with pytest.raises(mod.ProtectionUnreadable):
        mod.fetch_protection("o", "r", "main", "t")


def test_protection_404_means_no_protection_configured(mod, monkeypatch):
    def api(method, url, token, body=None):
        raise RuntimeError(f"GitHub API GET {url} -> 404: Branch not protected")
    monkeypatch.setattr(mod, "_api", api)
    assert mod.fetch_protection("o", "r", "main", "t") == ((), {})


def test_renaming_a_tripwire_out_from_under_the_deny_is_caught(mod, monkeypatch):
    """A rename reports the NEW path in `filename`; the OLD path appears only in
    `previous_filename`. Checking filename alone lets a PR move a tripwire to an
    allowed path, then edit it freely in a follow-up PR."""
    monkeypatch.setattr(
        mod, "_paginate",
        lambda *a, **k: [{
            "filename": "tests/renamed_harmlessly.py",
            "previous_filename": "tests/test_grok_approve_gate_workflow.py",
            "status": "renamed",
        }],
    )
    assert mod.fetch_pr_files_denied("o", "r", 1, "t") is True


def test_truncated_file_listing_fails_closed(mod, monkeypatch):
    """The files listing caps at 3000; a PR past the cap could hide a denied
    path in the part we never see."""
    monkeypatch.setattr(
        mod, "_paginate", lambda *a, **k: [{"filename": "src/a.py"}] * 3000
    )
    assert mod.fetch_pr_files_denied("o", "r", 1, "t") is True


def test_a_veto_landing_mid_gather_wins_over_a_stale_success(mod, monkeypatch):
    """Two concurrent gate runs: the second observation must win when it is not
    success, so a REQUEST_CHANGES cannot be overwritten by a stale snapshot."""
    seen = iter([
        mod.GateDecision("success", "ok", "all green"),
        mod.GateDecision("failure", "REQUEST_CHANGES outstanding", "vetoed"),
    ])
    posted = {}
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_gather_and_decide", lambda *a, **k: next(seen))
    monkeypatch.setattr(
        mod, "post_check_run",
        lambda o, r, t, s, d: posted.update(conclusion=d.conclusion),
    )
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "failure"
    assert posted["conclusion"] == "failure"


@pytest.mark.parametrize(
    "early",
    [("head moved", "Abort"), ("draft", "Draft"), ("fork", "Fork")],
)
def test_early_exits_still_post_a_terminal_conclusion(mod, monkeypatch, early):
    """An early return that posts nothing leaves a previous run's green in
    place on that SHA."""
    posted = {}
    monkeypatch.setattr(
        mod, "_preflight",
        lambda *a, **k: mod.GateDecision("failure", early[0], early[1]),
    )
    monkeypatch.setattr(
        mod, "post_check_run",
        lambda o, r, t, s, d: posted.update(conclusion=d.conclusion, title=d.title),
    )
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "failure"
    assert posted["title"] == early[0]


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
            "commit_id": HEAD_SHA,
            "body": f"ok\n{mod.ELIGIBILITY_MARKER}\n",
        },
    ]
    eligible, blocked = mod.analyze_reviews(reviews, HEAD_SHA)
    assert eligible is True
    assert blocked is False


def _runs_payload(name: str, app_id: int, conclusion: str = "success") -> dict:
    return {
        "check_runs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "app": {"id": app_id},
            }
        ]
    }


def test_bound_context_rejects_a_check_from_the_wrong_integration(mod, monkeypatch):
    """A same-repo workflow can mint `claude-review` success under the GitHub
    Actions app. Where protection binds that context to an integration, the
    gate must not be more permissive than protection is."""
    monkeypatch.setattr(mod, "_api", lambda *a, **k: {"statuses": []})
    monkeypatch.setattr(
        mod, "_paginate", lambda *a, **k: [_runs_payload("claude-review", 15368)]
    )
    # Unbound: the Actions-minted run counts.
    assert mod.fetch_combined_statuses("o", "r", "sha", "t") == {
        "claude-review": "success"
    }
    # Bound to a different app: must not read as success.
    bound = mod.fetch_combined_statuses(
        "o", "r", "sha", "t", {"claude-review": 4456296}
    )
    assert bound["claude-review"] != "success"


def test_bound_context_accepts_the_right_integration(mod, monkeypatch):
    monkeypatch.setattr(mod, "_api", lambda *a, **k: {"statuses": []})
    monkeypatch.setattr(
        mod, "_paginate", lambda *a, **k: [_runs_payload("claude-review", 4456296)]
    )
    states = mod.fetch_combined_statuses(
        "o", "r", "sha", "t", {"claude-review": 4456296}
    )
    assert states["claude-review"] == "success"


def test_plain_commit_status_cannot_satisfy_a_bound_context(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "_api",
        lambda *a, **k: {"statuses": [{"context": "smoke", "state": "success"}]},
    )
    monkeypatch.setattr(mod, "_paginate", lambda *a, **k: [{"check_runs": []}])
    assert mod.fetch_combined_statuses("o", "r", "sha", "t")["smoke"] == "success"
    assert (
        mod.fetch_combined_statuses("o", "r", "sha", "t", {"smoke": 4456296})["smoke"]
        != "success"
    )


def test_any_app_sentinel_is_not_treated_as_a_binding(mod, monkeypatch):
    """GitHub uses app_id -1 for 'explicitly allow any app'; treating that as a
    binding would make every context permanently unsatisfiable."""
    monkeypatch.setattr(
        mod, "_api",
        lambda *a, **k: {"checks": [{"context": "smoke", "app_id": -1},
                                    {"context": "bound", "app_id": 4456296}]},
    )
    contexts, app_ids = mod.fetch_protection("o", "r", "main", "t")
    assert app_ids == {"bound": 4456296}
    assert set(contexts) == {"smoke", "bound"}


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_grok_approve_gate.py",
        "tests/test_grok_approve_gate_workflow.py",
        "tests/test_parse_grok_verdict.py",
        "pyproject.toml",
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "tox.ini",
        "setup.cfg",
        "conftest.py",
        "tests/conftest.py",
    ],
)
def test_deleting_the_gate_tripwires_is_path_denied(mod, path):
    """These pin every invariant above and do not live under .github/, so
    without an explicit deny a PR could remove them and still go green."""
    assert mod.path_denied(path) is True


# --- mechanical APPROVE -----------------------------------------------------


def _app_review(state, sha, rid=1):
    return {
        "id": rid,
        "user": {"login": "infektydgrokreviewer[bot]"},
        "state": state,
        "commit_id": sha,
    }


def test_approval_body_never_carries_the_eligibility_marker(mod):
    """The gate reads App review bodies to decide eligibility. A marker here
    would let the gate's own approval feed its next eligibility check."""
    body = mod.build_approval_body("abc123def456", ("Forbidden Files",))
    assert not mod._has_marker(body)
    assert mod.ELIGIBILITY_MARKER not in body
    assert "Mechanical approval" in body


def test_approval_is_idempotent_on_the_same_sha(mod):
    reviews = [_app_review("APPROVED", "sha1")]
    assert mod.already_approved(reviews, "sha1") is True
    # A newer non-approve on the same SHA means the approval no longer stands.
    reviews.append(_app_review("CHANGES_REQUESTED", "sha1", rid=2))
    assert mod.already_approved(reviews, "sha1") is False


def test_submitter_skips_when_an_approval_already_stands(mod, monkeypatch):
    """Not just that already_approved() is correct — that the submitter
    actually consults it. Otherwise every gate re-run posts another approval."""
    calls = []
    monkeypatch.setattr(
        mod, "fetch_app_reviews", lambda *a, **k: [_app_review("APPROVED", "sha1")]
    )
    monkeypatch.setattr(
        mod, "_api",
        lambda method, url, token, body=None: (
            calls.append(method) or {"head": {"sha": "sha1"}}
        ),
    )
    note = mod.submit_mechanical_approval(
        owner="o", repo="r", pr=1, head_sha="sha1", token="app", required=()
    )
    assert "already present" in note
    assert calls == [], "must not re-post, or even re-read, when already approved"


def test_approval_on_an_older_sha_does_not_count_as_present(mod):
    """Otherwise a push would leave the PR unapproved and the gate would think
    it had already done its job."""
    assert mod.already_approved([_app_review("APPROVED", "old")], "new") is False


def test_dismissed_approval_does_not_count_as_present(mod):
    assert mod.already_approved([_app_review("DISMISSED", "sha1")], "sha1") is False


def test_approval_is_submitted_sha_bound_and_only_when_head_is_unmoved(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "fetch_app_reviews", lambda *a, **k: [])
    monkeypatch.setattr(
        mod, "_api",
        lambda method, url, token, body=None: (
            calls.append((method, url, body)) or {"head": {"sha": "sha1"}}
        ),
    )
    note = mod.submit_mechanical_approval(
        owner="o", repo="r", pr=1, head_sha="sha1", token="app", required=("CI",)
    )
    post = [c for c in calls if c[0] == "POST"]
    assert len(post) == 1
    assert post[0][1].endswith("/pulls/1/reviews")
    assert post[0][2]["commit_id"] == "sha1"
    assert post[0][2]["event"] == "APPROVE"
    assert "sha1" in note


def test_approval_withheld_when_head_moved_mid_flight(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "fetch_app_reviews", lambda *a, **k: [])
    monkeypatch.setattr(
        mod, "_api",
        lambda method, url, token, body=None: (
            calls.append(method) or {"head": {"sha": "sha2"}}
        ),
    )
    note = mod.submit_mechanical_approval(
        owner="o", repo="r", pr=1, head_sha="sha1", token="app", required=()
    )
    assert "withheld" in note
    assert "POST" not in calls, "must not approve a SHA it did not evaluate"


def test_dismissal_touches_only_this_apps_own_approvals(mod, monkeypatch):
    """Dismissing someone else's review — above all a CHANGES_REQUESTED — is the
    worst thing this token could do."""
    reviews = [
        _app_review("APPROVED", "sha1", rid=10),
        _app_review("CHANGES_REQUESTED", "sha1", rid=11),
        {"id": 12, "user": {"login": "a-human"}, "state": "APPROVED", "commit_id": "sha1"},
        {"id": 13, "user": {"login": "a-human"}, "state": "CHANGES_REQUESTED",
         "commit_id": "sha1"},
    ]
    # fetch_app_reviews filters to the App; feed it raw to prove the filter.
    monkeypatch.setattr(
        mod, "fetch_app_reviews",
        lambda *a, **k: [r for r in reviews
                         if mod._is_app_bot((r.get("user") or {}).get("login"))],
    )
    seen = []
    monkeypatch.setattr(
        mod, "_api",
        lambda method, url, token, body=None: seen.append(url) or {},
    )
    n = mod.dismiss_stale_approvals(
        owner="o", repo="r", pr=1, head_sha="sha1", token="app", reason="CI went red"
    )
    assert n == 1
    assert len(seen) == 1
    assert "/reviews/10/dismissals" in seen[0]
    for rid in (11, 12, 13):
        assert f"/reviews/{rid}/" not in seen[0]


def test_failure_paths_never_touch_the_reviews_api(mod, monkeypatch):
    """Only a success may approve. A red or pending gate must not submit
    anything to the Reviews API."""
    submitted = []
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "_gather_and_decide",
        lambda *a, **k: mod.GateDecision("failure", "required check red", "nope"),
    )
    monkeypatch.setattr(mod, "post_check_run", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "submit_mechanical_approval",
        lambda **k: submitted.append("approve"),
    )
    monkeypatch.setattr(mod, "dismiss_stale_approvals", lambda **k: 0)
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "failure"
    assert submitted == []


def test_a_flipped_decision_dismisses_the_stale_approval(mod, monkeypatch):
    dismissed = []
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "_gather_and_decide",
        lambda *a, **k: mod.GateDecision("failure", "REQUEST_CHANGES outstanding", "x"),
    )
    monkeypatch.setattr(mod, "post_check_run", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "dismiss_stale_approvals",
        lambda **k: dismissed.append(k["reason"]) or 1,
    )
    mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                 base_branch="main", post_token="app")
    assert dismissed == ["REQUEST_CHANGES outstanding"]


def test_approval_failure_never_masks_the_posted_decision(mod, monkeypatch):
    """The check run is the merge gate; the approval rides on top. A broken
    approval channel must not turn a decided gate into a crashed job."""
    monkeypatch.setattr(mod, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "_gather_and_decide",
        lambda *a, **k: mod.GateDecision("success", "ok", "green"),
    )
    monkeypatch.setattr(mod, "post_check_run", lambda *a, **k: None)
    monkeypatch.setattr(mod, "fetch_protection", lambda *a, **k: ((), {}))

    def boom(**k):
        raise RuntimeError("GitHub API POST ... -> 403: not granted")

    monkeypatch.setattr(mod, "submit_mechanical_approval", boom)
    d = mod.run_gate(owner="o", repo="r", pr=1, head_sha="abc", token="t",
                     base_branch="main", post_token="app")
    assert d.conclusion == "success"
