#!/usr/bin/env python3
"""Mechanical merge gate: required check run, not Reviews API APPROVE.

Measured 2026-08-01 on infektyd/minni (#222): App/bot APPROVE posts but does
not clear reviewDecision (authorAssociation=NONE). Merge trust for v2 is the
required status check named CHECK_NAME, posted with GITHUB_TOKEN.

Eligibility comes from a prior Grok App review body containing
ELIGIBILITY_MARKER (stamped when the model emitted VERDICT: APPROVE). The
model retains a veto (no marker / later REQUEST_CHANGES) but cannot alone
turn the check green — every required branch-protection context (except this
check) must be success, and the head SHA must not move mid-flight.

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
APP_BOT_SUFFIX = "[bot]"

# Paths that must never get a green mechanical check from eligibility alone.
PATH_DENY_PREFIXES = (
    ".github/workflows/grok-approve-gate.yml",
    ".github/scripts/grok_approve_gate.py",
    ".github/scripts/parse_grok_verdict.py",
    ".github/workflows/grok-review.yml",
)


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


def fetch_combined_statuses(
    owner: str, repo: str, sha: str, token: str
) -> dict[str, str]:
    """Map context → latest state (success/pending/failure/error)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status"
    data = _api("GET", url, token)
    states: dict[str, str] = {}
    for st in data.get("statuses") or []:
        ctx = st.get("context")
        if not ctx:
            continue
        # API returns newest first in statuses array for combined; keep first seen.
        if ctx not in states:
            states[ctx] = st.get("state") or "error"
    # Check runs (Actions) often appear only here:
    runs_url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100"
    )
    runs = _api("GET", runs_url, token)
    for run in runs.get("check_runs") or []:
        name = run.get("name")
        if not name or name == CHECK_NAME:
            continue
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            states.setdefault(name, "pending")
        elif conclusion == "success":
            states[name] = "success"
        elif conclusion in ("failure", "timed_out", "cancelled", "action_required"):
            states[name] = "failure"
        elif conclusion == "neutral" or conclusion == "skipped":
            # Skipped/neutral do not satisfy required checks.
            states.setdefault(name, "failure")
        else:
            states.setdefault(name, "failure")
    return states


def _is_app_bot(login: str | None) -> bool:
    if not login:
        return False
    return login.endswith(APP_BOT_SUFFIX) or "grokreviewer" in login.lower()


def analyze_reviews(reviews: list[dict[str, Any]]) -> tuple[bool, bool]:
    """Return (eligible, blocked_by_request_changes).

    Any non-dismissed CHANGES_REQUESTED (any author) blocks.
    Eligibility: newest App-bot review body contains ELIGIBILITY_MARKER,
    and no later App-bot CHANGES_REQUESTED supersedes it.
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
        if state == "CHANGES_REQUESTED":
            eligible = False
            break
        body = rev.get("body") or ""
        if ELIGIBILITY_MARKER in body:
            eligible = True
            break
    return eligible, blocked


def fetch_pr_files_denied(owner: str, repo: str, pr: int, token: str) -> bool:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100"
    files = _paginate(url, token)
    for f in files:
        path = f.get("filename") or ""
        for prefix in PATH_DENY_PREFIXES:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return True
            # exact file match already covered by ==
            if path == prefix:
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


def run_gate(
    *,
    owner: str,
    repo: str,
    pr: int,
    head_sha: str,
    token: str,
    base_branch: str,
) -> GateDecision:
    # SHA binding: re-read PR head before deciding.
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
    if (pr_data.get("head") or {}).get("repo") and (
        (pr_data["head"]["repo"].get("full_name") or "")
        != f"{owner}/{repo}"
    ):
        return GateDecision("failure", "fork", "Fork PRs are out of scope.")

    required = fetch_required_contexts(owner, repo, base_branch, token)
    states = fetch_combined_statuses(owner, repo, head_sha, token)
    reviews = _paginate(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100",
        token,
    )
    eligible, blocked = analyze_reviews(reviews)
    path_denied = fetch_pr_files_denied(owner, repo, pr, token)

    decision = decide(
        GateInput(
            head_sha=head_sha,
            required_contexts=required,
            check_states=states,
            eligible=eligible,
            blocked_by_request_changes=blocked,
            path_denied=path_denied,
        )
    )
    # Re-check SHA immediately before posting.
    pr_data2 = _api("GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}", token)
    live2 = (pr_data2.get("head") or {}).get("sha") or ""
    if live2 != head_sha:
        decision = GateDecision(
            "failure",
            "head moved",
            f"Abort before post: head is now {live2[:12]}.",
        )
    post_check_run(owner, repo, token, head_sha, decision)
    return decision


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", type=int, default=0)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--base-branch", default="")
    p.add_argument("--token-env", default="GH_TOKEN")
    args = p.parse_args(argv)
    token = os.environ.get(args.token_env, "")
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
    )
    print(f"{decision.conclusion}\t{decision.title}\t{decision.summary}")
    # Job stays green even when the *check run* is failure — the required
    # check context is what blocks merge.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
