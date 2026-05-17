# Sovereign Memory Page Types — Example Pages

**Contract version:** 1.0.0
**Last updated:** 2026-04-26

This document provides one rendered example page per page type. Each example
includes complete YAML frontmatter and a short body demonstrating the expected
structure. Use these as templates when writing vault pages.

Frontmatter schema reference:
```yaml
---
title: "Human-readable title"
type: <entity|concept|decision|procedure|session|artifact|handoff|synthesis>
status: <draft|candidate|accepted|superseded|rejected|expired>
privacy: <safe|local-only|private|blocked>
agent: <agent_id>
created: 2026-04-26T11:42:00Z
updated: 2026-04-26T11:42:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/sessions/20260326-relevant-session]]"
tags: [tag1, tag2]
trace_id: t8f2a1b3
---
```

---

## 1. Entity

**Path:** `wiki/entities/sovereign-memory-daemon.md`

```markdown
---
title: "Sovereign Memory Daemon (sovrd)"
type: entity
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T09:00:00Z
updated: 2026-04-26T11:42:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/sessions/20260326-sovrd-architecture-review]]"
  - "raw/20260325-initial-design-notes.md"
tags: [daemon, ipc, json-rpc, sovereign-memory]
trace_id: t1a2b3c4
---

# Sovereign Memory Daemon (sovrd)

The Sovereign Memory Daemon (`sovrd`) is a long-running Python process that
exposes the Sovereign Memory engine over a Unix domain socket using JSON-RPC 2.0.
It manages FAISS + SQLite hybrid retrieval, deduplicates embedding model loads
across requests, and maintains per-agent recall context.

## Key facts

- **Socket:** `/tmp/sovrd.sock` (configurable via `--socket`)
- **Protocol:** JSON-RPC 2.0, line-delimited (newline-separated messages)
- **Engine:** FAISS + FTS5 dual retrieval, cross-encoder re-ranking, RRF fusion
- **Source:** `engine/sovrd.py`

## Related

- [[wiki/concepts/hybrid-retrieval]] — RRF fusion algorithm
- [[wiki/decisions/use-unix-socket-for-ipc]] — why Unix socket over HTTP
```

---

## 2. Concept

**Path:** `wiki/concepts/hybrid-retrieval.md`

```markdown
---
title: "Hybrid Retrieval (FTS5 + FAISS + RRF)"
type: concept
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T09:15:00Z
updated: 2026-04-26T09:15:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/sessions/20260320-retrieval-benchmarking]]"
  - "raw/20260318-rrf-research-notes.md"
tags: [retrieval, faiss, fts5, rrf, cross-encoder]
trace_id: t2b3c4d5
---

# Hybrid Retrieval (FTS5 + FAISS + RRF)

Sovereign Memory uses a three-stage retrieval pipeline:

1. **FTS5 keyword search** — SQLite's full-text search with BM25 ranking. Fast,
   no model required. Returns top-K document-level matches.

2. **FAISS semantic search** — Approximate nearest-neighbor search over
   float32[384] sentence embeddings. Returns top-K chunk-level matches.

3. **Reciprocal Rank Fusion (RRF)** — Merges both result lists using
   `RRF(d) = Σ 1 / (k + rank_i(d))` with configurable `k` and per-source
   weights. Produces a single merged ranking.

4. **Cross-encoder re-rank** — A cross-encoder scores `(query, passage)` pairs
   directly for the top-K merged candidates. Much more accurate than bi-encoder
   similarity; used as a precision refinement pass.

## Why this matters

No single retrieval method dominates. FTS5 excels at exact-match and rare-term
queries. FAISS handles paraphrase and semantic similarity. RRF + cross-encoder
combine them reliably without a large tuning cost.
```

---

## 3. Decision

**Path:** `wiki/decisions/use-unix-socket-for-ipc.md`

```markdown
---
title: "Use Unix Domain Socket for Daemon IPC"
type: decision
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T10:00:00Z
updated: 2026-04-26T10:00:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/sessions/20260322-daemon-design-session]]"
tags: [daemon, ipc, architecture, decision]
trace_id: t3c4d5e6
---

# Decision: Use Unix Domain Socket for Daemon IPC

## Decision

The Sovereign Memory daemon communicates with clients via a Unix domain socket
(`/tmp/sovrd.sock`) using JSON-RPC 2.0 over a line-delimited protocol.

## Rationale

- **Latency:** Unix sockets avoid TCP stack overhead; round-trip is sub-ms on
  localhost vs. ~1ms+ for TCP loopback.
- **Security:** Unix socket permissions (`chmod 0o600`) restrict access to the
  owner; no network exposure.
- **Simplicity:** No port allocation, no firewall rules, no service discovery.
- **Cross-language:** JSON-RPC is trivially implementable in any language.

## Alternatives considered

- **HTTP/REST:** Adds latency and port management complexity.
- **gRPC:** Adds protobuf dependency; overkill for local IPC.
- **Shared memory:** Too complex for structured data; no cross-language support.

## Consequences

Daemon is not accessible over the network by default. An optional HTTP fallback
(`--port` flag) is provided for environments without Unix socket support (e.g.,
Windows WSL2 edge cases).
```

