# Adaptive agent model roster

**Purpose:** Min/max model (and effort *guidance*) for multi-agent workflows.  
**Rule:** Defaults live here + in workflow scripts. **Real runs update the table** — not theory.

Change process:

1. Run a workflow (e.g. `docs-accuracy-converge`).
2. Note in `/workflows` report: which lane was weak (missed High / wasted spend / thrash).
3. Edit **Defaults** below and the matching map in `.grok/workflows/*.rhai`.
4. Optional one-off: pass `args.roster` without editing files.
5. Log a one-liner under **Changelog**.

## Available models (this host)

| Slug | Role fit |
|------|----------|
| `grok-build` | Breadth, mechanical, App command owner, routine wright |
| `grok-4` | Mid implement / second opinion |
| `grok-4.5` | Loom, Cassandra, parent conductor, hard Highs |

Session can also set `/effort low|medium|high|xhigh` on the **parent**; workflow API pins **`model`** today. Effort in the table is **guidance** baked into prompts until the host exposes per-agent effort.

## Defaults (v1 — post #271 friction + min/max design)

| Role key | Model | Effort guidance | Why |
|----------|--------|-----------------|-----|
| `thread` | inherit / `grok-build` | low | Minni bookkeeping |
| `audit` | `grok-build` | low–medium | Wide fan-out, code-anchored greps |
| `loom` | `grok-4.5` | high | Rank/dedupe conflicts |
| `wright` | `grok-build` | medium | Apply plan; escalate via cass if fails |
| `wright_high` | `grok-4.5` | high | Reserved for future auto-escalate |
| `cassandra` | `grok-4.5` | high–xhigh | Fail-closed refute |
| `app_owner` | `grok-build` | low | Bare `/grok-review` only |

## Override (any run)

```json
{
  "roster": {
    "audit": "grok-build",
    "loom": "grok-4.5",
    "wright": "grok-build",
    "cassandra": "grok-4.5",
    "thread": "grok-build",
    "app_owner": "grok-build"
  }
}
```

Omit keys to keep defaults.

## Adaptive signals (when to change)

| Signal | Adjustment |
|--------|------------|
| Audit misses Highs that cass catches | Bump `audit` → `grok-4` or medium effort guidance |
| Audit is clean but expensive | Keep/cheapen `audit`; confirm with banlist scripts |
| Wright rework rounds > 2 | Bump `wright` → `grok-4.5` for next run |
| Cass false RED thrash | Soften cass prompt / lower rounds; don't cheapen cass first |
| App cancel/skip storm | Process, not model — one owner, bare command |
| Stamp-only failures | Prefer scripts (`check_versions`), not smarter models |

## Changelog

| Date | Change | Evidence |
|------|--------|----------|
| 2026-08-04 | v1 defaults: build width, 4.5 loom+cass | #271 friction + min/max design; no Composer in this host |
