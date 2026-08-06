# Accept-with-rationale dispositions expire: the 60-day re-check policy

**Status:** operator-approved 2026-08-05, at the close of the audit-remediation
campaign (R13). Tracking issue: #345.

## The rule

Any finding that is **accepted with a rationale** — dispositioned as
"won't fix / fix later / exempt, because X" rather than fixed — must record a
**re-check due 60 days from the acceptance date**, at accept time, as a
**dated GitHub issue labeled `re-check`** whose title carries the due date
("Re-check by YYYY-MM-DD: …") and whose body cites the original disposition
and the evidence that justified acceptance.

The issue is the **system of record**. A plan slice may point at the issue
(and should, when a campaign is active), but must never be the only copy:
plans complete, get superseded, and get replaced — which is exactly how the
June backlog vanished. If a plan carrying open re-check slices is closed or
superseded, each open slice becomes a dated issue **before** the plan dies.

The re-check verifies the rationale still holds against **current** code and
measurements — not against the state of the world when it was written.
Allowed outcomes:

1. **Re-accept** — the rationale survives re-measurement; record a fresh
   60-day re-check.
2. **Fix** — the rationale no longer holds (or never did); the item goes back
   to work.
3. **Escalate** — the situation changed shape; re-triage from scratch.

A re-check that silently lapses is itself a finding — and lapses must be
discoverable, not aspirational:

- **Query:** `gh issue list --label re-check` (open = outstanding; a due date
  in the past on an open issue = lapsed).
- **Owner and cadence:** the operator (or an agent acting on the operator's
  standing instructions) runs the query at every campaign close-out and at
  least monthly between campaigns; any lapsed re-check gets raised as a
  finding in the next session, not silently re-dated.

## Why (the evidence)

Two measurements from the 2026-08 campaign, both proven rather than argued:

- Both 2026-06-10 accept-with-rationale items were **wrong or stale** when
  re-checked on 2026-08-01. One rationale rested on a mechanism
  (a background TTL reaper in `agent_ping`) that did not exist; the correct
  fix was a different, smaller change than the one the rationale deferred.
- The un-re-checked "accepted backlog" class produced the entire
  R12 slice: eight findings dispositioned "backlog" in June were never filed,
  never re-read, and all eight needed real fixes when finally examined —
  a deferred-twice cluster that a dated re-check would have surfaced in July.

The pattern: an acceptance rationale is a claim about the code at one moment,
and nothing in the process ever forced a second look. Fixes are re-verified by
tests forever; acceptances were verified exactly once. This policy gives
acceptances the same property fixes already have — a mechanism that
re-asserts them or catches their decay.

## What counts as accept-with-rationale

- Issue closed as "working as intended, because …"
- Issue left open with a written disposition ("carry forward: …")
- In-code exemption markers — the marker's justification is the rationale.
  Concrete instance: `SEC-G9-EXEMPT` in `.github/workflows/claude.yml`
  (landed in #314). Note for anyone inventorying markers: `rg` skips hidden
  directories like `.github/` by default — use `rg --hidden` or `grep -r`
- Plan-slice evidence that accepts a residual ("known gap, tolerable
  because …")

Not covered:

- items actually fixed — their tests are the re-check;
- log-only observations that dispositioned nothing;
- **permanent non-goals** recorded as accepted scope in
  [`../contracts/THREAT_MODEL.md`](../contracts/THREAT_MODEL.md) §7 — the
  same-uid owner being inside the boundary, no multi-tenant hardening,
  `gate.shared` not being an ACL. Perpetual 60-day churn on explicit
  non-goals is noise, not vigilance. These re-enter scope only when the
  residual's *boundary* changes (new tenant class, new privilege boundary).
  **This exemption is item-by-item, never section-by-section**: §7 also
  contains *deferred* work (e.g. SEC-021 cryptographic agent
  authentication), and a deferral is a risk acceptance that goes stale on
  its own — deferred items take the 60-day re-check until fixed, or until a
  fresh operator decision reclassifies them as a permanent non-goal;
- `goal_next_pr` items that are pure feature ambition with no risk
  acceptance — goals are tracked by the truth policy's own mechanism. A
  `goal_next_pr` that *papers over an accepted risk*, and any
  `honesty_partial` whose residual is a risk acceptance, DOES need the
  re-check.

## Bookkeeping

- The re-check date is `acceptance date + 60 days`, rounded to a real date in
  the filing.
- One re-check may cover several dispositions accepted together, but must
  list each one it covers.
- When a re-check issue closes, its outcome (re-accept / fix / escalate) and
  the evidence go in the closing comment, and a re-accept files the next
  dated re-check before closing.

## Backfill at adoption

Filed 2026-08-05, due 2026-10-04, covering the four dispositions carried
forward at campaign close: #346 (for #227), #347 (for #311), #348 (for #316),
#349 (for #317).
