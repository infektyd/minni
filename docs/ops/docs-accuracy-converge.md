# Docs accuracy converge workflow

Nested multi-agent Grok Build workflow that fans out **outer audit lanes**, each with nested judgment, then **loom → wright → Cassandra ping/pong**, optional bare `/grok-review`. **Never merges.**

## Files

| Path | Role |
|------|------|
| `.grok/workflows/docs-accuracy-converge.rhai` | Project workflow (source of truth in repo) |
| `~/.grok/workflows/docs-accuracy-converge.rhai` | User copy for global discovery |
| `docs/ops/agent-roster.md` | **Adaptive** model min/max roster (update after runs) |

## Adaptive model roster

Defaults (v1): **`grok-build`** for audit/wright/thread/app_owner; **`grok-4.5`** for loom + cassandra.

- One-off override: `args.roster = { "audit": "grok-4", "wright": "grok-4.5", ... }`
- After a real run: edit `agent-roster.md` + sync defaults in the `.rhai` if the evidence says so
- Report includes `roster=...` for learning
- Effort is **prompt guidance** until the host supports per-agent effort pins

## What it does

1. **Discipline** — Minni `thread_create` + activate (skippable).
2. **AuditFanout** — 6 parallel **explore** lanes (stamps, deny matrix, AFM/install, fleet/cursor, team rename, runtimes/release).
3. **Loom** — dedupe/rank fixes + `rg` banlist.
4. **Implement** — **wright** in isolated worktree; optional PR open.
5. **Cassandra** — ping/pong until `GREENLIGHT` or `max_cass_rounds`.
6. **AppOptional** — if `grok_review=true` and PR exists, **one** owner posts body **exactly** `/grok-review`.
7. **Report** — scratch report; operator land (admin if stamp path-filter).

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
