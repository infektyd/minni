# Investigation: recall returns empty

- **Date:** 2026-07-25
- **Branch:** `hooks/platform-native-audit` @ `e71ea83`
- **Status:** mechanism confirmed; fix not yet designed
- **Scope:** why `pending_learnings` are full of raw audit lines, why the pending
  per-event throttle fix may make it worse, and which explanation to discard first

## Summary

Stop-time learn candidates are not extracted from anything. `handleStop` reads the
tail of the vault's own audit log and hands it to `prepareOutcome` as the `summary`,
and `prepareOutcome` concatenates `task` and `summary` verbatim into the single
learn candidate. There is no extraction step anywhere in the path. Every session
therefore deposits a candidate whose body is Minni's own bookkeeping — which is
exactly what the inbox shows:

```
64d8a594-...: ## [2026-07-25T06:52:40.544Z] hook_stop | stop 64d8a594-...
```

This is a **corpus-poisoning** defect, not a retrieval defect. It is separate from,
and upstream of, the empty-recall symptom: it degrades what goes *into* memory while
the empty recalls are about what comes *out*. They are being tracked together because
the poisoned candidates are the most visible artifact of the same Stop path.

## 1. Mechanism — the extractor drinks from the audit log

The chain, verified on this branch:

1. [`hook.ts:531`](../../plugins/minni/src/hook.ts) — `handleStop` reads the audit tail:
   ```ts
   const tail = await auditTail(CLAUDECODE_VAULT_PATH, 30);
   ```
2. [`hook.ts:534`](../../plugins/minni/src/hook.ts) — the last five audit entries become the `summary`:
   ```ts
   summary: tail.entries.slice(-5).join("\n").slice(0, 600) || "session ended",
   ```
3. [`task.ts:1125`](../../plugins/minni/src/task.ts) — `prepareOutcome` passes that through to `outcomeDraft`.
4. [`task.ts:1000-1005`](../../plugins/minni/src/task.ts) — `outcomeDraft` **is** the extractor:
   ```ts
   const learnCandidates = [
     `${input.task}: ${input.summary}`.replace(/\s+/g, " ").slice(0, 500),
   ];
   ```

There is no summarization, no salience filter, no distillation. `learnCandidates` is
a one-element array built by string concatenation. Whatever `auditTail` returns is
the learning.

The shared platform factory has the identical chain at
[`hook-handlers.ts:648`](../../plugins/minni/src/hook-handlers.ts) and
[`hook-handlers.ts:651`](../../plugins/minni/src/hook-handlers.ts), so this is not a
`hook.ts`-only path — every platform that routes through the factory inherits it.

`auditTail` ([`vault.ts:890-915`](../../plugins/minni/src/vault.ts)) reads today's
`logs/<date>.md` (falling back to `log.md`), splits on `^## `, and returns the last
`limit` entries. Each entry is a markdown record with a `## [timestamp] tool | summary`
header and a fenced JSON details block. That markdown is the candidate text.

### Why the candidates look recursive

`handleStop` writes its own audit record before returning
([`hook.ts:545-553`](../../plugins/minni/src/hook.ts)):

```
## [...] hook_stop | stop <session-uuid>
```

So the next Stop's `slice(-5)` window contains the previous Stop's record. The loop is
self-feeding, which is why the inbox entries nest `hook_stop | stop <uuid>` inside a
candidate that is itself keyed on a session uuid.

### The `task` half was recently half-fixed

Commit `a874f1f` fixed the `task` side of the concatenation: `handleStop` had been
reading `payload.last_user_message`, a field Claude Code never sends, so `lastTask`
always fell through to the session uuid. It now routes through
`claudeCodeWire.lastTaskText` ([`hook.ts:530`](../../plugins/minni/src/hook.ts)).

That fix does **not** address this defect. It replaces the uuid prefix with real
assistant text; the `summary` half is still the raw audit tail. Candidates written
after `a874f1f` will read `<real last message>: ## [ts] hook_stop | stop <uuid>...`
instead of `<uuid>: ## [ts] hook_stop | ...`. Better provenance, same poison.

## 2. Risk — the per-event throttle raises audit volume into the same window

`a874f1f` also rekeyed the audit throttle. Before, the dedupe window was keyed on the
agent alone; now it is keyed per `(agent, event)`
([`vault.ts:667-668`](../../plugins/minni/src/vault.ts)):

```ts
const throttleKey = `${agentId}__${entry.tool}`.replace(/[^A-Za-z0-9_-]/g, "_");
```

against the same fixed 5s window ([`vault.ts:679-682`](../../plugins/minni/src/vault.ts)).

That fix is correct on its own terms — a burst of *different* events was collapsing
into one record, which is how `agy`'s `PreInvocation` came to look like it had never
dispatched. But it interacts badly with §1:

- **More records per session.** Distinct events in a burst are no longer suppressed.
- **The extractor's window is fixed-count, not fixed-time.** `slice(-5)` always takes
  five entries. Denser records mean those five cover a shorter wall-clock span.
