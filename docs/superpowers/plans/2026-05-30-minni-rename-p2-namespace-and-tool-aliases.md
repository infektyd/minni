# Minni Rename — P2 (Namespace + Tool-Verb Aliases) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan one task at a time. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `minni` the canonical MCP surface — `mcp__minni__minni_recall` etc. — while keeping the existing `sovereign-memory` / `sovereign_*` surface fully working as an alias, so no agent breaks. Additive and reversible.

**Architecture:** All changes are in the **TypeScript MCP server** (`plugins/minni/src/server.ts`) and the plugin **manifests**. The live Python daemon (`sovrd`) is NOT touched. Changes are **branch-only / non-live**: the running session loads the already-installed `dist/`, so nothing changes at runtime until a deliberate reinstall + new-session step (the one live checkpoint, done with the operator present).

**Tech Stack:** TypeScript (`@modelcontextprotocol/sdk` `McpServer.registerTool`), Node `--test`, manifest JSON.

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md` (§3 vocabulary, §4 compat layer, P2)

**Scope boundary:**
- IN: register `minni_*` tool aliases for all 26 verbs; add the `minni` MCP namespace; local build + dispatch-parity test.
- OUT (separate follow-ons): env-var `MINNI_*`→`SOVEREIGN_*` fallback (P2b — needed before P4 flips platform env, or P4 sets both); Python daemon changes (none needed); the bare-verb form `mcp__minni__recall` (optional polish, P6); live reinstall/cutover (P4).

---

## THE ONE DESIGN DECISION (confirm before Task 3)

How to expose the `minni` namespace without clutter or breakage. The MCP prefix `mcp__<key>__` comes from the **client's mcpServers key** (manifest), and tool verbs come from what the server registers. Two strategies:

- **Option A — Additive both-keys (recommended for zero-downtime):** keep the `sovereign-memory` manifest key AND add a `minni` key, both pointing at the same `dist/server.js`; register every tool under both `minni_*` and `sovereign_*`. Result: `mcp__minni__minni_recall` (canonical) and `mcp__sovereign-memory__sovereign_recall` (alias) both work. Cost: transitional tool-list shows both prefixes × both verbs (clutter) until P6 drops the sovereign side. Nothing breaks for un-migrated configs.
- **Option B — Single key cutover:** rename the one manifest key to `minni`, register both verb names under it. Result: `mcp__minni__minni_recall` + `mcp__minni__sovereign_recall`. Cleaner list, BUT the `mcp__sovereign-memory__*` namespace disappears immediately → any agent/config still pointing at the old key is broken until P4 reconfigures it. Not zero-downtime.

**Recommendation: Option A.** It honors the compat-layer promise (nothing breaks mid-migration); the clutter is temporary and removed at P6. This plan assumes Option A; if the operator picks B, Task 3 changes (rename the key instead of adding one) and the manifest gate flips.

---

## Task 1: Add a tool-registration helper that registers each verb under both names

**Files:**
- Modify: `plugins/minni/src/server.ts`

- [ ] **Step 1: Read the current registration pattern**

Read `plugins/minni/src/server.ts` lines 64–130 to confirm the `server.registerTool(name, config, handler)` shape and that all 26 calls follow it.

- [ ] **Step 2: Add a dual-register helper near the top of the tool section (after the `server` is constructed, ~line 63)**

```ts
// Canonical verbs are minni_*; sovereign_* are kept as aliases during the
// rename transition (removed in P6). Registering under both names makes the
// same handler answer to either, so no client breaks.
function registerDual(
  sovereignName: string,
  config: Parameters<typeof server.registerTool>[1],
  handler: Parameters<typeof server.registerTool>[2],
) {
  const minniName = sovereignName.replace(/^sovereign_/, "minni_");
  server.registerTool(minniName, config, handler);
  if (minniName !== sovereignName) {
    server.registerTool(sovereignName, config, handler);
  }
}
```

- [ ] **Step 3: Convert all 26 `server.registerTool("sovereign_…", …)` calls to `registerDual("sovereign_…", …)`**

Mechanically, per call, change the function name `server.registerTool` → `registerDual` for the calls whose first arg starts with `"sovereign_"`. Do them as individual edits (NO blanket sed across the repo — targeted edits within this one file only). The 26 verbs: sovereign_prepare_task, sovereign_prepare_outcome, sovereign_team_runtime, sovereign_team_evidence, sovereign_team_promotion, sovereign_status, sovereign_compile_vault, sovereign_route, sovereign_recall, sovereign_drill, sovereign_export_pack, sovereign_learn, sovereign_resolve_candidate, sovereign_learning_quality, sovereign_vault_write, sovereign_audit_report, sovereign_audit_tail, sovereign_negotiate_handoff, sovereign_ping_agent_request, sovereign_ping_agent_inbox, sovereign_ping_agent_decide, sovereign_ping_agent_status, sovereign_ack_handoff, sovereign_list_pending_handoffs, sovereign_await_handoff, sovereign_subscribe_contradictions.

- [ ] **Step 4: Gate — build compiles**

Run: `cd /Users/hansaxelsson/Projects/Minni/plugins/minni && npm run build:server 2>&1 | tail -10`
Expected: exit 0.

- [ ] **Step 5: Commit**

`git add plugins/minni/src/server.ts` → commit "feat(minni-p2): register minni_* tool aliases via registerDual (sovereign_* kept)" + the Co-Authored-By trailer.

---

## Task 2: Test — both verb names are registered and dispatch identically

**Files:**
- Create or extend: `plugins/minni/tests/dual-register.test.mjs`

- [ ] **Step 1: Write a failing test that asserts both names exist**

The MCP SDK exposes registered tools; if a direct registry getter isn't available, assert via the server's ListTools handler output. Write a test that builds the server module and checks that the tool list contains BOTH `minni_recall` and `sovereign_recall` (and a second pair, e.g. `minni_learn`/`sovereign_learn`), and that the count of `minni_*` equals the count of `sovereign_*`.

```js
import test from "node:test";
import assert from "node:assert/strict";
// import the constructed server / tool registry from ../dist/server.js
// (adapt to however server.js exposes its tool list; if it does not export
// one, add a minimal `export function listToolNames()` to server.ts)
test("every sovereign_ verb has a minni_ alias", async () => {
  const { listToolNames } = await import("../dist/server.js");
  const names = listToolNames();
  const sov = names.filter((n) => n.startsWith("sovereign_"));
  const min = names.filter((n) => n.startsWith("minni_"));
  assert.ok(sov.includes("sovereign_recall"));
  assert.ok(min.includes("minni_recall"));
  assert.equal(min.length, sov.length);
});
```

- [ ] **Step 2: Run it, watch it fail** (`npm test` — expect the new test red if `listToolNames` not yet exported)

- [ ] **Step 3: Add the minimal `listToolNames()` export to `server.ts` if needed**, build, re-run until green.

- [ ] **Step 4: Full suite stays green**

Run: `npm test 2>&1 | grep -E "ℹ (tests|pass|fail)"` — expect fail 0 (currently 137 pass; new test makes it 138).

- [ ] **Step 5: Commit** "test(minni-p2): assert minni_*/sovereign_* alias parity" + trailer.

---

## Task 3: Add the `minni` MCP namespace in the manifests (Option A)

**Files:**
- Modify: `plugins/minni/.claude-plugin/plugin.json`, `plugins/minni/.mcp.json`, and the per-platform manifests that declare an `mcpServers` block (`.kilocode-plugin/plugin.json`, `.gemini-plugin/gemini-extension.json`, `.codex-plugin/.mcp.json`).

- [ ] **Step 1: In each manifest's `mcpServers` object, ADD a second key `minni` duplicating the existing `sovereign-memory` entry** (same command/args/cwd/env), leaving the `sovereign-memory` key intact. Targeted JSON edits per file — no sweeps.

- [ ] **Step 2: Gate — both keys present, JSON valid**

Run: `for f in plugins/minni/.claude-plugin/plugin.json plugins/minni/.mcp.json plugins/minni/.kilocode-plugin/plugin.json; do node -e "JSON.parse(require('fs').readFileSync('$f'))" && echo "$f ok"; done` and `rg -n '"minni":|"sovereign-memory":' plugins/minni/.claude-plugin/plugin.json plugins/minni/.mcp.json`
Expected: both keys present in each, all JSON valid.

- [ ] **Step 3: Commit** "feat(minni-p2): add minni MCP namespace alongside sovereign-memory (Option A)" + trailer.

---

## Task 4: Verification gate

- [ ] **Step 1: Build + full tests** — `npm run build:server && npm test 2>&1 | grep -E "ℹ (tests|pass|fail)"` → fail 0.
- [ ] **Step 2: Local dispatch smoke (non-installed)** — start the built server against a temp vault and confirm a `minni_status` (or `minni_recall`) call returns the same shape as `sovereign_status`. Use the existing `smoke:status` script pattern (`node dist/cli.js status`) if it exercises a verb; otherwise a short inline harness.
- [ ] **Step 3: Protected-surface audit** — `rg -c "sovereign_recall|mcp__sovereign-memory__|~/.sovereign-memory" --glob '!dist' --glob '!node_modules'` still non-zero (sovereign side intact as alias).
- [ ] **Step 4: Confirm WIP untouched** — `git status --short` shows only the same pre-existing WIP set; nothing bundled.

---

## LIVE CHECKPOINT (operator present) — not a branch step

After the above is green on the branch, the only live action: reinstall the plugin from `plugins/minni` and open a NEW Claude Code session to confirm `mcp__minni__minni_recall` appears and answers, with `mcp__sovereign-memory__sovereign_recall` still working. This is where Option A's dual surface is verified end-to-end. Do this together; do not auto-deploy.

## Follow-ons (tracked, not in this plan)
- **P2b — env-var fallback:** route `SOVEREIGN_*` reads in `config.ts`/`vault.ts`/`sovereign.ts` through a `resolveEnv(name)` that checks `MINNI_<name>` then `SOVEREIGN_<name>`. Needed before P4 flips platform env (or P4 sets both). Sovereign.ts is WIP — isolate.
- **Bare-verb polish:** optionally also expose `mcp__minni__recall` (drop the `minni_` prefix) — defer to P6.
