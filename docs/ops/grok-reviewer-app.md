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
    leave an earlier `success` standing on the SHA.

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
  "restrictions": null
}
JSON
```

Keep `required_approving_review_count: 1` only if you still want a **human**
review in addition to the mechanical check (bots still will not count).

### The name-only required context is NOT a trust root

Branch protection matches a required status check **by name**. Any same-repo
workflow with `permissions: checks: write` can create a check run called
`grok-mechanical-approve` with `conclusion: success` using the default
`GITHUB_TOKEN` — and it appears under the same GitHub Actions app as the real
gate. A PR can therefore mint its own required check. The `.github/` path
filter does not help: it only forces the *legitimate* gate red.

Two operator steps are required before this gate means anything:

1. **Grant the Grok App `checks: write`** (App settings → Permissions →
   Repository → Checks: Read and write, then accept the permission request on
   the installation). The gate mints an installation token and posts the check
   under the App; without that token it now refuses to post `success` at all
   and reports `no app token`.
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

## Residual risk

A green mechanical check means “eligible Grok verdict + required CI green” —
**not** correctness. Agent-authored PRs can still ship bad-but-green code.
Keep `enforce_admins: false` as a manual escape hatch. Path filter forces
human attention on gate/workflow changes.

## Recovery

- **Eligibility is bound to the reviewed commit.** The marker only counts if
  the stamping review's `commit_id` is the current head. Pushing invalidates it,
  and Grok does not re-review on `synchronize` (metered). To re-arm after a
  push: mark ready-for-review, or close/reopen, to re-run Grok on the new SHA.
  This is deliberate — without it, "approve clean code, then push a bad commit"
  mints merge trust for code the reviewer never saw.
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
