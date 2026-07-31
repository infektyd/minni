# `/minni:plan` — first-class durable planning + layer-1 continuity (design note)

> Origin (Hans, 2026-06-04): make Minni more *part of* the agent via a first-class `/minni:plan` that
> (A) survives crash/compaction (plan lives on disk, re-hydrates), and (B) keeps the layer-1 identity
> shelf *reachable* across compactions WITHOUT force-injecting the ~4k-token shelf — the plan carries a
> costed, drift-aware, pull-on-demand handle to layer-1.
> Researched + grounded 2026-06-04 (SOTA survey + Minni file:line). Design sketch, NOT built. Greenfield:
> `/minni:plan` does not exist today; only the dormant `intent==="plan"` classifier (task.ts:217).

## Two sub-goals, kept separate
- **(A) Plan durability** — the plan/notes file IS durable state; context is disposable. Agent re-grounds
  from the file after a reset. (The easy, high-value half.)
- **(B) Layer-1 continuity** — keep the identity shelf reachable across compaction as a *deferred resource*
  (known by name + cost, pulled on demand), never force-loaded.

## 5 SOTA patterns worth stealing
1. **Structured note-taking re-consulted after reset** (Anthropic context-engineering, Sept 2025): the
   plan file is the durable spine; agents re-consult NOTES.md after context resets. → sub-goal A spine.
2. **Deferred / lazy tool loading** (Anthropic late-2025; opencode ToolSearch; TanStack): tools known by
   name+cost, schema pulled on demand only when used (50 tools ≈ 77k tokens/turn otherwise). → THE exact
   ergonomic for sub-goal B. This is also how the current claude-code session loads its own tools.
3. **Temporal-style append-only event journal + replay**: immutable mutation log; crash → replay skips
   completed steps. → tamper/truncation-resistant plan state.
4. **HTN decomposition + verifier gates + re-plan-on-failure**: compound goal → primitive steps each with
   a checkable postcondition; failure mutates the plan, doesn't abort it.
5. **Letta/MemGPT agent-chosen paging**: identity-class context paged in BY AGENT CHOICE via tool call,
   not blindly stuffed. → precedent for "pull layer-1 only when warranted."

## Recommended design

### Data model
- **`PlanArtifact`** (markdown note + YAML frontmatter): `goal`, `plan_id`, `status`, `constraints`,
  `slices[]`, `open_questions`, `scar_tissue`, `next_action` pointer, `shelf_ref`, `plan_digest`.
- **`PlanSlice`**: `id`, `title`, `status` (pending/in_progress/done/blocked/superseded), `gate`
  (verification postcondition), `depends_on[]`, `evidence`, `superseded_by`.
- **Append-only event log** `plan-<id>.log.md` (Temporal-style: status_changed / replan / gate_passed /
  shelf_pulled / rehydrated) — replayable ground truth even if the in-context view is stale.

### Persistence (zero schema migration)
- `writeVaultPage({ section:"artifacts", frontmatter:{ minni_plan:true, plan_id } })` (vault.ts:691).
  `artifacts` already in VaultSection/VAULT_DIRS (vault.ts:17-26, :116-132). No new DB table — the daemon
  already indexes vault notes.
- Crash/compaction hardening: canonical note + journal + `plan_digest` (sha256 of slices+status) +
  **flush on EVERY status change** (LangGraph `sync`-style, not exit-style — "checkpoints aren't durable").

### Lifecycle
create (draft, proposal-first) → update slice status as gates pass → rehydrate after compaction *from the
plan note, not the transcript* → complete/handoff. Re-plan = preserve-superset (`superseded` +
`superseded_by`, never delete).

### Re-injection tie-in
Add one `active_plan` key to the envelope keyOrder (agent_envelope.ts:56-66); the UserPromptSubmit +
SessionStart hooks (hook.ts:92-248) inject only the COMPACT plan view (goal + next_action + pending slices
+ scar/open-questions + shelf handle), bounded by envelopeBudgetFor (agent_envelope.ts:83).
NOTE: PreCompact can't inject; SessionStart is the continuity path.

### Layer-1 reference (sub-goal B) = deferred resource with a PULL CONTRACT
Designed against two failure modes: **FM1 force-inject** (wastes ~4k every plan-continue + gets compacted
out) and **FM2 dead pointer** (no incentive → agent never pulls → layer-1 dies). Three mechanisms:
1. **Cost-visible handle** — renders as `layer-1 shelf · claude-code · v7 · ~4.1k tokens · [[wikilink]] ·
   pull: cli.js read claude-code`. Informed cost/benefit, never a blind fetch.
2. **Drift = trigger** — store `shelf_hash = sha256(shelfContent).slice(0,16)` (reuse team.ts:235 pattern;
   `stableStringify` for multi-part shelves). On rehydration compare stored vs LIVE hash:
   **MATCH → run on plan alone, zero pull, zero 4k**; **DIFFER → drift IS the reason to re-pull** (targeted,
   re-stamp, log `shelf_pulled`). The shelf emits no `version:` today (propagate.py:643-687), so version is
   DERIVED from the content hash.
3. **Division-of-labor standing incentive** — plan = task-local context (always cheap, always injected);
   shelf = identity/standing context. Contract: *"reach for the shelf only when drift fired OR a decision
   depends on identity-level facts the plan doesn't carry."*

### Reuse vs build (mostly composition)
REUSE: `prepareTask()` + `intent==="plan"` (task.ts:217,646), `constraintsForTask` (:227),
`extractScarTissue` (:773), `HandoffPacket` fields (:807), `prepareOutcome` (:854), `writeVaultPage`/
`PageStatus` (vault.ts:691,:41), `wrapEnvelope`/`stableStringify` (agent_envelope.ts:54), sha256 pattern
(team.ts:235), `privacyForSource` leak gate (task.ts:268-283).
BUILD NEW: one `src/plan.ts` (types + ~6-8 small fns), 4 MCP tools, one `commands/plan.md`, ~10 lines hook
wiring.

### Tool/command surface (mirrors minni:prepare-task)
`minni_plan_create`, `minni_plan_update`, `minni_plan_status` (also reports shelf drift in the same call),
`minni_plan_replan` — registered via server.registerTool + zod shape (server.ts:64-116).

### Risks / guardrails
plan-drift → gates + evidence + journal; stale-shelf → drift hash; over-injection (FM1) → compact-view-only;
dead-pointer (FM2) → pull contract; confabulated plans → dreams-propose/waking-endorses (`done` requires
recorded evidence); silent step loss → preserve-superset; leakage → reuse privacyForSource gate.

## Open decisions for Hans
1. Plan storage location: vault `artifacts/` note (recommended, zero migration) vs a dedicated dir.
2. Auto-create on `intent==="plan"`, or explicit `/minni:plan` only? (Recommend explicit; proposal-first.)
3. Shelf-drift behavior: auto-pull on drift, or just *surface* "shelf drifted, pull recommended" and let
   the agent decide? (Recommend surface-only — preserves the voluntary-pull contract.)
4. Does this supersede/merge with the existing `buildHandoffPacket` (heavy overlap), or sit beside it?
