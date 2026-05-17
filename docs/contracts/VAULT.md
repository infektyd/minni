# Sovereign Memory Vault Operating Contract

**Contract version:** 1.0.0
**Last updated:** 2026-04-26

This document is the canonical operating contract for all Sovereign Memory vaults.
It governs how agents read, write, and maintain vault pages. All agents — Claude
Code, Codex, Hermes, OpenClaw, and any future peer — are bound by this contract.

---

## 1. Vault Layout

Every vault is a directory with the following canonical structure:

```
<vault-root>/
├── raw/                          # Immutable raw sources and session excerpts
├── wiki/
│   ├── entities/                 # People, projects, repos, services, machines
│   ├── concepts/                 # Reusable ideas, patterns, abstractions
│   ├── decisions/                # Decisions with rationale and sources
│   ├── procedures/               # How-to procedures and runbooks
│   ├── syntheses/                # Cross-source summaries and comparisons
│   ├── sessions/                 # Task/session learnings as durable notes
│   ├── artifacts/                # Generated artifacts (configs, schemas, specs)
│   └── handoffs/                 # Agent-to-agent handoff packets
├── schema/
│   └── AGENTS.md                 # Stub pointing at docs/contracts/VAULT.md
├── logs/
│   └── YYYY-MM-DD.md             # Daily audit logs (append-only)
├── inbox/                        # Incoming structured payloads (JSON)
├── index.md                      # Master index of all wiki pages
└── log.md                        # Append-only audit of all vault operations
```

### Key invariants

- `raw/` is **append-only and immutable**. Never edit a raw file in-place; write
  a new one instead.
- `wiki/` is **LLM-maintained synthesis**. Agents write, revise, and supersede
  wiki pages according to the status lifecycle (Section 4).
- `index.md` is appended on every vault page creation. It is never rewritten in
  full; only new lines are appended.
- `log.md` is appended on every create, edit, or supersession. It is never
  truncated.
- All durable writes go through the daemon JSON-RPC or the vault plugin API.
  Direct filesystem writes bypass audit logging and are strongly discouraged.

---

## 2. Per-Agent Vaults

Each agent has its own vault. The vault path is determined by environment
variable, with a documented default:

| Agent | Default vault path | Override env var |
|-------|--------------------|-----------------|
| Claude Code | `~/.sovereign-memory/claudecode-vault` | `SOVEREIGN_VAULT_PATH` |
| Codex | `~/.sovereign-memory/codex-vault` | `SOVEREIGN_VAULT_PATH` |
| Hermes | `~/.sovereign-memory/hermes-vault` | `SOVEREIGN_VAULT_PATH` |
| OpenClaw | `~/.sovereign-memory/openclaw-vault` | `SOVEREIGN_VAULT_PATH` |

Additional env vars:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SOVEREIGN_VAULT_PATH` | Override the vault root for the current agent. | See table above |
| `SOVEREIGN_DB_PATH` | Override the SQLite database path. | `~/.sovereign-memory/sovereign.db` |
| `SOVEREIGN_SOCKET` | Override the daemon Unix socket path. | `/tmp/sovrd.sock` |

### Cross-agent recall

The daemon maintains a shared recall pool indexed from **all** agents' vaults.
Results carry `agent_origin` in the provenance so the consuming agent knows
which agent authored each indexed document.

**An agent may never write pages into another agent's vault directory.**

---

## 3. Page Types

Every wiki page has a `type` field in its frontmatter. The eight canonical types:

### entity

A named, persistent thing: a person, project, repository, service, machine, or
named system. Entities accumulate facts over time; the page is updated in-place
(with `updated` timestamp bumped) or superseded when the entity changes
fundamentally.

### concept

A reusable idea, pattern, or abstraction relevant to the workspace. Concepts are
synthesized from multiple sources and updated as understanding evolves.

### decision

A recorded decision: what was decided, why, who decided, and what alternatives
were considered. Decisions move to `accepted` once ratified and to `superseded`
when reversed.

### procedure

A how-to procedure or runbook. Step-by-step instructions for repeatable tasks.
Procedures are updated in-place as the steps change; superseded when fundamentally
replaced.

### session

A set of durable learnings from a single task or session. Sessions capture what
happened, what was learned, and what should be remembered. They do not decay
quickly.

### artifact

A generated artifact that should be preserved for reference: a config file
snippet, a schema, a spec, a diagram source, or generated code. Artifacts are
usually immutable once written; create a new artifact rather than editing an
existing one.

### handoff

A structured context packet passed from one agent to another. Contains the
handing-off agent's current understanding, pending learnings, open questions,
and recommendations. The receiving agent reads the handoff on startup.

### synthesis

A cross-source summary or comparison. Syntheses compile multiple entities,
sessions, and decisions into a coherent view. They are the highest-level
compiled knowledge layer.

---

## 4. Status Lifecycle

Every wiki page has a `status` field that controls recall inclusion and
lifecycle transitions.

```
draft → candidate → accepted
                        ↓
                   superseded
                   rejected
                   expired
