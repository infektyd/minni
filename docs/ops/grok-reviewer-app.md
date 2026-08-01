# Grok Reviewer GitHub App + mechanical check gate (v2)

User-owned GitHub App posts **formal** PR reviews (`REQUEST_CHANGES` /
`COMMENT`) from `.github/workflows/grok-review.yml`.

**Merge trust is not Reviews API APPROVE.** Measured 2026-08-01 on PR #222:
`infektydgrokreviewer[bot]` (and Cursor bot) can post `APPROVE`, but
`reviewDecision` stays `REVIEW_REQUIRED` (`authorAssociation=NONE`) on this
user-owned repo. v2 therefore uses a **required check run**
`grok-mechanical-approve` from `.github/workflows/grok-approve-gate.yml`.

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

```bash
# PROPOSE to the operator; confirm strict=true (forces up-to-date) before running.
gh api -X PUT repos/infektyd/minni/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "Forbidden Files",
      "Free public cloud smoke",
      "claude-review",
      "grok-mechanical-approve"
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
2. **Bind the required context to the App's `app_id`**, not a bare name:

```bash
APP_ID=$(gh api /repos/infektyd/minni/commits/main/check-runs \
  --jq '[.check_runs[] | select(.name=="grok-mechanical-approve") | .app.id][0]')
gh api -X PATCH repos/infektyd/minni/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'checks[][context]=grok-mechanical-approve' -F "checks[][app_id]=$APP_ID"
```

A `GITHUB_TOKEN`-minted check of the same name carries the GitHub Actions
`app_id` and will not satisfy an App-bound context.

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
