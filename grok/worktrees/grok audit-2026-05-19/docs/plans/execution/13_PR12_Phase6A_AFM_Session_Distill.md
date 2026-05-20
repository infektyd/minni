# PR-12: Phase 6 PR-A — AFM Scheduler + Session Distillation

> **Scope:** AFM compilation scheduler, session distillation pass, prompt contracts, lifecycle gating, single-writer queue, observability. The minimum end-to-end self-organizing loop.
>
> **Depends on:** PR-11 (hygiene, health endpoint, trace).
>
> **Behavior change:** Opt-in AFM loop. When enabled and idle, auto-drafts session/entity/concept pages. Never auto-accepts. Kill switch: `SOVEREIGN_AFM_LOOP=off`.

---

## 6.2 — Scheduling and Triggers

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_scheduler.py` | Idle scheduler: part of daemon. Uses `last_activity_ts` check. When idle ≥300s with no active long-running ops, picks most-overdue pass and runs it. |
| **CREATE** | `engine/afm_writer.py` | Single-writer queue: dedicated background thread. Drains proposal queue, acquires per-page lock, applies `assessLearningQuality` + contradiction detection, then writes. Eliminates concurrent write corruption. |
| **MODIFY** | `engine/config.py` | Add `afm_loop_schedule` dict with per-pass intervals. Add `SOVEREIGN_AFM_LOOP` env var check. |
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.compile(pass_name, vault_path?, dry_run=True)`. Default `dry_run=True`. CLI: `python -m engine.sovereign_memory compile --pass session_distillation --dry-run`. |
| **MODIFY** | `plugins/sovereign-memory/src/server.ts` | New MCP tool: `sovereign_compile_vault`. |

### Config

```python
afm_loop_schedule = {
    "session_distillation": "1h",
    "synthesis": "24h",
    "procedure_extraction": "24h",
    "reorganization": "7d",
    "pruning": "24h",
}
```

### Kill Switch: `SOVEREIGN_AFM_LOOP=off` disables scheduler entirely. Per-call AFM uses (prepare_task/prepare_outcome) unaffected.

---

## 6.1.a — Session Distillation Pass

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_passes/__init__.py` | Package init. |
| **CREATE** | `engine/afm_passes/session_distillation.py` | Reads recent `raw/` ingest + episodic events (configurable window, default 24h). Drafts: new `session` pages, new `entity` pages for unrecognized systems/repos, new `concept` pages for repeatedly-referenced ideas. |

### Outputs

All drafts emitted as `status: draft`, `agent: afm-loop`, `trace_id: <pass_run_id>`, `sources: [...]`.

---

## 6.3 — Per-Pass Prompt Contracts

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_prompts/session_distillation.md` | Frozen, versioned prompt template. States goal, input slots (JSON), output schema (drafts list with frontmatter + body + sources + confidence). Refuses to emit without source citations. |

### Prompt Template Structure

1. Goal in one sentence.
2. Input slots as structured JSON.
3. Output schema: drafts list with frontmatter + body + sources + confidence.
4. Hard rule: no drafts without source citations.

Versioned in-tree so prompt drift is reviewable. Trace records prompt version used.

---

## 6.4 — Output Handling and Lifecycle Gating

Every AFM draft:

1. Goes through `assessLearningQuality` + contradiction detection (Phase 3.3).
2. Lands as vault page: `status: draft`, `agent: afm-loop`, `trace_id`, `sources`.
3. Announced via inbox: `inbox/afm-drafts-YYYY-MM-DD.json` with draft wikilinks.
4. Awaits explicit endorsement: `daemon.endorse(page_id, decision="accept"|"reject"|"edit")`.
5. Auto-expires from `draft` after 14 days if never endorsed.

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.endorse(page_id, decision)`. Audited. |

---

## 6.5 — Observability

- **Per-run audit entry** in `log.md` and daily log: `## [timestamp] afm_loop_<pass> | <summary>` with trace_id, draft count, AFM latency.
- **Per-draft trace** via Phase 4.2 trace endpoint: full input → prompt → output.
- **Health endpoint** gains: `afm_loop: {last_run_per_pass, drafts_pending, drafts_pending_oldest, afm_latency_p95}`.

---

## PR-12 Verification

1. **Dry-run sanity:** `python -m engine.sovereign_memory compile --pass session_distillation --dry-run` returns N drafts, no writes, no state change.
2. **Wet-run gating:** Without `--dry-run`: drafts land as `status: draft`, inbox file written, audit shows `afm_loop_*` entries, no `accepted` pages without endorsement.
3. **Endorsement round-trip:** `daemon.endorse(page_id, decision="accept")`: page transitions, audit recorded, draft removed from inbox.
4. **Degraded safety:** AFM bridge stopped → scheduler skips cleanly, stats show `afm_loop_status: "afm_unavailable"`, vault unchanged.
5. **Quality gate:** Contradicting draft blocked by contradiction detection, conflict surfaced.
6. **Observability:** Every draft visible in `daemon.trace(trace_id)` with full prompt, inputs, response.

---

## PR-12 Completion Checklist

- [ ] `engine/afm_scheduler.py` with idle-driven scheduling
- [ ] `engine/afm_writer.py` single-writer queue with per-page locking
- [ ] `engine/afm_passes/session_distillation.py` drafts session/entity/concept pages
- [ ] `engine/afm_prompts/session_distillation.md` frozen prompt template
- [ ] `daemon.compile()` with `dry_run=True` default
- [ ] `daemon.endorse()` with auditing
- [ ] `sovereign_compile_vault` MCP tool registered
- [ ] `SOVEREIGN_AFM_LOOP=off` kill switch works
- [ ] Lifecycle gating: no auto-accept, 14-day auto-expire
- [ ] Observability: audit entries, trace, health endpoint updated
- [ ] All existing tests pass

---

## Next Steps

→ [14_PR13_Phase6B_Synthesis_Procedures.md](./14_PR13_Phase6B_Synthesis_Procedures.md)
