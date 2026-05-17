# Sovereign Memory — Privacy and Policy Contract

**Contract version:** 1.0.0
**Last updated:** 2026-04-26

This document defines the default privacy posture, redaction rules, retention
rules, and cross-agent data sharing rules for Sovereign Memory. All agents,
operators, and integrations are bound by this contract.

---

## 1. Default Privacy Posture

**Vaults are local-only by default.** No vault content is transmitted to any
external service, model API, or remote system unless explicitly configured by
the operator.

Specific defaults:

| Surface | Default |
|---------|---------|
| Vault storage | Local filesystem only (`~/.sovereign-memory/<agent>-vault/`) |
| Database | Local SQLite only (`sovereign_memory.db`) |
| FAISS index | Local disk only (`~/.sovereign-memory/*.faiss`) |
| Daemon socket | Unix domain socket, local only (`/tmp/sovrd.sock`) |
| Cross-agent recall | Enabled for `privacy_level=safe` pages within the same installation |
| Handoff packets | Opt-in per handoff; never automatic |

### Cross-agent recall sharing

Cross-agent recall is enabled by design: agents share a common recall pool
indexed from all agents' vaults. Every result carries `agent_origin` in its
provenance envelope so the consuming agent knows the source.

Only documents tagged `privacy_level=safe` participate in the shared recall
pool. Documents tagged `local-only`, `private`, or `blocked` are filtered at
retrieval time:

| privacy_level | Cross-agent recall | Handoff packets |
|---------------|-------------------|-----------------|
| `safe` | Yes | Yes |
| `local-only` | Yes (same workspace only) | No |
| `private` | No (author agent only) | No |
| `blocked` | Never | Never |

See `docs/contracts/VAULT.md` Section 7 for the full privacy level definitions.

---

## 2. Redaction Rules

The following categories of content MUST be redacted from any envelope that
crosses a process boundary (including daemon JSON-RPC responses, handoff packets,
and any future network transport).

### 2.1 Secret patterns

Any chunk text matching one or more of the following patterns is redacted to
`[REDACTED]` before leaving the engine process:

| Pattern | Examples |
|---------|---------|
| `api_key` | `api_key=...`, `"api_key": "..."` |
| `token` | Bearer tokens, access tokens, refresh tokens |
| `password` | Plaintext passwords in any format |
| `private key` / `private_key` | PEM-encoded private keys, SSH private keys |
| `secret` | Generic secret values |
| `credential` | Credential objects or values |

These patterns are matched case-insensitively. The redaction is applied by the
daemon before serialising results to JSON-RPC callers.

### 2.2 Local filesystem paths

Absolute paths that identify local filesystem layout (home directory, username,
internal project paths) are redacted from envelopes sent to external consumers.
Within the local installation, full paths are preserved for indexing and recall.

### 2.3 Adapter and launchd filenames

Internal infrastructure filenames — daemon socket paths, launchd plist names,
adapter configuration files — are redacted from cross-process envelopes to
avoid fingerprinting the local installation.

### 2.4 `blocked` privacy level

Any document with `privacy_level=blocked` is excluded from all recall results
unconditionally. This exclusion is applied in `engine/retrieval.py` before any
result is returned, and is not overridable by any flag or config.

### 2.5 Enforcement

Redaction is the responsibility of the daemon (`engine/sovrd.py`). Agents that
receive recalled content MUST NOT strip or bypass redaction markers. An agent
that receives `[REDACTED]` in a result MUST treat the redacted field as absent.

---

## 3. Retention Rules

### 3.1 Episodic events

Episodic events written via `log_event` have a **7-day TTL** by default.
After 7 days, the decay pass (`engine/decay.py`) marks them as `expired` and
they are excluded from future recall unless `include_drafts=True` is set.

Episodic events are never hard-deleted from the database; they remain as
`expired` rows for audit purposes. An operator may purge them explicitly.

### 3.2 Raw session notes

Raw files in `raw/` are **immutable and append-only**. They are never edited
in place. They may be marked `expired` in the index if their content is
superseded by a wiki synthesis, but the raw file itself is not deleted.

Retention of raw files is indefinite by default. Operators may configure a
maximum age for raw files, but this is not a default behavior.

### 3.3 Wiki pages (learnings)

Wiki pages written via `learn` or the vault plugin have **no TTL by default**.
They persist indefinitely unless:

- An `expires` field is set in the page frontmatter (e.g., `expires: 2026-12-31`).
  The daemon's nightly hygiene pass marks them `expired` after this date.
- An operator or agent explicitly supersedes or rejects the page.
- The operator runs a manual purge.

The `expires` field is optional. Omitting it means the page persists forever.

### 3.4 Score distribution table

The `score_distribution` table (used for confidence calibration in
`engine/scoring.py`) accumulates raw scores indefinitely. An operator may
truncate it if it grows excessively. Rows older than 90 days are not used in
calibration (the engine weights recent scores more heavily).

### 3.5 Summary

| Content type | Default TTL | Hard delete? |
|-------------|------------|-------------|
| Episodic events | 7 days (marked `expired`) | No |
| Raw session notes | Indefinite | No |
| Wiki pages (learnings) | Indefinite (or `expires` field) | No |
| Score distribution rows | Indefinite (operator can purge) | Operator only |

---

## 4. Cross-Agent Rules

### 4.1 Reading across agents

An agent may **read** from the shared recall pool regardless of which agent
indexed a document. The shared pool includes all documents with
`privacy_level=safe` or `local-only` (same workspace).

Results always carry `agent_origin` in the provenance envelope so the consuming
agent can evaluate the source authority of each result.

### 4.2 Writing — agent vault boundary

**An agent may NEVER write pages into another agent's vault directory.**

Each agent's vault is scoped to its own path (see `docs/contracts/VAULT.md`
Section 2). Writing a document into a different agent's vault directory
violates the vault boundary and is not permitted. The daemon plugin
(`plugins/sovereign-memory/src/vault.ts`) enforces the path constraint.

### 4.3 Writing — learnings and episodic events

An agent may only write learnings and episodic events under its own `agent_id`.
Impersonating another `agent_id` in a `learn` or `log_event` call is not
permitted. The daemon validates the `agent_id` against the authenticated
session on each call.

### 4.4 Handoff packets as the cross-agent write channel

The only sanctioned mechanism for an agent to pass durable data to another
agent is a **handoff packet** — a structured wiki page of type `handoff`
written to the originating agent's own vault and then explicitly addressed to
the receiving agent.

The receiving agent reads the handoff on startup (via `read()`) and may
incorporate its contents into its own vault via normal `learn` or vault-write
calls. The handoff packet itself is never modified by the receiving agent;
it remains as an immutable record in the sender's vault.

### 4.5 Privacy-level inheritance in cross-agent results

When an agent recalls a document authored by a peer, the document's
`privacy_level` is respected:

- `safe`: returned normally.
- `local-only`: returned if the requesting agent is in the same workspace.
- `private`: not returned; treated as if the document does not exist.
- `blocked`: not returned under any circumstances.

The consuming agent MUST NOT attempt to infer the content of redacted or
excluded documents from the absence of a result.
