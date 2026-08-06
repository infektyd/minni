# Docs ↔ code truth policy (preserve next PR fuel)

**Problem this solves:** If accuracy work always treats “code is truth” as
“**delete every overclaim**,” we get honest docs *and* erase the **goal signal**
that should have become the next PR. Cutting the claim is only half a
decision.

## Dual track (required)

Every mismatch is classified **twice**:

| Track | Question | Output |
|-------|----------|--------|
| **A — Honesty now** | What may operator docs assert *today* without lying? | Doc edit: cut, mark `PARTIAL` / provisional, or add underclaim |
| **B — Intent / goal** | Was the false claim describing a **wanted capability**? | If yes: **keep a durable next-PR goal** (issue, plan slice, or `## Goals` in the report) — do **not** only delete the sentence |

Silent erase of ambition is a process defect.

## Disposition enum (for audits / loom / wright)

| `disposition` | Meaning | This PR may… | Must also… |
|---------------|---------|--------------|------------|
| `honesty_cut` | Claim was wrong and **not** a goal | Rewrite/cut docs | Optional: note “not pursuing” |
| `honesty_partial` | Goal exists; ship is incomplete | Mark PARTIAL / provisional + what works | Open or list **goal_next_pr**; residuals that accept a risk also need the 60-day re-check ([disposition-expiry-policy.md](disposition-expiry-policy.md)) |
| `implement_now` | Clear defect vs an **accepted** contract (hooks matrix, security must) | Fix **code** (+ tighten docs) | Stay scoped; no drive-by features |
| `goal_next_pr` | Wanted capability; not this PR’s job | Minimal honesty so docs don’t overclaim | Write goal title + acceptance sketch; if the gap being carried is an **accepted risk** (not pure ambition), file the 60-day re-check per [disposition-expiry-policy.md](disposition-expiry-policy.md) |
| `underclaim_add` | Code has feature docs omit | Add docs with anchors | — |
| `ops_fleet` | Install/fleet lag, not prose | Point at the fix command (`minni sync`, etc.) | — |

Default for “doc says X, code doesn’t”: prefer **`honesty_partial` + `goal_next_pr`**
over bare **`honesty_cut`**, unless the claim is actively dangerous (security
overclaim, false privacy guarantee) — then cut hard *and* still file the goal
if the *intent* was legitimate.

## What “code is source of truth” still means

- Operators must not be told a lie about **current** behavior.
- Cassandra GREENLIGHT = no residual **false present-tense claims** on operator
  paths — **not** “no ambition left in the tree.”
- Goals may live in: GitHub issues, Minni plan slices, PR body “Follow-ups”,
  or `docs/ops/*` goal tables — not only in deleted git history.

## Anti-patterns

| Anti-pattern | Why it hurts |
|--------------|--------------|
| Accuracy PR only deletes overclaims | Next PR queue starves |
| Accuracy PR implements every ambition | Scope explosion; no honesty-only land |
| “PARTIAL” without a goal pointer | PARTIAL becomes permanent fog |
| Inventing behavior in docs to “match a goal” | Same as lying |

## Workflow binding

`docs-accuracy-converge` must:

1. Tag each gap with `disposition` (and `goal_title` when disposition is
   `goal_next_pr` or `honesty_partial`), plus `risk_acceptance` and
   `re_check_issue` on every gap (#354 — schema-required in the converge
   workflow).
2. Wright: apply **honesty now** edits; for `implement_now` only when High and
   scoped; never drop goals on the floor. For risk-accepting residuals
   (any disposition leaving an accepted risk — `goal_next_pr` /
   `honesty_partial` carrying one, or a scoped `implement_now` with a known
   residual), wright or
   the operator files the dated `re-check` issue per
   [disposition-expiry-policy.md](disposition-expiry-policy.md). The converge
   workflow binds the reference in-schema (`risk_acceptance`/`re_check_issue`,
   both gap arrays, #354), instructs wright to file proposed titles during
   implement, and emits a Re-checks required section that marks filed refs
   (`#NNN`) apart from PROPOSED titles. Emission may precede filing — a
   PROPOSED row is the owed-work signal, not a process bug. Nothing in the
   workflow or CI enforces this: clearing every PROPOSED row (or confirming
   wright's `#NNN` in Follow-ups) before merging is the OPERATOR's gate, and
   N/A is claimable only when the section says none.
3. Report: sections **Honesty shipped**,
   **Goals preserved for next PRs**, and **Re-checks required**. The `.rhai`
   builder emits Goals preserved and Re-checks required (#354); **Honesty
   shipped** it does not — wright/operator appends it to the scratch report
   and/or the PR-body Follow-ups after the run, manual for other report
   paths. Any PROPOSED entry in Re-checks required means filing is still
   owed. For the manual section machine silence is not N/A; state N/A
   explicitly when none.

See also: personal Grok App playbook in [grok-reviewer-app.md](grok-reviewer-app.md)
(“Assumptions that became next PR goals”).
