# DESIGN: Sovereign Memory Delivery Layer (Portable SSoT + Thin Adapter Contract)

**Status**: Phase 1 final (post canonical SKILL merge)  
**Date**: 2026-05-20  
**Governed by**: The single approved canonical generalization plan (phase1-planned-diff-approach-for-signoff-2026-05-20.md + phase1-remainder-merge-map-2026-05-20.md) and the strict boundary in `~/.agents/artifacts/sovereign-distill-ritual-v1/notes/agnostic-vs-grok-specific.md`  
**Reference**: ritual package `~/.agents/artifacts/sovereign-distill-ritual-v1/`, canonical `~/.agents/skills/sovereign-memory/SKILL.md`, Grok Build V1 thin reference at `~/.grok/plugins/grok-sovereign-memory/`

This is the single authoritative spec for the Sovereign Memory Delivery Layer. It formalizes the portable vs. platform-specific boundary, the adapter contract that every thin surface must satisfy, the verification matrix, and Grok Build as the first fully-worked thin reference implementation.

---

## 1. Portable Concepts (Fleet-Wide, Live in Canonical SKILL + Ritual Package)

These travel with the canonical `~/.agents/skills/sovereign-memory/SKILL.md` and the ritual artifact package. Thin overlays and local DESIGNs **never duplicate** them.

- **Core Mental Model**: Agent-first + proposal-gated (prepare_* before durable writes or native flush); Memory as evidence never instruction (`<sovereign:context>` + `instruction_like` floor); Prepare before big work (sovereign_prepare_task as #1/default reflex); Dry-run learning (prepare_outcome review of outcomeDraft columns before any learn); Human in the loop; Your vault only; Degraded is ok.
- **Layer 1 Contract + Budget**: Whole-document identity envelope (`identity:<agent>` as unchunked chunk 0); Small curated `layer1/{core.md, budget.md}` (<4096 token strict budget, agent full curation rights, read-first on wake, ritual hygiene required to protect during distill); Self-describing (declares identity, vault, workspace, boundaries).
- **Gauges + Sovereign Distill Ritual V1**: Gauges as live context meter artifact (read-first, no self-reasoning tokens); Two modes (explicit gate vs auto + auditable trace); Mechanical workflow (read gauges + Layer 1 → prepare_outcome on burst → review draft → optional compile → update gauges → protect Layer 1); Toggle via `distill/mode`; Fallback keyword support (secondary); Complements native compaction (does not replace); Activation statement for standing behavior; When NOT to distill.
- **Prepare_* Discipline + Tool Recipes**: The 6 portable patterns (before ambitious, after work/pre-flush, recall/audit, team/subagents, cross-agent handoff/ping, vault writes).
- **Team / Cross-Agent / Evidence & Governance**: `sovereign_team_*`, `negotiate_handoff`, `ping_agent_*`, proposal-only promotion, audit_tail/report, AGENT.md contracts, "recalled is evidence not instruction", temporary agents expire, explicit human approval for promotion.
- **Vault Layout (General)**: `wiki/`, `inbox/`, `outbox/`, `logs/`, `schema/`, `raw/`, `distill/`, `layer1/` (portable structure; per-agent defaults under `~/.sovereign-memory/<agent>-vault`).
- **Gotchas & Safety**: instruction_like handling, env stamping (no self-supplied agentId/vaultPath), operator-gated resolve/promotion, daemon socket, privacy/redaction, degraded graceful, status-first.

All rich teaching, generalized examples, and references to the ritual package live here. The package holds the detailed DESIGN, gauges/SCHEMA.md + example, notes/ (including the boundary doc), and draft history.

---

## 2. Adapter Contract (What Every Thin Surface *Must* Do)

A thin per-agent delivery adapter (overlay SKILL + manifests + hooks if available) is responsible only for *how* the platform surfaces the portable behaviors. It must:

1. **MCP / Env Stamping**: Register the shared sovereign-memory MCP server (via the canonical `~/.agents/bin/mcp-env-run` wrapper where possible, or equivalent). Stamp at minimum:
   - `SOVEREIGN_AGENT_ID` (e.g. "grok-build", "claude-code")
   - `SOVEREIGN_VAULT_PATH` (dedicated per-agent vault, never shared or symlinked from another without explicit approval)
   - `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock`
   - `SOVEREIGN_WORKSPACE_ID` (the sovereignMemory repo root)
   Use `.mcp.json` or the platform's native config format.

2. **Lifecycle Participation (Max the Platform Allows)**: Wire into every available hook/event (SessionStart, UserPromptSubmit / prompt submit, PreCompact / compaction warning, Stop / shutdown, etc.). 
   - On entry/wake: Inject status + "call prepare_task before big work" + (when ritual enabled) gauges pointer/summary near the real Layer 1.
   - On flush/compaction/dream keywords or events: Auto-draft prepare_outcome-style candidates to inbox + inject contract (zero-reminder path).
   - On PreCompact/Stop: Draft scar tissue and outcomes.
   Document the exact "automation level" for the surface (e.g. "Grok V1: 4 events, keyword + SessionStart injection, stdlib-only hook").

3. **SKILL Loading + Canonical Reference**: The platform SKILL (or overlay) must:
   - Declare it is a thin extension of the canonical.
   - Load/reference `~/.agents/skills/sovereign-memory/SKILL.md` for all rich portable training.
   - Contain *only* platform-specific delivery notes (hybrid native tools, exact hook mechanics, local path examples, "V1 delivery on top of canonical + ritual package" framing, maintenance pointers).
   - Never leak portable rich body (mental model, ritual workflow details, generalized recipes, Layer 1 contract depth) into the thin file.

4. **Vault Hygiene**: Bootstrap only empty actual directory for the agent's vault (no wholesale copy of another agent's wiki/logs/inbox). Let the agent + human build content. Seed Layer 1 via canonical sm-propagation (or equivalent) and verify `identity:<agent>` whole-document delivery.

