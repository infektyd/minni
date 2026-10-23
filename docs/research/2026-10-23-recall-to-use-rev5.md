# Recall-to-use — revision 5 (Phase 0, approved)

Status: **approved by Hans on 2026-10-23, as written, including the authorized test edits in §8.3.**
Implementation starts with S0 and S1a.
Base: `origin/main` at `eaf294fd`. Line numbers are as of that commit.

Decisions this revision implements, all Hans's (relayed by the coordinating session, then confirmed in
approval):
- Ship on, with a before/after comparison. The randomized holdout is gone.
- Instrument Item 1 first.
- Q2(a): a boot form of the Layer 1 shelf.
- Subagent briefs, gated on the host checks. Both checks have since passed (§2).
- Item 3 moves to Phase 1b.
- A smaller revision.

History: revision 4 (about 4,400 lines) and its four hardening logs are the record of how this design was
reached. Revision 4 rests on private evidence, so it is kept in the operator's private notes, not in the
repository. Revision 5 replaces it as the design of record. Appendix A lists what was cut from revision 4 and
where each of its 20 open findings went.

---

## 0. Goal, scope, and how each item is proven

**Goal.** The memories Minni already holds should change what agents do, and the change should be
measurable. That means better use of existing notes, not more notes.

**Phase 1 delivers Items 1, 2 and 4.** Item 3 (lessons surfaced at the moment of failure) is Phase 1b, and
Phase 1 makes no promise on it (§7).

| Item | What it changes | How it is proven |
|---|---|---|
| 1. Use telemetry | Records what Minni put in front of the model, and whether the model used it | Detector validity on a labelled set (offline), then a **live before/after** of the used rate |
| 2. Recall precision | Recall stops firing on machine output, ranks durable notes above journals, names up to 3 memories | **Deterministic replay** against fixed thresholds (§5.4) |
| 4. Cost | The boot envelope fits its budget and the host cap; the guard stops bouncing | **Deterministic replay** against fixed thresholds (§6.5) |

**Order.**
1. Ship Item 1 on its own, with Items 2 and 4 at today's behaviour.
2. Collect a short baseline window, sized in days (§4.7).
3. Deploy Items 2 and 4 together, each switched on and already proven by replay.
4. Collect the after window. Report before and after.

**Caveats, stated now and repeated in the report:**
- There is no randomization. Before and after are different days with different work, under one operator.
- The used rate is lexical. It sees quoting and application, not silent use, so it is a **floor**.
- Item 1 gives a number, not a cause.

**Every feature ships on, with one kill switch each** (§8.2), so Hans can switch one off for a spot
comparison or a rollback.

---

## 1. Evidence

Sources are private, under the private root (§3): the audit fixture, the claude-code vault audit log for the
Oct 19–22 campaign window, the SessionStart spill files, local transcripts, and a read-only DB copy. No
private path, id or content is quoted here. The numbers were computed by one-off private scripts. S1c's
committed tooling recomputes them, and any difference is reported.

| # | Fact | Value |
|---|---|---|
| B1 | Audited learnings used / written | 2 / 18 |
| B2′ | Audited surfacings followed by use, counting visible ones only | 2 / 29 (52 rows, 23 invisible) |
| B3 | Prompt turns gated "strong" | 554 / 564 |
| B5 | Strong turns whose top vault hit is a journal-class note | 523 / 554 |
| B6 | Harness chatter in boot `recent_learnings` slots | 30 / 110 over 11 boots |
| B7 | Active claude-code learnings since window start that are relays or reminders | 27 / 78 |
| B8 | SessionStart envelope vs the 4,000-token budget | 10 / 10 distinct bodies over budget: 37.7k–41.1k chars, of which recall is 25.9k–29.3k |
| B9′ | Guard-deny tool results in transcripts | 107; 82 in subagents |
| B10 | Denies in a session other than the arming one | 39 / 106 |
| B13 | Prompt turns by kind | 565: 316 task notifications, 58 relays, 16 `<bash-input>`, 109 scheduled check-ins, 55 typed prose, 11 short replies |
| B14 | SessionStart outputs over the 10,000-char host cap | 136 / 170 local attachments |
| B17 | Next call after a guard deny | identical re-issue 57; memory consult 7; other 43 (of 107) |
| B21 | Non-identity boot recall results with confidence ≥ 0.55 | 10 / 74 |
| B22 | Tool calls made inside subagents | 8,891 / 12,133 (73%) |
| B25 | Active claude-code learnings with a durable category | 56 / 808, of which 32 are `preference` |
| B26 | Denies from one armed state under 4 concurrent Reads | 4 / 4 |

**Mechanisms, verified in code at `eaf294fd`:**
- **The budget is stamped, never enforced.** `wrapEnvelope` writes `tokens=` and `budget=` and trims
  nothing (`plugins/minni/src/agent_envelope.ts:137-169`). `trimToBudget` (`:178`) has no SessionStart
  caller.
- **The boot envelope is mostly unseen.** `stableStringify` sorts keys alphabetically (`agent_envelope.ts:120-135`),
  so `keyOrder` has no effect. An envelope over the host cap spills to a file plus a 2,000-char preview, and
  that preview opens on `audit_tail`. 23 of the 26 audited SessionStart surfacings sat past it.
- **The boot recall slice ships each daemon result whole** (`daemon_transport.ts:236-271`): the snippet,
  `evidence_envelope` (the full chunk), provenance, rationale and recommended actions.
- **Prompt recall runs on everything.** `handleUserPromptSubmit` (`hook-handlers.ts:1212`) recalls on task
  notifications and relays.
  - The vault gate opens on a raw score ≥ 50 (`recall-state.ts:51-58`).
  - `scoreVaultNoteCached` adds +50 for a whole-prompt phrase plus up to 8 per query term, with no term cap
    (`vault.ts:2349-2386`).
  - `searchVaultNotes` sorts and slices by raw score before any caller can rerank (`vault.ts:2556-2562`).
- **The pointer names one hit**, with no handle (`recall-state.ts:334-347`).
- **The guard's state is shared by every session.** It is one file for the whole vault, with no session
  field (`recall-state.ts:29`, `:98-106`).
  - `markRecallConsumed` returns true to every racer (`:295-330`).
  - The handler denies whenever it returns true (`hook-handlers.ts:1595-1614`).
- **Harvest treats chatter as typed input.** `isHumanTypedInput` accepts every `type:"user"` entry
  (`jev-harvest.ts:116`). `DIRECTIVE_PATTERN` (`:265`) then makes relayed messages and reminders into
  `preference` learnings.
- **Boot `recent_learnings` is pure recency** (`src/minni/minnid_runtime/recall.py:2369-2432`).
- **Nothing records use.** The Stop receipt has no use fields (`vault.ts:1893`). The guard audits only
  `hits: N` (`hook-handlers.ts:1600-1612`), and SessionStart audits no item ids (`:1178-1201`).

---

## 2. Host contract (claude-code 2.1.301)

Probed on 2026-10-23 in an isolated scratch project: no user settings, no plugins, empty MCP config, Sonnet.
The hooks logged payload **keys only**, and the key logs are under the private root (`probes/`). Each
"delivered" result was confirmed by a nonce the model reported back.

| Probe | Question | Result |
|---|---|---|
| P1 | PreToolUse `additionalContext` with no `permissionDecision`: delivered? | **Yes**, next to the tool result, as `PreToolUse:<Tool> hook additional context: …` |
| P2 | Same, when another PreToolUse hook denies the call | **Yes.** The model sees the deny text *and* the context. |
| P3 | Does `PostToolUseFailure` fire for a call another hook denied? | **No.** (This bears on Phase 1b.) |
| P5 | Is `agent_id` on a subagent's PreToolUse? | **Yes**, with `agent_type`. It is absent on main-thread calls. UPS did not fire for the subagent's prompt. |
| P7 | Is `prompt_id` on UserPromptSubmit and PreToolUse? | **Yes**, on every event logged |
| P8 | Does SubagentStop carry the subagent transcript? | **Yes**: `agent_transcript_path` and `last_assistant_message`, plus `session_crons` and `background_tasks` |
| P13 | Does SubagentStart fire and inject context? | **Yes.** Its payload has `agent_id`, `agent_type`, `prompt_id`, `session_id` and the parent `transcript_path`, but **not the subagent's task prompt**. Its `additionalContext` reached the subagent. |

**Seen during the probe.** A subagent reported a context line, said the line came from tool output, and did
not follow it. Context delivered next to a tool result may carry less weight than a deny. Item 1 measures the
guard channel's uptake before and after. Nothing here assumes it.

**Settled from the host docs:** P6. The 10,000-char cap and its 2,000-char preview apply to every
`additionalContext` string, PreToolUse included. An empirical confirmation is optional.

**S0, probed on 2026-10-23** (same isolation; a persisted scratch session, then a resume, `/compact`, and
another resume):

| Probe | Question | Result |
|---|---|---|
| P12 | Do PreCompact and PostCompact carry `session_id`? Which SessionStart `source` values exist? | **Yes**, plus `trigger`; PreCompact adds `custom_instructions` and PostCompact `compact_summary`. Sources seen: `startup`, `resume`, `compact` (`clear` not exercised). |
| P15 | Is `last_assistant_message` the turn's final text, and is it flushed by Stop? | **Yes, and yes**: in 4 of 4 Stops, a transcript entry with the same text hash was already present. Provisional `judged` rows should be rare. The interrupt case needs an interactive session and was not run. |
| P16 | Does the host wait for a hook process after it closes stdout? | **Yes.** A Stop hook that closed stdout and then slept 8 s made that run 16 s, against 7–9 s. Post-delivery maintenance adds directly to wall time, so its 150 ms cap is load-bearing (§6.2.5). |
| P17 | At SubagentStart, is the parent's `Agent`/`Task` `tool_use` already in the transcript? | **No.** It appears afterwards (name `Agent`, input keys `description, prompt, run_in_background, subagent_type`), but it was not flushed when SubagentStart ran. The brief takes the task prompt from the parent's PreToolUse instead (§6.3.6). |

---

## 3. Constraints

- **Never change `stableStringify`.** Plan digests, thread claims and thread events hash through it. Envelope
  ordering uses a new top-level serializer (§6.2).
- **Never edit an existing test** without operator authorization. Approving revision 5 authorizes exactly
  the edits listed in §8.3: the inline shelf size pin (Q2(a), §6.2.4) and the claude-code deny-shape
  assertions that context delivery replaces (§6.3.3). All RED tests are new files. Everything else keeps
  its existing fixtures green.
- **New hook events get their own gate.** Do not widen `VALID_EVENTS` (`hook-utils.ts:31-44`), which
  `gemini-hook.ts` and `grok-hook.ts` share. Each new event needs a dispatch `case` arm and its manifest entry
  in the same change (`r8-hook-parity.test.mjs` P4). `gemini-hook.test.mjs:190` stays green.
- **Paired TS/Python rules get parity tests over a shared case file.** There is no runtime-shared data file:
  a bare JSON import fails typecheck under `NodeNext`, and `dist/data/` would fail `stage_payload.py`. Shared
  rules live in typed `.ts` constants with a Python mirror. Precedent: `inbox_ingest.is_audit_echo`.
- **New injected prefixes must be masked** by `maskTelemetry` (`jev-harvest.ts:86-105`), or harvest re-reads
  them.
- **Frontmatter is scalar only** (`vault.ts:207`, `:523`).
- **Private root.** One root outside every slice worktree:
  - `MINNI_PRIVATE_ROOT` if set, else `<main worktree>/_private/recall-to-use/`
  - realpath checks, symlink escapes refused, a root inside a tracked directory refused
  - corpus, labels, reports and probe notes live only there
- **Public repo.** Committed files carry no home paths, session ids, URLs, tokens or transcript content.
  Fixtures are synthetic.
- **Installed copies.** A source change is live only once every deployment is current (`make
  check-deployments`). The before/after switch points are deployments (§4.7).
- **Flags must reach hook processes.** Environment variables do not reliably reach hooks launched by the
  desktop app. Kill switches are therefore read from a file, with env as an override (§8.2).

---

## 4. Item 1 — Use telemetry (S1a, S1b, S1c)

### 4.1 Current behaviour

- UPS audits vault match paths and `top_score` (`hook-handlers.ts:1543-1559`). The guard audits `hits: N`
  (`:1600-1612`), and SessionStart audits counts (`:1178-1201`). None names what the model saw.
- `minni_recall` audits daemon ids (`daemon_transport.ts:1218`). They are keyed to the MCP process, not to the
  host session, and its result text carries wikilinks, not ids (`daemon_transport.ts:999-1045`).