---

## 4. Procedure

**Path:** `wiki/procedures/index-new-vault.md`

```markdown
---
title: "Index a New Vault into Sovereign Memory"
type: procedure
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T10:30:00Z
updated: 2026-04-26T10:30:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/sessions/20260323-onboarding-new-agent]]"
  - "[[wiki/entities/sovereign-memory-daemon]]"
tags: [procedure, indexing, vault, onboarding]
trace_id: t4d5e6f7
---

# Procedure: Index a New Vault into Sovereign Memory

Use this procedure when adding a new agent vault or a new document collection
to the shared recall pool.

## Prerequisites

- Daemon is running: `python engine/sovrd.py`
- Vault directory exists and contains markdown files

## Steps

1. **Ensure vault structure** is correct:
   ```bash
   python -c "
   import sys; sys.path.insert(0, 'engine')
   from db import SovereignDB
   "
   ```

2. **Run the indexer** against the vault:
   ```bash
   python engine/index_all.py --vault-path ~/.sovereign-memory/claudecode-vault \
       --agent claude-code
   ```

3. **Rebuild the FAISS index** to include new vectors:
   ```bash
   python -c "
   import sys; sys.path.insert(0, 'engine')
   from faiss_index import FAISSIndex
   from config import DEFAULT_CONFIG
   idx = FAISSIndex(DEFAULT_CONFIG)
   idx.rebuild_from_db()
   "
   ```

4. **Verify** results appear in search:
   ```bash
   python engine/sovrd_client.py search "test query from new vault"
   ```

## Rollback

If indexing fails partway through, run `index_all.py` again — it is idempotent
(documents are upserted by content hash).
```

---

## 5. Session

**Path:** `wiki/sessions/20260426-auth-spike.md`

```markdown
---
title: "Auth Migration Spike — 2026-04-26"
type: session
status: candidate
privacy: safe
agent: claude-code
created: 2026-04-26T11:42:00Z
updated: 2026-04-26T11:42:00Z
expires: null
superseded_by: null
sources:
  - "raw/20260426-auth-spike-transcript.md"
tags: [auth, migration, jwt, session-note]
trace_id: t5e6f7a8
---

# Auth Migration Spike — 2026-04-26

## Summary

Investigated replacing session cookies with JWTs for the API layer. Concluded
that JWTs are appropriate for machine-to-machine agent calls but that human
sessions should retain HttpOnly cookies for XSS protection.

## Key learnings

- JWT signing key must be rotated every 90 days; add rotation to the ops runbook.
- The current session store (Redis) can be retained for human sessions; no
  migration needed there.
- Agent-to-agent calls should use short-lived JWTs (TTL: 15 minutes) signed with
  the agent's identity key.

## Open questions

- [ ] Where to store agent identity keys? (see [[wiki/decisions/agent-key-storage]])
- [ ] Do we need refresh tokens for long-running agent tasks?

## Follow-up

- Create [[wiki/decisions/jwt-for-agent-auth]] with the final decision.
- Update [[wiki/procedures/rotate-signing-key]] once rotation procedure is set.
```

---

## 6. Artifact

**Path:** `wiki/artifacts/sovrd-openapi-schema.md`

```markdown
---
title: "Sovrd JSON-RPC Schema (OpenAPI-style reference)"
type: artifact
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T12:00:00Z
updated: 2026-04-26T12:00:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/entities/sovereign-memory-daemon]]"
  - "engine/sovrd.py"
tags: [artifact, schema, json-rpc, api-reference]
trace_id: t6f7a8b9
---

# Sovrd JSON-RPC Schema

This artifact captures the wire format for the three most common sovrd calls,
for use by clients implementing the protocol without a client library.

## search

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "search",
  "params": {
    "query": "websocket architecture",
    "agent_id": "claude-code",
    "limit": 5,
    "depth": "snippet"
  }
}
```

