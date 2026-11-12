# Concepts

## The four verbs

| Verb | What happens | Surface |
|---|---|---|
| **Recall** | Repo-First cited, provenance-tagged retrieval across Living Repo Maps, project docs, learnings, and scars (`mode="fast"` `< 10 ms` lexical Trie + FTS5 BM25, or full hybrid with FAISS + weighted RRF + Cross-Encoder reranking, strict confidence floor across all hits, and typed graph expansion) | MCP: `minni_recall`, `minni_drill` · CLI: `minni recall`, `minni map` |
| **Learn** | Runs the vault-first quality and secret gate (`assessLearningQuality`, stripping wrapper chatter), stages a workspace-tagged `candidate_packets` row (`proposed`) and writes a personal vault note (`wiki/sessions/`). Neither is the same as an accepted shared learning unless `auto_accept_own` or `--accept` promotes it | MCP: `minni_learn` · CLI: `minni learn` |
| **Approve** | A resolution decision — `accept` / `learn` / `reject` / `redact` / `log_only` / `merge` / `supersede` / `do_not_store`. Accepting decisions commit the workspace-scoped shared learning (preserving original author `principal` and `category`, with resolver in `resolved_by`), wire typed `memory_links` / `learning_links` with automatic supersession, refresh `~/.minni/TODAY.md`, and synchronously index the durable record | MCP: `minni_list_candidates`, `minni_resolve_candidate` · CLI: `minni learn --accept` |
| **Handoff** | Explicit 1-step session or cross-agent transfer under a lease (`< 1 KB` receipt); the receiver previews pending handoffs in its workspace and acknowledges by `lease_id` or `ack: true` | MCP: `minni_handoff_send`, `minni_handoff_inbox` |

One explicit operator override is `force=true` on `learn` (backing `minni learn --accept`), which
writes a durable learning directly, **only** for an operator principal, and is
audit-stamped `FORCE_DURABLE_LEARN`. A non-operator force attempt is denied
with an `operator_only` error. Operator-configured `auto_accept_own` and the optional consolidation path are separate approval mechanisms described below; they are not implied by ordinary tool access.

Three success states need different follow-up: a **personal note** (`wiki/sessions/`) is written immediately to the caller's vault; a **staged candidate** awaits a
governance decision; an **accepted shared learning** is durable in SQLite, linked into the typed memory graph, and synchronously indexed upon acceptance (even if follow-up
indexing fails, it remains durable in SQLite). Check the response's indexing result: if `indexed=false`, repair
or reindex the accepted record rather than resubmitting it as a new learning.

## Delegating approval

Human approval is the **default policy, not the architecture**. Governance has
two independent dials — **who initiates a learn** (stages the candidate) and
**who approves it** (makes it durable) — and the operator sets each one.

**Initiation.** By default the human drives: they invoke `/minni:learn` (or
ask for it) and the agent stages the candidate. The plugin never auto-fires a
learn from hooks — recall is automatic, writes are deliberate. But the human
can hand the agent the initiative by allowing the `minni_learn` tool to run
without per-call confirmation in their runtime's permission settings (e.g. the
Claude Code permission allowlist). Then an agent that cracks a problem three
hours into a session stages the learning on the spot instead of losing it
because the human was AFK. Proposal-first is what makes this a reasonable
grant: nothing enters the shared durable tier without approval. Know exactly
what you are allowing, though — `minni_learn` does two things per call. It
stage the `proposed` candidate, and it also writes a Markdown note into the
agent's **own vault** immediately (`wiki/sessions/`, audit-logged, with
`vault_ingest` sweeping personal wiki pages in the background while accepted
shared learnings are synchronously indexed by the daemon). The quality gate blocks weak
content by default (`requireQuality` defaults to `true`, enforcing `>= 15`
non-whitespace chars and `>= 4` meaningful tokens after stripping runtime
wrappers; a caller must pass `requireQuality: false` to store a weak note
deliberately, though credential-shaped values are never opt-outable). Allowlisting
the tool therefore means trusting the agent to write its own vault notes unprompted —
the shared tier still waits for a resolution decision.

