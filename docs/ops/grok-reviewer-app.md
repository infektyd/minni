# Grok Reviewer GitHub App + mechanical check gate (v2)

User-owned GitHub App posts **formal** PR reviews (`REQUEST_CHANGES` /
`COMMENT`) from `.github/workflows/grok-review.yml`.

**Merge trust is not Reviews API APPROVE.** Measured 2026-08-01 on PR #222:
`infektydgrokreviewer[bot]` (and Cursor bot) can post `APPROVE`, but
`reviewDecision` stays `REVIEW_REQUIRED` (`authorAssociation=NONE`) on this
user-owned repo. v2 therefore uses a **required check run**
`grok-mechanical-approve` from `.github/workflows/grok-approve-gate.yml`.

**CI is the only supported path.** There is no machine-side option: the local
poller that answered `@grok-local` from the operator's Mac is retired. See
[grok-local-path-retired.md](grok-local-path-retired.md) for why it existed, the
measured macOS egress finding that ended it, and the safety posture it
implemented.

## v2 policy

| Model line | Reviews API | Mechanical check |
|---|---|---|
| `VERDICT: REQUEST_CHANGES` | `REQUEST_CHANGES` | stays **failure** (blocks) |
| `VERDICT: COMMENT` | `COMMENT` | **failure** until eligibility |
| `VERDICT: APPROVE` | `COMMENT` + eligibility marker | **success** only if every *other* required status is `success` |
| missing / garbage | `COMMENT` | **failure** |

Eligibility marker stamped into the App review body:

```html
<!-- grok-mechanical-eligibility: APPROVE -->
```

Invariants (enforced in `.github/scripts/grok_approve_gate.py`):

1. Red or pending required checks → never success.
2. Empty required-context list → fail closed.
3. Head SHA re-read before post; abort if moved.
4. Gate script loaded from **default branch** only.
5. Required contexts read from branch protection API (not hardcoded), with
   `grok-mechanical-approve` itself excluded so the check is never its own
   prerequisite.
6. PRs touching **any** `.github/` path stay red (path filter) — a workflow
   there can weaken a required check the gate trusts.
7. Only `infektydgrokreviewer[bot]` may carry the eligibility marker
   (`APP_BOT_LOGINS`). A `[bot]`-suffix test would be forgeable: any same-repo
   PR can post a review as `github-actions[bot]` with `GITHUB_TOKEN`.
8. The marker must be a line of its own. The review body embeds the model's
   reply verbatim, so `grok-review.yml` also defangs any marker the model
   echoed out of the diff before posting.
9. Eligibility is bound to the reviewed commit (`review.commit_id == head`) and
   read from the **newest** App review only. A stamp does not survive a push,
   and a later unmarked App review revokes it.
10. Per-context state is worst-wins: an in-flight re-run or a stale `success`
    can never mask a pending or failed observation of the same check.
11. `success` is posted only under the Grok App installation token. With only
    `GITHUB_TOKEN` the gate reports `no app token` and stays red, because a
    name-only check is mintable by any same-repo workflow (see below).
12. Every input is re-read immediately before posting and both observations
    must agree on `success`; concurrency is keyed on head SHA for all events,
    so a stale snapshot cannot publish green over a veto that already landed.
13. Every non-skip path posts a terminal conclusion, so a superseded run cannot
    leave an earlier `success` standing on the SHA. After publishing a green the
    gate re-reads once and revokes it if state moved.
14. The gate honours protection's own `app_id` bindings when judging its
    prerequisites: a check run of the right name from the wrong integration does
    not count, and a plain commit status cannot satisfy an app-bound context.
    **Residual:** contexts that protection leaves name-only (no `app_id`, or the
    `-1` any-app sentinel) are still satisfiable by any same-repo workflow. Bind
    every required context, not just `grok-mechanical-approve`.
15. Deleting the gate's tests is path-denied. `tests/test_grok_approve_gate*.py`
    and `tests/test_parse_grok_verdict.py` pin these invariants and do not live
    under `.github/`, so they are named explicitly in `PATH_DENY_PREFIXES`.
16. Success is published only after `GATHER_ROUNDS` independent observations
    all agree. The gate never publishes a green and retracts it afterwards: if
    the retraction call failed or the job were killed first, the green would
    stand and the merge channel would never learn otherwise.
17. Re-evaluation on CI completion is `workflow_run`, never `check_suite`.
    check_suite does not trigger a workflow when the suite was created by
    GitHub Actions, and every CI suite here is Actions-created — measured on
    this branch: 40 runs, 0 of them check_suite. **Adding a required context
    means adding its workflow to that `workflows:` list**, or the gate stops
    re-evaluating when that check finishes.
