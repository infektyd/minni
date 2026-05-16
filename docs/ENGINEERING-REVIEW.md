# Engineering Review Note

This note exists for reviewers who want the shortest honest version of the
Sovereign Memory abstraction.

## One-Sentence Claim

Sovereign Memory tests whether persistent agent memory should be an explicit,
inspectable state layer rather than an accidental mixture of chat history, RAG,
and ad-hoc markdown notes.

## Core Abstraction

> **Identity loads whole. Knowledge loads chunked.**

The project separates memory into layers with different loading and validation
rules:

| Layer | Loading rule | Validation posture |
| --- | --- | --- |
| Identity | Whole | Must be small, explicit, and stable. |
| Standing principles | Whole or pinned | Must be reviewed before becoming durable. |
| Current project state | Compact packet | Must be refreshed against current artifacts. |
| Evidence | Retrieved by need | Must carry source, timestamp, and validation status. |
| Knowledge | Chunked retrieval | Must be cited and treated as possibly stale. |

This is the main bet. If these layers do not improve restart quality, the repo
should collapse toward a simpler wiki or filesystem model.

## What It Is Not

- Not a replacement for model context.
- Not a vector database wrapper with branding.
- Not a hidden autonomous learning system.
- Not an attempt to make memory feel human.
- Not proven yet.

## Why Existing Approaches Feel Insufficient

### Chat history

Chat history preserves detail, but it becomes hard to audit, expensive to carry,
and easy to misread after long sessions.

### Plain RAG

RAG retrieves relevant chunks, but it does not automatically represent open
loops, stale claims, contradiction state, or what has already been verified in
the current working session.

### Plain markdown/wiki memory

Wiki pages are readable and durable, but they need additional structure to answer
questions like:

- Which claims are verified now?
- Which claims are remembered but stale?
- Which open loops block the next action?
- Which artifact should be checked first?
- Which claims should the agent avoid repeating?

Sovereign Memory explores whether those structures belong in the memory system
itself rather than in every prompt.

## Observed Usage Signals

Practical use so far suggests Sovereign Memory is strongest when it does three
things:

1. Recovers high-specificity working state such as paths, missing artifacts,
   prior script names, and unfinished checks.
2. Converts memory into a verification path instead of a confident claim.
3. Tracks open loops as first-class state rather than burying them in summaries.

The current honest verdict is mixed: the system has demonstrated situational
rehydration and continuity, but it has not yet proven artifact-grounded
comprehension or superiority over a disciplined wiki-only workflow.

See [OBSERVED-USAGE.md](OBSERVED-USAGE.md) for the longer note.

## Desired Resume Packet

A useful resumed session should start with a packet like this:

```text
Verified now:
- Facts checked against current files, commands, logs, or user-provided sources.

Remembered but not yet verified:
- Prior state that may be useful but must not be treated as current truth.

Open loops:
- Unfinished tasks, unresolved decisions, and known blockers.

First verification action:
- The next concrete check before writing code or making claims.

Do-not-claim:
- Unsupported, contradicted, stale, or privacy-sensitive claims.
```

That packet is the product. Retrieval, vaults, vectors, and daemon machinery only
matter if they improve that packet.

## Evaluation Question

The right bakeoff is not “does retrieval return relevant text?” It is:

> After a cold restart, does the agent take the right next action with fewer
> unsupported claims and less context cost?

Recommended variants:

| Variant | Description |
| --- | --- |
| A | No memory, only the new task prompt. |
| B | Raw conversation summary. |
| C | Plain RAG over repo/docs. |
| D | Wiki-only filesystem memory. |
| E | Sovereign Memory typed rehydration packet. |

Recommended metrics:

- Correct next action.
- Unsupported claim count.
- Stale claim count.
- Evidence coverage.
- Time to useful action.
- Token cost.
- Contradiction handling.
- Whether the agent knows what not to do.

## Kill Criteria

Sovereign Memory should be simplified or abandoned if:

1. Wiki-only memory matches typed rehydration on restart quality.
2. The system cannot produce better evidence coverage than plain RAG.
3. Open-loop tracking adds ceremony without changing agent behavior.
4. The memory layer requires more maintenance than the work it saves.
5. Users cannot inspect or correct remembered state quickly.

## Why Keep Exploring

The abstraction remains worth testing because long-running agent work repeatedly
hits the same failure mode: the next session is not merely missing facts, it is
missing *working state*.

Working state includes what was verified, what was assumed, what failed, what is
pending, and what should not be repeated. Those are not just retrieval problems.
They are memory-governance problems.