The initiative grant is intended for the primary agent you are working with,
not necessarily for short-lived helper subagents it spawns: a spawned helper
handed the same host-level `minni_learn` tool access can call it under that
runtime's stamped principal, so scope MCP tool allowlists per subagent at the
runtime host level when you want helpers restricted to read-only recall/drill
or compact Thread updates. What is battle-tested daily is the core multi-agent
loop — several approved principals (e.g. `claude-code`, `codex`, `gemini`, and
`grok-build`) sharing one daemon, orienting via Living Repo Maps, staging and
resolving candidates, and handing off slices — because Minni is developed using
Minni. Automatic worker process spawning and immediate cross-host wake are
deliberately out of scope; hosts start and supervise their own workers.

**Approval.** Who gets to resolve candidates is decided per principal, and the
operator can delegate it. Four resolution paths exist, all landing in the
same audit trail:

1. **Manual (the default).** A human (or the local operator session) calls
   `resolve_candidate`. On a fresh install the anonymous local caller over the
   Unix socket is the synthesized `main` operator, so this works with zero
   configuration.

2. **A trusted agent.** Author `~/.minni/principals/<agent>.json` (mode 0600)
   granting the agent governance capability:

   ```json
   {"agent_id": "codex", "capabilities": ["learn", "resolve_candidate"]}
   ```

   With `resolve_candidate` (or `govern`, or `*`) in its capability list, that
   agent may resolve candidates itself and may use the `force=true` durable
   learn — its memory writes no longer wait for a human. Without it, the same
   agent can still `learn` (staging candidates) but a `force` attempt is
   denied `operator_only`. Principal files are read per request; no daemon
   restart is needed.

   Grant least privilege: any of these capabilities makes the principal an
   **operator**, which carries governance authority beyond approving its own
   candidates. In particular, adding `search` to an operator grant widens
   recall — the default combined scope can surface results from other agents'
   vaults — so scope capabilities (and `allowed_vault_roots`, if you use it)
   to what the delegation actually needs.

