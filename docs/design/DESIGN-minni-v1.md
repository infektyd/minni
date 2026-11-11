# DESIGN: Minni v1.0 Architecture & Release Blueprint

- **Status:** Approved Blueprint (6-Wave Execution Plan to `v1.0.0`)
- **Baseline:** `main` @ `89acf175`

---

## 0. Core Purpose & Dual Audience

Minni v1.0 serves **both the coding agent and the human operator** without ceremony, UI bloat, or half-wired subsystems:

1. **For the Agent:**
   - **Massive Repository Context Without Context-Window Bloat:** Minni indexes a repository's architecture docs (`AGENTS.md`, `docs/`) and maintains distilled, file-and-SHA-pinned **Subsystem Map Cards** (*what a subsystem owns, exact file paths, invariants, and linked scars*). A cold agent opening a large repository immediately knows where to look without scanning the whole tree into active context.
   - **Durable Lessons & Scars:** Agents record proven fixes, constraints, and failure scars once so cold sessions never repeat past mistakes.
2. **For the Human Operator:**
   - **Plain-Text Visibility (Zero Web UI Required):** Operators inspect live agent sessions, active shared plans, new learnings/scars per project, and genuine rule conflicts via `~/.minni/TODAY.md` and `minni activity` in the terminal.

---

## 1. Architectural Decisions Locked for v1.0

### 1.1 Living Repo Maps + Git-Diff Staleness (`minni map <repo>`)
- Minni stores **architecture maps, project docs, and scars** — never raw line-by-line source code (lexical/AST search belongs to `rg` and local code-outline tools).
- `minni map <repo>` indexes `AGENTS.md` and `docs/` and synthesizes concise **Subsystem Cards** pinned to owning file paths and the repository's `git` tree/commit SHA.
- On `minni_recall` and `SessionStart`, Minni checks `git diff --name-only <card_sha>..HEAD` against each card's tracked file paths. If owned files changed since `card_sha`, Minni stamps the card `"changed since <sha>"` in the recall envelope and queues a background refresh.

### 1.2 Project-Scoped Recall & Strict Confidence Floor
- Both `documents` and `learnings` carry first-class `workspace_id` tags (`workspace-<repo>` or `global`).
- `semantic_rerank.py` enforces its post-rerank confidence floor across **all** candidates (closing the defect where any single-token FTS match bypassed the `-4.0` Cross-Encoder floor and leaked unrelated negative-score hits) and drops empty `<EVIDENCE>` bodies.
- Ranking prioritizes **current workspace + `global`** memories first, surfaces other workspaces only when relevance clears a high cross-project bar, and returns `"no strong match"` instead of low-score filler.

### 1.3 Two-Part Boot Envelope (`fitEnvelope`) & Automatic Harvest
- `SessionStart` and `SubagentStart` assemble a bounded two-part permanent shelf via `fitEnvelope`:
  1. **Agent Permanent Shelf (`layer1/core.md`):** runtime identity and standing operator rules, kept strictly within token budget (`truncated: false`).
  2. **Project Permanent Shelf:** compact directory of the current repository's Subsystem Cards, top active repository scars, and active shared plan summary.
- Compaction and `Stop` hooks capture session bursts automatically; the unused legacy `distill/ritual.md` and `distill/gauges.md` templates are retired.

### 1.4 12-Tool MCP Surface + First-Class CLI
- `plugins/minni/src/server.ts` trims its MCP registration from **42 tools down to 12 self-explanatory tools**:
  - **Memory & Repo Context (5):** `minni_recall`, `minni_drill`, `minni_learn`, `minni_list_candidates`, `minni_resolve_candidate`
  - **Status & Handoff (3):** `minni_status`, `minni_handoff_send`, `minni_handoff_inbox`
  - **Shared Plans / Threads (4):** `minni_thread_create`, `minni_thread_status`, `minni_thread_update` (safe file lock under the hood, zero worker lease tokens or idempotency keys, compact `< 2 KB` response without echoing full `scar_tissue`), `minni_thread_scar`
