# ᛗ Minni

![Status](https://img.shields.io/badge/status-pre--v1_alpha-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square)
![Node](https://img.shields.io/badge/node-18+-339933?style=flat-square)
![Tests](https://img.shields.io/badge/tests-474_passing-brightgreen?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)

**Local-first memory and governance layer for AI agents.**

> *Identity loads whole. Knowledge loads chunked.*

Minni gives long-running agent work a durable spine — identity, working state,
retrieval, evidence, handoffs, learning proposals, and audit trails that stay
inspectable on your own machine. It sits between *chat-history-as-memory* and
*pure RAG*: an agent resumes with typed state, verified evidence, open loops,
and a clear next action instead of rediscovering context from scratch.

> **Pre-v1.** Core subsystems work and are tested, but integration depth varies.
> The [status table](#project-status) below is the honest state of each piece.

---

## Highlights

| | Feature | What it does |
|---|---|---|
| ♻️ | **Session rehydration** | Resume with verified facts, remembered-but-unverified state, open loops, and a first verification step |
| 🧩 | **Agent-agnostic MCP plugin** | One standard MCP server (`minni@minni`) — works with any MCP client. Ships manifests for Codex, Claude Code, Gemini, KiloCode |
| 🔒 | **Proposal-first learning** | No silent writes — `minni_learn` stages candidates; only operator-gated resolution writes durable memory |
| 🔍 | **Hybrid retrieval** | FTS5 + FAISS + reranking, query expansion, HyDE, token budgets, centralized read gates |
| 🍎 | **Native AFM support** | Apple Foundation Models via a local helper, with bridge fallback and opt-out |
| 📓 | **Obsidian vaults** | Human-readable wiki, logs, raw material, and per-agent inbox/outbox handoffs |
| 🤝 | **Cross-agent contracts** | Vault-backed ping contracts with explicit approve/deny — no agent reads another's private memory directly |
| 🛡️ | **Local-first governance** | Server-stamped identity, read policy, audit trails, and memory-hygiene contracts |

---

## Project status

Minni is in active development toward v1. Components sit at different maturity
levels; this table is the honest picture, not a roadmap.

| Component | Status | Notes |
|---|---|---|
| SQLite runtime + migrations | **stable** | WAL mode, additive migrations tracked by `PRAGMA user_version` |
| MCP plugin server | **beta** | 26 `minni_*` tools, agent-agnostic; any MCP client can connect |
| Hybrid retrieval (FTS5 + FAISS) | **beta** | FTS5 → FAISS → RRF → rerank works; needs comparative eval vs baselines |
| Proposal-first learning | **beta** | Stage → list → resolve; operator-gated writes enforced |
| Vault model + wiki indexer | **beta** | Structure stable; vault files → SQLite/FAISS pipeline works |
| Identity + read policy | **beta** | `EffectivePrincipal` stamps identity, vault roots, capabilities; one read gate |
| Handoff (inbox/outbox) | **beta** | Vault-backed handoff pages; ack/await flows work |
| Cross-agent ping contracts | **alpha** | Protocol works (request → inbox → decide → status); limited real-world use |
| AFM provider (native/bridge) | **alpha** | macOS-only; bridge is default; native needs the Foundation Models framework |
| Compile passes (AFM) | **alpha** | 5 passes; dry-run only by default |
| Team coordination | **alpha** | 3 tools registered; multi-agent scenarios largely untested |
| Per-agent vault isolation | **alpha** | Enforcement engine built + tested; hardening tracked separately |
| Qdrant / Lance backends | **stub** | Placeholders; FAISS is the only active vector backend |
| Comparative eval vs baselines | **not started** | Harness exists; no head-to-head against RAG / wiki-only yet |

*Stable* = relied upon, breaking changes need migration · *Beta* = works + tested, API may shift ·
*Alpha* = functional but early · *Stub* = interface only.

---

## How it works

Memory is layered state, not one flat blob:

| Layer | Loading rule | Purpose |
|---|---|---|
| **Identity** | load whole | who the agent is, role, constraints, standing rules |
| **Project state** | compact packet | active branch, status, blockers, recent decisions, next checks |
| **Evidence** | retrieve by need | source-backed facts, artifacts, logs, traces, citations |
| **Knowledge** | retrieve chunked | larger wiki/docs/history — cited and validated, never assumed |

A resumed session doesn't just return documents — it produces a small packet:

```
Verified now:            facts checked against current artifacts
Remembered (unverified): plausible memory needing confirmation
Open loops:              tasks left incomplete
First verification:      the next concrete check before acting
Do-not-claim:            stale, contradicted, or unsupported claims
```

The goal is the *smallest* packet that lets an agent resume safely. **SQLite is
runtime truth; vault pages, FAISS files, and compile drafts are derived surfaces.**
If a simpler model achieves the same recovery quality, the right move is to
delete complexity.

---

## Getting started

**Prerequisites:** Python 3.11+, Node.js 18+.

```bash
# 1. Engine (Python daemon)
cd engine
python3 -m pip install -r requirements.txt
python3 minnid.py --socket ~/.minni/run/minnid.sock

# 2. Plugin (TypeScript) — in another terminal
cd plugins/minni
npm install && npm test

# 3. Verify the daemon answers
cd engine
python3 minnid_client.py --socket ~/.minni/run/minnid.sock status
python3 minnid_client.py --socket ~/.minni/run/minnid.sock search "memory handoff"
```

> **Tip:** for reproducible NumPy/FAISS, use a clean venv:
> `python3 -m venv .venv && source .venv/bin/activate && pip install -r engine/requirements.txt`

---

## Architecture

```mermaid
flowchart TD
    Agents["Agents — Codex · Claude Code · Gemini · KiloCode"]
    Plugin["minni@minni plugin (MCP) — tools · hooks · console"]
    Daemon["minnid daemon (JSON-RPC over Unix socket)"]
    Core["Identity & read policy · Hybrid retrieval · Learning pipeline"]
    Storage[("SQLite + FAISS — runtime truth")]
    Vaults["Obsidian vaults — wiki · logs · raw · inbox/outbox"]

    Agents --> Plugin --> Daemon --> Core --> Storage
    Vaults --> Daemon
    Daemon --> Vaults
```

Each agent is its own pipeline into the plugin; the plugin alone talks to
`minnid`, which is the single gatekeeper to the vault. Agents never touch the
filesystem directly — they ask `minnid`, which applies the caller's identity and
read policy and returns only what that agent is allowed to see.

---

## Plugin surfaces

The plugin implements the [Model Context Protocol](https://modelcontextprotocol.io/),
so any MCP client can connect. Per-agent manifests are thin wrappers that
register the same server with a pinned identity and vault:

| Integration | Manifest |
|---|---|
| Any MCP client | [`.mcp.json`](plugins/minni/.mcp.json) |
| Claude Code | [`.claude-plugin/`](plugins/minni/.claude-plugin/) + hooks |
| Codex | [`.codex-plugin/`](plugins/minni/.codex-plugin/) |
| Gemini | [`.gemini-plugin/`](plugins/minni/.gemini-plugin/) |
| KiloCode | [`.kilocode-plugin/`](plugins/minni/.kilocode-plugin/) |

**Automatic behavior is recall-only.** Durable learning is proposal-first
(`minni_learn` stages → `minni_resolve_candidate` writes), and cross-agent
sharing requires an explicit vault-backed ping contract.

<details>
<summary><strong>All 26 tools</strong></summary>

`minni_status` · `minni_recall` · `minni_drill` · `minni_prepare_task` ·
`minni_prepare_outcome` · `minni_route` · `minni_export_pack` ·
`minni_learning_quality` · `minni_learn` · `minni_resolve_candidate` ·
`minni_vault_write` · `minni_audit_report` · `minni_audit_tail` ·
`minni_compile_vault` · `minni_negotiate_handoff` · `minni_ack_handoff` ·
`minni_list_pending_handoffs` · `minni_await_handoff` ·
`minni_ping_agent_request` · `minni_ping_agent_inbox` ·
`minni_ping_agent_decide` · `minni_ping_agent_status` ·
`minni_subscribe_contradictions` · `minni_team_runtime` ·
`minni_team_evidence` · `minni_team_promotion`

</details>

---

## AFM provider modes

Apple Foundation Models calls are optional and local-only. Set
`MINNI_AFM_PROVIDER_MODE`:

| Mode | Behavior |
|---|---|
| `off` | skip AFM, use deterministic fallback |
| `bridge` | localhost OpenAI-compatible bridge (**default**) |
| `native` | local helper via the Foundation Models framework |
| `auto` | native when available, else bridge |

Adapter configuration is reported as a boolean only — private adapter paths are
never emitted.

---

## Vault model

Each agent has its own Obsidian vault under `~/.minni/<agent>-vault`, sharing the
same daemon and database:

```
<agent>-vault/
  index.md   log.md   logs/   raw/
  wiki/   wiki/handoffs/   inbox/   outbox/   schema/
```

Use short, sourced wiki pages with frontmatter for durable knowledge. Raw
session material and private logs stay local and out of public git unless
explicitly sanitized.

---

## Local-first security

Minni is local-first only when these hold on the host:

1. The **macOS user account** is the security perimeter (single-user box).
2. **FileVault on** — database and vault encrypted at rest.
3. **No cloud sync** — `~/.minni/` (incl. `minni.db` + `-wal`/`-shm`) is not under
   iCloud / Dropbox / Drive / OneDrive.
4. **Local-only transport** — JSON-RPC over a Unix socket; no remote fallback at v1.

The daemon ships as a launchd agent (`com.minni.minnid`) with `Umask 077` so logs
stay `0600`.

---

## Repository map

| Path | Contents |
|---|---|
| [`engine/`](engine/) | Python daemon (`minnid.py`), retrieval, migrations, compile passes, eval harness |
| [`plugins/minni/`](plugins/minni/) | Agent-agnostic MCP plugin + per-agent manifests |
| [`openclaw-extension/`](openclaw-extension/) | OpenClaw bridge and import tooling |
| [`docs/`](docs/) | Contracts, canonical paths, troubleshooting, design specs |

**Key engine files:** `minnid.py` (JSON-RPC daemon) · `principal.py` (identity,
vault roots, read authorization) · `retrieval.py` (hybrid retrieval + read gate)
· `db.py` (schema + migrations) · `sovereign_memory.py` (indexing/stats CLI).

---

## Verification

Before cutting a release candidate:

```bash
cd engine && PYTHONPATH=. pytest -q                 # expect 337 passed, 2 skipped
cd ../plugins/minni && npm run build && npm test    # expect 137 passed
bash scripts/repro-smoke.sh                         # hermetic daemon: status + recall + isolation
```

---

<sub>Minni is local-first — no telemetry, no remote endpoints, no cloud required. It can run on synced or cloud storage, but only stays passively secure (encrypted at rest, no exfil surface) when kept local.</sub>