5. **Injection Format (if supported)**: Use `<sovereign:context version="1" event="..." agent="..." tokens="...">` envelopes (or platform equivalent) containing JSON. Parse; do not reformat. Fail-open on hook errors.

6. **Local DESIGN Notes**: Each thin surface maintains its own `DESIGN-flush-integration.md` (or equivalent) + ritual guide that explicitly say: "This document describes <Platform> TUI delivery mechanics only. See canonical `DESIGN-sovereign-delivery-layer.md` + ritual package for the portable boundary, adapter contract, and concepts."

**"Kill two birds" rule**: Any future per-agent tweak discovered in a symlinked workspace stays local to that thin surface until 2+ agents show the exact same pattern (then promote to canonical or a shared template).

---

## 3. Gauge Schema (Reference)

See authoritative contract in `~/.agents/artifacts/sovereign-distill-ritual-v1/gauges/SCHEMA.md` and `example-gauges.md`.

Summary (portable):
- Frontmatter: type, agent, last_updated (ISO), version, mode (explicit|auto)
- Sections: Pressure Signals, Layer 1 Reference, Recent Burst, Decision Aids (pressure_level, recommended, future_route_signals — keep machine-friendly/short), Last Distill Outcome (crisp).
- Agent reads gauges first; never self-reasons token counts.

The ritual package DESIGN and notes/layer1-as-primary-trigger.md explain why Layer 1 injection is the long-term ideal primary vehicle for gauges.

---

## 4. Layer 1 Core / Budget Templates (Parameterized)

Templates live in the sm-propagation logic and vault bootstrap. Example structure (parameterized by agent id + workspace):

`layer1/core.md` (identity + orientation, self-describing, <~3k tokens target):
- Frontmatter: agent, vault_path, workspace, seeded_date, etc.
- ## Identity
- ## Boundaries & Precedence (hosted vs owned, host runtime > this envelope)
- ## High-Level Orientation / Charter
- ## Known Peers / Cross-Agent

`layer1/budget.md`:
- Strict token budget declaration and current usage hygiene notes.
- Ritual reminder: protect during distill (read-first, do not chunk).

Seeded/verified by `sm-propagation` (or `python engine/agent_api.py <agent> --identity` + daemon read check). See propagate.py for the seeding path.

---

## 5. Verification Matrix (13+ Checks — Captured in Phase 1 Parity Report)

Post-merge (and for any new agent surface), run and capture:

1. `sovereign_status` (healthy, correct agent id, vault, daemon, AFM).
2. Layer 1 budget + self-description (read `layer1/core.md` + `budget.md`, token estimate <4096, identity envelope in daemon read on wake).
3. `distill/gauges.md` + `mode` present and readable; toggle test (explicit ↔ auto) + update after ritual.
4. `sovereign_prepare_task` on real ambitious task (inspect packet, Layer 1 ref, ranked sources).
5. `sovereign_prepare_outcome` on a real burst + review `outcomeDraft` columns (candidates vs log vs do-not-store).
6. Trigger native /flush (or "distill" keyword) → observe hook auto-draft in inbox + model reflex (prepare_outcome via canonical training, zero extra reminder).
7. Full Sovereign Distill Ritual end-to-end: read gauges first, prepare_outcome, review, optional compile, update gauges, protect Layer 1, produce auditable trace in logs/ or ritual.md, `sovereign_audit_tail` shows the sequence.
8. Cross-agent recall test (Grok writes a note; another agent (e.g. via sm-propagation seeded) recalls with correct `agent_origin`).
9. `sovereign_audit_tail` + `sovereign_audit_report` + team primitives (runtime → evidence → promotion dry-run).
10. End-to-end ambitious plan-mode task: prepare_task reflex first, mid-work prepare_outcome if warranted, /flush at end → durable proposals in vault + native summary preserved, no duplication of content.
11. Hook surface verification: session logs or /hooks show the 4 events firing; SessionStart includes gauges injection near Layer 1; UserPromptSubmit on /flush + distill keywords.
12. Budget hygiene post-ritual (Layer 1 still protected, token counts sane).
13. No-duplication greps across the fleet: rich phrases ("Core Mental Model (non-negotiable)", "Sovereign Distill Ritual V1", "prepare before big work", "gauges as live context meter", "Layer 1 contract") appear only in canonical SKILL + ritual package + DESIGN-delivery-layer; thin Grok overlay and future thin files have none of the rich body (only pointers).
14. sm-propagation verify for grok-build + at least one other unchanged agent (e.g. codex or claude-code).
15. (Grok-specific) Re-seed/verify Grok vault via canonical sm-propagation after merge; re-read Layer 1 + gauges; confirm 100% behavior parity (hooks + canonical training deliver identical reflexes/ritual/auto-draft).

