# README audit — 2026-08-01

A real run of the `readme-audit` procedure against `README.md` at commit
`095c264`. It doubles as the skill's worked example: read it for calibration on
the evidence standard, the worthiness bar, and how the retirement rule behaves
when a README is *under* budget.

**None of the changes below were applied.** This was the validation run that
shipped with the skill; the first real audit PR is a separate later run.

---

## Headline

| | Count |
|---|---|
| Prose claims extracted and judged | **37** |
| — CORRECT | 36 |
| — STALE | **1** |
| — UNVERIFIABLE | 0 |
| Diagram elements judged (17 nodes + 13 edges) | **30** |
| — CORRECT | 24 |
| — STALE | **2** |
| — UNVERIFIABLE | **4** |
| **Total items checked** | **67** |
| Missing-capability candidates found | 9 |
| — cleared the worthiness bar | 1 HIGH, 2 MEDIUM |
| — rejected | 6 |
| Diagram topology gaps ranked | 5 (2 HIGH, 3 MEDIUM) |
| Retirement candidates named | 4 (0 forced by budget in prose; **1 forced in the diagram**) |
| Redesign proposals | 5 (**none applied**) |
| Findings outside the README | 2 |

**The one-line verdict:** the README's *facts* are in excellent shape and its
*picture* is not. Prose scored 36/37 because two repo-hygiene passes merged the
day before this audit (`69e8603`, `66e80b2`) swept exactly that dimension. The
diagram was not part of those sweeps, and it shows: it is the only part of the
README carrying stale content, and it is missing two component surfaces the
README's own prose already advertises.

That asymmetry is the general lesson. **Prose gets re-read; diagrams do not.**

### Content watermark

`git log -- README.md` shows edits on 2026-07-31, but reading the diffs, both
were hygiene (leak removal, version refresh, link/doc updates) — they moved
facts, not coverage. The last commit that added *new coverage* was **2026-07-04
(`f4039fe`)**, and that is the watermark Phase 3 swept from. A README touched
yesterday can still have a four-week-old watermark; recency of commit is not
recency of content.

---

## Phase 0 — Meter

```
section                                  lines         words      rows
----------------------------------------------------------------------
(preamble)                                8/10         23/40         0
The problem                                  5       100/110         0
What Minni is                               16       271/300         0
Recall is evidence, not instruction          5       125/140         0
How it compares                             13       115/140       5/6
Quickstart                               66/70       491/550         0
Architecture at a glance                 40/45        58/90          0
Status                                       5       220/240         0
Documentation                               12          0/20       8/10
Support                                      5         26/40         0
(total)                                175/190     1429/1600        13

mermaid @ line 122: 17/18 nodes, 13/16 edges
```

Under budget everywhere: 89% of the total word cap, 92% of the line cap. The
diagram is at 94% of its node cap with **one spare node** — which is what makes
Phase 4 interesting this run.

---

## Phase 1 — Claim verification

37 falsifiable claims. Grouped by section; evidence is the file actually opened.

### Preamble and badges (lines 1–8)

| # | Claim | Line | Verdict | Evidence |
|---|---|---|---|---|
| 1 | Badge: python 3.14 | 8 | CORRECT | `pyproject.toml:17` `requires-python = ">=3.14"` |
| 2 | MIT licence | 7 | CORRECT | `LICENSE`, `pyproject.toml` |
| 3 | CI and PyPI badges resolve | 5–6 | CORRECT | `.github/workflows/ci.yml` exists; package published |

### What Minni is (lines 16–31)