- Diagnostic and administrative commands (`doctor`, `map`, `activity`, `recall`, `learn`, `audit`) live on the `minni` CLI (`src/minni/cli.py`) so shell-only workflows and operators have direct access without MCP overhead.

### 1.5 Context-Only, Non-Blocking Hooks Across All Hosts
- Hooks inject context and reminders (`SessionStart`, `SubagentStart`, `UserPromptSubmit`, `PostToolUse`) and **never** deny or block `Read`, `Bash`, or edit tool calls on any host runtime.
- `UserPromptSubmit` ignores synthetic `<task-notification>` / background wakeup payloads; `PostToolUse` restricts scar checks strictly to file edits and failed commands.

### 1.6 Internal Link/Supersession Engine & Dead-Weight Removal
- Wire the existing typed graph & edge modules (`src/minni/graph_coordinator.py`, `edge_classifier.py`, `graph_expansion.py`, `minnid_runtime/graph_adapters.py`) into `learn`, `consolidation`, and `recall` so `learning_links` and `memory_links` actively connect subsystems, files, and scars and automatically retire superseded facts.
- Back local classification/extraction with structured local model evaluation plus chunked AFM fallback so oversized inputs never drop silently.
- Auto-reject hook wrapper noise and one-word fragments before durable promotion; surface only genuine rule contradictions in `~/.minni/TODAY.md`.
- Remove dead internal code: `src/minni/ax_memory.py` + `src/minni/minnid_runtime/ax.py` (`ax_snapshots` handlers), the legacy cosine contradiction loop, and the localhost React web console (`plugins/minni/src/ui-server.ts` and `frontend-src`).

---

## 2. Six-Wave Execution Plan to `v1.0.0`

1. **Wave 1 — Housecleaning, Non-Blocking Hooks & 42 → 12 MCP Tool Diet:** Remove `ax_memory` runtime handlers, retire the React web console (`ui-server.ts` / `frontend-src`), remove legacy `distill/ritual.md` & `gauges.md` seeding, make all host hooks strictly non-blocking, and consolidate the MCP server to the 12 v1 tools (`< 2 KB` compact `minni_thread_update`, 1-step `minni_handoff_send` / `minni_handoff_inbox`).
2. **Wave 2 — Project-Scoped Storage, Recall Floor Fix & CLI:** Add `workspace_id` to `learnings`, enforce the Cross-Encoder confidence floor regardless of `fts_rank`, prioritize `current_repo` + `global`, strip blank notes, and ship `minni recall`, `minni learn`, and `minni activity` CLI subcommands.
3. **Wave 3 — Internal Graph/Supersession Wiring & Hands-Free Hygiene:** Fold in any needed graph adapter pieces from archived salvage branches, wire `graph_coordinator` + `edge_classifier` into live `learn` / `consolidation` / `recall` with chunked local fallback, auto-reject wrapper junk, auto-supersede stale facts, and delete the legacy cosine contradiction loop.
4. **Wave 4 — Living Repo Maps (`minni map`) & Two-Part Permanent Shelf:** Ship `minni map <repo>` with file+SHA-pinned Subsystem Cards, git-diff staleness detection on recall, and the non-truncated Agent Shelf + Project Permanent Shelf in `fitEnvelope`.
5. **Wave 5 — Human Digest (`~/.minni/TODAY.md` & `minni activity`) & Docs Alignment:** Auto-maintain `~/.minni/TODAY.md`, align `minni status` / `minni activity` for human readability, and update `README.md`, `AGENTS.md`, `CHANGELOG.md`, and skills to match the exact shipped v1 surface.
6. **Wave 6 — Live Store Scrub, Multi-Repo Map Bootstrap & Cold-Agent E2E Proof (`v1.0.0`):** Scrub pre-v1 database noise, classify untagged memories by workspace, bootstrap Living Repo Maps on real multi-surface repositories, sync all host plugins (`make sync-root`), and pass live cold-agent E2E verification before tagging `v1.0.0`.