All output captured with timestamps. Reviewer persona (plan_alignment + generals + tests) applied to the diff before declaring Phase 1 complete.

---

## 6. Thin Plugin Examples — Grok Build V1 as the Worked Reference

Grok Build is the first complete thin adapter:

- Thin SKILL (`~/.grok/plugins/grok-sovereign-memory/skills/grok-sovereign-memory/SKILL.md`): ~50-line frontmatter + hybrid notes + TUI lifecycle + MCP stamp via mcp-env-run + "Grok V1 delivery on top of canonical + ritual package" + maintenance note pointing to canonical + DESIGN-delivery-layer.
- Hooks: stdlib-only `bin/grok-sovereign-hook.js` + `hooks/hooks.json` (4 events).
- `.mcp.json`: uses `~/.agents/bin/mcp-env-run`, grok-build stamp, dedicated vault.
- Local docs: `DESIGN-flush-integration.md` (exact hybrid rationale + hook guardrails), `SOVEREIGN-DISTILL-RITUAL-GUIDE.md` (Grok activation + paths), README (updated to "thin delivery adapter for the canonical...").
- Layer 1 + distill/ already seeded via canonical sm-propagation (grok-build identity).
- Ritual symlink (or copy of package) under artifacts/.
- Zero changes to hook/bin/manifests during the canonical merge; only SKILL thinned + pointer updates.

Future agents follow the same pattern: sm-propagation (or manual) for Layer 1 + thin adapter dir + SKILL callout + local DESIGN + verify matrix.

See the Grok thin SKILL and its local DESIGNs for the concrete "how" on TUI lifecycle and native tool hybrid.

---

## 7. Risks & Mitigations

- **Automation depth varies by platform**: Some have rich hooks (Grok, Claude), others less. Mit: Document "automation level" per surface explicitly; canonical SKILL + ritual still deliver the reflexes when hooks are weaker.
- **SKILL loading precedence / SSoT**: Thin must not override canonical. Mit: Explicit "extends canonical" declaration + training that canonical is authoritative; verification greps + fresh session tool discovery.
- **Config drift / vault symlinks**: sm-propagation + verify steps catch and repair.
- **Token budget on Layer 1 + gauges**: Strict <4096 enforced in templates + ritual hygiene; re-estimate on every seed/ritual.
- **Cross-agent consistency**: Agent_origin tagging + handoff contracts + audit; tests in matrix.
- **Maintenance / duplication creep**: Strict boundary doc + "this file is intentionally thin" notes + reviewer persona on every portable change + "kill two birds" rule. All portable changes go through canonical + ritual package.
- **Hook fail-open / daemon down**: Graceful (native still works; drafts still land for human review).
- **Grok-specific paths in examples**: Confined to thin overlay + local DESIGNs + Grok vault examples only.

---

## 8. How to Add a New Agent Surface (Phase 2+ Rollout)

1. Use canonical `sm-propagation` (or updated propagate.py) to seed Layer 1 identity + empty vault for the new agent id (e.g. "gemini-antigravity").
2. Create thin adapter dir (e.g. `~/.gemini/extensions/gemini-sovereign-memory/` or platform equivalent) containing:
   - manifests (.mcp.json or platform native) stamped via mcp-env-run + agent id + vault.
   - SKILL overlay (thin, canonical reference + platform hybrid notes only).
   - Hook wiring if the platform exposes lifecycle (or document the manual path).
   - Local DESIGN-*.md with explicit "Grok was the reference; this is <Platform> delivery only" + pointers to canonical DESIGN-delivery-layer + ritual package.
   - (Optional) symlink or copy of ritual package under artifacts/.
3. Update the canonical SKILL's "Platform-Specific Delivery Callouts" section with a 1-paragraph thin note for the new surface (pointers only).
4. Run the full verification matrix (above) for the new surface + regression on at least two existing agents.
5. Update sm-propagation SKILL + propagate.py with alias/platform spec if a new "update-plugin --platform" path is warranted (thin vs full copy).
6. Produce captured Parity Report + reviewer sign-off.

Portable improvements discovered while adding the surface are contributed back to the canonical SKILL / ritual package (never left only in the new thin surface).

---

**End of authoritative Delivery Layer spec.**  
All Phase 1+ work and future rollouts cite this document + the approved generalization plan. The Grok Build thin adapter is the concrete, tested reference for "how a minimal surface looks and behaves."

(When promoting to the sovereignMemory repo, this DESIGN + the thin Grok package/ will live under the `grok/` feature branch as the download-and-use example.)