- Boot `recent_learnings` reaches the plugin as rendered text only (`daemon_transport.ts:358-368`).
- `SessionReceipt` (`vault.ts:1893`) has no use fields. Its formatted line is pinned byte for byte
  (`vault.test.mjs:1880`, `:1906`).

### 4.2 Keys and handles

- **Memory key (`mkey`)**, from one helper `memoryKeyFor` in new `use-ledger.ts`:
  - `vault:<vault-relative path>` for vault hits, and for daemon results whose `source` is a file inside
    this vault. The match is on `source`, not on the wikilink text, because `_path_to_wikilink`
    (`retrieval.py:287-298`) emits the same `[[wiki/…]]` for notes from different vaults.
  - `doc:<src>:<doc_id>` for other daemon documents. `<src>` is the result's index tag (`tag_document_results`,
    `recall.py:130-140`). A doc id is unique only within one index.
  - `learning:<learning_id>` for daemon learnings.
  - `lhash:<sha1(line)[:12]>` for a boot learning when the daemon sent no ids (version skew).
- **Handles** are identifiers the agent can pass back to `minni_drill`:
  - `m:<sha1(vault-relative path)[:8]>`. The plugin resolves it.
  - `d:<src>:<doc_id>`. The daemon picks the index by `src` (`reference_candidates`, `recall.py:1820-1868`),
    and `reference_matches` checks `source` only when one is given (`:1880-1885`). So the handle alone is not
    enough.
    - UPS records each named hit's `{src, doc_id, source}` in a per-session handle registry,
      `.runtime/recall-session.d/<session_key>/handles.json` (at most 64 entries, oldest evicted).
    - The plugin rewrites `d:<src>:<doc_id>` to the daemon reference `{doc_id, src, source}` from that
      registry.
    - A handle not in the registry is sent as `{doc_id, src}`, and its response is marked
      `source_unverified`.
  - A colliding `m:` handle resolves to every candidate.
- **Session key.** `sha256(session_id)[:16]`. Failing that, `tx-` plus a hash of the transcript path.
  Otherwise `unlabeled`, which stays out of every rate.
- **Agent key.** `main` on the main thread. `a-<sha256(canonical id)[:12]>` when the payload has `agent_id`
  (P5), or when its transcript path sits under `subagents/` (then the id is the file basename).
  - The canonical id strips one leading `agent-` and one trailing `.jsonl`.
  - That is needed because the host spells one subagent both ways: its docs show `agent-abc123` in one
    payload and a bare id in another, and transcripts are named `agent-<id>.jsonl`.
  - Erratum from the S1a-1 review; the code already does this.

### 4.3 Ledger

- **Path:** `<vault>/.runtime/use-ledger/<session_key>/<yyyy-mm-dd>.jsonl`.
  - Append-only, `O_APPEND`, one `write()` per row, at most 4 KB.
  - Readers skip torn lines.
  - Every path goes through `assertWriteTargetUnder`.
  - Rows never go through `recordAudit`, so its 5 s throttle cannot drop them.
  - Rows a hook could not write are counted. After delivery, one throttled audit line,
    `<prefix>_use_ledger_failed`, gives the count only (S1a-2).
- **Common fields:** `{v:5, kind, ts, session_key, agent_key, wire, principal, prompt_id?, subagent_type?, features, build}`.
  `features` is the set of kill switches in effect, so before and after rows say what was on.
- **Row kinds:**
  - **`turn`** is written by a UPS wrapper on every prompt, with
    `{turn_ix, envelope_kind, subkind, exit}`.
    - It is the denominator for "per 100 human prompt turns".
    - It is written when the handler returns, before emission, because it records an input.
    - **The classifier ships in S1a, in record-only mode** (§5.2.1 rules, case file and parity tests). It
      fills `envelope_kind`, `subkind`, `template_rule` (`on` or `off`) and `rule_id` from the first day of
      the before window, and changes no behaviour.
      - It runs with its template context from day one, so UPS records each eligible prompt in the counts
        file during record-only too. Otherwise the same text would be `human` before the switch and
        `scheduled` after it.
      - S3a later wires the actions. Both windows therefore count human prompt turns with the same rules.
      - The template hash itself is never written to the ledger.
  - **`surfaced`** holds
    `{sid, channel, mkey, handles[], title ≤ 120 (sanitized), rank, score?, route?, turn_ix?, visible, delivered, tx_off?, shingles?, echo?}`.
    - `sid = sha256(session_key, ts, channel, mkey, rank)[:16]`.
    - `explicit_recall` rows use `sha256(tool_use_id, mkey)[:16]` instead, so re-reading a transcript
      cannot duplicate them.
  - **`judged`** holds `{sid, outcome, via, evidence, title_assessable, final, rule_version}`.
  - **`reset`** is written on PreCompact, PostCompact and SessionStart, for the payload's own `agent_key`.
- **Channels:**
  - `boot_recall`
  - `boot_recent_learnings`
  - `prompt_pointer`
  - `guard`
  - `explicit_recall`
  - `subagent_brief` (§6.3.6)
- **Visibility** comes from a pure helper, `locateItems(emitted, items, capChars)`, run on the exact string
  the hook emits.
  - An item is visible if it lies wholly inside an emitted string within the cap.
  - In an over-cap string, it is visible only if it ends before char 2,000.
  - A wire with no known cap records `visible: true, cap_unknown: true`.
- **Shingles:** at most 64 hashes of 8-word shingles from the memory body. Resolution sections come first,
  then rarer shingles. No body text is stored.
- **Echo:** at most 64 shingle hashes of the text that was injected for the item.
- **Deliver first, record second.** Surfacing rows are built in memory and registered with
  `deferUntilDelivered`. `emitAndCommit` writes them only when the flush succeeded and the wire did not drop
  the output (`hook-delivery.ts`).
  - `deferUntilDelivered` gains an optional `{onAbandon}` argument. Existing callers are unchanged.
  - The abandon path writes the rows with `delivered: false, visible: false`, judged
    `unknown(not_delivered)`.
  - `turn` and `reset` rows are immediate.
- **Every UPS exit is counted.** UPS has six exits (`hook-handlers.ts:1225`, `:1270`, `:1284`, `:1299`,
  `:1464`, `:1561`). S1a wraps the handler at the dispatch arm instead of patching each exit:
  `withTurnRecord(inner)`.
  - Every exit sets an `exit` reason. An exit that sets none is recorded as `error`, and a test pins that every
    exit sets one.
- **Boot learnings get ids (S1a-D, daemon).** The `read` response gains additive fields. The rendered text is
  pinned byte-identical:
  - `recent_learnings: [{learning_id, category, line, content ≤ 4,000}]`
  - `daemon_version`

  The plugin hashes `content` into shingles and never stores or injects it.
