#!/usr/bin/env python3
"""Mechanical merge gate: required check run, not Reviews API APPROVE.

Measured 2026-08-01 on infektyd/minni (#222): App/bot APPROVE posts but does
not clear reviewDecision (authorAssociation=NONE). Merge trust for v2 is the
required status check named CHECK_NAME, posted with GITHUB_TOKEN.

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

# Paths that must never get a green mechanical check from eligibility alone.
# All of .github/ is denied, not just the four gate files: any workflow under
# it can weaken a required check the gate trusts, or post as github-actions[bot].
PATH_DENY_PREFIXES = (".github/",)


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
        with urllib.request.urlopen(req) as resp:
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
        with urllib.request.urlopen(req) as resp:
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


def fetch_required_contexts(owner: str, repo: str, branch: str, token: str) -> tuple[str, ...]:
    url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks"
    try:
        data = _api("GET", url, token)
    except RuntimeError as e:
        if "404" in str(e):
            return ()
        raise
    contexts = list(data.get("contexts") or [])
    for check in data.get("checks") or []:
        ctx = check.get("context")
        if ctx and ctx not in contexts:
            contexts.append(ctx)
    # Never treat our own check as a prerequisite for itself.
    return tuple(c for c in contexts if c != CHECK_NAME)


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
    owner: str, repo: str, sha: str, token: str
) -> dict[str, str]:
    """Map context → state (success/pending/failure/error), worst wins.

    Check runs are authoritative for names they cover; commit statuses only
    fill in contexts that have no check run. Within either source, repeated
    observations of one name collapse to the WORST, so an in-flight re-run or a
    stale success can never read as green.
    """
    states: dict[str, str] = {}
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status"
    data = _api("GET", url, token)
    for st in data.get("statuses") or []:
        ctx = st.get("context")
        if not ctx or ctx == CHECK_NAME:
            continue
        state = st.get("state") or "error"
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
            run_states[name] = (
                state if name not in run_states else _worse(run_states[name], state)
            )
    states.update(run_states)
    return states


def _is_app_bot(login: str | None) -> bool:
    if not login:
        return False
    return login.lower() in {known.lower() for known in APP_BOT_LOGINS}


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
        if state != "CHANGES_REQUESTED" and _has_marker(rev.get("body")):
            eligible = (rev.get("commit_id") or "") == head_sha
        # First App-bot review seen wins, marker or not. Do not fall through to
        # older reviews.
        break
    return eligible, blocked


def fetch_pr_files_denied(owner: str, repo: str, pr: int, token: str) -> bool:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100"
    files = _paginate(url, token)
    return any(path_denied(f.get("filename") or "") for f in files)


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
        with urllib.request.urlopen(req) as resp:
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
    owner: str, repo: str, pr: int, head_sha: str, token: str, base_branch: str
) -> GateDecision:
    required = fetch_required_contexts(owner, repo, base_branch, token)
    states = fetch_combined_statuses(owner, repo, head_sha, token)
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
) -> GateDecision:
    decision = _preflight(owner, repo, pr, head_sha, token)
    if decision is None:
        first = _gather_and_decide(owner, repo, pr, head_sha, token, base_branch)
        # Re-read EVERYTHING immediately before posting, not just the SHA. A
        # concurrent gate run (or a REQUEST_CHANGES landing mid-gather) must not
        # be overwritten by this run's stale snapshot: success needs two
        # agreeing observations, anything else wins immediately.
        second = _gather_and_decide(owner, repo, pr, head_sha, token, base_branch)
        decision = second if second.conclusion != "success" else first
        moved = _preflight(owner, repo, pr, head_sha, token)
        if moved is not None:
            decision = moved

    # The check-run channel is only a trust root when the check is posted by the
    # Grok App: a bare name posted with GITHUB_TOKEN can be minted by ANY
    # same-repo Actions workflow, so protection must bind the context to the
    # App's app_id and we must post under that identity. Without the App token
    # we may still post red, never green.
    if decision.conclusion == "success" and not post_token:
        decision = GateDecision(
            "failure",
            "no app token",
            "Refusing to mint success with GITHUB_TOKEN: any same-repo workflow "
            "could post this check name. Configure the Grok App (checks: write) "
            "and bind the required context to its app_id.",
        )
    post_check_run(owner, repo, post_token or token, head_sha, decision)
    return decision


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", type=int, default=0)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--base-branch", default="")
    p.add_argument("--token-env", default="GH_TOKEN")
    p.add_argument(
        "--post-token-env",
        default="APP_TOKEN",
        help="Env var holding the Grok App installation token used to POST the "
        "check run. Without it the gate can only post failure.",
    )
    args = p.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    post_token = os.environ.get(args.post_token_env, "")
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
    decision = run_gate(
        owner=owner,
        repo=repo,
        pr=pr,
        head_sha=args.head_sha,
        token=token,
        base_branch=base,
        post_token=post_token,
    )
    print(f"{decision.conclusion}\t{decision.title}\t{decision.summary}")
    # Job stays green even when the *check run* is failure — the required
    # check context is what blocks merge.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