- **The added density is entirely hook bookkeeping.** The events the throttle
  un-collapses are `hook_session_start`, `hook_user_prompt_submit`,
  `hook_pretooluse_guard`, `hook_stop` — not substantive tool records. So the marginal
  entries pushed into the `slice(-5)` window are the least informative ones available.

Net expectation: after this lands, a larger fraction of each learn candidate is hook
chatter, and the odds that any substantive `minni_*` record survives inside the last
five entries drop. This is a prediction from reading the code, not a measurement —
it should be checked against the inbox before the branch merges.

**Recommendation:** do not block `a874f1f`; the throttle fix is right and the audit
gap it closes cost real debugging time. Instead, sever the §1 dependency — the
extractor should not be reading the audit log at all — so that audit volume and corpus
quality stop being coupled. Until that is done, treat rising audit volume as a corpus
regression risk and sample the inbox after the merge.

## 3. Hypothesis to kill first — workspace scoping

**The hypothesis:** agents were launched from `~/`, so `workspaceFromPayload` stamped
`workspace-operator` while the corpus lives under `workspace-minni`, and recall
found nothing because it was scoped to the wrong workspace.

**Verdict: this explains Grok, and does not explain claudecode. Discard it as the
primary cause before spending more time on it.**

Supporting the Grok half, the workspace stamp genuinely is unstable.
`workspaceFromPayload` ([`hook-utils.ts:61-73`](../../plugins/minni/src/hook-utils.ts))
falls back through `workspace_id` → `workspaceId` → `cwd` → `working_directory` → a
literal fallback, so a platform that supplies only `cwd` gets a raw filesystem path as
its workspace identity. The claudecode audit log contains all three shapes:

```
5  "workspace": "workspace-minni"
2  "workspace": "~/Projects/minni"
1  "workspace": "workspace-operator"
```

That is a real defect and worth its own fix. But it cannot be the explanation for the
claudecode empties, for a structural reason:

**The empty field is `vaultMatches`, and vault search is not workspace-scoped at all.**
`searchVaultNotes` ([`vault.ts:950-997`](../../plugins/minni/src/vault.ts)) takes
`(vaultPath, query, limit)`. It walks `<vault>/wiki/**.md`, scores each note by lexical
term overlap, drops notes whose frontmatter privacy is `blocked`, and filters
`score > 0`. There is no workspace parameter and no workspace filter anywhere in it.
Only the daemon leg carries workspace identity
([`task.ts:1105`](../../plugins/minni/src/task.ts) → `workspace_id` at
[`sovereign.ts:222`](../../plugins/minni/src/sovereign.ts)).

An empty `vaultMatches` therefore means **zero notes in the agent's own vault had any
scoring lexical overlap with the query**. No workspace stamp, correct or corrupt, can
produce that.

**Measured population** (over `~/.minni/claudecode-vault/log.md`, the audit-log-visible
subset — the true count may be higher since throttled recalls leave no record):

| | count |
|---|---|
| `minni_recall` records | 116 |
| with `"vaultMatches": []` | 40 |
| of those, `agentId: claude-code` | 38 |
| of those, `agentId: claude-desktop` | 2 |
| date span of empties | 2026-06-29 → 2026-07-19 |

Thirty-eight claude-code empties, in claude-code's own vault, through a code path with
no workspace filter. The workspace-scoping story is dead for this population.

What the empties actually point at is the scoring function
(`scoreVaultNote` + `queryTerms`, [`vault.ts`](../../plugins/minni/src/vault.ts)):
long multi-term natural-language queries against a lexical scorer with a hard
`score > 0` cutoff and no fallback. Queries like

> `water monolith Stage 6 MSL WaterSignalState transition parity fixture WaterColumnsGPUValidate`

return nothing rather than returning weak matches. That is the next thread to pull,
and it is unrelated to §1 and §2 except that a corpus polluted by §1 gives the scorer
less real signal to match against.

## Open questions

1. Does the Stop-time noise reach committed learnings, or is it confined to the inbox?
   The `recent_learnings` block in the session envelope shows audit-shaped entries at
   `conf=0.9`, which suggests it does — needs confirming.
2. Is the fix to stop feeding `auditTail` into `summary`, to filter audit-shaped text
   at the extractor, or both? Preference is the former: the audit log is the wrong
   source, and filtering its output treats the symptom.
3. Of the 40 empty-`vaultMatches` recalls, how many are scorer failures against a
   corpus that *did* contain a relevant note, versus genuinely absent coverage?
   Replaying those 40 queries against the current vault would answer it directly.

## Recommended sequencing

1. **Sever §1 first.** It is cheap, it is unambiguous, and every session that runs
   before it lands adds more poisoned candidates.
2. **Then re-measure §2** against the inbox rather than reasoning about it.
3. **Then attack the scorer** (`scoreVaultNote` / `queryTerms`), with the replay in
   open question 3 as the baseline.
4. **Separately, fix `workspaceFromPayload`'s raw-`cwd` fallback** — it is a real bug
   on the daemon leg, just not this one.
