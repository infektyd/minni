# Minni Rename — P5 Skills Audit (keep / merge / retire)

> **Propose, do NOT auto-act.** This resolves the open question from 2026-05-29: now that Minni ships as a plugin surfacing 26 MCP tools + 10 slash commands + 4 hooks directly, which of the bundled skills still need to exist as skills, which are redundant/collapsible, and which are historical. Final dispositions are the operator's call — especially the RETIREs and the two DECIDE items (depends on what's still live).

## What the plugin already surfaces directly (the baseline skills are judged against)
- **MCP tools (26):** `minni_recall/learn/status/route/drill/export_pack/vault_write/audit_tail/audit_report/prepare_task/prepare_outcome/compile_vault/resolve_candidate/learning_quality/negotiate_handoff/ack_handoff/list_pending_handoffs/await_handoff/subscribe_contradictions/team_runtime/team_evidence/team_promotion/ping_agent_request/ping_agent_inbox/ping_agent_decide/ping_agent_status`.
- **Slash commands (10):** recall, learn, prepare-task, prepare-outcome, status, audit, team-evidence, team-mode, team-promotion, team-runtime.
- **Hooks (4):** SessionStart, UserPromptSubmit (auto-recall), PreCompact (scar capture), Stop (candidate-learning drafting).

## Recommendation by skill (11 total)

| Skill | Disposition | Why |
|---|---|---|
| **sovereign-memory** (operating manual) | **KEEP → rename `minni`** | NOT redundant with raw tools: it encodes *behavior* (recall-only default, when/when-not, cross-agent `agent_origin` awareness, workflow patterns) the tools/hooks don't teach. This is the orienting doc. Already rebranded in the per-platform copies. |
| **sovereign-memory-engine** | **KEEP → `minni-engine`** | Dev reference for engine internals (extraction/rerank/retrieval). Distinct from *operating* the system. Refresh stale paths. |
| **sovereign-memory-consolidation** | **KEEP → `minni-consolidation`** | Ops runbook for real consolidation runs + troubleshooting (the `minni_compile_vault` tool is only dry-run). ⚠️ scripts still assume `~/.hermes` + Mac MLX (deferred pluggable-backend issue). |
| **sovereign-memory-health-check** | **KEEP or MERGE → `minni-health-check`** | Cron monitoring/anomaly detection — broader than the `minni_status` point-check. Reasonable to MERGE with consolidation (both ops/monitoring) into one `minni-ops` skill. |
| **sovereign-memory-auto-indexing** | **MERGE** | Overlaps wiki-ingestion (both: content → wiki → memory pipeline). |
| **sovereign-memory-wiki-ingestion** | **MERGE** | → fold auto-indexing + wiki-ingestion into one **`minni-ingestion`** skill. |
| **sm-propagation** (top-level dir) | **KEEP → `minni-propagation`** | Active propagation/repair-for-an-agent tool; already brand-aware ("Minni Propagation"). Resolve its home (see structural note). |
| **sovereign-openclaw-phase2-bridge** | **RETIRE / ARCHIVE** | Completed phase-2 *implementation* guide tied to legacy `~/.openclaw/sovereign-memory-v3.1` + `/tmp/sovereign.sock`. Historical; the bridge exists. |
| **sovereign-memory-day4-hardening** | **RETIRE / ARCHIVE** | Production-hardening runbook for the `~/.openclaw` deployment (launchd/seeding already done). Extract any still-useful pattern into an ops doc, then archive. |
| **sovereign-memory-hydration** (Hermes) | **DECIDE** | KEEP+refresh if Hermes is a live agent (the daemon runs as `hermes`), else ARCHIVE. Leaning KEEP. |
| **sovereign-memory-packaging** (pip+GitHub) | **DECIDE** | KEEP if shipping Minni as a pip library is still a goal; else ARCHIVE. |

### Net effect
- **KEEP (rename to `minni-*`):** operating `minni`, `minni-engine`, `minni-consolidation`, `minni-health-check` (or merged), `minni-propagation` → ~4–5 skills.
- **MERGE:** auto-indexing + wiki-ingestion → `minni-ingestion` (−1 skill).
- **RETIRE/ARCHIVE:** openclaw-phase2-bridge, day4-hardening (and possibly hydration/packaging per your DECIDE).
- From **11 → ~5–6** living, all `minni-*`, none redundant with the plugin's tool/command/hook surface.

## Structural cleanups (separate from keep/merge/retire — confirm too)
1. **Rename survivors' dirs + SKILL.md** `sovereign-memory-*` → `minni-*`; skill IDs `sovereign-memory:*` → `minni:*` (the per-platform operating-skill copies are already rebranded by P4).
2. **Per-platform duplication:** the *operating* skill is copied into `.kilocode-plugin/skills/`, `.codex-plugin/skills/`, etc. The 10 dev/ops skills live once in `plugins/minni/skills/`. Decide: keep per-platform copies of the operating skill (each platform self-contained) vs. a single canonical + thin deltas. **Recommend: keep per-platform copies** (self-contained install) — it's intentional, not accidental dup.
3. **Top-level `sovereign-memory/` dir** (holds `sm-propagation`, `docs/`, `workflows/`) is a rename leftover still named `sovereign-memory`. Fold into `plugins/minni/` or rename to `minni/`.

## Execution (only after operator confirms dispositions)
- Archive: `git mv` retired skills to an `archive/` (or delete — operator's call); don't lose them silently.
- Merge: combine the two ingestion skills, keep the superset, redirect references.
- Rename: survivors → `minni-*`, refresh stale `~/.openclaw`/`~/.sovereign-memory` paths to `~/.minni`.
