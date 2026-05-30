# Minni Deep Rename — Design Spec

- **Date:** 2026-05-29
- **Author:** claude-code (with operator)
- **Status:** Draft for review
- **Supersedes:** `docs/superpowers/specs/2026-05-26-minni-rename-design.md` (the "identity-only, do NOT rename runtime IDs" decision). That decision was a forced compromise to survive a commit with a hallucinating agent — **not** the real intent.

## 1. Goal & Motivation

Rename the project's runtime identity from `sovereign` / `sovereign-memory` to **Minni**, end to end — not just the brand surface.

**Why (real reasons, recorded so no future agent re-litigates this):**
- **Ergonomic friction.** Typing/saying "check sovereign memory", `sovereign-memory:recall`, `sovereign_recall` constantly is annoying. `minni:recall` / `minni_recall` is short.
- **Brand voice + the Nordic rune.** "Minni" carries intended meaning; "Sovereign Memory" was an architecture label, not a brand.

The earlier "identity-only" decision left the friction fully in place (every runtime identifier still said `sovereign`). This spec reverses it deliberately.

## 2. Constraints & Hard Facts (verified 2026-05-29)

- **The daemon is LIVE.** `~/.sovereign-memory/sovereign_memory.db-wal` is actively growing. Any vault/DB operation must assume concurrent writes → backup-first, copy+verify, never blind move.
- **Blast radius:** ~226 repo files reference `sovereign` (excluding node_modules/venv/dist/archives). Heaviest: `plugins/sovereign-memory/` (68 files), `docs/` (many plans/contracts), `engine/`, `openclaw-extension/`.
- **7 live vaults** under `~/.sovereign-memory/`: `claudecode-vault`, `codex-vault`, `gemini-vault`, `grok-beta-vault`, `grok-build-vault`, `kilocode-vault`, plus `identities/`, `learnings/`, `faiss/`, `principals/`, `run/` (socket), and `sovereign_memory.db`.
- **5 agent platforms** consume the MCP namespace `mcp__sovereign-memory__*`: Claude Code, Codex, Kilocode, Grok-build, Gemini/Antigravity.
- **Platform confidence (operator, corrected 2026-05-29):** Gemini/Antigravity is **good but not gold** — the most-trusted starting point, NOT an authoritative ground-truth template. The other four (Claude Code, Codex, Kilocode, Grok-build) are more suspect / possibly misconfigured. → Verify EVERY platform independently; Gemini just starts ahead. Treat no platform's config as canonical-by-fiat — the canonical config is defined by this spec, then each platform is checked against it.
- **Grok-build is the worst offender:** `plugins/grok-sovereign-memory/.mcp.json` and `~/.sovereign-memory/identities/grok-build/GROK-BUILD_HOSTED_AGENT_ENVELOPE.md` both point at the **stale path** `~/Projects/sovereignMemory`. No actual symlink-into-DB exists — the "symlinked into claude code DB" impression comes from the shared daemon socket + stale workspace pointers.

## 3. Target Vocabulary (canonical scheme)

| Layer | Now | Target | Typed by user? |
|---|---|---|---|
| Brand | Sovereign Memory | **Minni** | — |
| Plugin folder | `plugins/sovereign-memory/` | `plugins/minni/` | rarely |
| Installed plugin | `sovereign-memory@sovereign-memory` | `minni@minni` | rarely |
| MCP namespace | `mcp__sovereign-memory__` | `mcp__minni__` | yes |
| Tool verbs | `sovereign_recall`, `sovereign_learn`, … | `minni_recall`, `minni_learn`, … (drops redundant double-`sovereign`) | yes |
| Skill IDs | `sovereign-memory:recall` | `minni:recall` | yes |
| Vault dir | `~/.sovereign-memory/` | `~/.minni/` | yes (cd into it) |
| Daemon binary | `sovrd` | `minnid` | no |
| DB file | `sovereign_memory.db` | `minni.db` | no |
| Env vars | `SOVEREIGN_*` | `MINNI_*` | sometimes |
| Internal tags / frontmatter | `agent_origin`, `sovereign_learning:` | `minni_*` equivalents | no |

**Priority principle:** rename the *typed/ergonomic* surface aggressively and early (skill IDs, tool verbs, namespace, vault path). Rename deep internals (DB filename, daemon binary, wire-protocol strings) **last**, behind the compat shim, because they cause no friction and carry the most migration risk.

## 4. Compatibility Layer (zero-downtime guarantee)

Nothing breaks mid-migration because `sovereign` keeps resolving until explicitly retired:

