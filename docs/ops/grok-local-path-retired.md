# The local Grok path (retired)

**Status: retired.** Grok on this repo is served exclusively by GitHub Actions
(`.github/workflows/grok.yml`, `grok-review.yml`, `grok-boundary-test.yml`). No
Grok CI credential and no Grok agent execution lives on the operator's machine.

The retired implementation was `scripts/grok_local_watch.py` (a poller that
answered `@grok-local` GitHub mentions by running the Grok CLI locally against a
disposable clone) plus its launchd template
`scripts/com.minni.grok-local-watch.plist.template`. This page preserves the
reasoning that lived in that file, so the design does not have to be
re-derived — or the same mistake re-made — from git history.

## Why a poller and not a self-hosted runner

A self-hosted runner is the obvious way to move CI onto your own machine, and it
is the wrong tool for a **public** repo: GitHub's own guidance is not to do it,
because a pull request from a fork can cause code to execute on the runner host.
A poller inverts the direction — nothing inbound, nothing registered, no port,
no runner service. The machine reaches out, decides what it is willing to act
on, and acts.

If a machine-side path is ever revived, revive it as a poller. Do not register a
self-hosted runner against this repo.

## Why it was retired: the measured macOS egress finding

**Measured, not assumed:** on macOS the Grok CLI's `restrict_network` did **not**
block `curl` in testing. The CLI implements child-network blocking via seccomp,
which is Linux-only; on macOS the Seatbelt profile covers the filesystem and not
egress. On the Linux GitHub runners egress **is** genuinely blocked, verified
down to raw-IP connects.

So the local path read untrusted PR text — titles, bodies, diffs, comments —
while holding a live xAI subscription credential with **no egress boundary**,
and the pre-post credential leak check was carrying proportionally more of the
load than it does in CI. That is a strictly weaker boundary than the runners
provide, for the same work. Retiring it removes the weaker boundary and the last
Grok CI credential on the operator's laptop.

It was also already inert in practice at the time of removal: no process
running, no LaunchAgent installed, and its trigger was `@grok-local` rather than
`/grok`, so nothing routed to it.

## Safety posture it implemented (reference design)

Kept as a reference in case a local path is ever revived — these were the
controls, and any revival should start from at least this set:

- **Association allowlist.** Only comments whose `author_association` was
  `OWNER`, `MEMBER`, or `COLLABORATOR` were ever acted on.
- **Disposable clone.** The agent ran against a fresh clone in a temp dir, never
  the operator's working checkout, so an injection could not touch real work or
  a dirty tree. The temp dir was removed in a `finally`.
- **Isolated HOME.** `HOME` was pointed at `~/.grok-local` so the agent did not
  sit next to the operator's personal long-lived credentials in `~/.grok`.
- **Constrained CLI invocation.** `--sandbox local` (a profile extending
  `read-only` with `restrict_network = true`), `--no-subagents`,
  `--max-turns 30`, `--disable-web-search`, `--always-approve` and
  `--output-format plain` (both required for headless operation — without
  auto-approve the agent blocks waiting for interactive tool approval), and a
  timeout.
- **Prompt scaffold treating PR text as data.** The prompt stated explicitly
  that PR/issue content is untrusted data to analyze and never instructions,
  regardless of what it claims.
- **Leak check before posting.** Every reply passed
  `.github/scripts/check-no-credential-leak.py` against the isolated home's
  `auth.json` before anything was posted; a failure refused the post.
- **First-run watermark.** An empty state file started watching from *now*
  rather than replaying up to 50 historical mentions; `--backfill` was the
  explicit opt-in to reach backwards. The watermark was never advanced past a
  comment that failed to handle (timeout, leak gate, post error) — doing so
  hides it forever, because the next poll's `since` filter drops it server-side
  and it is not in the handled set, so nothing ever retries it.
- **Distinct trigger.** `@grok-local`, not `@grok`, so the local path could not
  double-reply alongside the workflow.

## Tradeoff accepted by going fully-CI

The local path bought two things that are now given up deliberately:

- **Actions minutes.** Every Grok invocation now burns GitHub Actions minutes,
  and Grok availability depends on the CI path being healthy.
- **Insurance.** The local path was partly a hedge against xAI not shipping a
  first-party GitHub action. That hedge is gone; if the CI path breaks, Grok
  review is unavailable until it is fixed.

Both were judged worth it against removing an unbounded-egress credential holder
from the operator's laptop.