| # | Claim | Line | Verdict | Evidence |
|---|---|---|---|---|
| 4 | Single local daemon `minnid` over a Unix socket | 18 | CORRECT | `src/minni/minnid.py` |
| 5 | Typed MCP surface | 18 | CORRECT | 37 `minni_*` tools registered in `plugins/minni/src/server.ts`, incl. 11 `minni_plan_*` |
| 6 | **Markdown vault per agent "(wiki / inbox / outbox / logs)"** | 18 | **STALE** | Bootstrap creates **six** dirs: `raw, wiki, logs, schema, inbox, outbox` — `src/minni/wire/writers.py:576`. The README lists four. |
| 7 | Personal store at `<agent>-vault/.index/` | 18 | CORRECT | index path construction in the vault/index layer |
| 8 | Shared store `~/.minni/minni.db` holds learnings + pooled docs | 18 | CORRECT | daemon storage layer |
| 9 | Daemon-mediated durable writes pass an identity-and-capability gate | 18 | CORRECT | `EffectivePrincipal` gate in the daemon runtime |
| 10 | Recall = lexical + vector + rank fusion + rerank | 22 | CORRECT | all four stages present in `src/minni/retrieval.py` |
| 11 | `learn` stages a candidate, not a memory | 23 | CORRECT | governance candidate staging |
| 12 | `resolve_candidate` accepts / rejects / redacts / merges / supersedes | 24 | CORRECT | exact action set at `src/minni/.../governance.py:730+` |
| 13 | Human-gated by default, delegable to a trusted agent | 24 | CORRECT | delegation config path |
| 14 | Background AFM auto-consolidation functional since #119 closed | 24 | CORRECT | issue #119 confirmed closed; AFM consolidation pass present |
| 15 | Handoff = cross-agent transfers with leases | 25 | CORRECT | lease handling in `minnid_runtime/handoff.py` |
| 16 | No hosted dependency, no cloud tier | 27 | CORRECT | cloud transports are stubs (P4–P6) in `providers.ts`; no `providers.json` ships |

### Recall is evidence, not instruction (lines 33–37)

| # | Claim | Line | Verdict | Evidence |
|---|---|---|---|---|
| 17 | Evidence envelope carries source, owning agent, score, review state | 35 | CORRECT | envelope fields in the retrieval return path |
| 18 | Instruction-shaped content defused at the data layer, pre-prompt | 35–37 | CORRECT | `src/minni/safety.py:102` invoked from `src/minni/retrieval.py:236-246` — i.e. genuinely at the data layer, as claimed |

### How it compares (lines 41–51)

| # | Claim | Line | Verdict | Evidence |
|---|---|---|---|---|
| 19 | Minni cell: memory lives on your machine (Markdown + SQLite) | 43 | CORRECT | vault + `minni.db` |
| 20 | Minni cell: multi-agent, one governed daemon | 44 | CORRECT | per-agent vaults, one socket |
| 21 | Minni cell: proposal-first, approval-gated (human by default, delegable) | 45 | CORRECT | see #11–#13 |
| 22 | Minni cell: readable in an editor | 46 | CORRECT | plain Markdown vaults |
| 23 | Minni cell: no benchmark claims published | 47 | CORRECT | `bench/membench` exists; no numbers in README or docs |
| 24 | Caveats: early v0.4, tiny adoption, no hosted/multi-device option | 51 | CORRECT | `pyproject.toml:14` `version = "0.4.0"` |
| 25 | Footprint: running daemon + FAISS/embedding models + Node >= 20 | 51 | CORRECT | `plugins/minni/package.json:30-32` `"node": ">=20"` |

### Quickstart (lines 53–118)

| # | Claim | Line | Verdict | Evidence |
|---|---|---|---|---|
| 26 | Payload bundled in wheels since v0.3; current release v0.4.0 | 55 | CORRECT | `CHANGELOG.md` 0.3.0; version agrees across `pyproject.toml`, `package.json`, `.claude-plugin/plugin.json`, git tag |
| 27 | Python >= 3.14 | 59 | CORRECT | `pyproject.toml:17` |
| 28 | First recall downloads ~320 MB of models, announced, one time | 59 | CORRECT | `src/minni/minni_cli.py:49` `MODELS_TOTAL_NOTE = "~320 MB, one time, cached in your HuggingFace cache"` |
| 29 | `minni up` / `doctor` / `down` exist and do what is said | 62–67 | CORRECT | `src/minni/minni_cli.py:372,375,377` |
| 30 | Plugin installs to `~/.minni/plugin/<version>/` | 71 | CORRECT | `src/minni/wire/paths.py` |
| 31 | Platform list, `all` expansion, Gemini provisional/skipped-with-warning, antigravity + `generic` individual-only | 87 | CORRECT | `src/minni/wire/platform.py` — verified as four separate sub-claims |
| 32 | Cursor is not yet wired by the `minni wire` CLI | 87 | CORRECT | absent from the CLI's platform set; `src/minni/wire/platform.py` |
| 33 | Three post-wire probes: MCP handshake, hook dry-run, config readback | 87 | CORRECT | `src/minni/wire/verify.py:121-137` |
| 34 | Reference-aware GC; `--use-version` rollback | 87 | CORRECT | `src/minni/wire/gc.py`, `src/minni/wire/install.py` |
| 35 | `python -m minni.minnid_client --socket ... search "..."` | 94 | CORRECT | module and subcommand exist |
| 36 | Docker: `ghcr.io/infektyd/minni:latest`, `-v minni-data:/home/minni` | 111 | CORRECT | `Dockerfile` home path matches; ghcr publish workflow present |
| 37 | `minni watch` tails recall/learn/guard; `npm run console` starts the console | 113–118 | CORRECT | `src/minni/minni_cli.py:402`; `plugins/minni/package.json:46` `"console": "npm run build && node dist/ui-server.js"` |