- **Vault:** `~/.minni/` becomes the real directory (copy + checksum-verify, NOT move). `~/.sovereign-memory` becomes a symlink → `~/.minni`. Both paths resolve for any un-migrated config.
- **MCP server:** register a `minni` server entry; keep `sovereign-memory` as an alias entry pointing at the same binary.
- **Skill IDs:** `minni:*` are canonical; `sovereign-memory:*` kept as thin deprecated shim files that forward.
- **Tool verbs:** the daemon dispatches BOTH `minni_*` and `sovereign_*` (alias table) during transition.
- **Env vars:** daemon reads `MINNI_*` first, falls back to `SOVEREIGN_*`.

## 5. Phases (each ≈ one PR, ordered by ascending risk)

### P1 — Repo-internal rename (in-git only, daemon untouched)
Rename plugin folder, in-repo code identifiers, skill IDs, docs, README/AGENTS/DESIGN brand strings. No live-system change. Fully reversible via git. Add `sovereign-memory:*` skill shims.

### P2 — Daemon dual-naming
Daemon exposes `minni_*` tools as aliases of `sovereign_*`; registers `minni` MCP server alongside `sovereign-memory`; accepts `MINNI_*` env alongside `SOVEREIGN_*`. No data moves yet. Verify both namespaces answer identically.

### P3 — Vault migration
Backup → copy `~/.sovereign-memory` → `~/.minni` → checksum-verify every vault/DB → repoint daemon to `~/.minni` → swap `~/.sovereign-memory` to a symlink. DB file renamed `sovereign_memory.db` → `minni.db` here (daemon-aware), or deferred to P6 if risk is high.

### P4 — Per-platform reconfig + repair (one platform at a time)
- The **canonical target config is defined by this spec** (Section 3), not by copying any platform.
- **Gemini/Antigravity FIRST** — best starting point (good, not gold); verify it against the spec, fix any drift, then use the *verified* result as a sanity cross-check for the others.
- Then audit + repair the four more-suspect platforms against the spec: **Claude Code, Codex, Kilocode, Grok-build**. Each gets independently verified — no platform is trusted by fiat.
- **Grok-build** additionally: fix stale `~/Projects/sovereignMemory` → canonical path in `plugins/grok-sovereign-memory/.mcp.json` and the grok-build identity envelope.
- Each platform: switch to `mcp__minni__` + `minni:*`, verify recall/learn round-trips, then move to the next. Sovereign aliases remain as the safety net throughout.

### P5 — Skills consolidation audit
Resolve the open question (2026-05-29): of the 10 plugin skills (`sovereign-memory` rich delivery, `-auto-indexing`, `-consolidation`, `-day4-hardening`, `-engine`, `-health-check`, `-hydration`, `-packaging`, `-wiki-ingestion`, `sovereign-openclaw-phase2-bridge`) + standalone `sm-propagation`, classify each **keep / merge / retire** against what the plugin's MCP tools + commands already surface. Propose, do not auto-act. Note: consolidation-skill scripts still depend on `~/.hermes` runtime paths + Mac-only MLX servers — flagged, not fixed here (deferred PRIVATE pluggable-backend tree).

### P6 — Deprecation (decision deferred to this point)
Once all 5 platforms are verified on `minni`: decide whether to delete the `sovereign` aliases/shims for a clean minni-only end state, or keep them as a permanent safety net. **Operator chose to decide at P6, not now.**

## 6. Deliverable: Agent Playbook

`docs/migration/MINNI-RENAME-AGENT-PLAYBOOK.md` — the "what to update on your side" reference the operator asked for. Per platform: current state, required config changes, verification command, and rollback. Gemini/Antigravity documented as the most-trusted-but-still-verified starting point; the other four documented as audit targets with their known/suspected breakage. The spec (Section 3), not any platform, is the source of truth for correct config.

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Vault data loss during migration | Backup-first; copy+checksum-verify; symlink not move; daemon live-write awareness |
| Missed `sovereign` reference breaks an agent | Compat aliases keep both names live until P6; grep audit gate per phase |
| Daemon downtime | P1 touches no runtime; P2/P3 add aliases before removing anything |
| `agent_origin` tag rewrite corrupts provenance | Tag migration is additive/aliased, validated against a vault snapshot before commit |
| Re-litigation by future agents | This spec + a superseding memory learning record the real intent and reasons |

## 8. Out of Scope (captured follow-ups)

- **Antigravity (Gemini) as a cheaper delegation workhorse** to save tokens — trial separately.
- **Pluggable model backend** (Gemma cloud / Ollama replacing Apple Foundation Models on non-Mac) — deferred PRIVATE feature tree; only *flagged* by P5.

## 9. Success Criteria

- All five platforms recall + learn through `mcp__minni__*` / `minni:*`.
- `~/.minni/` is canonical; no data lost (checksum parity vs pre-migration snapshot).
- Grok-build no longer references `~/Projects/sovereignMemory`.
- Skills set has an explicit keep/merge/retire decision per skill.
- Agent playbook exists and is accurate for each platform.
- No agent breaks at any phase boundary (compat layer holds).