18. Renames cannot walk a tripwire out of the deny list: the PR files listing
    is checked on `previous_filename` as well as `filename`, and a listing at
    the 3000-file API cap is itself treated as denied.
19. A missing App installation token is a hard job failure, not a red check.
    Posting under `GITHUB_TOKEN` would be ignored by the app-bound context, so
    it could neither grant nor revoke — and it would make the check's identity
    depend on whether a secret happened to be set.

Parser: `.github/scripts/parse_grok_verdict.py` — default path never emits
Reviews `APPROVE`; `--allow-approve` is for gate/eligibility readers only.

## Create the App

1. GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Name something like `infektyd-grok-reviewer` (must be globally unique).
3. Homepage URL: your choice (repo or `https://github.com/infektyd`).
4. **Webhook:** uncheck Active (Actions mints tokens; no webhook needed).
5. **Permissions → Repository:**
   - Pull requests: **Read & write**
   - Metadata: **Read-only**
   - Nothing else (check runs use `GITHUB_TOKEN` in the gate workflow).
6. **Where can this GitHub App be installed?** → Only on this account.
7. Create App → note **App ID**.
8. **Generate a private key** → download the `.pem`. Do not commit it.

## Install (allowlist)

Prefer **Only select repositories** (`minni` ± canary). If install is
`all`, tighten it.

## Secrets & variables

| Name | Kind | Value |
|---|---|---|
| `GROK_APP_ID` | Repository **variable** | numeric App ID |
| `GROK_APP_PRIVATE_KEY` | Repository **secret** | full PEM text |
| `GROK_CI_AUTH_JSON` | Repository **secret** | Grok CLI auth (see `.github/workflows/grok.yml`) |

## Branch protection (operator — propose, don't silent-apply)

To stop needing `--admin` for solo merges, make the mechanical check + real CI
required, and drop the approving-review count that bots cannot satisfy:

Use `checks` with an `app_id` per context — **never** the bare `contexts` array.
A name-only requirement is mintable by any same-repo workflow; see the next
section for why that is a self-merge primitive.

```bash
# PROPOSE to the operator; do not silent-apply. Prerequisite: the Grok App must
# already have `checks: write`, or the gate can only ever post red (by design).
gh api -X PUT repos/infektyd/minni/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "Forbidden Files",         "app_id": 15368 },
      { "context": "Free public cloud smoke", "app_id": 15368 },
      { "context": "claude-review",           "app_id": 15368 },
      { "context": "grok-mechanical-approve", "app_id": 4456296 }
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true
  },
  "required_conversation_resolution": true,
  "restrictions": null
}
JSON
```

**PUT is a full replace — every omitted property resets to its default.**
`required_conversation_resolution` is `true` on `main` today and defaults to
`false`, so leaving it out of the body silently turns it off. It is listed
above for exactly that reason. Diff before and after:

```bash
gh api repos/infektyd/minni/branches/main/protection > /tmp/prot-before.json
# ...apply...
gh api repos/infektyd/minni/branches/main/protection > /tmp/prot-after.json
diff <(jq -S . /tmp/prot-before.json) <(jq -S . /tmp/prot-after.json)
```

**Use PUT, not PATCH, for this change.** `PATCH .../protection/required_status_checks`
only edits the status-check block: it leaves `required_approving_review_count`
at its current value, which is `1` — so merges would still need `--admin` and
the whole exercise fails to achieve its goal. Only the full `PUT` above sets the
count to `0`. Reach for the PATCH form solely when adding or re-binding a
context and you intend everything else to stay as-is.

Keep `required_approving_review_count: 1` only if you still want a **human**
review in addition to the mechanical check (bots still will not count).

### The App private key is the real trust root — and push access reaches it

`GROK_APP_PRIVATE_KEY` is a plain **repository** secret. Repository secrets are
available to any workflow run triggered by a push to **any branch** of this
repo. So anyone with push access can add `.github/workflows/x.yml` on a throwaway
branch that mints an App installation token and posts `grok-mechanical-approve`
success directly, under `app_id 4456296`. `decide()` never runs. The `.github/`
path filter never sees it, because the malicious workflow is not in any PR being
reviewed.

**This defeats the app_id binding completely.** State the threat model plainly:
the gate contains what a *pull request* can do through the review-and-merge
flow. It does **not** contain an actor who can push a branch. On this repo the
agents doing the work have push access, so they are inside that boundary.

