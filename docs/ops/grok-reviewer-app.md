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
| `GROK_CI_AUTH_JSON` | Repository **secret** | Grok CLI subscription OAuth fallback (see `.github/workflows/grok.yml`) |
| `XAI_API_KEY` | Repository **secret** | PREFERRED: pay-per-use console.x.ai key; wins over the OAuth blob when both are set (and bills per run) |

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
      { "context": "grok-mechanical-approve", "app_id": 4456296 }
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
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

   Caveat, proven by execution: `workflow_dispatch` only registers workflows
   that exist on the **default branch** — dispatching this file from a topic
   branch returns 404. Either land the throwaway on main (and delete it after
   use, as above), or skip the throwaway entirely: a gate run on a real PR
   exercises the same mint + protection read, so one canary PR is an
   equivalent probe. The failure modes it reports, by case: skipped or
   empty mint (`GROK_APP_ID` unset) → hard job failure logging
   `APP_TOKEN is empty`, no check posted; mint **action** failure (bad
   key, permission not granted) → red mint step, no check posted and no
   gate log line at all; token OK but `administration: read` missing →
   check titled `cannot read protection`. Path-deny does not
   short-circuit the probe: protection is read during gathering, before
   the path filter is applied, and a 403 still posts
   `cannot read protection`. Prefer a docs-only canary anyway, so a
   successful probe is not masked by a `path filter` failure title.
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

