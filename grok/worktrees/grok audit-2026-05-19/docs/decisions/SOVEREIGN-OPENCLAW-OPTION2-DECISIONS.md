# Option 2 — User Decisions (Infektyd, 2026-04-17)

These answers resolve the 8 open questions in `~/SOVEREIGN-OPENCLAW-OPTION2-PLAN.md` and
lock in the design theory before Forge implements.

## Core Theory: Solo Pillars + Combined Memory + Provenance

The plan must mirror what the **Hermes `SovereignMemoryProvider` already does** — specifically
its `system_prompt_block()` method in `~/.hermes/hermes-agent/plugins/memory/sovereign/__init__.py`.
That method shells `python agent_api.py <agent_id> --full` which emits:

1. **Layer 1 — Identity (whole-document load, per-agent "solo pillar")**
   - `IDENTITY.md` + `SOUL.md` for that specific agent
   - Tagged `agent = identity:{agent_id}` with `whole_document = 1` in the DB
   - Loaded in FULL — NOT chunked — so the agent knows WHO it is first
   - This is each agent's **personal identity** — Forge's soul ≠ Syntra's soul

2. **Layer 2 — Knowledge (chunked RAG, combined memory)**
   - Agent-tagged vault documents (relevance-scored) + recent learnings + episodic events
   - Drawn from `agent_context` table + the shared wiki (`~/wiki/`)
   - This is **combined memory** — shared across agents where appropriate

3. **Provenance ("who made it")**
   - The `agent` column on every document records the creator
   - When Forge writes a learning, it's tagged `agent = forge`
   - When Syntra recalls, she SEES Forge's contribution with attribution preserved
   - This is the "who made it" thread — never lose authorship

**This is already implemented in the engine.** The adapter just calls `--full`, `--identity`,
`--context`, and `--learn` — same as the Hermes plugin.

---

## Answers to Syntra's 8 Open Questions

### Q1: Embedding model alignment — Sovereign's gte-large vs OpenClaw's SQLite-Vec
**Decision: Sovereign embeddings stay internal.** OpenClaw's SQLite-Vec is irrelevant to this
plugin. The adapter is a thin bridge — all vector math happens inside `sovrd`. OpenClaw
never sees embeddings, only `MemorySearchResult[]`.

### Q2: Identity file location
**Decision: Identity files live in Sovereign's existing schema**, not in agent workspaces.
Documents with `agent = identity:{agent_id}` AND `whole_document = 1` are already the
identity layer. If a workspace doesn't have identity docs yet, ingest its existing
`SOUL.md` / `IDENTITY.md` via `wiki_indexer.py` on migration.

Location map:
- Forge's identity → `agent = identity:forge`, stored in the Sovereign DB (not a file path)
- Syntra's identity → `agent = identity:syntra`, same
- Physical source files can stay in `workspace-forge/SOUL.md` etc., but they're ingested,
  not read live.

### Q3: agent_id scoping for vault queries
**Decision: Vault queries IGNORE `agent_id` filter.** The wiki is fleet-shared knowledge.
- `identity:{agent_id}` → agent-specific (Layer 1)
- `{agent_id}` (no `identity:` prefix) → agent's private learnings/episodic (Layer 2 personal slice)
- No agent tag, or wiki docs → shared (Layer 2 common slice)

The `agent_context` table already blends these with relevance scoring. Don't second-guess it.

### Q4: sovrd cold-start time
**Decision: Accept cold-start cost once per daemon lifetime.** Target: daemon stays resident
(Option B from the plan). If cold start is >5s, fine — adapter probes `/health` on first call
and waits. Warm calls target <100ms. Set adapter timeout at 30s for first call, 10s after.

### Q5: Write idempotency
**Decision: Content-hash dedup.** `learn()` writes with a SHA256 of normalized content.
If the hash already exists for that `agent_id`, skip. This is already the plan's behavior
(the plan says "idempotent upsert by content hash") — just confirming.

### Q6: Daemon supervisor
**Decision: launchd** (macOS native). `install-daemon.sh` generates a
`~/Library/LaunchAgents/dev.sovereign.sovrd.plist` with `KeepAlive = true` and
`RunAtLoad = true`. No systemd (Linux server is Vidar, but this integration is Mac-side).

### Q7: Sovereign version pinning
**Decision: Pin to a specific path, not a commit.** `~/.openclaw/sovereign-memory-v3.1/` is
the version. Future versions create a new directory (`v3.2`, `v4.0`). Adapter reads the
path from `openclaw.json > memory-core.sovereign.enginePath`. Default: `~/.openclaw/sovereign-memory-v3.1/`.

### Q8: One sovrd shared, or one per agent?
**Decision: ONE sovrd shared across all agents.** Memory budget is per-machine, not per-agent.
500MB RSS for one daemon serving 5 agents is far cheaper than 5×500MB. Agents pass their
`agent_id` in every call for scoping.

---

## Additional Constraints for Forge

1. **DO NOT reinvent `agent_api.py`** — it already does Layer 1 + Layer 2 correctly. The daemon
   is a wrapper that keeps the `SovereignAgent` instance resident instead of spawning a new
   Python interpreter per call.

2. **Mirror the Hermes plugin's `system_prompt_block()` pattern exactly** for hydration. See
   `~/.hermes/hermes-agent/plugins/memory/sovereign/__init__.py` lines 190–211.

3. **Preserve `agent` column provenance on every write.** When `learn()` is called from
   Forge's adapter, the DB record must have `agent = forge`. Non-negotiable — this is the
   "who made it" axis.

4. **Fallback behavior**: If `sovrd` is unreachable, adapter returns empty arrays and logs
   a warning. Agent keeps working with flat-file MEMORY.md as before. Do NOT block agent
   startup on Sovereign availability.

5. **Option 1 wrapper stays**: `~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh` is already
   deployed for all 4 agents as a quick-win. Option 2 supersedes it but doesn't remove it —
   leave it in place as a fallback and for CLI testing.

---

## Handoff to Forge

Implement the plan in `~/SOVEREIGN-OPENCLAW-OPTION2-PLAN.md` with the decisions above baked in.
Day 1 = Monday. Checkpoint with Infektyd at end of each day before proceeding.