- **Retention (S1b).** Ledger session directories (`.runtime/use-ledger/<session_key>`) past the 90-day
  horizon, with nothing open in the detector, are removed by a sweep (`sweepLedgerDirs`, `use-retention.ts`).
  - "Past the horizon" is the `recall-session.d` rule below: the directory and every entry in it are older
    than 90 days, and a directory with more than 256 entries is never judged idle.
  - "Nothing open" is read from the detector's per-session state (`.runtime/use-detector/sessions/<key>/
    windows.json`), read-only and through the containment walk. A directory is kept while any agent there has
    an open window, an open epoch (exposures not yet written), or an ended turn whose reply was not flushed.
    It is also kept while a run holds `windows.lock` (younger than 30 s), and while the state cannot be read
    with certainty (unreadable, oversize, refused, or an unknown version).
  - With no detector state at all, nothing is open, so the directory may go.
  - Retention removes only the ledger directory. The detector's own state and cursors are not its to remove.
  - It runs with the `recall-session.d` sweep, in the same SessionStart commit and abandon handler, on every
    outcome.
  - At most 20 per run, each renamed to `.trash-<ts>-<key>` first.
  - It runs within its own cooperative 150 ms budget, checked between items, and resumes from a cursor in
    `use-ledger/.sweep.json`. After a complete pass it waits a day.
  - It skips symlinks and plain files and never follows a link. Ledger readers skip dot names, so `.sweep.json`
    and `.trash-*` are invisible to them.
  - One race is accepted: a detector run that starts between the open-window check and the rename. That is a
    microsecond window on a session idle for 90 days, and the rows that run appends are lost with the
    directory.
- **Per-session side files (S1a-2).** `.runtime/recall-session.d/<session_key>/` holds the capture state that
  is not a ledger row. Every directory is reached through the ledger's containment walk, and every file is
  replaced through a temp name and a rename. None of them holds text.
  - `counters.json` = `{v, turn_ix}`. UPS is the only writer. S3c extends it (§5.2.7).
    - Its read-modify-write holds an `O_EXCL` lock, `counters.json.lock`, so concurrent prompts never share
      an index. The wait is bounded (200 ms), and a lock older than 2 s is broken. If the lock is not taken,
      the turn records `turn_ix: 0`.
    - A stale lock is broken by renaming it to a unique name, so of several breakers exactly one takes it.
      If the renamed lock turns out fresh, it is linked back, never over a newer lock. Each lock carries a
      random token, and a release removes only its own.
    - Two residual windows remain. Both are microsecond-scale, need a crashed holder, and fail open:
      - A third taker between the rename and the link-back gives two holders, so at worst one duplicate
        `turn_ix`.
      - A holder that releases inside that window gets its lock linked back with no owner. That session's
        prompts record `turn_ix: 0` for at most 2 s, until the lock is stale and broken again.
    - On a filesystem without hard links the link-back fails and the live lock is removed, which allows the
      same one duplicate. APFS has hard links.
    - A file that is present but not a counter (corrupt, foreign version, not a regular file) is left as it
      is, and the turn records `turn_ix: 0`. Only an absent file starts the count at 1.
  - `<agent_key>.reset` = `{reset_id, at, reason}`, written on PreCompact, PostCompact and SessionStart.
  - `hit-keys.json` = `{v, task_signature, hits: [{wikilink, score, mkey}]}`, at most the 5 `top_hits`.
    - **Writer:** UPS, on a strong turn that armed the guard (the recall-state file was written).
    - **Reader:** the guard capture, when it returns a deny. It matches each hit the deny lists by
      `wikilink` and `score` under the same `task_signature`, and keys that hit's `guard` row with the
      recorded `mkey`.
    - **Why:** the recall-state file holds titles and wikilinks only, and a wikilink alone does not
      identify one memory (§4.2). The file holds keys and the two match fields, nothing else.
    - **Read back as untrusted.** A `mkey` read back must have the shape `memoryKeyFor` gives. A `vault:` key
      must be vault-relative: no leading `/`, drive letter, backslash, control character, or empty, `.` or
      `..` segment. A listed hit with no valid recorded key gets no row; its key is never guessed from the
      wikilink.
  - **Retention of `recall-session.d`.** These directories follow the ledger's horizon. They are removed
    once every entry is idle for 90 days, by a sweep (`sweepSessionDirs`):
    - It runs once a SessionStart's output is settled, on every outcome. It is registered with
      `deferUntilDelivered` both as the commit and as the abandon handler, because it consumes nothing. A
      wire that drops SessionStart (Grok) still sweeps.
    - It removes at most 20 directories per run, each renamed to `.trash-<ts>-<key>` first.
    - It also removes orphaned counts-file temp files (`.runtime/prompt-templates.json.<pid>.<uuid>.tmp`)
      older than an hour: regular files only, at most 20 per run.
    - It stays inside the cooperative 150 ms budget, checked between items (§6.2.5).
    - It resumes from a cursor in `recall-session.d/.sweep.json`. After a complete pass it waits a day.
    - It skips symlinks and plain files, and never follows a link out of the vault.
    - A directory with more than 256 entries is never judged idle.

### 4.4 Use detector (S1b)

It is passive: the agent does nothing extra. It runs from `handleStopCore` (`hook-handlers.ts:351`), from
SubagentStop (§6.3.6), and from `use-report`.

- **Windows.**
  - A per-turn surfacing (pointer, guard, explicit recall) is open from the surfacing to the Stop that ends its
    turn, capped at 50 assistant actions.
  - A boot surfacing is open until the Stop that ends the session's second non-skip prompt turn.
  - A `reset` row for the same `agent_key` closes open windows.
  - If a turn has no Stop, its windows close at the next prompt entry.
- **Exposures: the unit of the before/after comparison.** Today's pointer re-names a memory on every strong
  turn, while cross-turn suppression names it once. Counting per surfacing row would give the two windows
  different denominators by construction. So the comparison counts exposures, and the rules apply
  identically in both windows.
  - **An exposure** is `(session_key, agent_key, reset_id, mkey)`, opened by that memory's first visible
    surfacing in the reset epoch on any channel. Its channel is the channel of that first surfacing.
  - **It stays creditable** after that first surfacing until a reset of its `agent_key`, or 50 human prompt
    turns after its latest visible surfacing, whether or not it is named again.
  - **Credits.** Each turn's `used_ii` evidence for the mkey gives one credit for that (exposure, turn) pair.
    Two surfacing rows on one turn (boot plus pointer, or pointer plus guard) share that one credit.
    - A credit is the turn's `turn_ix`. A turn with no `turn` row (a subagent, or a row that never landed)
      is credited under a detector-local id, `-(prompt line offset + 1)`, which can never collide with a
      real `turn_ix`.
    - Dismissal never gates a credit (S1b review): the same transcript credits alike whether the memory is
      re-named every turn or named once.
  - **The `exposure` row** is written when the epoch closes:
    `{exposure_id = sha256(session_key, agent_key, reset_id, mkey)[:16], first_sid, channel, visible, outcome, credits: [turn_ix…], final}`.
    - The epoch closes at a reset, at the session's end, or after 24 h idle. Until then the row is
      provisional.
    - `outcome` is the strongest **per-turn evidence** across the epoch (applied, cited, cited_handle,
      opened), else `dismissed` when some turn was dismissed, else `ignored` or `unknown`. It is never
      folded from window judgments, which exist only on named turns and would differ by naming cadence.
    - `dismissed_turns` counts the creditable turns whose first assistant text matched the dismissal
      lexicon. It is checked on every such turn, named or not, so it is symmetric across pointer formats;
      it is a diagnostic only.
  - **A SessionStart reset due with boot surfacings** (no transcript entry between them) is applied first,
    whatever the timestamps, so the boot rows open in the SessionStart epoch. S1a writes the reset first;
    this is defence in depth.
  - Per-surfacing `judged` rows stay, as diagnostics per channel. They are not the comparison unit.
- **Closing at Stop(N).**
  1. Read the transcript delta.
  2. Add `last_assistant_message` as evidence for the final reply.
  3. If the transcript already holds that reply (matched by hash), the row is final.
  4. Otherwise it is provisional. The next run re-reads up to the end of turn N and writes the final row.
  5. A window no run reaches is finalized after 24 h, with `final_reason: "unflushed_tail"`.
- **Outcomes.** The strongest evidence wins, in this order:
  1. **`applied`**: a stored body shingle, not in the echo set, appears in assistant text or a tool input.
     - For channels whose injected text *is* the memory (boot learnings, boot recall text), injected shingles
       count only inside a tool input. Reciting them is not applying them.
     - In assistant text, a shingle the current or previous 2 human prompts carried does not count:
       restating a prompt that quotes a memory body is not applying it. In a tool input it still counts.
  2. **`cited`**: by title: at least 3 distinctive title tokens in one assistant sentence.
     - Title tokens that also occur in the current or the previous 2 human prompts are dropped first.
     - A title with fewer than 3 distinctive tokens left is `title_assessable: false`. This is computed for
       each turn the window is judged in, never latched at activation; a boot window is assessable when the
       rule could fire on any of its turns.
     - For boot learnings, which have no title, the same rule runs over the stored content's distinctive
       tokens.
     - **The echo rule (S1b review): recital is not use.** For channels whose injected text is the memory
       (boot learnings, boot recall), title and content tokens count only outside an 8-word echo of the
       injected line. A verbatim recital is `ignored`; outside the echo, a paraphrase (3 or more distinctive
       tokens in one sentence) or an application counts.
     - Echo coverage is computed on the same tokenisation as the ledger's echo hashes (every word, handles
       and identifiers included) and then mapped onto the words the title/content pass keeps, so a learning
       that carries an identifier or a path (`minni_recall`, `.runtime/use-ledger/`) is still recognised as
       recited.
  3. **`cited_handle`**: by handle: a wikilink, slug, vault path, `m:`/`d:` handle or `learning #id`. It is
     its own bucket in both windows and **never `used_ii`**.
     - Every handle-style form is removed from the text before the title/content pass, so a handle's own
       words (a slug tokenises into title words) never become title evidence. The shingle (`applied`) and
       echo passes see every word: a body sentence that contains a path or an identifier still matches.
  4. **`opened`**: a Read of the note's path, a read-verb Bash over it, or a `minni_drill`/`minni_recall`
     that names it. Paths and slugs match bounded (a longer name that contains the note's is not it).
     - Writing a handle and then opening it is `opened`, not `cited_handle`. A title citation stands: an open
       never cancels it (each window opens its own way, recall by title or drill by handle).
  5. **`dismissed`**: the first assistant text after the surfacing matches the dismissal lexicon.
     - Dismissal is sticky: the window can still end `applied` or `opened`, never cited.
  6. Otherwise `ignored` if visible, else `unknown(<reason>)`.
- **Headline used = `applied` + `cited` + `cited_handle`.**
- **The before/after comparison uses `used_ii`**, the identifier-independent subset: `applied`, plus
  title/content `cited`. The after pointer prints handles and today's does not, so a handle citation would
  favour the after window by construction; it is reported as its own column.
- **Symmetry is the governing property** (S1b review): every rule is applied identically whatever the
  pointer format; `use-detector-symmetry.test.mjs` feeds one transcript through a before-format and an
  after-format ledger per rule and requires equal `used_ii`, dismissed counts and exposure outcomes. The
  one intended difference is that a handle can be cited only where one was printed. Format-dependent
  actions are modelled too: each window opens the memory its own natural way. One residual asymmetry is
  known: the 50-turn horizon restarts at each visible surfacing, and the after pointer re-names at most
  every 10 turns. (The capture records each memory's full title; the after pointer prints titles up to
  100 characters, §5.2 item 6.)
- **Distinctive tokens.** A token is distinctive when its document frequency over non-noise notes is at most
  `max(1, ⌈5% × notes⌉)`.
  - The DF snapshot lives in `.runtime/df-snapshot.json`. It is written by UPS from the scan UPS already
    runs, at most once a day.
  - The detector never scans the vault. With no snapshot, the title rule is off and reported as off.
- **`explicit_recall` rows** are rebuilt from the transcript's `minni_recall`/`minni_drill` results.
  - Result text carries wikilinks and sources, not ids (§4.1). So cards are keyed through `memoryKeyFor` on
    their source.
  - A card that cannot be keyed is counted `unnameable` and reported. It is never silently dropped.
  - An `explicit_recall` row carries no body shingles (the detector never reads note bodies), so its own
    window never ends `applied`; it can end cited, cited_handle or opened. An exposure it opens can still be
    `applied` through shingles another channel recorded for the same mkey.
- **Reading the transcript.**
  - **Fast path:** a byte scan with no JSON parse, when no window is open, no surfacing row is new, and the
    delta contains neither `minni_recall` nor `minni_drill`.
  - **Slow path:** at most 2 MB of complete lines.
  - A cursor per transcript lives in `.runtime/use-detector/cursors/`. A new cursor starts 64 KB before the
    earliest open surfacing, never at byte 0.
  - Hard caps: 1.5 s and 2 MB. Unfinished work carries forward.
  - A run that recognizes no assistant event judges its windows `unknown(unparsed_transcript)`.
- **Single writer.** The detector holds an `O_EXCL` lock, `windows.lock`, per session. A lock older than 30 s
  is broken. Hooks that surface memories never touch `windows.json`.
- **Idempotent.** Deterministic `sid`s, and a check against existing ledger `sid`s before appending.
- **There is no LLM judge in the hooks.** A Jev classifier is used offline only, to calibrate the rules.

### 4.5 Reports

- `minni_status` gains a fixed 7-day `use` block. `minni_audit_report` gains `useDays`.
- New CLI: `node dist/cli.js use-report --days N [--memory <mkey>] [--window before|after]`.
- Contents:
  - counts by outcome
  - used and `used_ii` per visible exposure (and per surfacing, as diagnostics), by channel and by agent key
    (main, and by subagent type)
  - `used_ii` per 100 human prompt turns
  - top 10 surfaced-never-used and top 10 used memories
  - the share of rows left provisional, and the share `unknown`
  - the share of explicit-recall cards counted `unnameable`
  - the `features` in effect
  - `cited_handle` as its own column, and exposures with a dismissed turn
  - the `title_assessable` share, overall and per channel and agent (in `minni_status` too), so "not
    cited" can be told apart from "not assessable"
  - the fixed line "lower bounds; silent use is invisible", and the fixed undercount caveat (§4.7)
- Phase 1 measures one principal, claude-code. Other wires appear as `unmeasured` in a coverage table.
- **Stop receipt.** `SessionReceipt` gains use counts.
  - `formatSessionReceiptLine` stays **byte-identical** when `surfaced` is 0, which keeps
    `vault.test.mjs:1880` and `:1906` green unedited.
  - Otherwise it appends ` Use: <n> surfaced, <c> cited, <a> applied.` to the same line, which
    `maskTelemetry` already drops.
  - `sessionReceipt` reads the counts by default. The Stop hook passes `useCounts` only when ledger-gate says
    the ledger may be on, because reading them loads the ledger reader (`use-report.js`). With the ledger off
    the line is byte-identical to the line without counts, and Stop loads no ledger module (§8.2).
  - The counts stand as of the Stop's start. The detector judges that Stop's turn after the output is
    delivered, so a surfacing first shows as surfaced and uncited, and its outcome shows on the next Stop's
    receipt.

### 4.6 RED tests (new files)

- `use-telemetry-receipt.test.mjs`: through Stop, a pointer surfacing that the reply cites by title, and one
  followed by a synthetic dismissal line ("Not relevant to this task."). The receipt reports `cited: 1, dismissed: 1`. Today those fields are
  absent.
- `use-telemetry-guard-keys.test.mjs`: an armed state, then a Read. One `surfaced` row per named hit, with
  its `mkey`. Today the guard audits only `hits: N`.
- `use-telemetry-boot-visibility.test.mjs`: a SessionStart output over the 10k cap. Items past char 2,000 are
  `visible: false`, and items before it are `true`.
- `use-telemetry-turn-exits.test.mjs`: UPS driven through each of its six exits writes exactly one `turn`
  row with a non-`error` exit.
- `use-telemetry-delivery.test.mjs`: on a flushed output, rows are written. On a dropped output, rows carry
  `delivered: false`.
- `use-telemetry-report.test.mjs`: `auditReport()` returns a `use` section labelled with the principal.
- `use-telemetry-receipt-suffix.test.mjs`: with 0 surfaced the receipt line is byte-identical to today. With
  2 surfaced it carries the suffix, and `maskTelemetry` drops the line.
- `agent-key-subagent-context.test.mjs`: a payload with `agent_id`, and one with a `subagents/` transcript
  path, both get `a-…` keys and never close the parent's windows.
- `memory-key-source.test.mjs`: two daemon results with the same wikilink from different sources get
  different mkeys, and `d:` handles carry the source tag.
- `use-detector-rules.test.mjs`: one case each for `applied`, title-`cited` (prompt tokens excluded),
  `opened`, sticky dismissal, and naming before opening.
- `use-detector-exposure.test.mjs`:
  - a memory named on turn 1, suppressed on turns 2–3, and cited on turn 3 gives one exposure, `used_ii`, with
    one credit on turn 3
  - the same memory re-named on turns 1–3, today's behaviour, also gives one exposure
  - a reset between turns 1 and 3 closes the first exposure, so the turn-3 citation is not credited to it
  - boot plus pointer for one mkey on one turn gives one credit
- `use-detector-final-turn.test.mjs`: a one-turn session whose transcript lacks the final reply is judged
  from `last_assistant_message`, as provisional and then final.
- `use-detector-explicit-recall.test.mjs`: a `minni_recall` result with one vault card and one card whose
  wikilink resolves nowhere gives one `explicit_recall` row and one `unnameable` count.
- `use-detector-single-writer.test.mjs`: two concurrent Stops for one session leave a valid
  `windows.json` and no duplicate `judged` rows.
- `use-detector-fast-path.test.mjs`: a Stop with no open windows parses no JSON.
- `tests/test_read_recent_learning_ids.py`: `read` returns structured `recent_learnings` ids and content. The
  rendered text is byte-identical to today.

### 4.7 Metric and target

- **Detector validity (offline, S1c).**
  - A labelling pass labels the 29 visible audited surfacings and the 5 need moments, writing to
    `labels/labels.json` under the private root. One labeller does it, and the operator spot-checks 10 rows.
  - Bar:
    - each `helped` surfacing comes out `cited` or `applied`, 2 of 2
    - every labelled dismissal comes out `dismissed` or `ignored`
    - at most 2 non-used visible surfacings come out `cited` or `applied`
  - With 2 positives this is a sanity bar, and the report says so.
- **Live before/after (Item 1's used rate).**
  - **Before window.** It opens when S1 is deployed (verified by `make check-deployments`) and closes when
    all three hold:
    - at least 5 calendar days
    - at least 100 human prompt turns
    - at least 50 visible exposures on per-turn channels (§4.4)

    The cap is 10 days. If the cap hits first, the report says so and uses what it has.
  - **Switch.** One deployment carries every Item 2 and Item 4 slice, all switched on. Slices merge to local
    `main` as they pass, but the *deployment* waits for the before window to close.
  - **After window.** It opens at the switch, and uses the same closing rule.
  - **Reported per window:**
    - `used_ii` per visible exposure, overall and per channel of first surfacing
    - `used_ii` credits per 100 human prompt turns
    - headline used (cited + applied) per exposure
    - opened and dismissed rates
    - main thread and subagents separately
    - session-clustered bootstrap intervals for the differences
    - per-surfacing rates, as diagnostics
  - **Success:** the after window's `used_ii` per visible exposure is higher than the before window's, *and*
    its `used_ii` credits per 100 human prompt turns are not lower. The second condition stops the rate
    rising just because less was shown. Human prompt turns are classified by the same S1a rules in both
    windows (§4.3).
  - **The guard channel will be nearly silent after the switch,** by design. It surfaces only when no
    pointer was delivered (§6.3.4). The channel comparison that matters is the pointer's.
  - Either way, the result is reported, never auto-reverted. It carries the §0 caveats.
  - Every report also carries this fixed line: "Known undercount: a short rule applied by quoting it word
    for word reads as recital and is not counted; this affects both windows equally, and the used rate is a
    floor."
  - **`used_ii` is mostly `applied`.** `title_assessable` is about 3% of titles in this vault: few titles keep
    3 distinctive tokens once the prompt filter runs. Title citation therefore adds little to `used_ii`, and
    the headline number should be read with that in mind. S1c calibration revisits the 5% DF threshold.
  - The after pointer prints titles up to 100 characters (§5.2 item 6) so that both windows show comparable
    title text; the title rule can then fire in either window.

---

## 5. Item 2 — Recall precision (S3a, S3b, S3c)

### 5.1 Current behaviour

See §1: prompt recall on everything, the miscalibrated vault gate, the cut before any rerank, the
single-name pointer, harvest of chatter, and recency-only boot learnings.

Also:
- 109 of the prompts that looked human are **scheduled check-ins**. They are plain text with no tag (B13).
- `scoreVaultNote` (`vault.ts:867`) has no callers.
- `searchVaultNotes` also feeds `prepareTask`, handoff packets and the `minni_recall` prescan
  (`task.ts:1054`, `:1363`; `server.ts:680`). So any change to its scoring has to be opt-in.

### 5.2 Mechanism

**1. Prompt classification.** The rules ship in S1a in record-only mode (§4.3). S3a wires their actions.
Rules live in `harness-envelope-rules.ts` (typed constants, each with a
`rule_id`), with a text-rules Python mirror in `src/minni/afm_passes/harness_envelopes.py`. A shared case file
`tests/fixtures/harness_envelope_cases.json` runs in node and pytest. Each side asserts that every one of its
`rule_id`s has a case.

- `blockTags`: blocks stripped anywhere, such as `system-reminder`.
- `skip` (no prompt recall, no harvest):
  - `<task-notification`, `<side-sessions-event>`, `<ci-monitor-event>`
  - `<command-name>`/`<command-message>`, `<local-command-stdout>`/`<local-command-caveat>`
  - `<bash-input>` (subkind `human_shell`)
- `wrappers`: stripped, leaving the body as the payload.
  - the `<cross-session-message …>` tags
  - the `Another Claude session sent a message` lead line
  - `[Workflow harness …]` and `[Artifact comment …]` headers
- **`scheduled`**, checked in this order:
  1. **Host marker.** At Stop the payload carries `session_crons` (P8 saw it on SubagentStop; S0 confirms
     Stop). A prompt equal to a listed cron prompt, after normalization, is `scheduled`. UPS carries no cron
     list (P7 keys), so UPS uses the template rule.
  2. **Template rule.**
     - Normalize the whole prompt: strip blocks, lowercase, digit runs → `N`, collapse whitespace. Hash it to
       `sha1[:12]`.
     - Only prompts of at least 80 chars are eligible.
     - **The rule.** For a prompt at time `at` with id `pid`, the hash is `scheduled` when **at least 3 prior
       occurrences** exist. Prior means `ts < at` and `pid` differs from the prompt's own. In addition, either
       at least 2 distinct sessions were first seen before `at`, or `at − first_seen ≥ 2 h`.
     - **The counts file** is `.runtime/prompt-templates.json`, holding
       `{hash: {first_seen, occ: [{ts, pid} ≤ 16, newest kept], sessions: {<8-char key prefix>: first_ts} ≤ 8}}`.
       At most 500 entries, pruned after 14 idle days. It stores no text.
     - **Why both stores.** The 16-entry ring answers "how many before `at`", and it saturates far above the
       threshold of 3. `first_seen` and the per-session first timestamps sit outside the ring, so a check-in
       recurring every 10 minutes in one long session no longer flips class once the ring rolls past its
       start. Revision 4 derived both from the ring.
     - **UPS and harvest agree.** UPS records `{ts, pid}` when it counts the prompt. Harvest at Stop evaluates
       the same transcript prompt by its `promptId`, so its own occurrence is excluded and it sees the same
       priors UPS saw.
     - Only UPS writes the file.
- **At UPS:**
  - `skip`: no recall, no recall-state write or clear, no pointer. The plan pointer, pending attention,
    degraded notes and the lifecycle line still ride as on the normal path.
  - `wrapped`: recall on the body only.
  - `scheduled`: recall as usual, under cross-turn suppression.
  - Classification is recorded on the `turn` row, and the audit gains `envelope_kind`, `subkind` and
    `suspicious_prefix?`.

**2. Harvest provenance gate (S3a)**, in `isHumanTypedInput`, in this order:
1. Reject `isMeta`.
2. Reject a present `origin.kind` other than `human`.
3. Screen the block-stripped text, and reject it if empty.
4. Reject `skip` and `wrapped`.
5. Reject `scheduled`, by host marker or by the read-only template rule.

Rules 1–2 key on Claude fields. Rules 3–5 apply to every wire.

**3. One durability predicate (S3b)**: `durabilityOf(item) → noise | durable | ordinary`. It is a TS function
(`durability.ts`) with a Python mirror (`src/minni/durability.py`) and a shared case file.

- **noise**, by content class:
  - relays and task notifications (the envelope rules)
  - compaction summaries (`source: compact_distillation:` frontmatter, or `wiki/sessions/*-compact-*`)
  - plan journals (`wiki/artifacts/plan-*.log.md`)
  - finished plan notes: status `complete`, or `candidate` with every slice resolved

  `accepted` plan notes are not noise.
- **durable** (not noise, and any of):
  - a normalized category in {decision, fix, procedure, architecture}. Vault synonyms map onto these, for
    example `bugfix`/`scar` → fix and `adr` → decision.
  - the content test, a leading `Decision:`, `Fix:`, `Root cause:`, `Resolution:`, `Invariant:`, `Never ` or
    `Always `, or a `## Resolution`/`## Fix` section
    - It ships only if its precision is at least 0.8 on a labelled sample of 100 `general` rows.
  - a `preference` created on or after `MINNI_PROVENANCE_GATE_SINCE`, the date the harvest gate deployed
    - An unknown-origin `preference` is ordinary. Before the gate, `preference` is exactly the chatter
      population.
- **ordinary:** everything else.

**4. Durability-ranked boot `recent_learnings` (S3b, daemon, all wires).**
- Fetch two groups:
  - the durable-category group, newest 20
  - all other active rows, newest 100, to which the content test is also applied
- Drop noise and today's low-signal rows. Dedupe by hash, as now.
- Fill 10 slots:
  - up to **K** reserved slots from the durable pool, newest first, rendered first
  - the rest by recency
- `K = min(4, max(1, ⌊median durable pool / 2⌋))`, computed by S1c. The default is 2. With a pool of 0,
  reservation is off.
- `tests/test_issue_437_learnings_dedup.py` semantics are kept.

**5. Vault gate and ranking (S3c, UPS only, opt-in arguments).**
- `searchVaultNotes(…, opts?)` gains `{rank: "ups_gate" | "noise_last", excludePaths?}`. With no `opts`,
  behaviour is unchanged. Under `ups_gate` it:
  1. records, per scored entry, which query terms contributed and where (title, heading or body)
  2. computes DF over non-noise entries
  3. assigns a gate route:
     - **(a)** a phrase hit on a stripped prompt of at least 24 chars
     - **(b)** coverage of at least `k(q) = max(3, ⌈ρ × d_q⌉)` distinctive query terms, one of them in the
       title or a heading
  4. sorts by `classWeight × gateScore`, where `classWeight` is 0.25 for noise and 1 otherwise, before
     slicing
- `ρ` starts at 0.2 and is calibrated in S1c over {0.1, 0.2, 0.3}. It is the smallest value meeting the §5.4
  noise and recall targets in every prompt-length and domain-density slice with at least 20 turns.
- A vault hit opens the gate only if it is not noise and has route (a) or (b). The daemon's calibrated
  confidence (≥ 0.55) stays the main gate.
- **Explicit `minni_recall`.** Daemon results are stably partitioned, non-noise first, by resolving each
  result's `source` to its class. Nothing is dropped. Behind `MINNI_RECALL_NOISE_LAST`.
  - The vault prescan runs only when the daemon is down or empty (`shouldPrescanVault`,
    `daemon_transport.ts:1175-1177`), and it is **never merged** with a daemon answer (the X5 invariant,
    `slice-x5-recall-prescan.test.mjs`). It gets `noise_last` on that path only.

**6. Multi-hit pointer with handles (S3c).**
- `selectPointerHits` takes these steps:
  1. Dedupe by mkey.
  2. Drop hits with no resolvable handle.
  3. Choose up to 3, each clearing its own route's gate.
  4. Scores are never compared across routes. Each route's leader goes first, in the order daemon, then
     vault. Then come in-band candidates, within 0.15 of their own route's top.
  5. A noise hit may take slot 2 or 3 only when a non-noise hit holds a slot.
- **Rendering.**
  - Each named hit is `"<title>" (m:<8hex>|d:<src>:<id>) <score>`, where `<title>` is the hit's sanitized
    title, up to 100 characters. (Decided after the S1b review: a 32-character clip made title citation
    impossible in the after window, 0 of 49 assessable titles; see §4.7.)
  - No hit carries its full wikilink: the `m:`/`d:` handles resolve through `minni_drill`.
  - Budget: a header of about 80 characters plus 3 hits of about 120 characters is about 440 characters,
    about 110 estimated tokens, inside the pinned 120-token budget. Identifiers are never clipped, and a hit
    that does not fit is dropped.
  - The pinned phrases "relevant memor" and "Consult minni_recall" stay.
- **`minni_drill`** accepts these handles in `references`:
  - `m:` resolves locally, safe notes only.
  - `d:` becomes `{doc_id, source}`.
  - Anything else passes through.
  - Text comes back in the EVIDENCE envelope. With the daemon down, `m:` still resolves.
- **The persisted hit set is the named set.** UPS persists `top_hits` (≤ 3), `pointer_named` and an
  `arming_id` nonce. `pointer_named` is added by a delivery commit, so a dropped pointer leaves the guard its
  backstop.

**7. Cross-turn suppression (S3c).**
- **Counters file:** `.runtime/recall-session.d/<session_key>/counters.json` =
  `{turn_ix, surfaced: {mkey: {turn_ix, reset_id, scores}}}`, with at most 64 entries.
  - UPS is the only writer.
  - `clearRecallState` never touches it.
- **Reset ids:** `.runtime/recall-session.d/<session_key>/<agent_key>.reset`, written with a fresh id on
  PreCompact, PostCompact and SessionStart.
- **A hit X is suppressed when all of these hold:**
  - it was named under the current reset id
  - it was named within the last 10 turns
  - no route present both now and then shows a score rise of at least 0.10
- **All hits suppressed:** no pointer, and the guard is not armed.
- **What counts as named.** `surfaced[X]` entries are delivery commits, so a pointer that was never seen
  suppresses nothing.

**8. Masking.** `maskTelemetry` drops the new pointer and guard prefixes.

### 5.3 RED tests (new files)

- `jev-harvest-provenance.test.mjs`: no trigger for each of:
  - an unflagged relay
  - an `origin.kind: "peer"` entry
  - an `isMeta` entry
  - an `origin.kind: "human"` entry whose only directive sits in a `system-reminder`
  - a scheduled template

  A typed human directive still triggers. Today all five trigger.
- `ups-envelope-skip.test.mjs`: a task-notification prompt against a fake daemon and vault with strong hits
  gives no pointer and no recall-state write. The plan pointer still rides. Today the pointer is planted.
- `ups-relay-body-recall.test.mjs`: a relay whose header words match a journal and whose body matches a
  decision names the decision.
- `prompt-template-rule.test.mjs` covers:
  - 20 occurrences in one session within 1 h stay `human`
  - the 4th occurrence 2 h after `first_seen` is `scheduled`
  - one session at a 10-minute cadence for 72 h never flips back to `human` after it first becomes
    `scheduled`, although the ring has rolled over many times
  - a 3-session recurrence is `scheduled`
  - harvest evaluating a prompt by its own `promptId` gets the same class UPS recorded
  - a short prompt is never `scheduled`
- `recall-state-vault-calibration.test.mjs`:
  - a term-count-only score of 50 or more does not open the gate. Today it opens with `top_score: 1`.
  - a 1-word phrase hit does not open it
  - route (b) on 3 distinctive terms, one in the title, does open it
- `ups-durable-crowdout.test.mjs`: 22 plan journals echoing a 14-term prompt, plus one decision note with 3
  of those terms in its title. The decision note is named. Today it ranks 23rd and is cut.
- `recall-pointer-multihit.test.mjs`: the 120-token budget stays a pinned test. Its fixture is 3 gated hits
  with maximal 100-character titles and 90–150-char paths; with the header and their unclipped handles,
  the pointer stays within 120 tokens, and no wikilink is printed.
- `recall-pointer-title-sanitize.test.mjs`: a title containing control characters, bidi overrides
  (U+202A–U+202E, U+2066–U+2069) and newlines comes out with none of them, and is still at most 100
  characters.
- `recall-pointer-route-bands.test.mjs`: a vault phrase hit at 1.0 does not exclude daemon hits at 0.6–0.7.
- `drill-handle-resolve.test.mjs`:
  - `m:` resolves locally with the daemon down
  - a `d:<src>:<id>` named on this session's pointer reaches the fake daemon as `{doc_id, src, source}`,
    with `source` from the handle registry
  - an unregistered `d:` handle is sent as `{doc_id, src}` and comes back marked `source_unverified`
  - an unsafe note returns `unresolved`
- `ups-cross-turn-suppression.test.mjs`:
  - a hit named on turn 1 is not re-named on turns 2–5
  - it is re-named after a reset, and after a score rise of 0.10 or more on the same route
  - a dropped pointer suppresses nothing
- `recall-noise-last.test.mjs`: two paths, as two tests.
  - Daemon results with 3 compaction summaries ahead of a decision are reordered, decision first.
  - With the daemon empty, the vault prescan applies `noise_last`.

  The X5 tests stay green.
- `tests/test_recent_learnings_durability.py`: 10 recent relay rows plus 1 older `decision`. The decision
  is in `## Learnings` and no relay is. Today: 10 relays and no decision.
- Parity tests: `harness-envelope-parity.test.mjs` with `tests/test_harness_envelope_parity.py`, and
  `durability-parity.test.mjs` with `tests/test_durability_parity.py`.
- `mask-telemetry-new-prefixes.test.mjs`.

### 5.4 Metric and target (deterministic replay, fixed now)

These run over the campaign corpus in S1c's replay harness (§8.1), base `eaf294fd` against head, with
daemon inputs re-queried against a time-filtered DB copy and a time-filtered vault snapshot.

| Metric | Today | Target |
|---|---|---|
| Machine-output turns that run recall (task notifications plus `<bash-input>`) | 332 | 0 |
| Relays recalled on the body only | 0 / 58 | 58 / 58 |
| Harvest triggers from non-human entries | (replay counts) | 0 |
| Re-derived check-ins classed `scheduled` | 0 | ≥ 90%, with false-`scheduled` human prompts ≤ 2% |
| Strong-turn rate on recallable turns | (replay counts; 98% over all turns) | ≤ 50% |
| Noise share of the pointer's top slot on strong recallable turns | (replay counts; 94% over all turns) | ≤ 15% |
| Durable recall@6 against a noise-free reference run | (replay counts) | ≥ 80% |
| Durable notes crowded out of the cut | (replay counts) | 0 |
| Chatter share of boot `recent_learnings` slots | 30 / 110 | ≤ 5% (see the note below) |
| Noise share of the first 3 daemon results in explicit `minni_recall`, when ≥ 3 non-noise exist | (replay counts) | ≤ 15%, nothing dropped |
| Identifiers clipped; handles that fail to resolve | — | 0; 0 |

**The chatter target and stored rows.** The harvest gate stops new chatter. Already-stored chatter is caught
only when the durability predicate recognizes it. Pre-gate `preference` rows are `ordinary`, so they can
still fill recency slots. Stored rows are never changed (D2, as in revision 4).
- S1c first measures the predicate on the stored chatter population: how much of B7 it classes as noise.
- If it catches at least 80%, the ≤ 5% target applies to all slots.
- Otherwise the target is ≤ 5% over rows created after the gate date. The pre-gate residual is reported
  separately, and `use-report` lists the suspected rows for Hans to review.

**Pinned moments (offline):**
- Retention: both `helped` surfacings are still visible at their moments, 2 of 2.
- Need moments:
  - The 3 never-recalled learnings are addressable in Phase 1. At least 2 of 3 must be visible at their
    moment.
  - "Visible" means named in the pointer, or delivered inline in a non-spilled SessionStart envelope with no
    reset between boot and the moment.
  - The 2 recurrence moments need Item 3. They are reported as the Phase 1b baseline.

---

## 6. Item 4 — Cost: envelope and guard (S2a, S2b)

### 6.1 Current behaviour

See §1 and B8, B9′, B10, B14, B17, B26.

Pinned tests the change must respect:
- `sessionstart-layer1-shelf.test.mjs:96-130` pins an oversized shelf at exactly 8,192 inline chars
  (§6.2.4).
- `sessionstart-shelf-drift.test.mjs:196-230` pins reader-truncation semantics.
- `identity-body-delivery.test.mjs:157-165` pins `tokens ≤ budget` on a small fixture.
- `correction-reinjection.test.mjs` runs a `HOOK_MATRIX` across wires. Its fake boot correction has
  `score: 1.42` and no `confidence`, and `body.recall` must match `/port 9090/`.
- `recall-guard.test.mjs:325` pins that 20 concurrent `markRecallConsumed` calls all return true.

### 6.2 Envelope (S2a)

**1. Priority serializer.** `serializeEnvelope(body, priority)` emits top-level keys in priority order.
Nested values still go through the unchanged `stableStringify`. The order:
1. `contract`
2. `trimmed`/`over_budget`
3. `identity`
4. `degraded`
5. `corrections_reassert`
6. `lifecycle`
7. `pending_learnings`|`expired_handoffs`
8. `handoff_acks`, `handoff_context_withheld`, `stale_beliefs`
9. `active_thread`
10. `recall`
11. `recent_learnings`
12. `handoff_context`
13. `pending_attention`
14. `identity_body`
15. `layer1_shelf`
16. `layer1_source`, `fallback_commands`
17. `audit_tail`
18. anything else, alphabetically

Even a spilled envelope's 2 KB preview then carries the contract and the warnings.

**2. `fitEnvelope(body, limits)`**, called by SessionStart before `wrapEnvelope`.
- **Limits.** `tokenLimit` is the declared budget, always enforced. `charLimit` is the host inline cap minus a
  wrapper reserve; claude-code is 10,000, and the value comes from a wire capability field.
- **Never trimmed:**
  - `contract`, `identity`, `degraded`, `corrections_reassert`, `lifecycle`
  - `pending_learnings`/`expired_handoffs`
  - `handoff_acks`, `handoff_context_withheld`, `stale_beliefs`
  - `layer1_source`, `fallback_commands`
- **Trimmed in this order, each to a floor:**
  1. `audit_tail` → 0
  2. `recall` → the top 1, plus up to 2 results with correction salience; `episodic` first
  3. `recent_learnings` → 3, with the reserved durable rows rendered first
  4. `handoff_context` → refs only
  5. `pending_attention` → a count
  6. `layer1_shelf` → the boot form (§6.2.4)
  7. `identity_body` → 2,000 chars plus a marker
  8. `active_thread` → its pointer form, last

  The shelf goes before the active plan. On claude-code the floors alone (boot form 4,000, plus
  `identity_body` 2,000, the contract and the lifecycle line) take about 6.9k of the 10k cap. An earlier
  plan trim would demote the current plan on most boots with a large `core.md`.
- **Accounting.** `trimmed: {section: {from, to}}`. If the floors alone exceed the limits:
  `over_budget: {tokens, chars, limit, reason}`.
- **Agent-context wires.** `nativeLayer1` counts toward the same total, with a 2,000-char floor.

**3. Boot recall diet** (`buildBootRecallSlice`).
- Each result is projected to `{wikilink, title, text, confidence?, score?, layer, review_state, age_days, correction?}`.
  - `text` keeps its EVIDENCE wrapper.
  - Dropped: `evidence_envelope`, provenance internals, `rationale`, `recommended_*`, `attribution*`.
- **No 0.55 gate at boot.** Only 10 of 74 boot results clear it (B21). Results without `confidence` would
  break `correction-reinjection`, and the widened boot layers exist to deliver corrections
  (`daemon_transport.ts:205-215`).
- **The cap instead:** keep the first 3 non-identity results in daemon rank order. Correction-salience
  results stay, up to 2 beyond the cap. `episodic` is capped at 2.
- **Noise ordering at boot (S3b, after the predicate exists).** Results whose `source` path matches the noise
  path patterns (compaction, plan journal) move after non-noise results before the cap.
  - This is classified from fields the result already carries. **No vault read on the boot path.**
- **Empty recall** gets `recall.note: "no boot recall results; call minni_recall"`.

**4. Q2(a): the Layer 1 boot form.**
- **What it is.** When the fit reaches step 8, the shelf becomes a boot form of at most 4,000 chars:
  - If `layer1/boot.md` exists, it is used, clipped to 4,000 chars at a paragraph boundary. The operator
    writes and owns it.
  - Otherwise, the first paragraphs of `layer1/core.md` up to 4,000 chars.
  - Either way it ends with the marker
    `[layer1 shelf: boot form, <n> of <m> chars; full file: layer1/core.md]`.
- **What changes for the current fixed 8 KB shelf.**
  - Today the inline shelf is the first 8,192 bytes of `core.md`, verbatim, and
    `sessionstart-layer1-shelf.test.mjs:96-130` pins exactly that.
  - Under Q2(a), whenever the envelope would exceed the 10k-char cap, the inline shelf is the boot form
    instead.
  - The reader (`readLayer1Shelf`, `vault.ts:3567`) and its `truncated`/`omitted_bytes` result are
    unchanged. The fit records `layer1_shelf.inline_trim` beside them, so `shelf_drift` and its test
    (`sessionstart-shelf-drift.test.mjs:196-230`) are unchanged.
  - **One existing test is updated, under this operator decision:**
    `sessionstart-layer1-shelf.test.mjs:96-130`. It now asserts the boot form, the marker and the unchanged
    reader fields, instead of the 8,192-char inline pin. The slice records the decision. No other existing
    test is edited.
  - `LAYER1_SHELF_MAX_BYTES` (`vault.ts:3476`) stays the reader cap.

**5. SessionStart critical path.**
- Only envelope assembly and the immediate rows (`reset`, the boot `surfaced` rows' construction) run before
  emission.
- Pruning and rotation run after delivery, registered with `deferUntilDelivered`, in at most 150 ms per run
  with resumable cursors.
- That cap is cooperative. Each step is written to stop at item boundaries, because `runDeliveryCommits`
  awaits each commit with no timeout (`hook-delivery.ts:68-79`).
- No step reads the whole vault.

### 6.3 Guard (S2b)

**1. Session and agent scoping.**
- Recall state moves to `.runtime/recall-state.d/<session_key>.json`, written by that session's UPS and read
  by that session's PreToolUse.
- The legacy `.runtime/recall-state.json` remains as a last-writer mirror carrying `session_id`. It keeps the
  Board, `/api/recall-state`, the gemini dedupe and the existing fixtures working. A payload without
  `session_id` reads the mirror, as today.
- `writeRecallState` gains the new fields in its fixed field list: `session_id`, `agent_id`, `prompt_id`,
  `pointer_named`, `arming_id`.
- **The envelope's `recall_state` field keeps the mirror path.** `recall-pointer.test.mjs:140` pins
  `<vault>/.runtime/recall-state.json`. An `unlabeled` session (no `session_id`) writes only the mirror.

**2. One surfacing per arming.**
- `markRecallConsumed` and its all-true contract are **untouched** (`recall-guard.test.mjs:325`).
- A new `claimArming(vault, session_key, arming_id)` creates
  `.runtime/recall-state.d/<session_key>.<arming_id>.claim` with `wx` (`O_CREAT|O_EXCL`).
  - The first creator surfaces. Every other racer allows silently.
  - On any error, allow.
  - Claims older than 24 h are pruned after delivery.
  - `arming_id` is a fresh random nonce that UPS writes into every plant, so re-planting an identical state
    is a new arming. This keeps the tests that re-plant `STRONG_STATE` green.

**3. Delivery: context, not deny, on claude-code.**
- P1 and P2 passed, so the claude-code wire sets a new capability, `preToolUseContext: true`, and the guard
  returns:

  ```json
  {"continue": true, "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "<reason>"}}
  ```

  with **no `permissionDecision`**. The call runs. The user's permission flow is untouched. Nothing is
  bounced.
- `PreToolUseDecisionOutput` (`recall-guard.ts:151-158`) widens to a union.
- **Other wires.** The guard arms only on wires that inject at UPS: claude-code, codex, kilocode and agy (the
  gemini wire). grok-build, cursor and the unprofiled generic wire cannot inject at UPS, so
  `handlePreToolUse` allows before the guard runs (`hook-handlers.ts:1575`; `GROK_INJECTABLE`,
  `CURSOR_INJECTABLE` and `unprofiledWire` in `hook-platform.ts`). They stay that way, and their existing
  allow-tests stay unedited.
  - codex, kilocode and agy keep deny delivery, now session-scoped and claimed once per arming. Each gets an
    explicit "still denies" test (Hans's approval condition).
  - The adapters already fall through to allow for any non-deny shape (`cursor-adapter.ts:34`,
    `gemini-adapter.ts:142`, `grok-adapter.ts:113`). A test pins that the context shape maps to allow.
- **Surfacing still consumes.** Context delivery calls `markRecallConsumed` after a won claim, exactly as
  today's deny does, so `consumed` semantics are unchanged (`recall-guard.test.mjs:132`, `:209`).
- **Claim key without an `arming_id`.** Legacy plants and today's fixtures carry no `arming_id`. The claim key
  then falls back to `sha1(task_signature + ts)`.
- `MINNI_RECALL_GUARD_DELIVERY=deny` restores deny on claude-code.
- The stale header comments (`recall-guard.ts:10-16`, `:124-127`) are corrected.
- **Existing tests that pin the claude-code deny shape.** These fixtures build with `agentId: "claude-code"`,
  which resolves to the claude-code wire (`hook-handlers.ts:619`). Context delivery turns these assertions
  red:
  - `recall-guard.test.mjs:123`, `:125-128`, `:143`, `:233`
  - `recall-guard-dropped-inject.test.mjs:244-245`
  - `hook-behavior.test.mjs:984`, `:1007`

  They pin the bounce shape that this item removes. They are on the §8.3 list of edits that approval
  authorizes. Each edit is one of two kinds:
  - set `MINNI_RECALL_GUARD_DELIVERY=deny` in the fixture, so it keeps testing deny delivery
  - or assert the context shape plus `consumed === true`

  **The other choice** is to ship context delivery behind a default-off flag, which needs no test edits. The
  bounces would then stay until the flag is flipped, which contradicts "ship on".

**4. Already surfaced, or already consulted.**
- In soft mode on the main thread, the guard surfaces only when the state has **no** `pointer_named`, that is,
  when the pointer was never delivered.
- A PreToolUse for a tool whose name ends in `minni_recall` or `minni_drill` claims the arming and allows. The
  agent's own consult satisfies the guard.
- When the guard does surface, it lists the named set (≤ 3).

**5. No arming on `skip` prompts** (§5.2.1).

**6. Subagents: a brief at start, judging at stop** (Q3; P5, P8 and P13 passed).
- **The guard never surfaces in a subagent context.** The parent's arming is the parent's.
- **Wiring.** SubagentStart and SubagentStop get their own dispatch arms and manifest entries.
  - They bypass the envelope gate the way PreToolUse does. The gate keeps its literal form:
    `event !== PRE_TOOL_USE_EVENT && !SUBAGENT_EVENTS.has(event) && !VALID_EVENTS.includes(event as EnvelopeEvent)`.
    `r8-hook-parity.test.mjs:680` searches for the last clause.
  - `VALID_EVENTS` is not widened.
  - Each arm emits its `hookSpecificOutput` shape directly, as PreToolUse does, so neither `EnvelopeEvent` nor
    `CLAUDE_INJECTABLE` changes.
- **SubagentStart brief.**
  - It injects `additionalContext` of at most 600 chars:
    - the parent session's current named set (titles plus handles)
    - plus, when the task prompt is recoverable, up to 2 gated hits from a recall on it
  - **Where the task prompt comes from.** It is not in the SubagentStart payload (P13), and the parent
    transcript has not flushed the `Agent` call yet (P17). The parent's own PreToolUse for that call does
    carry it, as `tool_input.prompt`, with `tool_use_id` and `subagent_type`.
    - So Minni's PreToolUse, for `Agent` or `Task`, appends `{tool_use_id, subagent_type, prompt_hash, prompt ≤ 2,000 chars, ts}`
      to `.runtime/recall-session.d/<session_key>/subagent-pending.json`.
    - That file is private and pruned after 10 minutes. The prompt is clipped and never written to the
      ledger.
    - SubagentStart claims the oldest unclaimed entry with its `agent_type`, through a `wx` claim marker.
      Parallel subagents of one type are paired in order. That is approximate, and the report counts it.
    - With no entry, the brief carries only the named set.
  - Each brief writes `subagent_brief` ledger rows under the subagent's `agent_key`.
  - Kill switch: `MINNI_SUBAGENT_BRIEF`.
- **SubagentStop judging** (new event arm). It runs the detector over `agent_transcript_path` for that
  `agent_key`'s windows: the brief, explicit recalls made inside the subagent, and its final
  `last_assistant_message`.

**7. PreToolUse matcher.** `hooks.json` PreToolUse gains a matcher built from the guard's scope sets:
`Grep`, `Read`, `Glob`, `Bash`, `run_terminal_command`, `mcp__.*minni_(recall|drill)`, and `Agent|Task`
for the subagent brief's prompt stash. A test derives it from `recall-guard.ts`. This removes the hook spawn on Edit, Write and other MCP calls (17,761 PreToolUse
spawns in the corpus).

### 6.4 RED tests (new files)

- `session-start-budget-enforced.test.mjs`: 4 large daemon results plus an 8 KB shelf give `tokens ≤ budget`
  and `chars ≤ 10,000`, with `trimmed` present. Today the envelope is about 2.5× over, with no `trimmed`.
- `envelope-priority-order.test.mjs`: the emitted envelope starts with `contract`. Plan digest tests stay
  green.
- `boot-recall-slice-diet.test.mjs`:
  - no result carries `evidence_envelope`
  - 3 non-identity results plus a correction are kept
  - a result without `confidence` is kept
- `boot-recall-empty-note.test.mjs`.
- `layer1-boot-form.test.mjs`:
  - an envelope over the cap gets the boot form with its marker
  - `layer1/boot.md` is preferred
  - the reader's `truncated` and `omitted_bytes` are unchanged
  - an active plan plus an 8 KB shelf keeps `active_thread` in full form
- `recall-guard-session-scope.test.mjs`: session A plants, then session B's Read gets no surfacing. Today it
  is denied.
- `recall-guard-claim-once.test.mjs`: 4 concurrent Reads against one arming give exactly 1 surfacing. Today
  4 denies.
- `recall-guard-context-delivery.test.mjs`:
  - a claude-code Read with armed state returns `additionalContext`, no `permissionDecision`, and leaves
    `consumed === true`
  - codex, kilocode and agy still deny, once per arming
  - grok-build, cursor and the generic wire allow, unchanged

  Today claude-code denies.
- `recall-guard-already-surfaced.test.mjs`: with `pointer_named` set, a main-thread Read gets no surfacing.
- `recall-guard-consult.test.mjs`: `minni_recall`, then Read, gives no surfacing.
- `recall-guard-subagent.test.mjs`: a payload with `agent_id` gets no guard surfacing.
- `subagent-brief.test.mjs`:
  - SubagentStart returns a brief within 600 chars, holding the parent's named set and the hits from a task
    prompt stashed by a preceding parent PreToolUse(`Agent`)
  - two stashed prompts of one type are claimed once each, in order
  - with no task prompt found, the named set alone
- `subagent-stop-judging.test.mjs`: a synthetic subagent transcript that cites the brief's memory gives a
  `cited` `judged` row under the subagent's `agent_key`.
- `pretooluse-matcher-coverage.test.mjs`.

### 6.5 Metric and target (deterministic replay, fixed now)

| Metric | Today | Target |
|---|---|---|
| Captured boot bodies within 4,000 estimated tokens | 0 / 10 | 10 / 10 |
| Captured boot bodies within 10,000 chars | 0 / 10 | 10 / 10 |
| Median boot recall slice | about 7,200 tokens | ≤ 600 tokens |
| Correction-salience results retained at boot | — | 100% |
| claude-code guard denies | 107 | 0 (surfacing by context instead) |
| Guard surfacings in a session other than the arming one | 39 | 0 |
| Guard surfacings per arming under parallel batches | 4 of 4 (B26) | ≤ 1 |
| Guard surfacings after the agent's own `minni_recall`/`minni_drill` in the same turn | (replay counts) | 0 |
| Main-thread guard surfacings on turns whose pointer was delivered | (replay counts) | 0 |
| Guard surfacings in subagent contexts | 82 denies | 0 (the brief replaces them) |

**Latency.** S1a measures today's baseline over 50 runs. Targets:
- SessionStart envelope assembly: p95 at most the baseline plus 50 ms
- UPS: at most 15 ms p95 added
- SubagentStart brief: p95 at most 150 ms above the trivial-hook floor (0.15–0.18 s)

**Live check after the switch.** The next 10 claude-code SessionStart attachments show no persisted-output
stub.

---

## 7. Item 3 — Phase 1b (not delivered in Phase 1)

Phase 1 makes no promise on Item 3. This section keeps the evidence and the shape of the work, so Phase 1b
starts from facts.

- **Evidence.**
  - No hook observes tool results today (`hooks.json`).
  - Agent-written scar signals never matched real tool output: 0 of 31 claude-code plan scars against 11,387
    tool results.
  - The two audited recurrences:
    - 64 hook-blocked `git show`/`git checkout` calls, after a lesson that said to use `git cat-file`
    - a test-registration failure that recurred after its lesson was recorded
- **The open blocker, carried here.** In the campaign transcripts, 350 of 352 Bash results carrying Swift
  test-failure text had `is_error: false`. In 202 of them the command masked its exit code (`| tail`,
  `echo exit=$?`, `; …`). A trigger on `PostToolUseFailure` or on a non-zero exit therefore misses almost
  all failing-test output. **Phase 1b must match the output text of every Bash call** (`PostToolUse`
  `tool_response`), plus `PostToolUseFailure` for real failures.
- **What the probes settle.**
  - `PostToolUseFailure` does not fire for a hook-denied call (P3).
  - PreToolUse context reaches the model even when another hook denies the same call (P2). So the
    hook-blocked `git show` class is reachable by a pre-emptive `command` signature at Minni's own
    PreToolUse.
- **Shape.**
  - Signatures are mined from recurring errors and approved by the operator.
  - They are normalized literals scoped to a project key (the git common directory), not a checkout.
  - They fire through `additionalContext`, rate-limited per session and lesson.
- **Forecast, stated now: no-go** unless the miner finds at least 5 recurring, firing-eligible classes with an
  approved lesson. The Phase 1b report puts the next step to Hans.

---

## 8. Slices, order, flags, tests

### 8.1 Slices

| Slice | Content | Depends on | Owns (single writer) |
|---|---|---|---|
| **S0** | Done on 2026-10-23: P1, P2, P3, P5, P7, P8, P12, P13, P15, P16 and P17 probed; P6 settled from the docs (§2). Open: P15's interrupt case and non-claude-code wires. | — | probe notes, private root |
| **S1a** | Ledger, keys, the UPS turn wrapper, deliver-first capture with `onAbandon`, SessionStart/UPS/guard capture calls, **the prompt classifier in record-only mode** (envelope rules, template rule, Python mirror, case file, parity tests), wire capability field (`hostInlineCapChars`, `preToolUseContext`, `transcriptShape`), receipt fields, flags file reader, latency baseline. S1a-D: daemon `read` ids. | S0 (P12, P15) | new `use-ledger.ts`, `flags.ts`, `harness-envelope*.ts`; `harness_envelopes.py`; case file; `hook-delivery.ts` (additive); `hook-platform.ts` capability field; capture calls and wrapper in `hook-handlers.ts`; `vault.ts` receipt; `recall.py` `read` |
| **S1b** | Detector, exposures, reports, `use-report`, explicit-recall rebuild, retention | S1a | new `use-detector.ts`; Stop wiring; `server.ts` status/report blocks; `cli.ts` |
| **S1c** | Offline harness: private-root guard, corpus builder, labelling pass, replay runner (pure plus handler layers), noise-free reference run, ρ and K calibration | S1a (for its pure functions) | `scripts/recall_replay/`; `tests/test_recall_replay_*.py` |
| | **Deploy S1a+S1b. The before window opens (§4.7).** | | |
| **S2a** | Priority serializer, `fitEnvelope`, boot diet, Layer 1 boot form | S1a | `agent_envelope.ts`; `daemon_transport.ts` boot slice; SessionStart in `hook-handlers.ts`; `vault.ts` shelf boot form |
| **S2b** | Guard scoping, `claimArming`, context delivery, the authorized deny-shape test edits, adapter mapping test, matcher, SubagentStart brief, SubagentStop judging | S2a; S1b; S0 P17 for the brief's prompt lookup | `recall-guard.ts`; `recall-state.ts` storage and field list; PreToolUse, SubagentStart and SubagentStop arms and the envelope-gate bypass; `hooks.json`; `hook-platform.ts` claude-code capability values |
| **S3a** | Wire the classifier's actions: UPS `skip`/`wrapped`/`scheduled` behaviour, the harvest gate | S2b (shared UPS ownership) | `jev-harvest.ts` gate; the UPS action step |
| **S3b** | Durability predicate and mirror, durability-ranked `recent_learnings`, boot noise ordering | S3a; S1c sample | `durability.ts`; `durability.py`; `recall.py` `recent_learnings`; `consolidation.py`; boot noise step |
| **S3c** | Vault gate and `ups_gate` ranking, noise-last explicit recall, multi-hit pointer, handles and drill contract, named set, cross-turn suppression, guard already-surfaced/consult rule | S3b; S1c ρ | `vault.ts` `searchVaultNotes` options; `recall-state.ts` selection; `server.ts` drill and recall ordering; UPS pointer step |
| | **The switch: deploy S2a–S3c together, all on. The after window opens.** | | |
| **S5** | Before/after report, plus the final replay table for §5.4 and §6.5 | both windows | reports, private root |

**Rules.**
- One fresh worktree per slice, and one writer per file set.
- **One total merge order:** S1a → S1b → S1c → S2a → S2b → S3a → S3b → S3c. Each slice branches after the
  one before it merges. Several pairs share files: `hook-handlers.ts` (S2a, S2b), `vault.ts` (S2a, S3c) and
  `daemon_transport.ts` (S2a, S3b). S1c can be written alongside S1b, but merges after it.
- Each new slice worktree builds its `.venv` the way §10 records (project-local CPython 3.14).
- Each slice:
  - shows its RED tests red on its base and green on its head
  - passes `make check` and `make test`, with verbatim final lines
  - carries its replay row from S1c
  - gets a cross-model review
- Nothing pushes. Promotion is Hans's.
- **Plan-scar channel and provenance plumbing** (revision 4's S3-3b and S3b) are Phase 1b options, not
  Phase 1.

### 8.2 Kill switches

There is one switch per feature, and every one defaults to `on`.
- **Where they are read.** Each process reads `<vault>/.runtime/flags.json`
  (`{"<FLAG>": "on" | "off"}`), with the same-named env var as an override.
  - The read uses the same containment check as `readRecallState`: symlink escapes are refused.
  - Values are parsed strictly. Anything but `on` or `off` is ignored, with a one-line audit note.
  - **Ledger off costs about nothing (S1a-2).** Before any of that, dispatch makes one cheap check
    (`ledger-gate.ts`), and loads the capture modules only when it says the ledger may be on. The check reads
    the env var, then a non-blocking, no-follow read of `flags.json` (regular file, 64 KiB cap, strict
    value). It may answer "off" only where `flags.ts` would too; any doubt answers "maybe on", and then the
    full read above decides.
  - **The cost of that.** When the check says off, `flags.ts` is never loaded. A hook then audits no note
    about the other flags: a typo in any other flag goes unreported until the ledger is on again. PreToolUse
    goes further: it runs the check only for a deny it is about to return, so an allowed call never loads
    the capture either.
  - **A capture module that will not load** (a stripped or half-updated install) makes the hook run exactly
    as with the ledger off. It is audited once, throttled, as `<prefix>_use_capture_unavailable`, by code and
    never with the loader's message, which names a path.
  - **Stop (S1b).** Stop records nothing of its own. It reads the ledger for the receipt and, after delivery,
    runs the detector. Pinned by `use-telemetry-flag-off-cost`, `use-telemetry-stop-detector` and
    `use-telemetry-stop-receipt`.
    - **Measured wires only.** Phase 1 measures the wires whose transcript the detector parses: those whose
      `transcriptShape` capability (`hook-platform.ts`) is `claude-code`, which today is claude-code alone.
      Every other wire is unmeasured (§4.5), so on its Stop neither the detector nor the receipt's use counts
      run, and Stop behaves as with the ledger off below, whatever the flag.
    - **Ledger off** (env, or `flags.json`, as ledger-gate decides it), or an unmeasured wire: Stop loads no
      ledger module. None of `use-capture.js`, `use-ledger.js`, `flags.js`, `harness-envelope.js`,
      `harness-envelope-rules.js`, `use-detector.js` or `use-report.js` loads, and the receipt line is
      byte-identical to the line without use counts.
    - **Ledger on** (ledger-gate says it may be on), on a measured wire:
      1. Stop loads `use-detector.js` (lazily, once delivered, and only after `flags.js` confirms the ledger)
         and `use-report.js` (the receipt), with their `use-ledger.js` and `flags.js`.
      2. It never loads `use-capture.js`.
      3. `harness-envelope.js` and `harness-envelope-rules.js` load only through `use-detector.js`'s import
         (the detector's prompt classifier, for turns with no `turn` row); nothing else on the Stop path
         imports them.
      4. Stop writes no template counts: `.runtime/prompt-templates.json` stays absent when it was absent, and
         byte-identical with the same mtime when it was present.
      5. Stop's writes under `.runtime` are a closed list:
         - The session's ledger day file, `use-ledger/<session_key>/<yyyy-mm-dd>.jsonl`, append-only
           (`appendLedgerRows`). Every appended row is the detector's own: `judged`, `exposure`, or a
           `surfaced` row with `channel: "explicit_recall"`, which the detector rebuilds from the
           transcript's `minni_recall`/`minni_drill` results (§4.4). It is never `turn`, never `reset`, and
           never a `surfaced` row on another channel.
         - The detector's per-session state, `use-detector/sessions/<session_key>/windows.json`
           (`writeJsonAtomic`).
         - Its lock, `windows.lock` in the same directory (`acquireLock`), released before exit, so it is
           absent afterwards.
         - The per-transcript cursor, `use-detector/cursors/<tx_key>.json` (`writeCursor`).
         - Transient names only, gone by exit: `*.tmp` temps renamed into place, and the detector's
           stale-lock asides (`windows.lock.stale-*`).
         - `jev-harvest-state.json`: pre-existing Stop behaviour, independent of the ledger (the Jev harvest
           cursor, written whenever the payload carries a transcript path). Same presence and the same bytes
           with the ledger on and off.
         - Nothing else. In particular, nothing under `recall-session.d/` changes (`counters.json`,
           `hit-keys.json`, `<agent_key>.reset`), and neither does `df-snapshot.json`, which only UPS writes.
    - **Order.** The detector's commit is registered last, after every commit Stop registers to consume or
      confirm something (a parked correction's archive, a relay cursor). Deferred commits run in registration
      order, so those settle before the detector starts, never up to its 1.5 s cap later.
    - **Failures.** A detector that throws, will not load, is slow, or stops at its caps (1.5 s, 2 MB) changes
      no byte of the Stop output, which is already delivered, and never fails the hook. A failure (a module
      that will not load; a status of `invalid`, `refused`, `write_failed` or `error`; a throw) is audited
      once, throttled, as `<prefix>_use_detector_failed`, by code and never with a message. A carry at the
      caps is not a failure. An undelivered Stop runs no detector; the next Stop carries the transcript
      forward.
    - **A transcript that is not a regular file** (a FIFO, a directory) is passed to the detector as no
      transcript, after a `stat` that follows links as the detector's `realpath` does. The host waits for the
      hook to exit, so the detector's own open, which a FIFO would block, must never be reached. A
      stat-then-open window remains until the detector's open is itself non-blocking (S1b r4b); swapping the
      path for a FIFO inside that window needs an active local attacker.
- **The daemon flag** is read from the daemon's config or env, because the daemon runs under `minni up` or
  launchd.
- **Ledger-off stretches stay visible.** With `MINNI_USE_LEDGER=off`, no ledger rows are written, but the
  SessionStart audit row records the flags in effect. `use-report` lists those stretches from the audit
  log.
  - The fields (S1b): every `<prefix>_session_start` row carries `details.use_ledger`, `"on"` or `"off"`.
    With the ledger on it also carries `details.features`, the switches in effect, sorted. "On" means this
    boot opened a ledger capture. "Off" means ledger-gate or `flags.ts` said off, or the capture module would
    not load. With the ledger off no capture module loads to compute the field.
  - **Caveat: the audit throttle.** `<prefix>_session_start` is not exempt from the 5 s per-event audit
    throttle (`vault.ts` `shouldThrottleAudit`). Two boots of the same agent within 5 s keep only the first
    row. Each row stands for its own boot, so a dropped row loses one sample, and the stretch boundaries
    come from the rows that survive. Exempting it would need edits to pre-rev-5 throttle tests
    (`audit-rcm008.test.mjs` uses `hook_session_start` as its duplicate case), so it is left for Hans.

| Flag | Feature |
|---|---|
| `MINNI_USE_LEDGER` | Item 1 ledger and detector |
| `MINNI_BOOT_FIT` | Priority order, fit, boot diet, Layer 1 boot form |
| `MINNI_GUARD_V2` | Guard scoping, claim-once, context delivery, already-surfaced/consult rule |
| `MINNI_SUBAGENT_BRIEF` | SubagentStart brief (SubagentStop judging follows `MINNI_USE_LEDGER`) |
| `MINNI_PROMPT_CLASSES` | UPS skip/wrapped/scheduled behaviour and the harvest gate |
| `MINNI_RECENT_LEARNINGS_RANK` | Durability-ranked boot learnings (daemon) |
| `MINNI_UPS_RANK_V2` | Vault gate, `ups_gate` ranking, multi-hit pointer, cross-turn suppression |
| `MINNI_RECALL_NOISE_LAST` | Noise-last ordering in explicit `minni_recall` |

`MINNI_RECALL_GUARD_DELIVERY=deny` and `MINNI_RECALL_GUARD_MODE` keep their meanings. Every ledger row
records the flags in effect (`features`), so a spot comparison is attributable.

### 8.3 Tests

- **New RED test files:** 16 (Item 1) + 17 (Item 2, including 4 parity files) + 14 (Item 4) = **47**.
  Revision 4 had 86 across four items.
- **Existing tests that must stay green unedited:**
  - `vault.test.mjs:1880`, `:1906` (receipt line)
  - `recall-guard.test.mjs`, apart from the authorized deny-shape lines below, including `:132`, `:209` and
    `:325`
  - `recall-guard-dropped-inject(-timeout).test.mjs`
  - `recall-pointer.test.mjs:79-140`, `:135-137`, `:301-312`
  - `sessionstart-shelf-drift.test.mjs:196-230`
  - `identity-body-delivery.test.mjs:157-165`
  - `correction-reinjection.test.mjs`
  - `gemini-hook.test.mjs:190`
  - `r8-hook-parity.test.mjs`
  - `slice-x5-recall-prescan.test.mjs`
  - `inbox-lifecycle.test.mjs`
  - `tests/test_issue_437_learnings_dedup.py`
  - the plan digest tests
- **Edits that approval authorizes**, each recorded in its slice, and nothing else:
  - `sessionstart-layer1-shelf.test.mjs:96-130`: the inline shelf size pin, under Q2(a) (§6.2.4)
  - the claude-code deny-shape assertions that context delivery replaces (§6.3.3). Hans's approval condition
    (2026-10-23):
    - each replaced deny assertion gets a positive assertion of the new behaviour: context delivered, no
      `permissionDecision`, guard text and wikilink present
    - each replacement is shown RED on the old code
    - codex, kilocode and agy each keep a "still denies" test
    - the shelf pin is replaced by a pin on the 4,000-char boot form, not just removed

    The assertions:
    - `recall-guard.test.mjs:123`, `:125-128`, `:143`, `:233`
    - `recall-guard-dropped-inject.test.mjs:244-245`
    - `hook-behavior.test.mjs:984`, `:1007`

    Each one either pins `MINNI_RECALL_GUARD_DELIVERY=deny` in its fixture or asserts the context shape plus
    `consumed === true`. The rest of `recall-guard.test.mjs`, including `:132`, `:209` and `:325`, stays
    unedited.

    S2b first runs the full plugin suite with the claude-code capability flipped, to confirm this list is
    complete. Any extra red assertion stops the slice for Hans.
- **Gates:** `make check` and `make test`, with verbatim final lines, per slice. New light pytest files are
  added to `CHECK_PYTEST`. That tightens the gate; it does not loosen it.

---

## 9. Adversarial notes

- **Before/after is confounded by the work itself.** Different days bring different tasks. The report shows
  per-window mixes (prompt kinds, subagent share, channels) next to the rates. Kill switches allow a spot
  check of one feature.
- **Goodhart.** A rate rises when surfacing falls. Success needs `used_ii` per 100 human turns not to fall.
  The after pointer prints handles, so handle citations are kept outside `used_ii`.
- **Context delivery may be weaker than a deny.** The probe saw a subagent decline a context instruction as
  tool output. The guard channel's uptake is reported per window. If it collapses, Hans can set
  `MINNI_RECALL_GUARD_DELIVERY=deny` for a spot comparison.
- **Detector false positives.**
  - Common title words: the distinctive-token rule and the prompt-echo filter.
  - Recital of injected text: injected shingles count only inside tool inputs.
  - Narration of a rejection: dismissal is sticky.
- **Identity collisions.** Daemon doc ids are per index, and wikilinks are vault-agnostic. Hence mkeys and
  handles carry `src`, and vault keys match on `source`.
- **Privacy.**
  - The ledger holds keys, sanitized titles and hashes. No excerpt is stored by default.
  - It is never committed and never sent to the daemon.
  - Probe logs hold payload keys only.
- **Concurrency.**
  - Per-session files; `O_APPEND` rows; a `wx` claim per arming; an `O_EXCL` detector lock.
  - Temp-name renames for every rewritten JSON file.
  - Session ids are hashed before use in paths.
- **Hook budgets.**
  - No vault-wide read on SessionStart, Stop or SubagentStart.
  - The DF snapshot is written by UPS from its own scan.
  - Maintenance is post-delivery and cooperative, and it is stated as such.
- **Injection.** Every injected title, handle and brief goes through `sanitizeRecallField` and the EVIDENCE
  framing.
- **Wires.**
  - Only claude-code is measured, and only claude-code gets context delivery.
  - codex, kilocode and agy keep deny, now session-scoped and claimed once per arming.
  - grok-build, cursor and the generic wire never arm the guard (`hook-handlers.ts:1575`), today or after.
  - Every non-claude-code wire is reported `unmeasured`.

---

## 10. Open questions for Hans

None blocks approval. The Q1–Q3 decisions recorded at the top are inputs to this revision. Approving it
confirms them.

**Python 3.14 is resolved project-locally** (Hans: "upgrade uv, project-only", 2026-10-23).
- No upgrade was needed. Homebrew already has uv 0.13.0, and the 0.5.9 copy earlier on `PATH` was shadowing
  it.
- CPython 3.14.8 lives in the worktree's gitignored `_private/uv-python/` (`UV_PYTHON_INSTALL_DIR`), with
  `.venv` built on it.
- `make setup` passes on 3.14.8, including `scikit-learn==1.9.0`.
- Homebrew Python and system Pythons are untouched.
- One side effect is left for Hans: uv also created a `python3.14` shim in the user bin directory. The Jev
  guard blocked removing it, so it is still there.

Every slice worktree needs the same steps, so they belong in a short note in the slice brief.

Two follow-ups this turned up, both outside this design and not done here:
- **The Makefile's `>= 3.14` check admits 3.15**, against `.python-version` = 3.14. A system `python3` of
  3.15 builds a venv on which `scikit-learn==1.9.0` cannot install. Pinning the check to 3.14 tightens it.
- **`scripts/diagnose_legacy_aliases.py:54` opens its DB without `uri=True`.** It works only where SQLite
  enables URI filenames by default (Homebrew's 3.54.1 does; python-build-standalone's 3.53.1 does not). So
  `tests/test_legacy_alias_diagnostic.py::test_offline_cli_uses_explicit_read_only_database` fails on the
  uv-managed 3.14. It is a one-line fix in its own change.

---

## 11. NOT RUN

- **P15's interrupt case**, which needs an interactive session, and every non-claude-code wire probe.
- **The full plugin suite with the claude-code `preToolUseContext` capability flipped.** The §8.3 deny-shape
  list comes from reading the fixtures, not from a run. S2b runs it first.
- **The committed harness.** The corpus builder, labelling pass and replay runner do not exist yet. The §1
  numbers come from one-off private scripts, and S1c recomputes them.
- **Gates on the unmodified base `eaf294fd`** (Python 3.14.8, §10). Nothing here is a change from this
  design. They establish the baseline that every slice's RED and GREEN runs start from.
  - `make check`: `CHECK_EXIT=0`. Plugin `ℹ pass 1523` · `ℹ fail 0`, including the real-daemon E2E test.
    Engine check set `62 passed`.
  - `make test`, engine half: `1 failed, 3712 passed, 7 skipped`. The failure is the pre-existing
    `uri=True` defect in §10.
  - `make test` stops after a failing engine half, so the plugin half was run separately with
    `make test-plugin`: `ℹ tests 1523` · `ℹ pass 1523` · `ℹ fail 0`, real-daemon E2E `✔`.
- **Any calibration of ρ, K, window sizes or the content test.** S1c fixes them from data.
- **Revision 5 has one scoped review** (Appendix B), not another hardening loop.

---

## Appendix A — What revision 5 cut from revision 4, and where its 20 open findings went

**Cut:**
- The randomized holdout and everything that served it:
  - `ab-arm.ts`, `ab start|stop` and `.runtime/ab-config.json`
  - `arm` on rows
  - placebo and boot-placebo rows, and the `capture` row with its placebo pool
  - the power script, the baseline fortnight, the D5 checkpoint, the G-RI pause gate
  - the S6 retirement slice, and the control branches with their tests
- The tool-activity counter and `toolcount` rows. They existed to test whether bounces prevent errors
  between arms. On claude-code there are no bounces left to test.
- Item 3's slices (S4, S4b–S4d), the miner, project keys and repeat watches. All moved to Phase 1b (§7).
- Revision 4's S3-3b plan-scar channel and S3b provenance plumbing (Phase 1b options).
- The maintenance runner's signature-index step and lazy cross-session finalization in SessionStart.

**Revision 4's open findings:**

| # | Finding (short) | Disposition in revision 5 |
|---|---|---|
| 1 | The tool-error endpoint is unpowered | Removed with the holdout. Bounces are gone on claude-code (§6.3.3). |
| 2 | Suppression biases T1/T2 | Fixed: exposure-level judging, `(session, agent, reset epoch, mkey)`, in both windows (§4.4). The first draft's "carry-over credit" was under-defined (review F5). |
| 3 | The template rule's 16-entry window flips classes | Fixed: an occurrence ring of `{ts, pid}` plus a separate `first_seen` and per-session first timestamps; prior means `ts < at` and a different `pid`, so UPS and harvest agree (§5.2.1, review F6) |
| 4 | The `capture` row exceeds 4 KB | Removed: no `capture` row |
| 5 | Full vault reads on SessionStart/Stop; cooperative cap | Fixed: boot noise from result fields; DF snapshot written by UPS; no vault-wide read in hooks; cap stated as cooperative (§6.2.3, §6.2.5) |
| 6, 15 | Flags are env vars that don't reach hooks | Fixed: a flags file with env override (§8.2) |
| 7, 8, 9 | Arm pinning, placebo eligibility, net-T1 estimator | Removed with the holdout |
| 10, 20 | Need-moment target has no mechanism, no in-context condition | Restated: 2 of 3 addressable moments, with an in-context visibility definition; recurrences deferred (§5.4) |
| 11 | Guard race design mis-cited a test and missed `:325` | Fixed: `markRecallConsumed` untouched and still called; a separate `wx` claim per arming id, with a `task_signature + ts` fallback; `:325` on the keep-green list. Context delivery does need the listed deny-shape edits, which approval authorizes (§6.3.3, §8.3, review F1). |
| 12 | doc_id and wikilink are not unique | Fixed: `src` in `doc:` mkeys and `d:` handles; vault keys match on `source`; drill sends `{doc_id, src, source}` from a per-session handle registry (§4.2, review F3) |
| 13 | Noise-last assumed the vault prescan merges | Fixed: daemon reorder only; prescan only on daemon down or empty; X5 kept (§5.2.5) |
| 14 | Explicit-recall text carries no ids | Fixed: keyed on source and wikilink; unkeyable cards counted `unnameable` (§4.4) |
| 16 | Item 3 misses exit-masked test output | Carried into Phase 1b as its blocker (§7) |
| 17 | Fixes ship dark | Fixed: ship on, with a before/after (§0) |
| 18 | The live criterion was powered for an unjustified 2× | Removed: a descriptive before/after with intervals, no power claim (§4.7) |
| 19 | Scope and reviewability | This revision |

## Appendix B — Scoped review of revision 5 (same-lab review)

The author is Claude Opus 5.5. The reviewer was Claude Fable 5.1, a different model but the same lab, as a
read-only adversarial reviewer. It was briefed to check only what revision 5 changed against revision 4,
with every finding verified in the code at `eaf294fd`. The author re-checked F1, F3 and F4 in the code
before applying them.

Verdict before fixes: "not fit as written; fit once F1–F3 are fixed and F4–F7 addressed."

| # | Severity | Finding | Change |
|---|---|---|---|
| F1 | blocker | Context delivery turns red claude-code deny-shape assertions that the doc promised to keep green; the consume call and the claim key were unspecified | §6.3.3 lists the assertions and the two ways to edit each; §3 and §8.3 make them approval-authorized; surfacing still calls `markRecallConsumed`; claim key falls back to `task_signature + ts`; S2b first confirms the red list by a run |
| F2 | blocker | The before window has no classifier, so "per 100 human prompt turns" shifts denominator at the switch | The classifier ships in S1a, record-only (§4.3, §5.2.1, §8.1) |
| F3 | should-fix | `d:` handles cannot be resolved by `{doc_id, source}`; the daemon keys on `src` | A per-session handle registry; drill sends `{doc_id, src, source}`; the test asserts what the fake daemon receives (§4.2, §5.3) |
| F4 | should-fix | Cursor and grok cannot "still deny"; their guard is disarmed | §6.3.3, §6.4 and §9 corrected. The reviewer's "only agy injects at UPS" was itself incomplete: codex and kilocode inject too and keep deny (author's check of `hook-platform.ts`, after approval). |
| F5 | should-fix | Carry-over credit collides with final `judged` rows and leaves the per-surfacing rate biased | Replaced by exposure-level judging (§4.4, §4.7, test renamed) |
| F6 | should-fix | The template rule cannot count "before `at`" from its stored fields; UPS and harvest disagree at the 3rd occurrence | An occurrence ring of `{ts, pid}`, with harvest excluding its own `promptId` (checked: every transcript user entry carries `promptId`) |
| F7 | should-fix | Unordered slices write the same files; SubagentStart's output seam was unowned | One total merge order; S2b emits the subagent arms' output directly and owns their wiring (§8.1, §6.3.6) |
| F8 | should-fix | The trim order demotes the active plan before the shelf | Shelf → boot form moves before `identity_body` and `active_thread`; new RED case (§6.2.2, §6.4) |
| F9 | should-fix | §10/§11 said the Python gates could not run | Gates run on 3.14.8, with the lines in §11 |
| F10 | should-fix | The ≤ 5% chatter target had no mechanism for stored chatter | S1c measures the predicate on stored chatter first, with a stated fallback target (§5.4) |
| F11 | nit | The envelope's `recall_state` path after scoping | Keeps the mirror path (§6.3.1) |
| F12 | nit | The r8 test pins the gate's literal clause | The bypass keeps it (§6.3.6) |
| F13 | nit | Flags-file containment and parsing; ledger-off stretches invisible; P6 answerable from the docs | Containment and strict parsing; ledger-off listed from the audit log; P6 settled (§8.2, §2) |
| F14 | nit | The guard channel is near-silent after the switch | Stated in §4.7 |

What the reviewer tried to refute and could not:
- the §1 mechanism citations
- that the `wx` claim is exclusive, and fails open
- that no existing UPS test feeds envelope prompts
- that the adapters fall through to allow
- the receipt, shelf-drift and identity-body pins
- the RED count of 47

Not re-reviewed: this revision's own fixes. That is one scoped round, as decided.