*(Status-section claims — PyPI OIDC trusted publishing, six-platform hook
coverage, hermetic CI smoke proving status/recall/home-isolation, membench with
no published numbers, `minni_team_*` unit-tested but unproven, and the v0.3
headline — were each verified CORRECT against the release workflow, the
per-platform compiled hooks, `.github/workflows/`, `bench/`, the team test
files, and `CHANGELOG.md` 0.3.0. They are folded into the count above.)*

**Every relative link and `#anchor` in the README resolves.**

### The single stale claim

Claim #6 is one word-list in one sentence, and it is worth dwelling on because
of *how* it was caught. Two independent verifiers looked at the same fact:

- the claims sweep read the sentence, checked that a per-agent Markdown vault
  exists, and returned CORRECT;
- the diagram sweep read the same inventory as a **node label** and went looking
  for the code that creates the directories — `wire/writers.py:576` — and found
  six, not four.

The claim is only false in its *completeness*, which is the failure mode a
sentence-level check waves through. **A list is a claim about what is not in
it.** Worth adding to the reader's instincts: enumerations get counted against
the source, never spot-checked.

Correct fix: `wiki / raw / inbox / outbox / logs / schema` in both README:18 and
the diagram node — or, better, drop the inventory from the prose (the diagram
already carries it) rather than maintaining the same list in two places.

---

## Phase 2 — Diagram audit

`README.md:122-157`, `flowchart TD`. 17 nodes, 13 edges, all judged.

### Nodes — 15 CORRECT, 1 STALE, 1 UNVERIFIABLE

| Node | Verdict | Evidence / note |
|---|---|---|
| Runtimes subgraph + Claude Code, Codex, Gemini/Antigravity, Grok, Cursor, Kilo Code, Any MCP client | CORRECT (8) | each has a real compiled hook / wiring path |
| `minni MCP plugin` | CORRECT | `plugins/minni/src/server.ts` |
| `minnid daemon` | CORRECT | `src/minni/minnid.py` |
| `EffectivePrincipal gate` | CORRECT | class name in code matches the label exactly |
| `Learn → candidate → approve` | CORRECT | governance path |
| `Handoff leases` | CORRECT | `minnid_runtime/handoff.py` |
| Personal index `<agent>-vault/.index/` | CORRECT | path matches |
| Shared `~/.minni/minni.db + FAISS` | CORRECT | FAISS is the vector backend |
| **Per-agent vaults `wiki / inbox / outbox / logs`** | **STALE** | six dirs at `src/minni/wire/writers.py:576` — same defect as claim #6 |
| `Recall — scope: personal · combined · both` | **UNVERIFIABLE** | the three scope literals were not read line-by-line in `retrieval.py` this run. Flagged as unchecked, **not** as correct. |

### Edges — 9 CORRECT, 1 STALE, 3 UNVERIFIABLE

| Edge | Verdict | Note |
|---|---|---|
| Runtimes → Plugin → Daemon → Gate (3 edges) | CORRECT | matches the real call path |
| Plugin → Vaults | CORRECT | the plugin does write vault files directly |
| Gate → Retrieval / Governance / Handoff (3 edges) | CORRECT | |
| Governance → Shared, Handoff → Shared | CORRECT | |
| **Vaults —"vault_ingest indexes wiki"→ Personal** | **STALE** | the label collapses **two** mechanisms into one: a batch `vault_ingest` pass and a live `vault_index_doc` RPC. Both exist; the reader learns a false thing about how indexing happens. Stale-by-conflation, even though nothing in the label is individually untrue. |
| Retrieval —personal leg→ Personal | UNVERIFIABLE | the leg split was not read line-by-line |
| Retrieval —shared leg→ Shared | UNVERIFIABLE | as above |
| Handoff → Vaults | UNVERIFIABLE | inferred from the handoff surface, not traced to a write |

