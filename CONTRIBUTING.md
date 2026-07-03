# Contributing to Minni

Minni is pre-v1. The workflow below is what the repo actually enforces today
(hooks, CI, Makefile) — not aspirational process.

## Requirements

- Python 3.14+ (pinned in `.python-version`). The engine venv is built for you
  by `make setup` using your system `python3`; you don't manage it by hand.
- Node >=20 (pinned in `.nvmrc`), for the TypeScript plugin under
  `plugins/minni/`.

## Setup

```bash
make setup
```

This builds `.venv` and installs engine dependencies, runs `npm ci` in
`plugins/minni/`, and sets `git config core.hooksPath .githooks` so the local
hooks are active for this clone. See `.githooks/README.md` for what the hooks
do and the `MINNI_SKIP_HOOKS` escape hatch.

## The commands you'll actually run

```bash
make check   # fast gate: lint + typecheck + plugin build/test + scoped engine pytest
make test    # full suites (engine pytest + plugin test) — heavier, loads embedding/FAISS models
make smoke   # hermetic daemon smoke (scripts/repro-smoke.sh), isolated MINNI_HOME
```

`make check` is what the pre-push hook runs, and it's the fast gate CI expects
to pass before review. Run it before you push. `make test` runs the full
engine and plugin suites and is slower. `make smoke` runs the daemon in an
isolated `MINNI_HOME` and asserts no pollution of your real `~/.minni`.

See the Makefile (`make help`) for the full target list, including `lint`,
`typecheck`, `build`, `coverage`, and `daemon`.

## Pull requests

- **One concern per PR.** Keep the diff scoped to a single fix, feature, or
  refactor so it's reviewable.
- **No scratch or throwaway files in the diff.** The `pr-hygiene` CI workflow
  (`.github/workflows/pr-hygiene.yml`) rejects PRs that add:
  - PR-description scratch files (`pr_description.*`, `PR_DESCRIPTION.*`) —
    put that content in the actual GitHub PR body instead.
  - Generic scratch/temp/backup files (`*.scratch.*`, `*.tmp.*`, `*.bak`,
    trailing `~`).
  - OS junk (`.DS_Store`, `Thumbs.db`).
  - Any `.env*` file except `.env.example`, `.env.sample`, or `.env.template`
    — `.env` files hold secrets and must never be committed.
  - Top-level scratch notes (`notes.*`, `TODO.*`, `scratch.*`).

  If your branch trips this check, `git rm` the offending path, commit, and
  push again — the check reruns automatically.

## The memory firewall

Changes that touch memory storage, retrieval, scoring, or governance need a
maintainer discussion **before** you write code. Open an issue describing the
problem and proposed approach first. That surface spans both languages:

- **Engine (`src/minni/`)** — anything that affects how memories are written,
  ranked, retrieved, or authorized: `src/minni/db.py`, `src/minni/retrieval.py`,
  `src/minni/principal.py`, the AFM passes, the candidate/learning lifecycle.
- **Plugin security paths (`plugins/minni/src/`)** — the recall/privacy and
  model-facing-context gates enforced plugin-side: privacy gating and evidence
  fencing of vault content (e.g. the SEC-006/SEC-010 gates in `task.ts`),
  recall-guard and hook context injection, and AFM context preparation.
  Touching what reaches a model or bypasses a privacy gate is firewall
  territory even though it lives in TypeScript.

This surface is covered by `SECURITY_PLAN.md` and has correctness/security
properties that are easy to break silently; a PR that changes this behavior
without prior discussion is likely to be asked to start over as an issue.

Everything else — docs, plugin UI/tooling that doesn't gate model-facing
content, tests, packaging — doesn't need pre-approval; just open the PR.

## Commit style

This repo uses conventional-commit-style prefixes, observed in the git log:

```
feat(scope): ...
fix(scope): ...
chore: ...
test(scope): ...
docs: ...
security(scope): ...
```

The `(scope)` is optional but helpful when the change is localized (e.g.
`fix(engine): ...`, `feat(plugin): ...`). Write the subject line in the
imperative, present tense, and keep it short — put the "why" in the body if
it's not obvious.

## Code ownership

See `.github/CODEOWNERS`. Everything currently routes through @infektyd for
review.
