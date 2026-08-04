#!/usr/bin/env python3
"""Mechanical merge gate: required check run, not Reviews API APPROVE.

Measured 2026-08-01 on infektyd/minni (#222): App/bot APPROVE posts but does
not clear reviewDecision (authorAssociation=NONE). Merge trust for v2 is the
required status check named CHECK_NAME, posted under the Grok App
installation identity (never GITHUB_TOKEN - see MissingAppToken).

Eligibility comes from the NEWEST Grok App review whose body carries
ELIGIBILITY_MARKER on a line of its own (stamped when the model emitted
VERDICT: APPROVE) AND whose commit_id is the current head. The model retains a
veto (no marker / later REQUEST_CHANGES) but cannot alone turn the check green
— every required branch-protection context (except this check) must be
success, and the head SHA must not move mid-flight.

Eligibility does not survive a push: a stamp for an older SHA is worthless, so
"approve clean code, then add a bad commit" cannot mint merge trust.

Usage (Actions):
  python3 grok_approve_gate.py \\
    --repo owner/name --pr 123 --head-sha abcdef \\
    --token-env GH_TOKEN

Trust: load this file from origin/$default_branch, never from PR head.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

CHECK_NAME = "grok-mechanical-approve"
ELIGIBILITY_MARKER = "<!-- grok-mechanical-eligibility: APPROVE -->"

# Exact logins whose reviews may carry eligibility. This is the Grok reviewer
# App installation identity and nothing else. A suffix test like
# login.endswith("[bot]") is NOT sufficient: any same-repo PR may add a
# workflow that posts a review as github-actions[bot] with GITHUB_TOKEN, which
# would let the PR stamp its own eligibility.
APP_BOT_LOGINS = ("infektydgrokreviewer[bot]",)

# Who submits the mechanical APPROVE. NOT the App: measured 2026-08-01 on #243,
# the App posted APPROVED as the newest, non-dismissed review on the final head
# 2b0bdac and reviewDecision STAYED REVIEW_REQUIRED. GitHub counts approvals
# only from USERS with write access, and a GitHub App installation is not one
# (author_association is NONE for bots). infektydrelay-bit is a real user with
# push access, so its approval does count.
#
# Same exact-login discipline as APP_BOT_LOGINS: equality, case-insensitive,
# never a suffix test.
APPROVAL_LOGINS = ("infektydrelay-bit",)

# Every API call is bounded: an unbounded urlopen leaves the window between the
# final observation and the POST limited only by the job timeout.
API_TIMEOUT_S = 15

# How many independent observations must agree before success is published.
GATHER_ROUNDS = 3

# Paths that must never get a green mechanical check from eligibility alone.
# All of .github/ is denied, not just the four gate files: any workflow under
# it can weaken a required check the gate trusts, or post as github-actions[bot].
PATH_DENY_PREFIXES = (
    ".github/",
    # Approval policy and review rules. Cursor's approval agents and Bugbot
    # read these from repository contents, so they are instructions to an
    # approver in the same way .github/ is instructions to CI: a PR that can
    # rewrite "never auto-approve trust paths" into "approve on green CI" has
    # rewritten the merge gate, whatever the diff to the code looks like.
    ".cursor/",
    "APPROVAL_POLICY.md",
    "tests/test_approval_policies.py",
    # The reviewing agent's own configuration surface, and executable: #272
    # landed 472 lines of agent-orchestration workflow here with capability
    # mode "all". grok-review.yml deliberately writes its sandbox profile to
    # $HOME precisely so a PR cannot supply .grok/sandbox.toml — denying the
    # directory is the same reasoning applied to everything else under it.
    ".grok/",
    # The gate's own tripwires. Not under .github/, so without these a PR could
    # delete the tests that pin every invariant above and still go green.
    "tests/test_grok_approve_gate.py",
    "tests/test_grok_approve_gate_workflow.py",
    "tests/test_parse_grok_verdict.py",
    # The leak gate's only tripwire. Its three siblings above were denied and
    # this one was not, which is the whole deny list's logic applied to every
    # gate except the one guarding the credential path.
    "tests/test_credential_leak_check.py",
    # Collection config can disable every tripwire above without touching a
    # single test file: `addopts = "--ignore=..."`, dropping `testpaths`, or
    # `collect_ignore_glob`. CI runs bare pytest inside the proposed-required
    # "Free public cloud smoke" context, and pytest.ini wins over pyproject.toml
    # even when empty — so every rootdir config file has to be denied, not just
    # the one this repo happens to use today.
    "pyproject.toml",
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "tox.ini",
    "setup.cfg",
    "conftest.py",
    "tests/conftest.py",
)


class MissingAppToken(RuntimeError):
    """No Grok App installation token: the check has no trustworthy identity."""


class ProtectionUnreadable(RuntimeError):
    """Branch protection could not be read; the required set is UNKNOWN."""


@dataclass(frozen=True)
class GateInput:
    head_sha: str
    required_contexts: tuple[str, ...]
    check_states: dict[str, str]  # context -> success|pending|failure|error|missing
    eligible: bool
    blocked_by_request_changes: bool
    path_denied: bool


@dataclass(frozen=True)
class GateDecision:
    conclusion: str  # success | failure | neutral
    title: str
    summary: str


def decide(inp: GateInput) -> GateDecision:
    """Pure gate. Never returns success unless every invariant holds."""
    if inp.path_denied:
        return GateDecision(
            "failure",
            "path filter",
            "PR touches gate/trust paths; mechanical check stays red until a human merges.",
        )
    if inp.blocked_by_request_changes:
        return GateDecision(
            "failure",
            "REQUEST_CHANGES outstanding",
            "An unresolved CHANGES_REQUESTED review blocks the mechanical check.",
        )
    if not inp.eligible:
        return GateDecision(
            "failure",
            "not eligible",
            "No App review carries the APPROVE eligibility marker "
            f"({ELIGIBILITY_MARKER}).",
        )
    if not inp.required_contexts:
        return GateDecision(
            "failure",
            "empty required checks",
            "Branch protection required contexts are empty after excluding "
            f"{CHECK_NAME!r}; fail closed (never vacuous green).",
        )
    pending: list[str] = []
    bad: list[str] = []
    for ctx in inp.required_contexts:
        state = inp.check_states.get(ctx, "missing")
        if state == "success":
            continue
        if state in ("pending", "expected", "missing"):
            pending.append(f"{ctx}={state}")
        else:
            bad.append(f"{ctx}={state}")
    if bad:
        return GateDecision(
            "failure",
            "required check red",
            "Cannot green-light a red build: " + ", ".join(bad),
        )
    if pending:
        return GateDecision(
            "failure",
            "required check pending",
            "Cannot green-light while required checks are incomplete: "
            + ", ".join(pending),
        )
    return GateDecision(
        "success",
        "mechanical gate green",
        f"Eligible + all required contexts success on {inp.head_sha[:12]}. "
        "CI green is not correctness.",
    )


def _api(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "minni-grok-approve-gate",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"GitHub API {method} {url} → {e.code}: {detail}") from e


def _paginate(url: str, token: str) -> list[Any]:
    out: list[Any] = []
    next_url: str | None = url
    while next_url:
        req = urllib.request.Request(
            next_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "minni-grok-approve-gate",
            },
        )
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            chunk = json.loads(resp.read().decode())
            if isinstance(chunk, list):
                out.extend(chunk)
            else:
                out.append(chunk)
            link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1 : part.find(">")]
                break
    return out


def fetch_protection(
    owner: str, repo: str, branch: str, token: str
) -> tuple[tuple[str, ...], dict[str, int]]:
    """Return (required_contexts, context -> bound app_id) in ONE API call.

    Reading this endpoint needs Administration:read, which GITHUB_TOKEN cannot
    be granted — pass the App installation token. A 403 here is a misconfigured
    App, not an absent ruleset, and must not read as "no requirements": that is
    the difference between "fail closed" and "vacuously green".
    """
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}"
        "/protection/required_status_checks"
    )
    try:
        data = _api("GET", url, token)
    except RuntimeError as e:
        msg = str(e)
        if "404" in msg:
            # No protection configured at all; decide() rejects an empty set.
            return (), {}
        if "403" in msg or "401" in msg:
            raise ProtectionUnreadable(
                "Cannot read branch protection (HTTP 403/401). The token needs "
                "Administration:read — grant it to the Grok App and make sure "
                "APP_TOKEN is what reads protection."
            ) from e
        raise

    contexts = list(data.get("contexts") or [])
    app_ids: dict[str, int] = {}
    for check in data.get("checks") or []:
        ctx, app_id = check.get("context"), check.get("app_id")
        if ctx and ctx not in contexts:
            contexts.append(ctx)
        # -1 is GitHub's "explicitly allow any app" sentinel, not a binding.
        if ctx and ctx != CHECK_NAME and isinstance(app_id, int) and app_id > 0:
            app_ids[ctx] = app_id
    # Never treat our own check as a prerequisite for itself.
    return tuple(c for c in contexts if c != CHECK_NAME), app_ids


# Worst-wins ordering. Merging observations for one context must never let a
# `success` mask a pending or failed one — that is a direct false-green.
_STATE_RANK = {"success": 0, "expected": 1, "pending": 1, "failure": 2, "error": 2}


def _worse(a: str, b: str) -> str:
    return a if _STATE_RANK.get(a, 2) >= _STATE_RANK.get(b, 2) else b


def _check_run_state(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "success"
    # Everything else — failure, timed_out, cancelled, action_required, and
    # neutral/skipped — does not satisfy a required check.
    return "failure"


def fetch_combined_statuses(
    owner: str,
    repo: str,
    sha: str,
    token: str,
    required_app_ids: dict[str, int] | None = None,
) -> dict[str, str]:
    """Map context → state (success/pending/failure/error), worst wins.

    Check runs are authoritative for names they cover; commit statuses only
    fill in contexts that have no check run. Within either source, repeated
    observations of one name collapse to the WORST, so an in-flight re-run or a
    stale success can never read as green.

    `required_app_ids` binds a context to an integration. An observation from
    the wrong app does not count as success for that context — protection would
    reject it, and the gate must not be more permissive than protection.
    """
    bindings = required_app_ids or {}
    states: dict[str, str] = {}
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status"
    data = _api("GET", url, token)
    for st in data.get("statuses") or []:
        ctx = st.get("context")
        if not ctx or ctx == CHECK_NAME:
            continue
        state = st.get("state") or "error"
        # A commit status carries no app_id we can trust here; if protection
        # binds this context to an app, a plain status cannot satisfy it.
        if ctx in bindings:
            state = _worse(state, "pending")
        states[ctx] = state if ctx not in states else _worse(states[ctx], state)

    # filter=latest is the API default, but pin it: without it a re-run's older
    # `success` can arrive after the newer `failure` for the same name.
    runs_url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
        "/check-runs?per_page=100&filter=latest"
    )
    run_states: dict[str, str] = {}
    for chunk in _paginate(runs_url, token):
        for run in chunk.get("check_runs") or []:
            name = run.get("name")
            if not name or name == CHECK_NAME:
                continue
            state = _check_run_state(run)
            bound = bindings.get(name)
            if bound is not None and (run.get("app") or {}).get("id") != bound:
                # Right name, wrong integration — a spoof, or simply a check
                # protection will not accept. Never let it read as success.
                state = _worse(state, "pending")
            run_states[name] = (
                state if name not in run_states else _worse(run_states[name], state)
            )
    states.update(run_states)
    return states


def _is_app_bot(login: str | None) -> bool:
    if not login:
        return False
    return login.lower() in {known.lower() for known in APP_BOT_LOGINS}


def _is_approver(login: str | None) -> bool:
    """The identity that submits the mechanical APPROVE.

    Deliberately disjoint from _is_app_bot: the approver's reviews must never
    be read as App eligibility, or the approve/dismiss oscillation returns
    through the new identity.
    """
    if not login:
        return False
    return login.lower() in {known.lower() for known in APPROVAL_LOGINS}


def _has_marker(body: str | None) -> bool:
    """Marker must be a line of its own, not a substring of quoted text.

    The review body embeds the model's reply verbatim, so a plain `in` test
    would let a PR that plants the marker string in its own diff get it echoed
    back by the reviewer and read as eligibility.
    """
    return any(line.strip() == ELIGIBILITY_MARKER for line in (body or "").splitlines())


def analyze_reviews(
    reviews: list[dict[str, Any]], head_sha: str
) -> tuple[bool, bool]:
    """Return (eligible, blocked_by_request_changes).

    Any non-dismissed CHANGES_REQUESTED (any author) blocks.

    Eligibility is decided by the NEWEST non-dismissed App-bot review only, and
    that review must have been written against `head_sha`. Both halves matter:

    * Newest-only — otherwise a later App review saying "I no longer approve"
      is skipped and an older stamped review still grants eligibility.
    * SHA-bound — otherwise eligibility survives a push. Approve clean code,
      then force in a bad commit: the gate re-runs on `synchronize`, finds the
      old marker, sees green CI on the NEW sha, and mints success for code the
      reviewer never saw.

    head_sha is REQUIRED and the binding is unconditional. It previously
    defaulted to "" and "" skipped the comparison, which meant any call
    site that forgot the argument silently restored the pre-fix mint.
    A fail-open default on the invariant this function exists to enforce
    is not acceptable; omit the argument and you get a TypeError.
    """
    blocked = False
    for rev in reviews:
        if (rev.get("state") or "").upper() == "CHANGES_REQUESTED":
            # GitHub lists all; dismissed reviews have state DISMISSED.
            blocked = True
            break

    eligible = False
    # reviews API returns chronological; walk newest first.
    for rev in reversed(reviews):
        user = (rev.get("user") or {}).get("login") or ""
        state = (rev.get("state") or "").upper()
        if state == "DISMISSED":
            continue
        if not _is_app_bot(user):
            continue
        # The gate's OWN mechanical APPROVE is not an eligibility signal, and
        # must not end this search. It is deliberately marker-free, so without
        # this skip it reads as "newest App review, no marker" -> not eligible
        # -> the gate goes red and dismisses its own approval -> the next run
        # sees the marker review again -> green -> re-approve. Forever, and
        # self-sustaining: the pull_request_review trigger fires on both submit
        # and dismiss, and App-token actions DO trigger workflows (unlike
        # GITHUB_TOKEN, which has anti-recursion).
        #
        # LOAD-BEARING PRECONDITION: grok-review.yml submits only
        # REQUEST_CHANGES or COMMENT, never APPROVE, so an APPROVED App review
        # can only be ours. If the reviewer is ever changed to post APPROVE,
        # this skip will silently swallow it and eligibility will never be seen.
        if state == "APPROVED":
            continue
        if state != "CHANGES_REQUESTED" and _has_marker(rev.get("body")):
            eligible = (rev.get("commit_id") or "") == head_sha
        # First App-bot review seen wins, marker or not. Do not fall through to
        # older reviews.
        break
    return eligible, blocked


def fetch_pr_files_denied(owner: str, repo: str, pr: int, token: str) -> bool:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100"
    files = _paginate(url, token)
    # GitHub caps this listing at 3000 files. A PR big enough to truncate could
    # hide a denied path past the cap, so treat truncation itself as denied.
    if len(files) >= 3000:
        return True
    for f in files:
        # A rename reports the NEW path in `filename` and the OLD one only in
        # `previous_filename`. Checking `filename` alone lets a PR rename a
        # tripwire out from under the deny list, then edit it in a second PR.
        if path_denied(f.get("filename") or ""):
            return True
        if path_denied(f.get("previous_filename") or ""):
            return True
    return False


def path_denied(path: str) -> bool:
    """True if `path` is a gate/trust path (exact file or directory prefix)."""
    for prefix in PATH_DENY_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix:
            return True
    return False


def post_check_run(
    owner: str,
    repo: str,
    token: str,
    head_sha: str,
    decision: GateDecision,
) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/check-runs"
    return _api(
        "POST",
        url,
        token,
        {
            "name": CHECK_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": decision.conclusion,
            "output": {
                "title": decision.title,
                "summary": decision.summary,
            },
        },
    )


def resolve_pr_for_sha(owner: str, repo: str, sha: str, token: str) -> int | None:
    # pulls-for-commit needs the groot preview media type
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.groot-preview+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "minni-grok-approve-gate",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            prs = json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        return None
    for pr in prs:
        if pr.get("state") != "open":
            continue
        if (pr.get("head") or {}).get("sha") == sha:
            return int(pr["number"])
    for pr in prs:
        if pr.get("state") == "open":
            return int(pr["number"])
    return None



# --- Mechanical APPROVE -----------------------------------------------------
# When decide() returns success, the same decision is ALSO expressed as a real
# Reviews API APPROVE from the App, so a `required_approving_review_count: 1`
# rule can be satisfied without a human. This is not the model approving: it is
# the gate's mechanical decision on a second channel, minted only when every
# invariant in decide() held.


def build_approval_body(head_sha: str, required: tuple[str, ...]) -> str:
    """Body for the mechanical approval.

    MUST NOT contain ELIGIBILITY_MARKER: the gate reads App review bodies to
    decide eligibility, so a marker here would let the gate's own approval feed
    its next eligibility check — a self-licking loop.
    """
    contexts = ", ".join(required) if required else "(none)"
    body = (
        "Mechanical approval - not a judgment about whether this code is good.\n\n"
        f"`{CHECK_NAME}` evaluated `{head_sha[:12]}` and every condition held:\n"
        f"- all required contexts success: {contexts}\n"
        f"- Grok App eligibility stamped on `{head_sha[:12]}`\n"
        "- no outstanding CHANGES_REQUESTED\n"
        "- no gate/CI trust path touched\n\n"
        "Approval is bound to this commit. Any push invalidates it and the gate "
        "re-evaluates from scratch."
    )
    assert not _has_marker(body), "approval body must never carry the marker"
    return body


def fetch_approver_reviews(
    owner: str, repo: str, pr: int, token: str
) -> list[dict[str, Any]]:
    """Reviews written by the APPROVING identity — not the App's."""
    reviews = _paginate(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100",
        token,
    )
    return [r for r in reviews if _is_approver((r.get("user") or {}).get("login"))]


