# Observed Usage Notes

These notes summarize observed usage patterns from long-running agent work. They
are not benchmark results and should not be treated as proof. They are the
practical signals that motivated the current Sovereign Memory abstraction.

## Core Verdict So Far

Sovereign Memory has shown promising situational rehydration and operational
continuity, but it has not yet proven artifact-grounded comprehension.

The most important lesson is that the system must keep a hard boundary between:

- **verified state**: checked against current files, logs, commands, or user-provided sources.
- **remembered state**: useful prior memory that may be stale or incomplete.
- **failed validation**: remembered claims that were checked and found missing, contradicted, or outdated.

That distinction matters more than raw recall volume.

## What Has Been Useful

### 1. High-specificity recovery

Memory was useful when it recovered precise operational details that would have
been easy to lose in a long chat history. Examples include exact paths, missing
artifact names, prior script names, and specific next-step context.

The useful part was not merely “the agent remembered a topic.” The useful part
was that memory helped choose the next verification action faster.

### 2. Open-loop tracking

The system is most valuable when it remembers unfinished work rather than just
summaries of completed work.

Useful open-loop examples:

- missing files that still need to be created or checked.
- stale docs that need reconciling with current code.
- partial implementation plans that should not be treated as complete.
- prior claims that need revalidation before being repeated.

Open loops turn memory from a scrapbook into a work queue.

### 3. Verification discipline

Memory can make agents more dangerous if remembered claims are presented as
current truth. The observed safe pattern is:

```text
remember -> verify -> act -> record evidence
```

The unsafe pattern is:

```text
remember -> claim -> act
```

Sovereign Memory should optimize for the first pattern, even when it feels slower.

### 4. Role separation across agents

Long-running work benefits from simple agent role boundaries:

| Role | Responsibility |
| --- | --- |
| Hermes | Hydrate memory, prepare evidence, route handoffs. |
| Recon | Inspect the current repo, logs, docs, and environment. |
| Forge | Implement changes. |
| Pulse | Run fast validation and catch missing-file or stale-doc problems. |
| Hermes again | Record verified outcomes and open loops. |

The important constraint: Pulse can report fast validation signals, but it should
not claim final success unless the evidence supports it.

### 5. “Identity loads whole, knowledge loads chunked”

Observed usage supports a layered loading model:

1. Agent identity and operating rules should be small enough to load whole.
2. Standing principles should be pinned or compact.
3. Current project state should be a concise resume packet.
4. Evidence should be retrieved on demand.
5. Large knowledge should load chunked with citation and staleness checks.

This avoids turning memory into either a giant prompt blob or an unstructured
vector search swamp.

## Evidence Record Shape

The memory layer should store evidence in a shape close to this:

```json
{
  "claim": "The repo has a missing README section for evaluation direction.",
  "claim_type": "project_state",
  "source": "repo inspection / tool output / user-provided source",
  "observed_at": "2026-05-16T00:00:00Z",
  "verified_status": "remembered_unverified | verified | failed_validation",
  "confidence": 0.0,
  "stale_policy": "verify_before_claiming",
  "next_verification": "Open README.md and check for the section."
}
```

The strict status labels matter. A missing artifact should not disappear into a
vague note. It should become a first-class `failed_validation` record with a next
verification action.

## Bakeoff Direction

The useful test is not whether memory feels helpful. The useful test is whether
it changes behavior after restart.

Recommended variants:

| Variant | Description |
| --- | --- |
| A | No memory, only the new task prompt. |
| B | Session search or raw conversation summary. |
| C | Sovereign Memory only. |
| D | Sovereign Memory plus session search and relevant skills. |

Recommended scoring:

- first action quality.
- recall specificity.
- verification discipline.
- false claim rate.
- artifact validation.
- open-loop handling.
- token efficiency.

## Instrumentation Worth Keeping

Token attribution should be tracked separately:

- identity tokens.
- retrieved memory tokens.
- session search tokens.
- skill or tool instruction tokens.
- tool output tokens.

Retrieval feedback should also be explicit:

- `useful_current`
- `useful_stale`
- `irrelevant`
- `misleading`
- `false`

Those labels make it possible to measure memory precision instead of trusting a
vibe.

## Known Gaps

Sovereign Memory still needs to prove:

1. It improves cold-start restart quality over a disciplined wiki-only workflow.
2. It lowers false claims rather than merely producing more confident summaries.
3. It can keep evidence fresh when repositories, docs, and local environments drift.
4. It can remain simple enough that users will actually correct remembered state.
5. It can produce meaningful metrics without turning every agent action into paperwork.

## Practical Design Rule

Memory should make the next correct action easier.

If a memory feature does not improve restart quality, verification discipline,
open-loop handling, or evidence coverage, it should be removed or pushed into a
simpler note-taking layer.