Four UNVERIFIABLE edges/nodes out of 30 is the honest result of a
time-boxed run. They are listed so the next audit knows exactly where to start,
which is the entire reason UNVERIFIABLE is a first-class verdict instead of a
rounding error toward CORRECT.

### Topology drift — shipping components absent from the diagram

| Rank | Component | Evidence | Why it ranks here |
|---|---|---|---|
| **HIGH** | Web console / Memory Board | `plugins/minni/src/ui-server.ts`, `/api/*` | A whole **second client surface** — an HTTP one. The diagram asserts a single ingress (MCP plugin) and is wrong at the shape level, not the detail level. |
| **HIGH** | The plan surface (11 `minni_plan_*` tools) | `plugins/minni/src/server.ts:1226-1636` | Nearly a third of the tool surface. Recall/learn/handoff are drawn; plans are not. |
| MEDIUM | Temporary-team surface (`minni_team_*`) | tool registry | **The README's own Status section names it** (line 165) while the diagram omits it — the document contradicts itself. |
| MEDIUM | Compaction-summary harvest path | PRs #194/#196 | hook → vault inbox → AFM distillation is a real ingestion path with no line on the diagram. |
| MEDIUM | Hook injection as a path distinct from MCP calls | `plugins/minni/src/hook-platform.ts:80-194` | The diagram's single `Runtimes → Plugin` arrow conflates *agent calls a tool* with *host fires a hook*; the per-platform event sets differ, so this is not a detail. |

### Diagram prose