```

### Status definitions

| Status | Recall inclusion | Description |
|--------|-----------------|-------------|
| `draft` | Excluded (opt-in with `include_drafts=true`) | Page is being written. Not ready for recall. |
| `candidate` | Included; flagged | Page is complete but not yet endorsed by a peer or human. |
| `accepted` | Included | Page has been endorsed. Authoritative. |
| `superseded` | Excluded by default | Page has been replaced by a newer page (linked via `superseded_by`). |
| `rejected` | Excluded | Page has been flagged as incorrect, harmful, or retracted. |
| `expired` | Excluded | Page has passed its `expires` timestamp. |

### Transition rules

| From | To | Who | What happens |
|------|----|-----|-------------|
| (new) | `draft` | Any agent | Page is created without sources or pending review. |
| (new) | `candidate` | Any agent | Page is created with at least one source citation. |
| `draft` | `candidate` | Agent that wrote it | Sources added; page is ready for recall. |
| `candidate` | `accepted` | Peer agent or human | Via `endorse` RPC or manual edit [PLANNED: PR-13]. |
| `accepted` | `superseded` | Any agent | New page created with `superseded_by` pointing to the old page's wikilink. Index entry updated. |
| `accepted` | `rejected` | Operator or privileged agent | Flagged as incorrect; excluded from recall immediately. |
| Any | `expired` | Daemon (automatic) | `expires` timestamp passes; daemon marks it expired on next hygiene pass. |

### Index and log.md behavior on transition

- On **creation**: new entry appended to `index.md`; creation event appended to `log.md`.
- On **status change**: `log.md` entry appended; `index.md` entry updated to reflect new status (appended as a correction line, not rewritten).
- On **supersession**: `log.md` records both the old page being superseded and the new page being created.

---

## 5. Sourcing Rules

Every wiki page MUST cite its sources. Sources are listed in the `sources` array
in YAML frontmatter, as wikilinks or relative paths.

```yaml
sources:
  - "[[wiki/sessions/20260318-auth-spike]]"
  - "raw/20260317-discussion.md"
```

Rules:

1. **Pages without sources start at `draft`** regardless of any other field.
   The daemon enforces this on indexing (planned for PR-2).
2. **Raw sources are immutable.** Do not add a raw file as a source and then edit
   it; raw files are append-only.
3. **Source chains must not be circular.** A synthesis may cite sessions and
   decisions; a decision may cite sessions and raw notes; neither may cite the
   synthesis that cites them.
4. **Minimum one source per non-draft page.** A `candidate` or `accepted` page
   with an empty `sources` list is a contract violation and will be flagged by
   the hygiene report.

---

## 6. Hygiene Rules

### Index maintenance

- `index.md` is appended on page creation. One line per page:
  `- [[relative/path/to/page]] - Brief summary (≤160 chars)`
- The index is never truncated or rewritten in full by an agent.
- Superseded pages remain in the index but are annotated with `[superseded]`.

### log.md maintenance

- `log.md` receives one append entry per vault operation (create, edit,
  supersede). The format is:
  ```
  ## [2026-04-26T11:42:00Z] <tool> | <summary>

  ```json
  { "notePath": "...", "section": "...", "source": "..." }
  ```
  ```
- Daily `logs/YYYY-MM-DD.md` files mirror the same entries for the day.
- The daemon plugin (`vault.ts`) handles these appends automatically for all
  writes that go through it.

### Durable writes through the daemon

All durable vault writes MUST go through the daemon JSON-RPC or the vault plugin
API (`plugins/sovereign-memory/src/vault.ts`). Direct filesystem writes:

- Do not trigger index or log appends.
- Do not trigger re-indexing into SQLite/FAISS.
- Are not visible in the shared recall pool until the next manual `index_all.py`
  run.

---

## 7. Privacy Rules

### Per-page privacy_level

Set in frontmatter as the `privacy` field (mapped to `privacy_level` internally):

| Value | Who can recall it |
|-------|------------------|
| `safe` | All agents; included in handoff packets. |
| `local-only` | All agents in the same workspace; excluded from handoffs. |
| `private` | Only the agent that wrote it. |
| `blocked` | No agent; redacted from all recall results. |

Default for new wiki pages: `safe`.
Default for raw sources: `local-only`.

### What goes in raw/ vs wiki/

| Content type | Location |
|-------------|----------|
| Session transcripts, raw notes, full chat excerpts | `raw/` |
| Synthesized knowledge, decisions, procedures | `wiki/` |
| Structured session learnings | `wiki/sessions/` |
| Handoff packets | `wiki/handoffs/` |

### What NEVER goes in the vault

The following MUST NEVER be written to any vault location:

- Secrets, API keys, passwords, tokens, or credentials of any kind.
- Personally identifiable information (PII) beyond what is strictly necessary
  for the workspace context, and only with `privacy: private`.
- Full authentication cookies or session tokens.
- Private keys, certificates, or cryptographic material.

If such content is found in a vault, the operator must immediately:
1. Delete the file and remove it from git history.
2. Rotate the exposed credential.
3. File a hygiene report documenting the incident.

Agents that receive content containing secrets MUST NOT write it to the vault.
Log an episodic event noting the refusal.