def already_approved(reviews: list[dict[str, Any]], head_sha: str) -> bool:
    """True if the approver's newest live review on this SHA is an APPROVE.

    Without this, every re-run of the gate posts another approval.
    """
    for rev in reversed(reviews):
        state = (rev.get("state") or "").upper()
        if state == "DISMISSED":
            continue
        if (rev.get("commit_id") or "") != head_sha:
            continue
        return state == "APPROVED"
    return False


def submit_mechanical_approval(
    *,
    owner: str,
    repo: str,
    pr: int,
    head_sha: str,
    token: str,
    required: tuple[str, ...],
) -> str:
    """Submit the APPROVE as the relay user. Returns a status for the job log."""
    reviews = fetch_approver_reviews(owner, repo, pr, token)
    if already_approved(reviews, head_sha):
        return "approval already present"

    # Same belt-and-braces as the check post: the Reviews API binds to the most
    # recent commit unless commit_id is given, so re-read and abort if the head
    # moved rather than approve code that was never evaluated.
    pr_data = _api("GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}", token)
    if ((pr_data.get("head") or {}).get("sha") or "") != head_sha:
        return "head moved; approval withheld"

    # GitHub rejects an approval from the PR's own author. Name it instead of
    # letting a 422 surface as a stack trace: the standing rule is that agent
    # PRs are opened as infektyd, never as the approving identity.
    author = ((pr_data.get("user") or {}).get("login")) or ""
    if _is_approver(author):
        return (
            f"PR authored by the approving identity ({author}) - needs a "
            "different opener; GitHub forbids self-approval"
        )

    _api(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews",
        token,
        {
            "commit_id": head_sha,
            "event": "APPROVE",
            "body": build_approval_body(head_sha, required),
        },
    )
    return f"approved {head_sha[:12]}"


