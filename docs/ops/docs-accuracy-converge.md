# Docs accuracy converge workflow

Nested multi-agent Grok Build workflow that fans out **outer audit lanes**, each with nested judgment, then **loom → wright → Cassandra ping/pong**, optional bare `/grok-review`. **Never merges.**

**Truth policy:** [docs-truth-policy.md](docs-truth-policy.md) — honesty *now*
without erasing **next PR goals**. Code is truth for *present-tense* claims;
overclaims that describe wanted capabilities become `goal_next_pr` /
`honesty_partial`, not silent cuts.

## Files

| Path | Role |
|------|------|
| `.grok/workflows/docs-accuracy-converge.rhai` | Project workflow |
| `~/.grok/workflows/docs-accuracy-converge.rhai` | User copy for global discovery |
| `docs/ops/agent-roster.md` | **Adaptive** model min/max roster |
| `docs/ops/docs-truth-policy.md` | Dual-track honesty + goal preservation |
| `docs/ops/disposition-expiry-policy.md` | 60-day re-check on accept-with-rationale dispositions — required for risk-accepting `goal_next_pr` / `honesty_partial`; the converge workflow binds `risk_acceptance`/`re_check_issue` in-schema (both gap arrays) and emits the `Re-checks required` section (#354); the issue itself is still filed by wright/operator |

## Adaptive model roster

Defaults (v1): **`grok-build`** for audit/wright/thread/app_owner; **`grok-4.5`** for loom + cassandra.

- One-off override: `args.roster = { "audit": "grok-4", "wright": "grok-4.5", ... }`
- After a real run: edit `agent-roster.md` + sync defaults in the `.rhai` if the evidence says so
- Report includes `roster=...` for learning
- Effort is **prompt guidance** until the host supports per-agent effort pins

## What it does

1. **Discipline** — Minni `thread_create` + activate (skippable).
2. **AuditFanout** — 6 parallel **explore** lanes (stamps, deny matrix, AFM/install, fleet/cursor, team rename, runtimes/release).
3. **Loom** — dedupe/rank; split **honesty_now** vs **goal_next_pr** lists.
4. **Implement** — **wright** applies honesty (+ scoped `implement_now` only);
   goals preserved in report / plan slices, not deleted. REQUIRED PROCESS:
   for any disposition that accepts a risk (not pure ambition), wright or
   the operator must file the dated `re-check` issue per
   [disposition-expiry-policy.md](disposition-expiry-policy.md). #354 wired
   the schema binding, the `Re-checks required` report section, and wright
   as the primary filer (it checks `gh issue list --label re-check
   --state open` first, reuses a live matching issue, creates only when
   none matches, and returns `re_checks_filed`); the operator's gate is clearing every row
   still marked PROPOSED or MISSING before merge — those rows are the
   failure signal, not noise.
5. **Cassandra** — GREENLIGHT = no false **present-tense** operator claims
   (goals remaining is OK).
6. **AppOptional** — if `grok_review=true` and PR exists, **one** bare `/grok-review`.
7. **Report** — emitted today by the `.rhai` builder: **Goals preserved for
   next PRs**, **Re-checks required** (risk-accepting entries from both loom
   arrays, filed refs marked distinctly from PROPOSED titles), plus loom
   summary / roster / operator next — do not re-author them except to
   correct loom/wright output. Appended manually, here and on every other
   report path: **Honesty shipped** (summarize the honesty edits wright
   applied in step 4) and the confirmation that PROPOSED re-checks were
   actually filed; state N/A explicitly when none — machine silence is not
   N/A.

## Run

From Grok Build (session in minni repo or any cwd that can load the workflow by path/name):

```text
/workflow docs-accuracy-converge
```

Or with args:

```json
{
  "skip_minni_thread": false,
  "open_pr": true,
  "grok_review": true,
  "max_cass_rounds": 4,
  "ghrepo": "infektyd/minni",
  "base": "origin/main"
}
```

Smoke check only:

```text
workflow tool: validate_only + script_path .grok/workflows/docs-accuracy-converge.rhai
```

Budget: plan for ~40–80 agent slots on a full run (`agent_budget` 96–128 is safe).

## Discipline scars (do not relearn the hard way)

- **Bare** `/grok-review` only (exact body). Prose comments skip or cancel.
- **One** re-request owner; concurrency group cancels in-flight App runs.
- **Freeze tip** before App command.
- Stamp PRs that touch `pyproject.toml` → mechanical PATH_DENY → **human/admin merge**, not thrash.
- Marketplace must stamp with pyproject (CI `check-versions`).

## Related

- `~/.grok/workflows/pr-shepherd.rhai` — full dual-lane PR babysit (CI + App + Cassandra + gate).
- `~/.grok/workflows/cassandra-pong.rhai` — Cassandra-only ping/pong on one PR.
- Friction log: `scratchpad/friction/GROK-REVIEW-APP-FRICTION.md` (session artifacts).