`strict: true` is load-bearing and IS the live setting on `main` (enabled
2026-08-01, preserved through the campaign's R14 close). It forces
re-evaluation after the base branch moves; without it a check evaluated
against an older `main` still authorises the merge. The live review count is
`1`, satisfied by the relay-approval channel.

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

### SETTLED: an App approval does NOT satisfy the review requirement

Measured 2026-08-01 on canary **#243**, and this time unconfounded:

| | |
|---|---|
| review | `infektydgrokreviewer[bot]` (type `Bot`), state `APPROVED` |
| commit | `2b0bdac` — **the final head** |
| position | **newest** review, nothing after it |
| dismissed | no |
| `reviewDecision` | **`REVIEW_REQUIRED`** |

Every confounder from the earlier readings is absent: not superseded by a later
review from the same author, not stale against a moved head, not dismissed. The
approval simply does not count.

The rule is that GitHub counts approvals only from **users with write access**.
A GitHub App installation is not a user — hence `author_association: NONE` on
every App review. (For completeness: the earlier #222 and #216 readings really
were confounded, by supersession and staleness respectively, so they never
established this; #243 does.)

**Therefore the mechanical APPROVE is submitted by `infektydrelay-bit`**, a real
user with push access on this repo, using a fine-grained PAT. The App keeps
reviewing and the gate keeps deciding; only the identity that signs the approval
changed.

| Identity | Role | Token |
|---|---|---|
| `infektyd` | opens PRs | — |
| `infektydgrokreviewer[bot]` | reviews, stamps eligibility | App installation token |
| the gate | decides, posts the check | App token (`checks: write`, `administration: read`) |
| `infektydrelay-bit` | submits the mechanical APPROVE | `RELAY_APPROVE_TOKEN` |

**Standing rule: agent PRs are opened as `infektyd`, never as
`infektydrelay-bit`.** GitHub rejects an approval from the PR's own author, so a
relay-authored PR can never be approved mechanically. The gate detects this and
logs `PR authored by the approving identity - needs a different opener` rather
than failing on a 422.

If `RELAY_APPROVE_TOKEN` is unset the gate degrades: the check run still posts,
the approval is skipped with a named warning, and merges need a human review.

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

### Blast radius of the App key after this change

Granting the gate `pull-requests: write` widens what a **key holder** can do.
The code scopes dismissal to the App's own approvals; the *token* does not. Be
explicit about what the key is now worth:

There are now TWO keys, with different reach. Both are repository secrets, so
both are reachable by anyone who can push a branch (see the private-key section
above) — that reachability is unchanged, but the split matters for what each
buys.

**Relay PAT (`RELAY_APPROVE_TOKEN`)** — fine-grained, `minni` only, Pull
requests RW. It buys **write access to pull requests as `infektydrelay-bit`**.
That is more than "submit an approval": Pull requests RW also allows
`PATCH /pulls/{n}` — which can **change a PR's base branch** — and close or
reopen PRs, edit titles and bodies, and dismiss *anyone's* review. The code
only ever uses approve-and-dismiss-its-own; the token is not so limited.

Changing the base branch is the one worth naming: it re-points a reviewed diff
at a different target, so what was approved and what would merge are no longer
the same thing.

It still cannot post the mechanical check, read or change protection, or merge.

**App key** — after this change the App no longer needs `pull-requests: write`
for the gate at all; the gate's token is `checks: write` + `administration:
read`. (The *reviewer* workflow still mints `pull-requests: write` to post its
reviews — that is a separate token and unchanged.)

| With the App key you CAN | You CANNOT |
|---|---|
| post `grok-mechanical-approve` (the required check) | **merge** — that needs `contents: write` |
| satisfy `required_approving_review_count` | **change branch protection** — needs `administration: write`; the gate holds only `read` |
| — | satisfy `require_code_owner_reviews` |
| — | **dismiss a human's `CHANGES_REQUESTED`** — that moved to the relay PAT |

So the App key is worth **the required check**, and the relay PAT is worth **the
review count plus the ability to clear a human veto** (PR write implies
dismissing others' reviews — the code scopes dismissal to the relay's own
approvals, the token does not).

Splitting them back apart is a real improvement over #244, where one key held
all three. An attacker now needs BOTH secrets to forge a full merge-ready state
— though on this repo both are reachable the same way, so treat that as defence
in depth rather than a boundary. Anyone with push access can reach that key (see the
private-key section above), so the honest summary is that the mechanical gate
does not contain a push-capable actor — it contains a pull request.

**CODEOWNERS is the one layer the key cannot forge.** App approvals do NOT
satisfy `require_code_owner_reviews`: code owners must be individuals or teams
with write access, and a GitHub App installation is neither. That is precisely
why it is worth configuring, and why it must own itself.

### Never review manually from the relay account

The gate cannot tell its own mechanical approval from one a human typed while
signed in as `infektydrelay-bit` — both are simply `APPROVED` reviews from that
login. So on the next non-success decision it will **dismiss a hand-made relay
approval** as a stale mechanical one.

Review as `infektyd`, or as yourself. If you want a human approval to stick,
it must come from an identity the gate does not manage.

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
`enforce_admins` is ON since the 2026-08 campaign close: the protection
binds administrators too, and there is deliberately no standing bypass.
Disabling it (`gh api -X DELETE .../protection/enforce_admins`) is an
explicit, temporary operator action for emergencies — re-enable when done;
it is not the default recipe. Path filter forces human attention on
gate/workflow changes.

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

## Personal operator playbook (friction from live use)

This App is **personal tooling for this repo**, not a published product.
The notes below are measured friction from multi-PR agent campaigns (2026-08,
especially #271). Use them so agents and humans stop thrashing the gate.

### Re-earn `/grok-review` (read this before scripting agents)

1. **Body must be exactly** `/grok-review` after trim (no markdown heading, no
   trailing prose). Job `if` uses `startsWith`; Resolve step uses **exact**
   equality — multi-line “re-request” comments **skip** and still look like
   success in the UI.
2. **One re-request owner per PR.** `concurrency` is
   `grok-review-${PR}` with `cancel-in-progress: true`. Dual shepherd +
   orchestrator comments cancel each other.
3. **Freeze the tip** before the command. Eligibility is tip-bound (invariant
   9). Push after APPROVE/ELIG → stamp dead; you pay another metered review.
4. **Same SHA is not re-billed** (including dismissed reviews on that SHA).
   Need a real code change (or accept dismiss is not a free re-roll).
5. **`issue_comment` runs from `main`.** Actions UI often shows
   `headBranch=main` / default-branch `headSha` even when the job reviewed the
   PR tip. Trust the formal review’s `commit_id`, not the run list SHA, for
   “what was reviewed.”
6. **CI max-turns for the App path is 60** (workflow pin; not the retired
   local `--max-turns 30` path in [grok-local-path-retired.md](grok-local-path-retired.md)).

### Why `reviewDecision` still says REVIEW_REQUIRED

App `APPROVE` does not count (settled above). Green path is:

- App review body with `<!-- grok-mechanical-eligibility: APPROVE -->` on
  **current head**, plus
- `grok-mechanical-approve` **success**, plus
- other required CI green.

`gh pr view … reviewDecision` lagging on `REVIEW_REQUIRED` while the App
posted ELIG is **normal** until the mechanical check and/or relay APPROVE
land. Do not re-fire `/grok-review` just to “fix” reviewDecision.

### PATH_DENY → mechanical stays red (use admin consciously)

`PATH_DENY_PREFIXES` includes `.github/`, gate tests, **and** `pyproject.toml`
/ pytest root configs (so collection config cannot silence tripwires).

**Implication:** a PR that only *stamps* `pyproject.toml` for a release
(e.g. 0.4.1 → 0.4.2) can be App-ELIG + Public CI green and still have
`grok-mechanical-approve` **failure by design**. That is not a flaky gate.

Personal rule:

- Prefer **split** release-stamp PR vs large docs/code PR when possible.
- When a stamp stack is intentional and App+CI are green: **`gh pr merge
  --admin --squash`** is the honest path — not dismiss thrash or another
  metered review.
- Agent policy elsewhere: never self-admin unless the operator said so.

### Campaign hygiene (reduces App bills)

| Do | Don’t |
|----|--------|
| Local Cassandra / `docs-accuracy-converge` **before** first App review | Use App as the first accuracy pass |
| One worktree owner for the PR tip | Parallel fixers on the same branch + dual `/grok-review` |
| Fix Highs in one tip freeze window | Push every residual and re-earn each time (whack-a-mole) |
| Trust `commit_id` on the formal review | Trust Actions `headSha` on `issue_comment` runs |

Optional local friction sampler: session notes under
`scratchpad/friction/` when dogfooding campaigns.

### Assumptions that became (or become) next PR goals

See **[docs-truth-policy.md](docs-truth-policy.md)**. Present-tense honesty
must **not** erase ambition: overclaims that describe wanted surfaces become
`goal_next_pr` / `honesty_partial`, not silent cuts.

| Assumption / claim | Disposition | Status / next |
|---|---|---|
| Multi-host PreToolUse deny (incl. Grok file-backed guard) | implement_now | **landed #274** (`grok-adapter` + unit bar); wet session optional |
| `minni doctor` runs wire verify probes | honesty_cut | fixed; keep doctor scoped |
| Fleet hosts track package/main automatically | ops_fleet | **landed #273** — use **`minni sync`** |
| Stamp set includes marketplace + pyproject | honesty + release hygiene | checklist; marketplace in 0.4.2 |
| Grok App cheaper re-earn / clearer PATH_DENY text | goal_next_pr (personal) | optional later (O3/O5/O6) |