def dismiss_stale_approvals(
    *, owner: str, repo: str, pr: int, head_sha: str, token: str, reason: str
) -> int:
    """Dismiss the approver's OWN live APPROVE reviews that no longer hold.

    Scoped hard to the approving identity's approvals. It must never touch the
    App's reviews, never a human's, and never a CHANGES_REQUESTED — that is the
    veto the gate exists to respect, and dismissing it would be the worst thing
    this token could do.
    """
    dismissed = 0
    for rev in fetch_approver_reviews(owner, repo, pr, token):
        if (rev.get("state") or "").upper() != "APPROVED":
            continue
        if not _is_approver((rev.get("user") or {}).get("login")):
            continue  # belt and braces; fetch_approver_reviews already filtered
        rid = rev.get("id")
        if not rid:
            continue
        _api(
            "PUT",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}"
            f"/reviews/{rid}/dismissals",
            token,
            {"message": f"Mechanical approval superseded: {reason}", "event": "DISMISS"},
        )
        dismissed += 1
    return dismissed


def _preflight(
    owner: str, repo: str, pr: int, head_sha: str, token: str
) -> GateDecision | None:
    """Cheap disqualifiers. None means "keep going"."""
    pr_data = _api("GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}", token)
    live_sha = (pr_data.get("head") or {}).get("sha") or ""
    if live_sha != head_sha:
        return GateDecision(
            "failure",
            "head moved",
            f"Abort: evaluated {head_sha[:12]} but PR head is {live_sha[:12]}.",
        )
    if pr_data.get("draft"):
        return GateDecision("failure", "draft", "Draft PRs stay red.")
    # head.repo is null when the source repo was deleted; treat unknown as fork.
    # The workflow's same-repo `if` only covers pull_request — check_suite and
    # workflow_run reach this code for fork PRs too, so it must decide here.
    head_repo = (pr_data.get("head") or {}).get("repo") or {}
    if (head_repo.get("full_name") or "") != f"{owner}/{repo}":
        return GateDecision("failure", "fork", "Fork PRs are out of scope.")
    return None