**Environment scoping does not fix it — verified, do not attempt it.** The
obvious move is to put the key in a GitHub Environment whose deployment-branch
policy allows only the default branch. It does not work here. Since November
2025, environment branch policies for the `pull_request` family evaluate against
`refs/pull/<n>/merge`, not the head or base branch name. A default-branch-only
policy therefore blocks our own PR-triggered jobs:

* `grok-review`'s automatic triggers are all `pull_request` — those would lose
  the App token entirely and silently degrade to `gh pr comment`: no formal
  reviews, no eligibility stamps.

  Note the one exception, which matters for option 1 below: the `/grok-review`
  command runs on `issue_comment`, and **`issue_comment` workflows run from the
  default branch**, so that path WOULD satisfy a default-branch-only policy. An
  architecture where the only review path is the explicit command is therefore
  compatible with environment scoping — at the cost of no automatic review on
  PR open.
* the gate would keep only its `workflow_run` triggers (those do run from the
  default branch) and lose `pull_request` + `pull_request_review`, taking the
  post-green veto with them — but see the relay in option 1, which gets it back.

Adding a `refs/pull/*/merge` policy to compensate re-opens the hole for every
PR, which is strictly worse than where we started.

Two architectures actually close it. Both are operator decisions:

1. **Split the identity.** Two Apps: a *reviewer* App (`pull_requests: write`,
   key reachable from PR runs) and a separate *gate* App (`checks: write` only)
   whose key lives in a default-branch-only Environment and is used solely by
   the gate — which then runs on `workflow_run` only. The trust-root key becomes
   unreachable from PR-triggered workflows.

   The `pull_request_review` veto survives this, via a relay. A tiny
   *unprivileged* workflow triggers on `pull_request_review`, holds no secrets
   and does nothing but exist; the gate lists it in its own `workflow_run`
   `workflows:`. The gate then runs from the default branch — satisfying the
   Environment policy — while still waking on every review submission. Chain
   depth is 2, which GitHub allows. So option 1's real cost is one extra
   workflow file and a few seconds of latency, not the loss of the veto.
2. **Remove push access from the agent class.** Agents open PRs from forks;
   fork PRs get no secrets. Then repository secrets are only reachable by the
   humans the gate is not trying to contain.

Until one of these lands, treat a green `grok-mechanical-approve` as evidence
about the PR's *content*, not as containment of the actor who opened it.

### The name-only required context is NOT a trust root

Branch protection matches a required status check **by name**. Any same-repo
workflow with `permissions: checks: write` can create a check run called
`grok-mechanical-approve` with `conclusion: success` using the default
`GITHUB_TOKEN` — and it appears under the same GitHub Actions app as the real
gate. A PR can therefore mint its own required check. The `.github/` path
filter does not help: it only forces the *legitimate* gate red.

Two operator steps are required before this gate means anything:

1. **Grant the Grok App `checks: write` AND `administration: read`** (App
   settings → Permissions → Repository, then accept the permission request on
   the installation). The gate mints an installation token, posts the check
   under the App, and — critically — uses that same token to READ branch
   protection. Without a token it refuses to run at all rather than post under
   `GITHUB_TOKEN`.

   `administration: read` is not optional. `GET /branches/{b}/protection/required_status_checks`
   requires it, and `GITHUB_TOKEN` cannot be granted it at all. If that read
   403s the gate posts a `cannot read protection` failure — deliberately, since
   an unreadable required-set is UNKNOWN, not empty.

   **Pre-flight this before binding anything.** If the context is already
   required and the read path is broken, every PR wedges permanently with no
   way to merge. Prove the read works first with a throwaway workflow:

```yaml
# .github/workflows/protection-preflight.yml — delete after use
name: Protection preflight
on: workflow_dispatch
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/create-github-app-token@v2
        id: t
        with:
          app-id: ${{ vars.GROK_APP_ID }}
          private-key: ${{ secrets.GROK_APP_PRIVATE_KEY }}
          permission-administration: read
      - env:
          GH_TOKEN: ${{ steps.t.outputs.token }}
        run: gh api repos/${{ github.repository }}/branches/main/protection/required_status_checks
```

   Bind nothing until that prints the JSON. A 403 means the permission grant
   has not been accepted on the installation yet.
2. **Bind the required context to the App's `app_id`**, not a bare name.

Classic branch protection already supports this and this repo already uses it:
`Forbidden Files` is bound to `app_id 15368` (GitHub Actions). A ruleset is not
required. `checks` REPLACES the whole list, so send every context in one call:

```bash
# 15368  = GitHub Actions (ordinary CI checks)
# 4456296 = infektydgrokreviewer App — equals vars.GROK_APP_ID; confirm with:
#   gh api repos/infektyd/minni/actions/variables/GROK_APP_ID --jq .value
gh api -X PATCH repos/infektyd/minni/branches/main/protection/required_status_checks \
  --input - <<'JSON'
{
  "strict": true,
  "checks": [
    { "context": "Forbidden Files",         "app_id": 15368 },
    { "context": "Free public cloud smoke", "app_id": 15368 },
    { "context": "claude-review",           "app_id": 15368 },
    { "context": "grok-mechanical-approve", "app_id": 4456296 }
  ]
}
JSON
```

A `GITHUB_TOKEN`-minted check of the same name carries `app_id 15368` and will
not satisfy the App-bound context.

Do **not** derive the app id from an existing `grok-mechanical-approve` check
run: none exists under the App until after step 1 lands, so that lookup returns
the GitHub Actions id and would bind the context to precisely the integration
you are trying to exclude.

`strict: true` is load-bearing and is **not** the current setting (`strict` is
`false` on `main` today). At `required_approving_review_count: 0` it is the only
thing forcing re-evaluation after the base branch moves; without it a check
evaluated against an older `main` still authorises the merge.

**Do not add `boundary` to this list.** Grok Boundary Test only runs on PRs
touching `.github/workflows/grok*.yml` or `check-no-credential-leak.py`. On an
ordinary PR the context never reports, the gate reads it as `missing`, and the
mechanical check can never go green — the exact opposite of the goal. Only
require checks that run on every PR.


## Merge without `--admin` (operator — propose, don't silent-apply)

The operator is the merge choke point and the repo stalls while they are at
work. Two phases, deliberately separated:

| | What changes | Gated on |
|---|---|---|
| **Phase 1 — now** | Merges stay manual and on the main account, but stop needing `--admin` | the App's mechanical APPROVE satisfying the review requirement |
| **Phase 2 — pre-wired, OFF** | Gated PRs merge themselves | one operator setting, `allow_auto_merge` |

Phase 2 is wired now so that turning it on is a setting and nothing else. The
target state is that mechanically-gated PRs merge without the operator, who
then only reviews the trust surface.

### The mechanical APPROVE

When `decide()` returns success the gate ALSO submits a real Reviews API
`APPROVE` from the App, bound to the evaluated commit. This is not the model
approving — it is the same mechanical decision on a second channel, minted only
when every invariant held. The body deliberately carries no eligibility marker,
so the gate's own approval cannot feed its next eligibility check.

The gate dismisses its own stale approvals when the decision flips. It is scoped
in code to this App's `APPROVED` reviews only: it must never dismiss a human's
review, and never a `CHANGES_REQUESTED`.

### UNPROVEN: does an App approval satisfy `required_approving_review_count`?

**Test this before relying on it.** The `#222` claim that "App/bot APPROVE does
not clear reviewDecision" is **not supported by the record** — I re-read it:

| PR | Evidence | Why it proves nothing |
|---|---|---|
| #222 | App posted `APPROVED` 15:15:22, then `COMMENTED` 15:15:42, then `CHANGES_REQUESTED` 15:16:05 — all on `030fb8d` | GitHub takes the newest review per author. The App superseded its own approval within 43s, so the final `REVIEW_REQUIRED` is expected either way |
| #216 | `cursor[bot]` approved three times, each dismissed ~12s later by `dismiss_stale_reviews` on the next push | No approval ever covered the final head `5d0a509` |

So there is **no measurement** on this repo of a live bot approval on the
current head. Both prior readings were confounded — by supersession and by
staleness, not (as once assumed) by the parser downgrading `APPROVE`: an actual
`APPROVED` review record exists on #222.

The hypothesis is therefore untested, not disproven, and it is plausible:
`author_association: NONE` is normal for bots and is a different concept from
write access, and App approvals are how bots like renovate-approve work.

**The live test:** after this lands, let the gate pass on a canary PR and check
that `reviewDecision` flips `REVIEW_REQUIRED` → `APPROVED` while the approval is
the App's newest review on the current head. Record the result here either way.

### Phase 1 (now): manual merges, no `--admin`

The near-term win is small and concrete: merges stay on the main account and
stay manual, but they stop needing a bypass. Once the App's mechanical APPROVE
satisfies `required_approving_review_count`, this is enough:

```bash
gh pr merge --squash <n>      # no --admin
```

That is the whole phase-1 goal. If `--admin` is still required after the gate
has approved, the hypothesis above has failed — record that and stop, rather
than reaching for the bypass out of habit.

### Phase 2 (pre-wired, OFF): auto-merge

Auto-merge is wired up now so that enabling it is a **single operator setting
and nothing else** — no second round of tooling changes.

**Standard practice from now on:** whoever opens a PR queues the merge at open
time, rather than merging at the end.

```bash
gh pr merge --auto --squash <n> \
  || echo "auto-merge not enabled; will merge manually when green"
```

While the repo setting is off this command **fails**, which is why it is written
with the fallback — treat that message as normal, not as an error to chase. The
moment the operator flips the setting, the identical command starts arming PRs
to merge themselves once the gate approves and required checks pass. Nothing
else changes.

The operator's one-time flip:

```bash
gh api -X PATCH repos/infektyd/minni -F allow_auto_merge=true
```

Use `-F`, not `-f`: `-f` sends the string `"true"`, while `-F` sends a real
boolean, which is what this field expects.

Do not enable it until phase 1 has actually been observed working — auto-merge
on top of an unproven approval path just means PRs sit queued forever, or worse,
merge on a signal nobody has verified.

### CODEOWNERS — the human-required trust surface

Once phase 2 is on, auto-merge plus a mechanical approval means nothing
human-gated remains unless you say so. Set this up **before** flipping
`allow_auto_merge`, not after. Require code-owner review for the trust surface:

```
# .github/CODEOWNERS
/.github/                @infektyd
/.github/CODEOWNERS      @infektyd
/scripts/check*          @infektyd
/docs/ops/grok-reviewer-app.md  @infektyd
```

Then set `"require_code_owner_reviews": true` in the protection payload.

**CODEOWNERS must own itself** — the second line above is not redundant. Without
it, a PR can edit CODEOWNERS to remove the owner gate and then auto-merge
everything else. Note this overlaps the gate's `.github/` path deny, which
already forces those PRs red; CODEOWNERS is the independent second layer, and
the one that still applies if the gate is ever bypassed.

## Residual risk

A green mechanical check means “eligible Grok verdict + required CI green” —
**not** correctness. Agent-authored PRs can still ship bad-but-green code.
Keep `enforce_admins: false` as a manual escape hatch. Path filter forces
human attention on gate/workflow changes.

## Recovery

- **Eligibility is bound to the reviewed commit.** The marker only counts if
  the stamping review's `commit_id` is the current head. Pushing invalidates it,
  and Grok does not re-review on `synchronize` (metered). This is deliberate —
  without it, "approve clean code, then push a bad commit" mints merge trust for
  code the reviewer never saw.
- **To re-arm after a push, comment `/grok-review` on the PR.** That is the
  supported re-review path: one deliberate metered review, run against the
  CURRENT head, which re-stamps eligibility on the new SHA and feeds the gate
  correctly. Collaborators only (`OWNER`/`MEMBER`/`COLLABORATOR`) — this is a
  public repo and each run bills the operator's Grok subscription. The command
  must be the entire comment; prose mentioning it does nothing. A repeat command
  on an already-reviewed SHA is skipped with a reply rather than double-billed,
  and you get an :eyes: reaction when one is accepted.
- **The command only works once this is on `main`.** `issue_comment` workflows
  always run the file from the default branch, so `/grok-review` is inert on the
  PR that introduces it — same bootstrap shape as the gate script. Nothing to
  fix; just do not expect it to answer before the merge.
- **Dismissing a review is not a substitute for re-earning one.** Dismiss only
  a review that a later re-review has made obsolete. Dismissing to clear a block
  without a fresh review throws away the signal the gate depends on.
- **Only the newest App review counts.** A later App `COMMENTED` review with no
  marker revokes eligibility; the gate does not fall through to an older
  stamped review.
- **A `CHANGES_REQUESTED` review keeps the check red until it is DISMISSED**,
  even after a later review stamps the marker. The gate reads every review, not
  just the newest per author, so a stale block does not silently expire. Dismiss
  it (`gh pr review --dismiss`) or push a new SHA and let Grok re-review.
- Push → new SHA → gate re-runs on `synchronize` / check completion (L4).
- The gate also re-runs on `pull_request_review`, so a `REQUEST_CHANGES`
  submitted *after* the check went green revokes it. Without that trigger the
  veto would be advisory only: bot reviews do not move `reviewDecision` here.
