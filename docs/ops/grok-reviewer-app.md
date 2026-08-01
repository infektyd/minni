# Grok Reviewer GitHub App (v1)

User-owned GitHub App that posts **formal** PR reviews from
`.github/workflows/grok-review.yml`. Comments from `GITHUB_TOKEN` never satisfy
`required_approving_review_count`; an App installation token can post Reviews
API events.

**CI is the only supported path.** There is no machine-side option: the local
poller that answered `@grok-local` from the operator's Mac is retired. See
[grok-local-path-retired.md](grok-local-path-retired.md) for why it existed, the
measured macOS egress finding that ended it, and the safety posture it
implemented.

## v1 policy (non-negotiable)

| Model line | Reviews API `event` |
|---|---|
| `VERDICT: REQUEST_CHANGES` | `REQUEST_CHANGES` |
| `VERDICT: COMMENT` | `COMMENT` |
| `VERDICT: APPROVE` | **downgraded to `COMMENT`** |
| missing / garbage | `COMMENT` |

LLM output must not mint merge trust. A human or Cursor Approval Agent still
supplies the approving review that clears the merge box.

Parser: `.github/scripts/parse_grok_verdict.py` — only the **last non-empty
line** of the model reply is parsed (loaded from the **default branch** when
present; PR-head fallback only while landing, and head fallback cannot mint
`REQUEST_CHANGES`).

## Create the App

1. GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Name something like `infektyd-grok-reviewer` (must be globally unique).
3. Homepage URL: your choice (repo or `https://github.com/infektyd`).
4. **Webhook:** uncheck Active (Actions mints tokens; no webhook needed for v1).
5. **Permissions → Repository:**
   - Pull requests: **Read & write**
   - Metadata: **Read-only**
   - Nothing else.
6. **Where can this GitHub App be installed?** → Only on this account
   (`infektyd`).
7. Create App → note **App ID**.
8. **Generate a private key** → download the `.pem`. Store offline; do not
   commit it.

## Install (allowlist)

1. Install App → **Only select repositories**.
2. Canary first: one private throwaway or low-stakes repo with the same
   workflow secrets pattern.
3. Then add `infektyd/minni`.
4. Do **not** install on all ~40–60 repos until canary proves REQUEST_CHANGES /
   COMMENT appear as the App bot, and APPROVE never appears from it.

## Secrets & variables (per repo or via reusable caller)

| Name | Kind | Value |
|---|---|---|
| `GROK_APP_ID` | Repository **variable** | numeric App ID |
| `GROK_APP_PRIVATE_KEY` | Repository **secret** | full PEM text |
| `GROK_CI_AUTH_JSON` | Repository **secret** | already required for Grok CLI (see `.github/workflows/grok.yml`) |

```bash
# From a machine that has the PEM (never commit the file):
gh variable set GROK_APP_ID --repo infektyd/minni --body '<APP_ID>'
gh secret set GROK_APP_PRIVATE_KEY --repo infektyd/minni < /path/to/app.pem
```

If `GROK_APP_ID` is unset, the workflow **degrades** to `gh pr comment` via
`GITHUB_TOKEN` (no formal review event). If the ID is set but the PEM is
missing/wrong, mint fails **closed** — fix the secret; do not treat that as
a silent comment fallback.

## Prove it

1. Open a same-repo non-draft PR (or re-open after `ready_for_review`).
2. Confirm the Actions run posts a **Review** from the App identity, not only
   an issue comment.
3. Force a REQUEST_CHANGES path (or temporarily stub the reply in a fork of
   the workflow on a canary) and confirm the merge box stays blocked until a
   human/Cursor APPROVE.
4. Confirm a reply ending in `VERDICT: APPROVE` still posts as **Comment**
   review (parser note in the body).

## Multi-repo

v1 keeps one workflow in each repo (or copy from Minni). Prefer setting the
same variable + secret on each allowlisted install over org secrets (owner is
a user account). Extract a `workflow_call` reusable later if copy drift hurts —
not a v1 blocker.

## Recovery after `REQUEST_CHANGES`

v1 does **not** re-review on every push (`synchronize` omitted to limit
subscription burn). GitHub keeps an App `CHANGES_REQUESTED` opinion until that
**same** review is dismissed or the App submits `APPROVE` (forbidden in v1). A
later `COMMENT` review from the App does **not** clear it; a human/Cursor
`APPROVE` is a different reviewer and also does not auto-dismiss the App.

1. **Dismiss** the App's review (required to clear `CHANGES_REQUESTED` in v1).
2. Optionally re-fire a review via draft → Ready for review, or close → reopen
   (informational / fresh findings only — does **not** unblock merge by itself).

`@grok` / the mention workflow only posts an issue comment — it does **not**
submit a Reviews API event and will not clear `CHANGES_REQUESTED`.

## Out of scope (v1)

- LLM `APPROVE` via the App
- Machine-user PAT as the review identity
- Fork PRs (job already skips them; secrets unavailable)
- `synchronize` re-review (subscription burn; use dismiss above)
- Auto-dismiss of prior App `REQUEST_CHANGES` when a later COMMENT posts