def _gather_and_decide(
    owner: str,
    repo: str,
    pr: int,
    head_sha: str,
    token: str,
    base_branch: str,
    protection_token: str = "",
) -> GateDecision:
    required, app_ids = fetch_protection(owner, repo, base_branch, protection_token)
    states = fetch_combined_statuses(owner, repo, head_sha, token, app_ids)
    reviews = _paginate(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100",
        token,
    )
    eligible, blocked = analyze_reviews(reviews, head_sha)
    denied = fetch_pr_files_denied(owner, repo, pr, token)
    return decide(
        GateInput(
            head_sha=head_sha,
            required_contexts=required,
            check_states=states,
            eligible=eligible,
            blocked_by_request_changes=blocked,
            path_denied=denied,
        )
    )


def run_gate(
    *,
    owner: str,
    repo: str,
    pr: int,
    head_sha: str,
    token: str,
    base_branch: str,
    post_token: str = "",
    approve_token: str = "",
) -> GateDecision:
    # Checked FIRST: the App token is what reads protection and posts the
    # check, so without it every later call is wasted and the failure would
    # surface as a confusing 401 instead of the real cause.
    #
    # The check-run channel is only a trust root when the check is posted by the
    # Grok App: a bare name posted with GITHUB_TOKEN can be minted by ANY
    # same-repo Actions workflow. Falling back to GITHUB_TOKEN is worse than not
    # posting, because an app-bound context ignores that check entirely — it
    # could neither grant nor REVOKE.
    if not post_token:
        raise MissingAppToken(
            "APP_TOKEN is empty - refusing to post the mechanical check under "
            "GITHUB_TOKEN. Set vars.GROK_APP_ID + secrets.GROK_APP_PRIVATE_KEY "
            "and grant the App `checks: write` + `administration: read`."
        )
    decision = _preflight(owner, repo, pr, head_sha, token)
    if decision is None:
        # Observe repeatedly and publish success only if EVERY observation
        # agrees. Publishing a green and retracting it afterwards is inherently
        # racy: if the retraction call fails or the job is killed first, the
        # green stands and the merge channel never learns otherwise. Any
        # non-success wins immediately and is what gets posted.
        decision = None
        try:
            for _ in range(GATHER_ROUNDS):
                obs = _gather_and_decide(
                    owner, repo, pr, head_sha, token, base_branch, post_token
                )
                if obs.conclusion != "success":
                    decision = obs
                    break
                decision = obs
        except ProtectionUnreadable as e:
            # Post the reason rather than dying with a stack trace: if the
            # context is already required, a job that posts nothing wedges
            # every PR with no visible explanation.
            decision = GateDecision("failure", "cannot read protection", str(e))
        moved = _preflight(owner, repo, pr, head_sha, token)
        if moved is not None:
            decision = moved

    post_check_run(owner, repo, post_token, head_sha, decision)

    # Express the SAME decision as a Reviews API APPROVE so a
    # `required_approving_review_count: 1` rule can be satisfied mechanically.
    # Only on success: the failure and pending paths never touch the Reviews API.
    # Submitted as the RELAY USER, not the App: a GitHub App's approval does not
    # satisfy required_approving_review_count (measured on #243). Degrade with a
    # named line rather than failing — the check run is the merge gate and has
    # already been posted; only the review-count half is missing.
    if not approve_token:
        print(
            "::warning::approval skipped: RELAY_APPROVE_TOKEN unset. The check "
            "run posted, but nothing satisfies required_approving_review_count."
        )
        return decision
    try:
        if decision.conclusion == "success":
            required, _ = fetch_protection(owner, repo, base_branch, post_token)
            note = submit_mechanical_approval(
                owner=owner, repo=repo, pr=pr, head_sha=head_sha,
                token=approve_token, required=required,
            )
        else:
            n = dismiss_stale_approvals(
                owner=owner, repo=repo, pr=pr, head_sha=head_sha,
                token=approve_token, reason=decision.title,
            )
            note = f"dismissed {n} stale approval(s)" if n else "no approval to dismiss"
        print(f"approval: {note}")
    except (RuntimeError, urllib.error.URLError) as e:
        # The check run is the merge gate; the approval is a convenience on top.
        # Never let an approval failure mask the decision that was already
        # posted — but say so loudly, because a stuck approval means a human
        # still has to merge.
        print(f"::warning::approval channel failed: {e}")

    return decision


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", type=int, default=0)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--base-branch", default="")
    p.add_argument("--token-env", default="GH_TOKEN")
    p.add_argument(
        "--approve-token-env",
        default="RELAY_APPROVE_TOKEN",
        help="Env var holding the relay USER token that submits the mechanical "
        "APPROVE. Unset means the check still posts, but nothing satisfies the "
        "approving-review requirement.",
    )
    p.add_argument(
        "--post-token-env",
        default="APP_TOKEN",
        help="Env var holding the Grok App installation token used to POST the "
        "check run. Without it the gate can only post failure.",
    )
    args = p.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    post_token = os.environ.get(args.post_token_env, "")
    approve_token = os.environ.get(args.approve_token_env, "")
    if not token:
        print(f"::error::{args.token_env} unset", file=sys.stderr)
        return 2
    owner, _, repo = args.repo.partition("/")
    if not owner or not repo:
        print("::error::--repo must be owner/name", file=sys.stderr)
        return 2
    base = args.base_branch
    if not base:
        meta = _api("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
        base = meta.get("default_branch") or "main"
    pr = args.pr
    if not pr:
        found = resolve_pr_for_sha(owner, repo, args.head_sha, token)
        if not found:
            print("::warning::No open PR for SHA; skipping check run.")
            return 0
        pr = found
    try:
        decision = run_gate(
            owner=owner,
            repo=repo,
            pr=pr,
            head_sha=args.head_sha,
            token=token,
            base_branch=base,
            post_token=post_token,
            approve_token=approve_token,
        )
    except MissingAppToken as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    print(f"{decision.conclusion}\t{decision.title}\t{decision.summary}")
    # Job stays green even when the *check run* is failure — the required
    # check context is what blocks merge.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