`README.md:159` — "Request flow: agent → MCP plugin → Unix socket → daemon →
identity gate → recall / learn / approve / handoff → Markdown + SQLite" — is
**CORRECT for the MCP path** but silently presents itself as *the* request flow.
With hook injection and the console both real, it needs a qualifier ("via MCP
tools"). Filed as a wording fix, not a stale claim.

### Diagram redesign — see Proposal 2 below

---

## Phase 3 — Missing claims

Nine candidates swept from `CHANGELOG.md`, merged PRs since 2026-07-04, new
`docs/` pages, and the skill/tool registries. **Six were rejected.** The
rejections matter more than the acceptances.

### Cleared the bar

**HIGH — compaction-summary harvest** (PRs #194/#196, plus fixups; documented in
`docs/concepts.md`, headlined in `CHANGELOG.md` 0.4.0)

Platform compaction summaries are harvested at the hook, stored to the agent's
vault inbox, and distilled on the AFM consolidation timer, with audience routing
that keeps session-specific context out of the shared pool.

- *For:* the README's own opening (line 12) names compaction as a core pain —
  "gets summarized away by compaction" — and the project now has a mechanism
  that directly answers it, which the README never mentions. This is the
  strongest possible case for an addition: the document already sold the problem
  and then failed to claim the solution.
- *Against:* the mechanism is intricate (audience routing, dedup, distillation
  passes) and belongs in `docs/concepts.md`, where it already is.
- *Resolution:* **one clause** in "What Minni is" claiming the capability, linking
  to concepts. The mechanism stays where it is. Cost: ~25 words, affordable
  inside that section's 29-word headroom.

**MEDIUM — the console is undersold** (PRs #150/#151/#152/#177/#179/#181/#192)

README:113-118 describes the console as "per-session receipts and a live
activity feed". It is now an infinite-canvas Memory Board with live daemon data,
free-layout zones, a staged-learnings zone, and traffic pulses.

- *For:* not a new claim but an inaccurate one by omission — this is an **edit to
  an existing sentence**, which is the cheapest kind of addition (Phase 3, bar 2).
- *Against:* observability is a post-install concern; an evaluator does not need it.
- *Resolution:* accept as a rewording, net ~0 words. No budget impact.

**MEDIUM — learn-gate detection of credential material** (#147/#182/#198)

The learn gate uses an AFM tier to catch unquoted multi-word passphrases.

- *For:* it is a *security property of the write path*, and security posture is
  something an evaluator weighs. The README makes governance claims and is silent
  on this one.
- *Against:* it is one heuristic inside a gate the README already describes as
  approval-gated; a reader may reasonably assume the gate does this.
- *Resolution:* **operator's call.** Best home is a sub-clause in "Recall is
  evidence, not instruction" — but that section is protected floor and
  compression-only, so it must not grow. Recommend: accept only if it displaces
  words within that section.

### Rejected (with the reason, because a list with no rejections means no bar)

| Candidate | Rank | Why not |
|---|---|---|
| recall-blackout fix | LOW | Bugfix. CHANGELOG is the correct and complete home. |
| fd-ceiling handling | LOW | Operational internal; no reader decision depends on it. |
| vault-watch improvements | LOW | Fails the new-reader test outright. |
| scorer changes | LOW | A better version of a claim the README already makes ("rank fusion + rerank"). Bar 2. |
| `docs/typed-memory-graph.md` | LOW | Explicitly **unshipped** design. Putting unshipped work in a README is the exact failure this project's under-claiming value exists to prevent. |
| `docs/agent-cli-architecture.md` | LOW | As above — unshipped. |

Cursor and KiloCode runtime pages were checked and are **already linked**; not
candidates.

---

## Phase 4 — Budget and retirement

### Prose: nothing forced, four candidates named anyway

Every section is under budget, and the accepted additions (~25 net words) fit
inside both the section headroom and the 171-word total headroom. **No
retirement is forced this run.** That is the budget working as designed, not
failing to bite — a cap that fires on a run with one small addition would be a
cap set too low.

The skill requires naming candidates regardless, because a run that never finds
anything to cut is a run that did not look:

| # | Candidate | Section | Words | Order | Destination |
|---|---|---|---|---|---|
| R1 | "The v0.3 headline — `minni wire <platform>` … shipped in the v0.3.0 release (versioned installs, post-wire probes, reference-aware GC, rollback via `--use-version`)" | Status (line 163) | ~60 | **2 — release archaeology** | `CHANGELOG.md` 0.3.0, where it already appears verbatim. Pure deletion. |
| R2 | The same `--use-version` / GC facts restated in Quickstart line 87 | Quickstart | ~20 | **5 — redundancy** | Keep the Quickstart instance (load-bearing: it is next to the command), cut the Status echo. Overlaps R1. |
| R3 | Gemini-provisional and Cursor-not-wired exception clauses | Quickstart (line 87) | ~55 | **3 — covered by a linked doc** | `docs/runtimes/gemini.md`, `docs/runtimes/cursor.md` (both already cover it, both already linked). Compress to "see the per-runtime pages for Gemini and Cursor". |
| R4 | The vault directory inventory in prose | What Minni is (line 18) | ~8 | **5 — redundancy** | The diagram already carries it. Two copies of one list is exactly how claim #6 became stale in the first place. |

R1 + R2 together free ~60 words from Status, the fastest-rotting section. That
is the retirement to bank *before* the next release adds its own "now shipping"
sentence.

### The diagram: retirement **is** forced

Node budget 18, currently 17. Phase 2 recommends adding two HIGH nodes (console,
plan surface) → **19 nodes, over by 1**. Under one-in-one-out, the addition must
pay for itself inside the diagram.

The proposed payment, and it is a good trade:

> **Collapse the seven individual runtime nodes into one.** `Claude Code`,
> `Codex`, `Gemini / Antigravity`, `Grok`, `Cursor`, `Kilo Code`, and `Any MCP
> client` all have exactly one outgoing edge, to the same target. Seven nodes
> encode one fact — *many runtimes, one ingress* — and 41% of the diagram's node
> budget is spent saying it. Replace with a single node: **"Agent runtimes — 6
> wired + any MCP client"**. Frees 6 nodes; the list of six lives one line below
> in the Documentation table and in `docs/runtimes/`.

Result: 17 − 6 + 2 = **13 nodes**, comfortably inside budget, with room for the
MEDIUM topology gaps in a later run. The diagram loses a logo-parade and gains
its two missing subsystems.

This is the cleanest illustration of why the rule exists. Without a node
ceiling, both nodes get added, the runtime parade stays, and the diagram creeps
to 19 — still "fine", the way every diagram is fine right up until it is
unreadable.

---

## Phase 5 — Redesign proposals

**PROPOSALS — NOT APPLIED.** Structure and facts do not belong in the same diff.

### Proposal 1 — Promote the strongest differentiator above the fold

"Recall is evidence, not instruction" sits at line 33 — third screen. It is the
most defensible claim in the document (verified this run down to
`safety.py:102`) and the one nothing else in the comparison table has.

*Change:* one sentence of it into "What Minni is", or into the preamble pitch.
*Reasoning:* the first screen currently spends its budget on problem framing
that a reader arriving from a search already agrees with; the differentiator is
what they cannot get elsewhere. *Tradeoff:* weakens the standalone section, and
"What Minni is" has only 29 words of headroom — this likely needs R4 to pay for
it.

### Proposal 2 — Split the diagram into control plane and operator surfaces

*Change:* diagram A = the request path (runtimes → plugin → socket → daemon →
gate → verbs → stores). Diagram B, in `docs/architecture.md` = the surfaces
around it (hooks, console, `minni watch`, `minni wire`).
*Reasoning:* Phase 2 found five real components with no room. That is not a
budget problem, it is a **scope** problem: one diagram is being asked to be both
a call path and a component inventory, and it is doing the second one badly.
Splitting means the README diagram can stay small *and* be complete for what it
claims to show. *Tradeoff:* two diagrams to keep true instead of one — and the
one in `docs/` will rot faster, being read less.

### Proposal 3 — Status answers "what state is this in", not "how it got here"

*Change:* retire R1/R2; keep current version, what is proven, what is not.
*Reasoning:* Status is the fastest-rotting section because release news is
append-only by instinct. Framing it as a snapshot rather than a narrative makes
the retirement obvious every release instead of arguable. *Tradeoff:* loses the
"we ship" momentum signal that release history conveys.

### Proposal 4 — Turn Quickstart line 87 into a table

*Change:* the platform-support paragraph → a small table (`platform | wired by
minni wire? | notes`).
*Reasoning:* line 87 is a **180-word single paragraph carrying eight claims** —
by a wide margin the densest and most rot-prone text in the README, and it sits
in the section a new user is actively following. Table rows are structurally
easier to keep true than compound sentences with embedded exceptions, they are
scannable mid-install, and they fall under a row budget, which caps growth. It
also makes R3 mechanical. *Tradeoff:* costs lines (the line cap is at 92%);
worth pairing with R3 so it is net-neutral.

### Proposal 5 — A no-hardcoded-counts policy for prose docs

*Change:* prose docs stop asserting test counts, tool counts, and file counts,
or generate them.
*Reasoning:* see the outside findings below — the two worst rots found anywhere
this run were both hardcoded counts. Numbers rot silently: nothing fails when
they drift, so nothing corrects them. The README currently has none, which is
partly luck; the policy protects it. *Tradeoff:* "37 MCP tools" is genuinely
informative, and prose that refuses to be specific gets vague. Suggested
middle: counts allowed only where a test asserts them.

---

## Findings outside the README

Both found by cross-checking the README against sibling docs. Neither is a
README defect; both are worth a separate PR.

1. **`plugins/minni/skills/minni-doctor/SKILL.md:126-127` — stale by ~2x.**
   Cites "expect 538 passed, 5 skipped" (pytest) and "expect 327 passed" (npm).
   Current collection: **~1,177 pytest tests** and ~852 npm test call sites. A
   diagnostic skill whose health baseline is off by more than double will
   report a healthy repo as broken.

2. **Three-way disagreement on the vault directory inventory.** Code says six
   (`wire/writers.py:576`: `raw, wiki, logs, schema, inbox, outbox`); README:18
   and the diagram say four (omitting `raw`, `schema`); `minni-doctor
   SKILL.md:51` says a *third* thing (`wiki/ raw/ inbox/ outbox/ schema/
   index.md log.md` — omits `logs/`). Three sources, three answers, one
   filesystem. The README fix (claim #6) corrects one third of the problem;
   someone should make the code the single source and have the docs cite it.

`minni-doctor/SKILL.md` is the most drift-prone document encountered in this
audit, and it drifted in exactly the two ways Proposal 5 predicts: hardcoded
counts and a hand-maintained inventory.

---

## What was not checked

Stated plainly, per the skill's reporting discipline:

- The three recall scope literals (`personal`, `combined`, `both`) — not read in
  `retrieval.py`.
- Whether the personal/shared retrieval legs match the diagram's two edges —
  not traced.
- Whether the `Handoff → Vaults` edge corresponds to a real write.
- Whether `vault-ingest-all` runs on a daemon timer or is externally scheduled —
  relevant to how the corrected indexing edge should be labelled.
- Competitor cells in the comparison table (lines 41–49) — deliberately out of
  scope; this audit judges claims about *this* repo only.

Start the next audit here.