3. **The background consolidation pass** *(opt-in; functional since
   [#119](https://github.com/infektyd/minni/issues/119) closed)*. Set
   `MINNI_AFM_LOOP=on` (also `1`/`true`/`yes`) to enable the daemon's AFM
   loop; default is **off**, and `MINNI_AFM_LOOP=off` is an explicit kill
   switch. When enabled, consolidation drains staged candidates on a schedule.
   Candidates with explicitly safe privacy levels (`safe`, `public`, `low`) that
   pass deterministic quality, deduplication, and non-instruction gates are
   auto-promoted into durable learnings with `resolved_by=afm-consolidation`.
   Learn-only candidate packets clamped to `privacy=review` are treated as
   examinable (`_EXAMINABLE_PRIVACY`), enabling the fleet's staged proposals to
   re-enter the consolidation drain and be deduplicated or quality-evaluated rather
   than permanently parking behind the `afm_review` fence (`examined=0`).
   Candidates passing quality triage stay parked as `proposed` for operator
   decision via `resolve_candidate` (they are never auto-promoted without
   operator authority), while synthesized wiki drafts land in the shared vault
   (`~/.minni/vault`) as `agent: afm-loop`. Unset/NULL privacy is strictly
   non-examinable and remains parked (I1/I2 security fence). The gate, promotion
   write, and loop path are implemented and covered by regression tests
   (`tests/test_afm_loop_promotion.py`, `tests/test_afm_review_privacy_examinable.py`).
   `minni doctor` does **not** fully wet-exercise this pass (it stays green
   whether the loop is on or off), so enable deliberately and watch daemon logs
   / `consolidation_actions` if you rely on it. (`MINNI_AFM_MODE` is unrelated
   — mode only toggles an advisory triage annotation that the promotion gate
   never consults.)

4. **Auto-accept an agent's own candidates** *(opt-in, per principal;
   [#290](https://github.com/infektyd/minni/issues/290))*. Add
   `"auto_accept_own": true` to `~/.minni/principals/<agent>.json`:

   ```json
   {"agent_id": "claude-code", "capabilities": ["learn"], "auto_accept_own": true}
   ```

   That agent's own `learn` candidates resolve to durable at store time,
   stamped `resolved_by=auto_accept_own(<agent>)` so an audit can tell them
   from a human resolve.

   This exists because path 2 is a blunt instrument for the common case. If
   what you want is "this agent's learnings are its own", granting
   `resolve_candidate` also hands it governance authority over *every*
   candidate, including other agents'. `auto_accept_own` grants strictly less:
   the agent still cannot call `resolve_candidate(accept)` — on its own
   candidates or anyone's — and still gets `operator_only` if it tries.

   The gates stay on, and they are the same set path 3 uses: content that is
   instruction-like, too short, a duplicate of an existing learning, outside
   the safe privacy levels **as the author declared it**, or failing the
   structural quality gate falls back to `proposed` for a human, and the
   response names the check that withheld it in `auto_accept_withheld`.
   Nothing is dropped. Auto-accepted learnings are embedded and hashed like
   any other durable write, so contradiction detection and the duplicate gate
   keep seeing them — an unattended writer that is invisible to its own guards
   gets blinder the more it writes.

   One consequence worth knowing before you opt an agent in: this is the first
   path that lets a `learn`-only agent OWN a durable learning without a human
   (a manual accept preserves the proposer as `learnings.agent_id` and records
   the resolver in `resolved_by`). Owning one
   is the precondition for `resolve_contradiction`, so that handler now applies
   the same floor to non-operators rather than being an unguarded way around it.
   Counts land in `health_report` under
   `memory_lifecycle.resolution_mix` (`auto_accept_own` / `manual` /
   `afm_consolidation`) — check there to see whether the channel is doing what
   you expected.

   **The knob is operator configuration and is refused over the wire.** A
   caller that passes `auto_accept_own` to `minni_learn` gets `operator_only`
   — for every principal, operator included — because config an agent can set
   per-call is a self-approval bypass with extra steps. Set it in the
   principal file, which is 0600 and outside any agent-writable vault.

   Per-agent only: a hosted agent listed in `platform_agent_ids` does **not**
   inherit the operator's `auto_accept_own`. Opt one in explicitly with
   `"platform_agent_auto_accept_own": {"codex": true}`, which mirrors how
   `platform_agent_capabilities` works — so temporary and hosted agents stay
   proposal-gated unless named.

One operational caveat: creating your **first** `principals/*.json` file flips
the daemon into strict identity mode, where the anonymous local caller is no
longer auto-elevated to operator. If you add per-agent grants, also author a
`principals/main.json` (for example `{"agent_id": "main", "capabilities":
["*"]}`) — or set `MINNI_LOCAL_OPERATOR` — so your own local sessions keep
operator access. That `main.json` remediation applies to the **anonymous**
caller only (one that omits `agent_id` entirely): a wire caller that
explicitly claims a reserved operator id (`main`/`operator`) is denied with a
`reserved_agent_id` diagnostic regardless of `main.json`, unless the daemon
itself runs with `MINNI_LOCAL_OPERATOR` set. Named agents (`claude-code`,
`codex`, …) always need their own `principals/<agent>.json` — see
[Provision agent identities](install.md#provision-agent-identities-principals).

Alongside the four verbs, sessions boot with the **Two-Part Permanent Boot Shelf**
(`layer1/core.md` up to `24 KB` un-truncated + `project_shelf` `<= 2,500` chars with
Living Repo Map subsystem cards, git-diff staleness notices, top repo scars, and
active plan summary, recovered automatically after `PostCompact`) and carry the
v1 lifecycle spine — `recall → drill → thread → learn` — in `<minni:context>` so
agents orient before editing and record durable findings before context is flushed.

Follow the [Thread workflow](thread-workflow.md) for copyable MCP arguments across the 4 compact Thread tools and 1-step Handoffs.

Durable, evidence-gated Threads (`minni_thread_create`, `minni_thread_status`,
`minni_thread_update`, `minni_thread_scar`) survive sessions and compaction,
backing multi-slice work through Markdown plan artifacts (`plan-*.md`) and
ordered event journals (`plan-*.log.md`):

- **4 compact `< 2 KB` tools, zero lease tokens**: `minni_thread_create` opens a plan (activating a per-session pointer `.runtime/active-thread.d/<sessionKey>.json` by default), `minni_thread_status` returns a `< 2 KB` summary of pending/in-progress slices, `next_action`, and capped scars, `minni_thread_update` advances slice statuses (`pending`, `in_progress`, `done`, `blocked`, `superseded`) under `withThreadPlanLock` without worker claim tokens or idempotency keys, and `minni_thread_scar` records `failed_command`, `dead_end`, or `rejected_hypothesis` entries (`< 1 KB` receipt) while promoting workspace scars.
- **Evidence & dependency enforcement**: Marking a slice `done` requires non-empty `evidence` and satisfied `depends_on` slices (`done` or `superseded`) unless `force: true` and a non-empty `force_reason` are supplied (`depends_on_override` journaled).
- **Multi-session notification relay**: Concurrent sessions isolate active plans per `sessionKey` and receive concise state deltas through composite `<agent>:<sessionKey>` relay cursors (`.runtime/thread-relay/cursors.json` + Python `thread_relay.py`), while operators inspect active plans, pending handoffs, and scars in plain Markdown via `minni activity` and `~/.minni/TODAY.md`.

## Recall is evidence, not instruction

Every recall result is wrapped in an evidence envelope carrying provenance:
source path, owning agent, score, review state, privacy level, and a
personal-vs-shared leg marker (`src: "p"` / `src: "c"` in the RPC payload).
The framing is enforced at the data layer: instruction-like content in stored
documents is detected and reversibly perturbed before it can reach a prompt
with authority, and recalled text is presented as material to weigh, not text
to obey. Combined with the propose→approve gate, this is Minni's
memory-poisoning defense: nothing writes itself into durable memory, and
nothing recalled speaks with the operator's voice.

## Two-tier storage

- **Personal tier** — each agent's vault wiki (`<agent>-vault/wiki/**/*.md`) is
  indexed on write (`vault_index_doc` / `index_single_file`) and by the background
  `vault_ingest` pass into that vault's own `.index/vault.db` + `vault.faiss` +
  `vault.manifest.json`.
- **Shared tier** — `~/.minni/minni.db` (SQLite, FTS5, WAL) holds `workspace_id`-scoped
  durable `learnings` and `documents`, Living Repo Map `repo_map_cards`
  (`~/.minni/vault/maps/<workspace>/`), typed `memory_links` / `learning_links`
  (`updates`, `contradicts`, `extends`, `relates`), `candidate_packets`,
  `handoff_leases`, and episodic/contradiction events, paired with a shared FAISS
  index and the auto-maintained `~/.minni/TODAY.md` (`0600`) operator digest.

Recall merges the tiers by `scope`: `personal` (caller's index, falling back
to shared), `combined` (all per-vault indexes plus shared, pre-filtering foreign
vaults via `_should_query_foreign_vault`), or `both` (the
default — personal and combined merged, deduplicating the caller's own hits),
while enforcing principal, workspace, and `privacy_level` read authorization across
`search`, `read`, `minni_drill`, and `project_shelf`.
Learnings always come from the shared DB. Full provenance — owning agent,
source vault, score components, `indexed_at`, and typed graph links — is available
via `minni_drill` (`m:<8hex>` handles, `d:<src>:<id>` refs, chunk IDs, or wikilinks,
including repo-map path wikilinks).

Vaults and `~/.minni/TODAY.md` are the human-readable surfaces: `layer1/core.md`
(standing agent identity), `wiki/` (synthesis pages, plan notes, session learning
notes), `inbox/` (candidate drafts, hook packets), `outbox/` (outgoing handoffs),
`logs/log.md` (append-oriented audit trail), and CLI views (`minni status`,
`minni activity`, `minni watch`, `minni doctor`). Agents use the 12 MCP tools and
daemon contracts instead of scraping another agent's private vault directly.

## Retrieval, Living Repo Maps & Typed Graph

The retrieval stack (`src/minni/retrieval.py` + `trie_index.py`, `fts_search.py`,
`semantic_rerank.py`, `repo_map.py`, `graph_coordinator.py`) is a workspace-aware
hybrid pipeline:

- **Living Repo Maps (`minni map [<repo>]`)**: synthesizes Subsystem Map Cards
  (`~/.minni/vault/maps/<workspace>/` + `repo_map_cards`) from `AGENTS.md`, `docs/`,
  and source symbols pinned to owning file paths and `git` HEAD SHA (storing maps
  and scars rather than raw line-by-line source code). On `minni_recall`,
  `minni_drill`, and `SessionStart`, a bounded `300ms` `git diff --name-only`
  check stamps cards whose owned files changed since build (`[Note: Map built at <sha7>; changed since then: ...]`) and queues a debounced background refresh.
- **Tier 1 (in-memory prefix/token Trie, `< 0.5 ms`)**: exact/prefix lexical
  lookup (`TrieIndexMixin` in the daemon and `searchVaultNotes` Trie in
  `plugins/minni/src/vault.ts`), invalidated on `(vault_path, mtime_ns)`.
- **Tier 2 (SQLite FTS5/BM25, `< 10 ms` `mode="fast"`)**: deterministic lexical
  recall (`FtsSearchMixin`) paired with Tier 1 Trie boosts for sub-10 ms hook
  and fast-mode queries without invoking neural inference.
- **Tier 3 (Repo-First hybrid vector + RRF + Cross-Encoder reranking + Typed Graph)**:
  FAISS vector search (`faiss-disk` / `faiss-mem`) fused with FTS5 via weighted
  Reciprocal Rank Fusion (`0.60` FTS / `0.40` semantic), reserving the top-10 FTS
  BM25 slots prior to Cross-Encoder reranking (`SemanticRerankMixin`) and enforcing
  the post-rerank confidence floor across **all** candidates (including single-token
  FTS overlap and HyDE enrichment, stripping blank `<EVIDENCE>` bodies). Ranking
  prioritizes **current `workspace_id` + `global`** first (holding foreign workspaces
  to a high cross-repo bar), expands typed `memory_links` (`expand_typed_graph`),
  automatically retires superseded durable projections via `GraphCoordinator` +
  `EdgeClassifier` (with chunked local AFM and deterministic fallback), and packs
  MMR-diverse results across `headline` / `snippet` / `chunk` / `document` depths.

## The AFM pass pipeline

Background curation runs as discrete passes under `src/minni/afm_passes/`:

| Pass | Role |
|---|---|
| `vault_ingest` | Builds each agent's personal `.index` from its wiki |
| `inbox_ingest` | Ingests hook-written inbox files into `candidate_packets` |
| `compact_distillation` | Distills harvested `compact_summary` inbox files into shared candidates + personal session notes (see [Compaction-summary harvest](#compaction-summary-harvest)) |
| `consolidation` | Promote / dedupe / mark-for-review triage of staged candidates |
| `synthesis` | Sourced synthesis pages in the vault wiki |
| `session_distillation` | Distills session transcripts into candidate material |
| `procedure_extraction` | Extracts reusable procedures |
| `inbox_archive`, `pruning`, `reorganization` | Hygiene: archive processed inbox files, age out stale material, reorganize |

Note the division of labor in the learning path: **`inbox_ingest`** moves
inbox files into `candidate_packets`; **`consolidation`** then proposes
promote/dedupe/review decisions that the daemon applies according to the
configured gates. Raw transcripts, status packets, hook envelopes, test junk,
and unverified claims route to review or rejection, not active memory.
Non-operator proposals clamped to `privacy=review` are drained and evaluated
for deduplication and quality triage, but are never auto-promoted without
operator resolution.

## Compaction-summary harvest

When a platform compacts its context it distills the session into a
structured summary — the highest-signal outcome document a session produces.
Minni harvests it in two stages, split across the boot/daemon boundary
([#194](https://github.com/infektyd/minni/pull/194),
[#196](https://github.com/infektyd/minni/pull/196)):

1. **Hook-side harvest (fast, fail-open).** On Claude Code, the `PostCompact`
   hook is the primary delivery path — it receives the summary directly, no
   transcript read. A `SessionStart` transcript tail-read is the backstop for
   summaries `PostCompact` missed (hook not yet registered, older CLI, a crash
   between compaction and the hook firing) — see
   `extractLatestCompactSummary` in
   `plugins/minni/src/compact-harvest.ts`. On Kilo Code, the SDK read-back
   fires after the native `session.compacted` bus event (`experimental.session.compacting`
   fires too early — before the summary exists — so the plugin fetches it
   afterward via the SDK client; see `plugins/minni/kilo/minni-plugin.js`).
   Platforms that share `createHookHandlers` (codex, grok-build, cursor, gemini)
   run the SessionStart transcript backstop when the payload marks `source` as
   compact/resume. A path-only residual (no `source`, non-empty
   `transcript_path`) is **opt-in** via an allowlist that is **empty until a
   live transcript is verified Claude-shaped** (`isCompactSummary: true`).
   Cursor's platform contract warns not to mine `transcript_path` blind; codex
   and grok-build transcript shapes are also unverified for this extractor, so
   they are not allowlisted either. Hosts that always stamp a non-compact
   `source` (e.g. `startup`) never hit path-only either — path residual
   requires `source` absent entirely. Default off also protects hosts that
   always attach a path and never send `source` (agy/gemini) from every-cold-boot
   I/O for a guaranteed miss against the tightest SessionStart budget
   ([#227](https://github.com/infektyd/minni/issues/227)). On shared platforms
   this SessionStart path is the **only** capture write (no `PostCompact`), so
   compact|resume harvest is **fully awaited** — matching Claude Code — rather
   than raced with `withBudget` (a race + `exitAfterDelivery` would kill
   mid-flight and permanently drop the write). Path-only residual stays
   wait-capped; `withBudget` does not cancel the tail-read, so path-only is not
   budget-safe concurrent I/O — only wait-capped before identity/corrections
   RPCs. Kilo Code uses the shared handler too, but
   its bridge SessionStart sends neither `transcript_path` nor `source`, so that
   backstop is dead for real Kilo boots (primary remains `CompactSummary` from
   the SDK). Both live paths converge on `harvestSummaryText`: the
   continuation-frame boilerplate is stripped, the text is capped, and it is
   written verbatim to the agent's vault `inbox/` as one file with
   `kind: "compact_summary"`. Dedup is keyed on a **content sha1** of the
   frame-stripped summary (not the platform's summary id), persisted under
   `<vault>/.runtime/compact-harvest-state.json` — a content key is what lets
   two delivery paths coexist on Claude Code without double-harvesting.

   **Platform capability table (compaction-summary harvest):**

   | Platform | Primary delivery | SessionStart transcript backstop | Notes |
   |----------|------------------|----------------------------------|-------|
   | Claude Code | `PostCompact` → `harvestSummaryText` | yes (`source` compact/resume only) | full path |
   | Kilo Code | `session.compacted` SDK read-back → `CompactSummary` | no (bridge supplies no transcript_path / source) | primary only |
   | Codex | `hooks-codex.json` `PostCompact` → `harvestCompactSummary` | yes (`source` compact/resume only) | receives `{session_id, transcript_path, ...}` and harvests from transcript tail via `harvestCompactSummary`; `SessionStart` scan is next-boot backstop |
   | Grok Build | — | compact/resume `source` only | no compaction hook events; relies on `SessionStart` transcript-tail scan when `transcript_path` is provided; path-only not allowlisted |
   | Cursor | — | compact/resume `source` only | no post-compaction hook event (`preCompact` only); relies on `SessionStart` (`source: "compact" | "resume"`) transcript-tail backstop; path-only not allowlisted |
   | Gemini / agy | — | compact/resume `source` only | always attaches `transcriptPath`, never `source`; path-only would I/O every cold boot for zero yield (non–Claude-shaped transcript) |

   `hooks-codex.json` wires `PostCompact` (which receives `{session_id, transcript_path, ...}`
   and harvests from the transcript tail via `harvestCompactSummary`), with `SessionStart`
   transcript-tail scan as the next-boot backstop. Cursor has no post-compaction hook event
   (`preCompact` only) and relies on the `SessionStart` (`source: "compact" | "resume"`)
   transcript-tail backstop, while Grok has no compaction hook events and relies on `SessionStart`
   transcript-tail scan when `transcript_path` is provided. Platforms without a Claude-shaped
   `isCompactSummary` transcript entry still *have* a harvest path when the gate fires (they no
   longer drop the event on the floor by absence of code), but capture is a no-op until the
   platform emits a compatible summary shape or a direct `CompactSummary`/`summary_text`
   delivery. Path-only stays empty-allowlist so the residual is not paid as an every-boot I/O
   cost on unverified hosts. That residual is declared here rather than discovered.
2. **Daemon-side distillation (`compact_distillation` AFM pass).** The same
   consolidation timer that ingests stop-candidate learnings picks up
   `compact_summary` inbox files (`src/minni/afm_passes/compact_distillation.py`).
   It splits the summary into numbered sections (whole-body fallback for
   unsectioned summaries) and routes each section by **audience**:
   sections matching `_SHARED_SECTION_TITLES` (Key Technical Concepts, Errors
   and Fixes, Problem Solving, Key Learnings/Learnings, Decisions) are
   transferable knowledge and go to the shared `candidate_packets` queue,
   AFM-distilled via the native `session_distill` op when available
   (deterministic section-flatten fallback otherwise). Everything else is
   session-personal narration and never reaches the shared pool — it is
   written only to a personal vault note,
   `wiki/sessions/<date>-compact-<session>-<hash>.md`, in the *source* vault,
   where the vault-watch sweep indexes it for that agent alone. The one
   exception: the unsectioned whole-body fallback can still earn the shared
   pool, but only if AFM actually distilled it into a crisp assertion — with
   AFM off, unsectioned summaries stay personal. Candidate content passes a
   local-path/secret scrub before insert, since summaries quote session
   content verbatim.

**Archive-on-insert lifecycle.** `compact_distillation` reuses the
`inbox_ingest` idempotency contract: it keys derived rows on
`(inbox_file, candidate_index)` with `derived_from.source == "inbox"`, which is
exactly what `inbox_archive` looks for — so once a summary's candidates all
reach a terminal state, the existing archive pass retires the inbox file with
no new code (moved, never deleted). A file that yields **no** shared candidate
has no candidate row to key idempotency on, so once its personal session note
is written, `compact_distillation` archives it immediately itself — otherwise
it would be rescanned every consolidation tick forever.

The harvest only proposes; nothing here writes a learning directly — shared
candidates still go through the normal propose→approve gate above.

## Core invariants

| Invariant | Meaning |
|---|---|
| Identity & boot shelf load whole | `layer1/core.md` (up to `24 KB`) and `project_shelf` (`<= 2,500` chars) are never chunked or floor-crushed |
| Maps orient, knowledge cites | Living Repo Maps (`minni map`) track SHA-pinned subsystems and scars; docs/learnings load by need with `<EVIDENCE>` provenance |
| Recall is evidence | Retrieved content is never automatically instruction, and hooks are strictly non-blocking across all hosts |
| Learning is proposal-first & graph-linked | `learn` stages a candidate; accepted learnings wire `memory_links`, auto-supersede stale facts, and refresh `~/.minni/TODAY.md` |
| Documents are two-tier & workspace-scoped | Personal `.index` per vault + shared pooled layer tagged by `workspace_id` (`workspace-<repo>` / `global`) |
| Learnings are shared-DB | Durable learnings, repo maps, memory links, candidates, leases, and events live in `~/.minni/minni.db` |
| Vaults are per-agent | Shared daemon, separate human-readable workspaces, separate personal indexes, zero localhost web UI |
| Local transport first | Unix socket by default; provider calls are explicit and gated |