**Response (success):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "query": "websocket architecture",
    "agent_id": "claude-code",
    "count": 3,
    "results": [
      { "text": "...", "source": "design.md", "heading": "## WebSocket Layer", "score": 0.92 }
    ]
  }
}
```

## learn

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "learn",
  "params": {
    "content": "JWT TTL for agent calls should be 15 minutes.",
    "agent_id": "claude-code",
    "category": "architecture"
  }
}
```
```

---

## 7. Handoff

**Path:** `wiki/handoffs/20260426-claude-to-codex-auth-context.md`

```markdown
---
title: "Handoff: Claude Code → Codex — Auth Migration Context"
type: handoff
status: accepted
privacy: local-only
agent: claude-code
created: 2026-04-26T14:00:00Z
updated: 2026-04-26T14:00:00Z
expires: 2026-05-03T14:00:00Z
superseded_by: null
sources:
  - "[[wiki/sessions/20260426-auth-spike]]"
  - "[[wiki/decisions/jwt-for-agent-auth]]"
tags: [handoff, auth, codex, context-transfer]
trace_id: t7a8b9c0
---

# Handoff: Claude Code → Codex — Auth Migration Context

**From:** claude-code
**To:** codex
**Task:** Continue auth migration implementation

## Context summary

I completed the auth migration spike (see [[wiki/sessions/20260426-auth-spike]]).
The decision is to use JWTs for agent-to-agent calls and retain cookies for
human sessions.

## Pending work

- [ ] Implement JWT signing in `engine/auth.py` (not yet created)
- [ ] Add `agent_id` + JWT validation to `sovrd.py` request handler
- [ ] Write the signing key rotation procedure

## Key facts to remember

- Agent JWTs: TTL 15 minutes, signed with agent identity key
- Human sessions: HttpOnly cookies, Redis store unchanged
- Signing key rotation: 90-day cycle, add to ops runbook

## Open questions

- Where to store agent identity keys? Proposal: `~/.sovereign-memory/keys/`
- Refresh tokens needed for long-running tasks? Leaning no; re-auth instead.

## Files touched in my session

- `engine/sovrd.py` — reviewed; no changes yet
- `docs/plans/SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md` — referenced
```

---

## 8. Synthesis

**Path:** `wiki/syntheses/sovereign-memory-architecture-overview.md`

```markdown
---
title: "Sovereign Memory Architecture Overview"
type: synthesis
status: accepted
privacy: safe
agent: claude-code
created: 2026-04-26T15:00:00Z
updated: 2026-04-26T15:00:00Z
expires: null
superseded_by: null
sources:
  - "[[wiki/entities/sovereign-memory-daemon]]"
  - "[[wiki/concepts/hybrid-retrieval]]"
  - "[[wiki/decisions/use-unix-socket-for-ipc]]"
  - "[[wiki/sessions/20260322-daemon-design-session]]"
tags: [synthesis, architecture, overview, sovereign-memory]
trace_id: t8b9c0d1
---

# Sovereign Memory Architecture Overview

This synthesis compiles the key architectural decisions and components of the
Sovereign Memory system as of 2026-04-26.

## System layers

```
Agent (Claude Code, Codex, Hermes, OpenClaw)
    ↓  JSON-RPC over Unix socket
Sovereign Memory Daemon (sovrd)
    ↓  Python API
Engine (retrieval.py, faiss_index.py, writeback.py, episodic.py)
    ↓  SQL + binary
SQLite DB  +  FAISS index  +  Vault (filesystem)
```

## Recall pipeline

1. Agent sends `search(query)` via JSON-RPC.
2. Daemon calls `RetrievalEngine.retrieve()`.
3. Parallel: FTS5 keyword search + FAISS semantic search.
4. RRF fusion merges both result streams.
5. Cross-encoder re-ranks top-K candidates.
6. Context budgeting trims results to token budget.
7. Result envelope returned to agent.

## Write pipeline

1. Agent calls `learn(content)` or writes vault page via plugin.
2. Daemon stores learning in `learnings` table (+ optional flat-file dual-write).
3. Vault writes trigger indexer → chunker → embedder → SQLite + FAISS update.
4. `index.md` and `log.md` are appended; agent can immediately recall new content.

## Design principles

- **SQLite is runtime truth.** FAISS, vault files, and flat-file memory are
  derived projections. If they disagree, SQLite wins.
- **Memory is evidence, not instruction.** Recalled content is always framed
  as a citation, never as a new directive.
- **Graceful degradation.** Every subsystem has a defined fallback; the agent
  always receives *something*, never a crash.

## Related documents

- [[wiki/entities/sovereign-memory-daemon]] — daemon entity page
- [[wiki/concepts/hybrid-retrieval]] — retrieval algorithm detail
- [[wiki/decisions/use-unix-socket-for-ipc]] — IPC transport decision
```
