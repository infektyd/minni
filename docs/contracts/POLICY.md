# Minni — Privacy and Policy Contract

**Contract version:** 1.0.1 (IMPLEMENTED — baseline; §2 redaction PARTIAL)
**Last updated:** 2026-08-02 (SEC-G7 / #237 — align §2 with engine coverage; G03 matrix tags retained)

This document defines the default privacy posture, redaction rules, retention
rules, and cross-agent data sharing rules for Minni. All agents,
operators, and integrations are bound by this contract.

---

## 1. Default Privacy Posture

**Vaults are local-only by default.** No vault content is transmitted to any
external service, model API, or remote system unless explicitly configured by
the operator.

Specific defaults:

| Surface | Default |
|---------|---------|
| Vault storage | Local filesystem only (`~/.minni/<agent>-vault/`) |
| Database | Local SQLite only (`~/.minni/minni.db`) |
| FAISS index | Local disk only (`~/.minni/minni_faiss.index`) |
| Daemon socket | Unix domain socket, local only (`~/.minni/run/minnid.sock`) |
| Cross-agent recall | Authorized shared documents; see the layered matrix below |
| Handoff packets | Opt-in per handoff; never automatic |

### Cross-agent document recall

The document read gate combines stamped principal capabilities, ownership,
document type, privacy, allowed vault roots, workspace metadata, and retrieval
lifecycle filters. A `safe` label alone is not permission to read every peer's
notes. The source of truth is `can_read_document` in `src/minni/principal.py`.

The following matrix applies **after** method capability, root containment,
workspace matching, and lifecycle checks:

| Document | Same owner | Ordinary foreign principal | Operator within permitted roots |
|---|---|---|---|
| `blocked`, any type | Denied | Denied | Denied |
| Attributed session, nonblocked | Allowed | Denied, including `safe` | Allowed |
| Attributed `private` / `local-only`, non-session | Allowed | Denied, even in the same workspace | Allowed |
| Nonprivate shared wiki / handoff / synthesis / decision | Allowed | Allowed when its type/attribution qualifies as shared | Allowed |
| Other attributed documents | Allowed | Default deny | Allowed |

Legacy `agent=unknown` non-session rows have a compatibility exception for
capable principals; unattributed sessions do not acquire a shared grant. This
exception is not a recommended attribution scheme. Absolute source paths are
checked against allowed roots; legacy relative paths and missing workspace
metadata have restricted owner compatibility behavior.

Workspace labels do not create universal per-project isolation. The gate accepts
matching workspace IDs or `*` on the document/request; missing legacy workspace
metadata is owner-readable across named workspaces. Cross-project recall remains
available through authorized scope. Foreign `local-only` content is still denied
to ordinary principals. Explicit cross-agent **learning** recall additionally
requires its capability; shared document visibility is a separate decision.

Handoffs have their own addressed-recipient, capability and lease contract;
this document matrix is not a grant to transfer arbitrary content.

### 1.1 Candidate staging privacy

When non-operator agents submit learnings via `learn` without eligible `auto_accept_own`, proposals are staged in
`candidate_packets` with `privacy=review`. This clamped level prevents unvetted
proposals from leaking into cross-agent recall or auto-promoting to durable
storage. The background AFM consolidation pass inspects and deduplicates
`privacy=review` candidate packets (`_EXAMINABLE_PRIVACY`), but durable promotion
into active memory requires explicit operator resolution (`resolve_candidate`)
or an explicit `auto_accept_own` principal configuration.

See `docs/contracts/VAULT.md` Section 7 for the full privacy level definitions.

---

## 2. Redaction Rules

The daemon applies **best-effort redaction** to envelopes that cross a process
boundary (JSON-RPC responses, handoff packets, and any future network
transport). This is an incomplete Medium control: it reduces accidental leakage
of keyword-labelled secrets (assignment + JSON-quoted forms), common bare
provider token prefixes, and common local absolute paths; it is **not** a
guarantee that every secret or absolute path is stripped. Residual gaps match
`docs/contracts/THREAT_MODEL.md` (unknown-prefix high-entropy blobs;
non-`/Users|/Volumes|/private|/home` path layouts).

### 2.1 Secret patterns (PARTIAL)

`src/minni/minnid_runtime/redaction.py` rewrites matched forms to
`keyword=[REDACTED]` (or `[REDACTED]` for PEM blocks) before the daemon
serialises results. Matching is case-insensitive and covers assignment forms
(`keyword` + `:`/`=` + unquoted value charset `[^\s,;<>"']+`), JSON-quoted
`"key": "…"` labels, bare known provider prefixes (length floors apply), and
PEM blocks. Residuals remain PARTIAL — see the known-miss column.

| Pattern class | Matched examples (implemented) | Known misses (not claimed) |
|---------------|--------------------------------|----------------------------|
| `api_key` / `password` / `secret` / `credential` / `private_key` | `api_key=sk-…`, `password: hunter2`, JSON `"api_key": "…"` | Other labels; quoted assignment `password="…"` (not unquoted charset, not JSON `:`) |
| `token` / `bearer` / `access_token` / `refresh_token` | `token=…`, `Bearer: …`, JSON `"token": "…"` | Quoted assignment `token="…"` (same miss as password row) |
| Bare provider prefixes | `sk-…` (≥16 trailing), `ghp_`/`gho_`/…, `github_pat_…`, `AKIA…`, `xox[baprs]-…` | Arbitrary high-entropy blobs with no known prefix |
| PEM private keys | `-----BEGIN … PRIVATE KEY-----` blocks | — |

Operators and agents MUST NOT treat redaction markers as proof that all secret
material was removed. Learn-gate credential blocking (plugin policy) is a
separate write-path control and does not expand this read-path redactor.

### 2.2 Local filesystem paths (PARTIAL)

Path redaction covers absolute layouts under `/Users`, `/Volumes`, `/private`,
and `/home` (Docker/Linux), rewritten to `[REDACTED_PATH]`. Other absolute
layouts (e.g. bare `/tmp/...`, Windows drive letters without a dedicated
pattern) are **not** rewritten by the current patterns. Within the local
installation, full paths are preserved for indexing and recall where redaction
does not match.

### 2.3 Adapter and launchd filenames (PARTIAL)

Path-shaped socket or file locations under the layouts in §2.2
(`/Users`, `/Volumes`, `/private`, `/home`) may be rewritten to
`[REDACTED_PATH]` by `redaction.py`. Bare infrastructure names — launchd plist
basenames (e.g. `com.minni.minnid.plist`), bare socket filenames
(e.g. `minnid.sock`), and path forms outside §2.2 (e.g. `path=/tmp/minni.db`)
— are **not** rewritten by the current redactor. This clause does not claim a
fingerprinting shield for bare adapter/plist/socket names.

### 2.4 `blocked` privacy level

Any document with `privacy_level=blocked` is excluded from all recall results
unconditionally. This exclusion is applied in `src/minni/retrieval.py` before any
result is returned, and is not overridable by any flag or config.

### 2.5 Enforcement

Redaction is the responsibility of the daemon (`src/minni/minnid.py`). Agents that
receive recalled content MUST NOT strip or bypass redaction markers. An agent
that receives `[REDACTED]` in a result MUST treat the redacted field as absent.

---

## 3. Retention Rules

### 3.1 Episodic events

Episodic events written via `log_event` have a **7-day TTL** by default.
After 7 days, the decay pass (`src/minni/decay.py`) marks them as `expired` and
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
`src/minni/scoring.py`) accumulates raw scores indefinitely. An operator may
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

Read access follows the layered document matrix in §1 and the separate learning
recall capability. Neither shared indexing nor a `safe` tag grants unrestricted
peer-session access. Results carry source/agent provenance for evaluation as
evidence, not authority.

### 4.2 Writing — agent vault boundary

**An agent may NEVER write pages into another agent's vault directory.**

Each agent's vault is scoped to its own path (see `docs/contracts/VAULT.md`
Section 2). Writing a document into a different agent's vault directory
violates the vault boundary and is not permitted. The daemon plugin
(`plugins/minni/src/vault.ts`) enforces the path constraint.

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

Peer results must pass §1 in full. `blocked` is always excluded; ordinary
principals cannot read attributed foreign private/local-only documents or sessions
(the legacy unknown-attribution exception is described in §1).
Operators remain constrained by roots, workspace metadata, and blocked privacy.

The consuming agent MUST NOT attempt to infer the content of redacted or
excluded documents from the absence of a result.
