# Minni Hygiene Backlog (3 items) — for a dedicated Minni session

> Surfaced 2026-06-03 from a live audit of the running daemon + vault DB while debugging
> a "565 inbox files" clog. The clog was NOT a consolidation stall — the AFM loop is healthy
> (MINNI_AFM_LOOP=on; candidate_packets fully drained: 63 accepted / 220 rejected / 0 proposed).
> These are the real follow-ups, in value order. Item 4 (prompt tune) is already DONE (commit f92288a).

## 1. Test isolation (HIGHEST VALUE — tests pollute the production DB)
**Evidence:** of 283 total `candidate_packets`, **223 (79%) are test fixtures** — the canonical
strings from `test_candidate_lifecycle.py` ("Some new fact", "Auth now uses session cookies",
"WebSocket reconnection needs 500ms backoff", "Always use ECDSA over RSA", "Database migrations
must be idempotent"). Some were even **promoted into durable `learnings`** (they appear in the
accepted pile), polluting recall.
**Root cause:** one or more test files write to the real `~/.minni/minni.db` instead of a temp DB.
`test_pr12_afm_loop.py` correctly uses `tmp_path`; `test_candidate_lifecycle.py` (and possibly
others) apparently do not.
**Fix:**
- Audit `engine/test_*.py` for any that open the real DB/vault path; force all onto a `tmp_path`
  fixture (or a `MINNI_DB_PATH` override per-test).
- Clean existing pollution: identify fixture-shaped rows in `learnings` + `candidate_packets`
  and prune them (verify against content before deleting — preserve any real learning).
**Done when:** a full `pytest` run leaves the prod DB row counts unchanged.

## 2. Inbox janitor (the actual "clog" — no GC exists)
**Evidence:** `~/.minni/claudecode-vault/inbox/` holds **567 `*.json` files** but only 283 packets
ever existed in the DB. A grep of the whole engine finds **no consumer/cleanup** for `inbox/*.json`
(only a test references `afm-drafts-*.json`). The per-turn hook writes a capture file every turn
and nothing deletes it → unbounded disk litter.
**Fix (deterministic — NO model needed):**
- On each AFM tick (or a dedicated sweep), delete/archive any `inbox/*.json` whose corresponding
  DB packet has reached a terminal `status` (accepted/rejected/expired/merged/superseded).
- TTL backstop: sweep capture files older than N days regardless.
**CRITICAL GUARD:** distinguish **hook-capture files** from **handoff files**. The oldest inbox
file (`20260426T184233Z-review-auth-migration-trace-pr.json`) is a **real pending agent handoff**
(`handoff_leases`), NOT litter. The janitor must NEVER GC a handoff/lease file. Match by filename
shape + cross-check `handoff_leases` before any delete. (Standing rule: verify, preserve superset,
never silently pick.)
**Done when:** inbox file count tracks live (non-terminal) packets + active handoffs, and a test
proves a terminal capture is GC'd while a pending handoff is preserved.

## 3. Honor `durability: temporary` in the writer (wire up the done prompt change)
**Context:** commit `f92288a` added a substance test + a `durability: durable|temporary` field to
`engine/afm_prompts/session_distillation.md`, so the AFM now CLASSIFIES ephemeral state (live
session IDs, PIDs, "currently running") as temporary. But the field is currently **inert** —
the promote path doesn't act on it.
**Evidence of the gap:** a live Grok session ID ("Current live Grok Build TUI session ID
f6d63707…") was promoted as a **durable** learning — exactly what the new field is meant to catch.
**Fix:** in the promote path (`engine/afm_writer.py::_write_one` / the durable-promote in
`engine/minnid.py`), branch on `draft.durability` and give `temporary` drafts a real TTL/expiry
treatment instead of `INSERT learnings`. Note: the old `mark_temporary` resolve decision was an
unenforced no-op (identical to accept) and has been removed from `resolve_candidate` — see
issue #123; implementing genuine TTL semantics (schema + retrieval filter) is the prerequisite
here. Add a test: a draft marked `temporary` does NOT land in durable `learnings`.
**Done when:** ephemeral-classified drafts get a TTL/temporary treatment, not durable promotion.

## prepare-task observations (added 2026-06-04 from a live `minni_prepare_task` run)

> Ran `minni_prepare_task` (profile=deep, useAfm=true) on the Praxis LLM-backend task. The CORE
> mechanic is sound — authority-weighted/freshness-decayed/privacy-gated re-ranking put the right
> note #1 (score 176) and beat the daemon's stale semantic recall. These four are the rough edges.
> Mechanism lives in `plugins/minni/src/task.ts`.

### 4. AFM enrichment path is dead (helper not serving) — HIGH
**Evidence:** `afm.used=false`, `error:"native helper unavailable"`, while `availability:"available"`
and `adapterConfigured:true`. The on-device Apple Foundation Models helper at `127.0.0.1:11437`
isn't actually serving, so `useAfm:true` SILENTLY falls back to deterministic on every call —
the enrichment layer is non-functional.
**Fix:** either make `native_afm_helper.swift` (currently bridge mode) actually serve, OR make the
reported `availability` reflect reality (don't report "available" while the helper is down) and
surface the fallback clearly to the caller instead of a silent downgrade.
**Done when:** `useAfm:true` either produces a merged AFM packet, or returns an honest
`availability:"unavailable"` + explicit fallback notice.

### 5. Lexical-vocabulary noise dilutes the tail — MED
**Evidence:** deep profile's 10-source budget pulled 8 low-value rows (Minni-rename, Vigil console,
Xcode-MCP-reprompt, propagate.py, Sluice) that matched only on shared PROJECT vocabulary
(Gemini, Praxis, agent) + the fresh-note bonus — not topical relevance. Only 2 of 10 were on-task.
**Fix:** add a topical/semantic relevance floor, or penalize pure-vocabulary lexical matches so the
freshness bonus can't float off-topic notes into the budget.
**Done when:** an off-topic note that matches only on common project words does not consume a slot.

### 6. Intent classification is keyword-fragile — MED
**Evidence:** an implement/build task classified as `intent:"review"` because the word "audit"
appeared, yielding generic `recommendedNextActions` ("read sources, narrow change, run tests")
instead of build-shaped guidance.
**Fix:** make intent detection less single-keyword-triggered (weight verbs/structure, not lone nouns).
**Done when:** "fix bugs X and add capability Y" classifies as implement/build, not review.

### 7. `daemonLead` / `currentState` raw-dumps FAISS JSON — MED (token cost)
**Evidence:** the daemon lead is injected as a giant unparsed FAISS blob (provenance, rrf_score, and
the full query repeated verbatim per hit) into both `currentState` and `contextMarkdown` — ugly and
token-expensive in a packet whose whole point is compactness.
**Fix:** summarize the daemon lead to `title + score + wikilink` per hit; never raw-dump the blob.
**Done when:** `contextMarkdown` contains no raw provenance/query_variants JSON.

## Rails
- TDD (RED→GREEN); the AFM dedup is deterministic + healthy — do NOT touch it.
- No push without Hans. Do destructive vault cleanup (items 1 & 2) only with explicit confirmation
  and a dry-run first — the handoff-vs-capture distinction is easy to get wrong.
