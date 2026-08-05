# Concepts

## The four verbs

| Verb | What happens | Surface |
|---|---|---|
| **Recall** | Cited, provenance-tagged retrieval across the personal and shared legs | `minni_recall`, `minni_drill`, `minni_route`, `minni_export_pack` |
| **Learn** | Propose, don't write: stages a `candidate_packets` row with status `proposed` and returns a `candidate_id`. No durable memory is written on this path | `minni_learn`, `minni_learning_quality` |
| **Approve** | A later resolution decision — accept / reject / redact / log-only / merge / supersede / do-not-store. Only accepting decisions write or keep a durable learning row, disk note, and index entry | `minni_resolve_candidate` |
| **Handoff** | Explicit cross-agent transfer under a lease; the receiver acks before the sender releases | `minni_negotiate_handoff`, `minni_ack_handoff`, `minni_await_handoff`, `minni_list_pending_handoffs` |

There is exactly one escape around the approve gate: `force=true` on `learn`
writes a durable learning directly, **only** for an operator principal, and is
audit-stamped `FORCE_DURABLE_LEARN`. A non-operator force attempt is denied
with an `operator_only` error.

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
stages the `proposed` candidate, and it also writes a Markdown note into the
agent's **own vault** immediately (audit-logged, and indexed into that
agent's personal tier by `vault_ingest`). The quality gate blocks weak
content by default (`requireQuality` defaults to `true`; a caller must pass
`requireQuality: false` to store a weak note deliberately). Allowlisting the tool
therefore means trusting the agent to write its own vault notes unprompted —
the shared tier still waits for a resolution decision.

The initiative grant is intended for the primary agent you are working with,
not for anything it spawns: temporary team agents (`minni_team_*`) carry
`learn: "manual-only"` in their memory policy. Note this is an instruction
serialized into the team packet for host adapters to honor, not a boundary
the daemon enforces — a spawned helper handed the same host-level
`minni_learn` tool access can still call it, so scope tool permissions at
the runtime level if that matters for your setup. A note
on maturity while we're here: the temporary-team surface itself hasn't been
exercised beyond its unit tests yet. What *is* battle-tested daily is the
core multi-agent loop — several approved principals (e.g. `claude-code` and
`codex`) sharing one daemon, staging and resolving each other's candidates —
because Minni is developed using Minni. At that scale it holds up; the team
harness is the untested frontier.

**Approval.** Who gets to resolve candidates is decided per principal, and the
operator can delegate it. Three resolution paths exist, all landing in the
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
   switch. When enabled, consolidation drains staged candidates on a schedule,
   auto-promoting the low-risk subset — explicitly safe privacy level, not
   instruction-like, not a duplicate, passing the deterministic quality gate —
   into durable learnings with `resolved_by=afm-consolidation`, and routing
   everything spicier to review. The gate, promotion write, and loop path are
   implemented and covered by regression tests (`tests/test_afm_loop_promotion.py`).
   `minni doctor` does **not** fully wet-exercise this pass (it stays green
   whether the loop is on or off), so enable deliberately and watch daemon logs
   / `consolidation_actions` if you rely on it. (`MINNI_AFM_MODE` is unrelated
   — mode only toggles an advisory triage annotation that the promotion gate
   never consults.)

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

Alongside the four verbs, sessions carry a lifecycle spine —
`prepare_task → prepare_outcome → thread → learn` — injected via the
`<minni:context>` envelope so agents orient before ambitious work and distill
before context is flushed.
Durable, evidence-gated threads (`minni_thread_*`) survive sessions and
compaction.

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
  indexed by the `vault_ingest` pass into that vault's own
  `.index/vault.db` + `vault.faiss` + `vault.manifest.json`.
- **Shared tier** — `~/.minni/minni.db` (SQLite, FTS5, WAL) holds durable
  learnings, candidates, episodic/contradiction events, handoff leases, and
  the pooled document layer, with a shared FAISS index for vectors.

Recall merges the tiers by `scope`: `personal` (caller's index, falling back
to shared), `combined` (all per-vault indexes plus shared), or `both` (the
default — personal and combined merged, deduplicating the caller's own hits).
Learnings always come from the shared DB. Full provenance — owning agent,
source vault, score components, `indexed_at` — is available via `minni_drill`.

Vaults are the human-readable surface: wiki (synthesis pages, handoff notes,
learning notes), inbox (candidate drafts, hook packets), outbox (outgoing
handoffs), logs (append-oriented audit trail). Agents use the plugin/daemon
contracts instead of scraping another agent's private vault directly.

## Retrieval

The retrieval stack is a hybrid pipeline: lexical search (SQLite FTS5/BM25) +
vector search (FAISS) fused with reciprocal-rank fusion, cross-encoder
reranking, optional NLI claim-attribution scoring, MMR-diverse token-budgeted
packing, and progressive depth tiers (`headline` / `snippet` / `chunk` /
`document`) — merged across the personal and shared legs by recall scope.

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
   | Codex | — | compact/resume `source` only | path-only not allowlisted (Claude-shaped JSONL unverified); capture no-op until shape or direct delivery |
   | Grok Build | — | compact/resume `source` only | harvest is a side effect (SessionStart is not injectable); path-only not allowlisted |
   | Cursor | — | compact/resume `source` only | do not mine `transcript_path` blind (undocumented schema); cold boots with non-compact `source` skip; path-only not allowlisted |
   | Gemini / agy | — | compact/resume `source` only | always attaches `transcriptPath`, never `source`; path-only would I/O every cold boot for zero yield (non–Claude-shaped transcript) |

   Platforms without a Claude-shaped `isCompactSummary` transcript entry still
   *have* a harvest path when the gate fires (they no longer drop the event on
   the floor by absence of code), but capture is a no-op until the platform
   emits a compatible summary shape or a direct `CompactSummary`/`summary_text`
   delivery. Path-only stays empty-allowlist so the residual is not paid as an
   every-boot I/O cost on unverified hosts. That residual is declared here
   rather than discovered.
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
| Identity loads whole | Agent identity and standing rules are not chunked |
| Knowledge loads chunked | Large docs/history are retrieved by need and cited |
| Recall is evidence | Retrieved content is never automatically instruction |
| Learning is proposal-first | `learn` stages a candidate; durable memory requires approval or the audited operator escape |
| Documents are two-tier | Personal `.index` per vault + shared pooled layer, merged by scope |
| Learnings are shared-DB | Durable learnings, candidates, leases, events live in `~/.minni/minni.db` |
| Vaults are per-agent | Shared daemon, separate human-readable workspaces, separate personal indexes |
| Local transport first | Unix socket by default; provider calls are explicit and gated |
