# Minni Rename — P2 (Direct MCP Identifier Rename) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use `- [ ]`.

**Decision (2026-05-30):** Operator chose **clean direct rename, NO aliases**, with the daemon **down for the entire migration**. So this plan renames the MCP identifiers outright — no `registerDual`, no dual namespace, no P6 dealias.

**Goal:** Rename the plugin's MCP surface from `sovereign` to `minni`: 26 tool verbs `sovereign_* → minni_*`, MCP server name + `mcpServers` key `sovereign-memory → minni`, plugin `name` identifier, and slash-command bodies. Result: `mcp__minni__minni_recall`, skill/command IDs `minni:*`.

**Architecture:** Branch-only / non-live (the running session loads the installed `dist/`; the live Python daemon is DOWN and is untouched by this phase). **Verified decoupling:** tool verbs are NOT the daemon RPC method names (handlers call generic `search`/`learn`/`status` over the socket), so renaming verbs cannot break daemon dispatch.

**Out of scope (later phases):** `~/.sovereign-memory/` vault dir, `SOVEREIGN_*` env vars, `sovereign_memory.db`, the launchd plist, `agent_origin` tags → P3 (vault+daemon) / P4 (platforms).

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md`

---

## Task 1: Rename the 26 tool verbs in server.ts

**Files:** Modify `plugins/minni/src/server.ts`; Modify `plugins/minni/tests/task.test.mjs` (one audit-name assertion).

- [ ] **Step 1:** In `server.ts`, rename each `server.registerTool("sovereign_X", …)` first-argument string `sovereign_X → minni_X`, for all 26 verbs (prepare_task, prepare_outcome, team_runtime, team_evidence, team_promotion, status, compile_vault, route, recall, drill, export_pack, learn, resolve_candidate, learning_quality, vault_write, audit_report, audit_tail, negotiate_handoff, ping_agent_request, ping_agent_inbox, ping_agent_decide, ping_agent_status, ack_handoff, list_pending_handoffs, await_handoff, subscribe_contradictions). **Targeted edits only — NO blanket sed across the repo.** (A `sed -n`-style targeted replace confined to this single file is acceptable if each match is verified, but per-occurrence Edits are preferred.)
- [ ] **Step 2:** Rename the cosmetic audit strings `tool: "sovereign_X"` (e.g. line ~415 `tool: "sovereign_recall"`) → `tool: "minni_X"` within `server.ts`.
- [ ] **Step 3:** Update the matching test assertion in `tests/task.test.mjs`: `assert.equal(audits[0].tool, "sovereign_prepare_task")` → `"minni_prepare_task"`. NOTE this file holds pre-existing WIP — stage ONLY this assertion change (use the stash-isolate technique if needed); do NOT bundle WIP.
- [ ] **Step 4: Gate** — `cd plugins/minni && npm run build:server` (exit 0) then `npm test 2>&1 | grep -E "ℹ (tests|pass|fail)"` (fail 0).
- [ ] **Step 5:** Audit — `rg -n '"sovereign_[a-z_]+"' plugins/minni/src/server.ts` returns EMPTY (no verb left). Commit `server.ts` (+ isolated test hunk): "refactor(minni-p2): rename 26 MCP tool verbs sovereign_* -> minni_*" + trailer.

## Task 2: Rename the namespace, server name, plugin identifier, command bodies

**Files:** `plugins/minni/src/server.ts` (server name line ~60); `plugins/minni/.mcp.json`; `plugins/minni/.claude-plugin/plugin.json`; `plugins/minni/.kilocode-plugin/plugin.json` + `.kilocode-plugin/.mcp.json`; `plugins/minni/.codex-plugin/plugin.json` + `.codex-plugin/.mcp.json`; `plugins/minni/.gemini-plugin/gemini-extension.json`; root `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`; `.agents/plugins/marketplace.json`; `plugins/minni/commands/*.md`.

- [ ] **Step 1:** `server.ts` line ~60: `name: "sovereign-memory"` → `name: "minni"`.
- [ ] **Step 2:** In every manifest, rename the `mcpServers` KEY `"sovereign-memory"` → `"minni"`, the plugin `"name": "sovereign-memory"` → `"minni"`, and the `keywords` entry `"sovereign-memory"` → `"minni"`. Targeted JSON edits per file (no sweeps). Keep all other structure.
- [ ] **Step 3:** In `plugins/minni/commands/*.md`, update any tool invocation `mcp__sovereign-memory__sovereign_X` → `mcp__minni__minni_X` and skill-ID refs `sovereign-memory:` → `minni:`. (Brand phrase "Sovereign Memory" → "Minni" too where it appears.)
- [ ] **Step 4: Gate** — JSON validity: `for f in $(rg -l --files-with-matches '"minni"' plugins/minni/.claude-plugin plugins/minni/.mcp.json); do node -e "JSON.parse(require('fs').readFileSync('$f'))"; done`. Build still exits 0.
- [ ] **Step 5: Audit** — `rg -n '"sovereign-memory"' plugins/minni .claude-plugin .agents/plugins/marketplace.json --glob '!dist' --glob '!node_modules'` returns EMPTY (no MCP identifier left). Commit (explicit paths): "refactor(minni-p2): rename MCP namespace + plugin id sovereign-memory -> minni" + trailer.

## Task 3: Verification gate
- [ ] Build (`npm run build:server`) exit 0; full tests fail 0.
- [ ] `rg -n "sovereign_[a-z]+|mcp__sovereign-memory__|\"sovereign-memory\"" plugins/minni/src plugins/minni/commands plugins/minni/.claude-plugin plugins/minni/.mcp.json --glob '!dist'` → EMPTY (MCP layer fully on minni).
- [ ] `git status --short` shows only the same pre-existing WIP; nothing bundled.
- [ ] Confirm OUT-of-scope refs untouched (still present, for P3/P4): `rg -c "SOVEREIGN_|\.sovereign-memory/" plugins/minni/src` non-zero.

## Live checkpoint (with operator, at bring-up — not now)
Reinstall plugin from `plugins/minni` → install dir becomes `minni@minni`; new session must show `mcp__minni__minni_recall` + `/minni:*` commands. Verified together when the daemon is brought back up on the minni identity.
