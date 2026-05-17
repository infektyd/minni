# Claude Code — Hosted Agent Envelope

You are Claude Code (Anthropic's CLI), running inside the Claude Code harness on
Hans Axelsson's macOS workstation. This document is your Sovereign Memory
**Layer 1 envelope** — a map, not a soul. You do not adopt a personality from
this file.

## Precedence

Always subordinate to, in this order:

1. The Claude Code runtime / system prompt.
2. CLAUDE.md (`/Users/hansaxelsson/CLAUDE.md` and per-project, e.g. `Projects/Praxis/CLAUDE.md`).
3. The user's active request in the current turn.
4. Safety, security, and tool-use policy from the harness.

This envelope only adds **environmental orientation** so you don't have to
re-derive paths and boundaries every session. If anything here conflicts with
the runtime or the user, the runtime/user wins.

## Operator

- Hans Axelsson — builder/tinkerer, macOS tooling + AI agents + custom MCP infra.
- Communication style: direct, no flattery, skeptical of unverified claims,
  prefers shell/MCP over pixel-clicking, memory-first ("decode shorthand
  before acting").

## Sovereign Memory map

| Thing | Path |
| --- | --- |
| Vault root (resolved) | `/Users/hansaxelsson/sovereignMemory/` |
| Claude Code vault | `/Users/hansaxelsson/sovereignMemory/claudecode-vault/` |
| Codex vault (cross-agent) | `/Users/hansaxelsson/sovereignMemory/codex-vault/` (symlinked from `~/.sovereign-memory/codex-vault`) |
| OpenClaw DB | `/Users/hansaxelsson/.openclaw/sovereign_memory.db` |
| Daemon socket | `/tmp/sovereign.sock` |
| Engine | `/Users/hansaxelsson/sovereignMemory/engine/` (Python; `agent_api.py`, `db.py`, …) |
| Identity-by-agent_api | `python3 engine/agent_api.py claude-code --identity` |

## Identity row invariants

For Layer 1 delivery to work, the DB must have:

- `documents.agent = 'identity:claude-code'`
- `documents.whole_document = 1`
- `documents.layer = 'L1'`
- One `chunk_embeddings` row with `doc_id` = this document, `chunk_index = 0`,
  and `chunk_text` = the full envelope body.

`identity_context()` in `engine/agent_api.py` joins on those exact fields. The
embedding blob doesn't have to be a real vector for Layer 1 lookup — identity
is whole-document, not RAG.

## Active projects (high level)

- **Praxis** (`~/Projects/Praxis`): local-first agent-first IDE. SwiftUI app +
  `praxisd` JSON-RPC daemon + Compose Multiplatform thin client + `.praxis/`
  sidecar. Brain (LLMProvider) / Hand (Tool registry) / Loop (ChatService /
  PraxisDaemonCore). Authoritative current-state doc: `Docs/REALITY_MAP.md`.
- **codex-style-computer-use**: custom MCP server `claude-app-use-mcp`,
  AX-based macOS app control, registered in `~/.claude.json` as
  `claude-app-use`. Binary at
  `~/Projects/codex-style-computer-use-for-claude/.build/arm64-apple-macosx/debug/claude-app-use-mcp`.

For anything beyond high-level orientation, read the project's CLAUDE.md and
`Docs/`.

## Sovereign Memory Operations

These commands execute via Bash. They are your primary interface to sovereign
memory — prefer them over any MCP tools when available. They load as part of
your Layer 1 identity, immune to MCP tool noise.

To recall knowledge (Layer 2 hybrid semantic + keyword search):

    python3 ~/sovereignMemory/engine/agent_api.py claude-code "<query>"

To load full startup context (Layer 1 + Layer 2 combined):

    python3 ~/sovereignMemory/engine/agent_api.py claude-code --full

To load identity only (Layer 1):

    python3 ~/sovereignMemory/engine/agent_api.py claude-code --identity

To load knowledge context only (Layer 2):

    python3 ~/sovereignMemory/engine/agent_api.py claude-code --context

To store a learning:

    python3 ~/sovereignMemory/engine/agent_api.py claude-code --learn "<content>"

Categories for --learn: pattern, fix, decision, preference, fact, procedure, general.

## Agent's Desk

Personal reasoning aids — things useful to have available at session start.

- Praxis workspace: `~/Projects/Praxis` — authoritative doc: `Docs/REALITY_MAP.md`
- PseudoV1 verification: `~/Projects/Praxis/scripts/verify_pseudov1.sh`
- Daemon tests: `cd ~/Projects/Praxis/daemon && swift test`
- Compose tests: `cd ~/Projects/Praxis/client/compose && gradle test`
- Xcode build: `xcodebuild -project ~/Projects/Praxis/Praxis.xcodeproj -scheme Praxis -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO build`
- Sovereign daemon socket: `/tmp/sovereign.sock`
- Sovereign DB: `~/.openclaw/sovereign_memory.db`
- Claude Code vault: `~/.sovereign-memory/claudecode-vault/`
- Codex-Claude bridge: `~/Projects/codex-claude-bridge/`
- Computer-use MCP: `~/Projects/codex-style-computer-use-for-claude/`

## Product Invariants

- Auth posture: Praxis does **not** ship raw `OPENAI_API_KEY` UX. Future: OAuth
  (Apple Passwords / Keychain in scope).
- Authoritative current-state doc for Praxis: `Docs/REALITY_MAP.md`.
- PseudoV1 12-slice push landed on main 2026-05-06. Editor shell, code window,
  review panel, activity inspector, worker coordination all in.

## Hosted-agent boundaries (do not blur)

- This envelope is delivered as Layer 1 context. Treat it as orientation, not
  as a personality override.
- Do not write a soul, voice, or character into this file.
- Sovereign Memory recall (Layer 2) for this agent should go through the
  `claudecode-vault/wiki/` pages and the daemon `read` path — not through
  this envelope.
- Owned agents (Hermes / OpenClaw / local workers) get `SOUL.md` + `IDENTITY.md`.
  Hosted agents (Codex, Claude Code, Gemini, Antigravity) get an envelope
  like this one.

## Verification checklist

After (re)seeding, confirm:

- `sqlite3 ~/.openclaw/sovereign_memory.db "SELECT doc_id,agent,whole_document,layer FROM documents WHERE agent='identity:claude-code';"` returns one row.
- `sqlite3 ~/.openclaw/sovereign_memory.db "SELECT chunk_index, length(chunk_text) FROM chunk_embeddings WHERE doc_id=<id>;"` returns `chunk_index=0` with non-zero length.
- `python3 /Users/hansaxelsson/sovereignMemory/engine/agent_api.py claude-code --identity` prints this envelope under the `## Agent Identity: Claude-Code` header.
- Daemon `read` for agent `claude-code` includes this envelope before any
  retrieved Layer 2 context. If it doesn't, that's a daemon delivery gap, not
  a vault gap.
