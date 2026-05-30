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

Everywhere `sovereign` / `sovereign-memory` appears, the target is `minni`. The full rule is "`sovereign-memory` → `minni`, `sovereign_` → `minni_`". Key cases:

| Now | Target |
|---|---|
| `mcp__sovereign-memory__sovereign_recall` | `mcp__minni__minni_recall` (also drop the redundant prefix → `mcp__minni__recall` where clean) |
| `sovereign-memory:recall` (skill ID) | `minni:recall` |
| `~/.sovereign-memory/` (vault) | `~/.minni/` |
| `plugins/sovereign-memory/`, `sovereign-memory@sovereign-memory` | `plugins/minni/`, `minni@minni` |
| `SOVEREIGN_*` env, `sovrd`, `sovereign_memory.db` | `MINNI_*`, `minnid`, `minni.db` |
| `sovereign_learning:` / tag fields | `minni_*` |

**Order of attack:** rename the *typed* surface first (skill IDs, tool verbs, namespace, vault path) — that's the whole ergonomic point. Rename deep internals (DB filename, daemon binary, wire strings) **last**, behind the compat shim — zero friction, highest risk.

## 4. Compatibility Layer (zero-downtime guarantee)

Nothing breaks mid-migration because `sovereign` keeps resolving until explicitly retired:

- **Vault:** `~/.minni/` becomes the real directory (copy + checksum-verify, NOT move). `~/.sovereign-memory` becomes a symlink → `~/.minni`. Both paths resolve for any un-migrated config.
- **MCP server:** register a `minni` server entry; keep `sovereign-memory` as an alias entry pointing at the same binary.
- **Skill IDs:** `minni:*` are canonical; `sovereign-memory:*` kept as thin deprecated shim files that forward.
- **Tool verbs:** the daemon dispatches BOTH `minni_*` and `sovereign_*` (alias table) during transition.
- **Env vars:** daemon reads `MINNI_*` first, falls back to `SOVEREIGN_*`.

## 5. Phases (each ≈ one PR, ordered by ascending risk)

- **P1 — Repo-internal rename** (in-git only, daemon untouched): plugin folder, in-repo code identifiers, skill IDs, docs, brand strings. Add `sovereign-memory:*` skill shims. Fully reversible via git.
- **P2 — Daemon dual-naming**: daemon serves `minni_*` as aliases of `sovereign_*`, registers the `minni` MCP server alongside `sovereign-memory`, reads `MINNI_*` then `SOVEREIGN_*`. No data moves. Verify both namespaces answer identically.
- **P3 — Vault migration**: backup → copy `~/.sovereign-memory` → `~/.minni` → checksum-verify → repoint daemon → swap old path to a symlink. DB file rename deferred to P6 if risky.
- **P4 — Per-platform reconfig + repair** (one at a time, spec is the source of truth — not any platform's config): Gemini/Antigravity first (verify, good-not-gold), then audit+repair Claude Code, Codex, Kilocode, Grok-build. Grok-build also gets its stale `~/Projects/sovereignMemory` pointers fixed. Each independently verified via a recall/learn round-trip; aliases stay live as the net.
- **P5 — Skills audit**: classify each of the 10 plugin skills + `sm-propagation` as **keep / merge / retire** vs. what the plugin's MCP tools + commands already surface. Propose, don't auto-act. (Flag only: consolidation scripts still assume `~/.hermes` + Mac MLX — deferred backend work.)
- **P6 — Deprecation** (decided here, not now): delete the `sovereign` aliases for a clean minni-only state, or keep them as a permanent net.

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
