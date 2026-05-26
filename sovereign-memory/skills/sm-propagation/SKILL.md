---
name: sm-propagation
description: Use when setting up, repairing, propagating, or verifying Minni for an agent, especially Layer 1 identity/envelope delivery, Codex/Claude/Gemini hosted-agent maps, Hermes/OpenClaw souls, per-agent vaults, plugin caches, closed MCP transports, or daemon/socket hydration drift.
---

# SM Propagation

## Overview

Propagate Minni for an agent by creating the right local memory
surface, seeding the right layer, and verifying delivery. Do not make the agent
reason from first principles about vaults, identity rows, socket paths, and
hosted-agent boundaries.

Core rule:

> Minni gives owned agents a soul. It gives hosted agents a map.

## When To Use

Use this when the user asks to:

- set up Minni for an agent
- propagate memory, pseudoenv, workspace envelope, identity, or Layer 1
- update or repair the Minni plugin/MCP install for a platform
- repair hydration, recall, daemon read, socket, per-agent vault, plugin cache, or MCP drift
- make Codex/Claude/Gemini/Grok beta/Grok Build/Antigravity use Minni more smoothly via their native session hook surfaces
- seed Hermes/OpenClaw/local-worker identity or soul files

Do not use this for ordinary recall-only context lookup. Use `sovereign-memory`
for that.

## Agent Classification

Classify before writing anything:

| Agent type | Examples | Layer 1 content |
| --- | --- | --- |
| Owned/Sovereign-authored | Hermes, OpenClaw variants, local workers, AFM-backed agents | `SOUL.md` / `IDENTITY.md` soul and identity |
| Hosted/runtime-authored | Codex, Claude Code, Gemini, Antigravity | hosted-agent envelope/map, never a replacement personality |

Hosted-agent envelopes must say they are subordinate to the host runtime,
system/developer instructions, safety policy, and active user request.

## Propagation Workflow

1. **Hydrate status**
   - Prefer Sovereign MCP tools.
   - If MCP transport is stale or fails on a non-canonical socket, use the
     installed plugin CLI with the canonical socket:
     `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock node ~/.codex/plugins/cache/sovereign-memory/sovereign-memory/0.1.0/dist/cli.js status`
   - Verify the daemon sees the active Minni DB before changing
     paths. A healthy baseline typically shows several hundred documents, hundreds
     of chunks, and over a thousand learnings with FAISS healthy.

2. **Resolve paths**
   - Codex vault default: `~/.sovereign-memory/codex-vault`.
   - Claude Code vault default: `~/.sovereign-memory/claudecode-vault`.
   - KiloCode vault default: `~/.sovereign-memory/kilocode-vault`.
   - Gemini vault default: `~/.sovereign-memory/gemini-vault`.
   - Grok beta vault default: `~/.sovereign-memory/grok-beta-vault` (legacy full package path, if still referenced).
   - Grok Build: `~/.sovereign-memory/grok-build-vault`; the Grok-specific session hook integration lives at `~/.grok/plugins/grok-sovereign-memory/` (sourced from this repo under `plugins/grok-sovereign-memory/`) using the canonical `~/.agents/bin/mcp-env-run` wrapper + grok-build identity (seeded via sm-propagation). 

     The integration wires four lifecycle events:
     - SessionStart: injects sovereign status + Layer 1 reminder
     - UserPromptSubmit: detects native `/flush`, `/compact`, and `/dream` commands, auto-drafts prepare_outcome candidates to the inbox, and prompts the agent to run `sovereign_prepare_outcome`
     - PreCompact / Stop: captures scar tissue and session outcomes for review

     See `plugins/grok-sovereign-memory/` and the Grok SKILL for exact hook behavior and injection text.
   - New/unknown agents default to `~/.sovereign-memory/<agent-id>-vault`.
   - The vault must be an actual directory owned by that agent surface, not a
     symlink and not a copy of another agent's vault. If an agent was pointed at
     Codex's vault, stop and create a clean vault for that agent instead.
   - Assume any agent, regardless of model quality or sophistication, may copy
     another agent's vault when under-specified. Always stamp
     `SOVEREIGN_AGENT_ID`, `SOVEREIGN_VAULT_PATH`, and
     `SOVEREIGN_SOCKET_PATH` explicitly for that platform.
   - Resolve symlinks before deciding what is canonical; symlinked vault roots
     are configuration drift unless the user explicitly approves them.
   - Confirm daemon socket: `~/.sovereign-memory/run/sovrd.sock`.
   - Confirm the active DB: `~/.sovereign-memory/sovereign_memory.db`.
     The source repo must not be treated as the vault or database root.
   - Do not assume large external directories (e.g. `~/.openclaw`) are available locally. Some users keep them in off-machine or cryo storage.

3. **Write recallable vault pages**
   - Use vault API/CLI, not raw manual edits, so `index.md` and `log.md` update.
   - New daemon learning is proposal-first by default. Durable writes require
     an explicit user request and, when bypassing candidate staging, an operator
     principal such as `main` with `force=true`.
   - Typical pages:
     - decision: hosted-agent vs owned-agent layering
     - concept: workspace envelope
     - procedure: agent hydration
     - artifact: pseudoenv
   - Never duplicate another agent's `index.md`, `log.md`, `logs/`, `inbox/`,
     or `wiki/` wholesale. Bootstrap only the empty vault structure and schema,
     then let the agent build its own notes.

4. **Seed Layer 1 whole-document delivery**
   - Owned agents: source from `SOUL.md` and `IDENTITY.md`.
   - Hosted agents: source from an envelope file such as
     `CODEX_HOSTED_AGENT_ENVELOPE.md`.
   - DB invariant: `documents.agent = identity:<agent_id>`,
     `whole_document = 1`, and one full chunk at `chunk_index = 0`.
   - Prefer existing seed tools. If missing, update the DB carefully and
     idempotently.

5. **Verify delivery**
   - `python engine/agent_api.py <agent_id> --identity` must show Layer 1.
   - Daemon `read` should include Layer 1 before prior context. If it does not,
     report a daemon delivery gap; do not pretend Layer 1 is fully live.
   - Vault search/prepare should find the recallable pages.
   - Check index/log propagation and `git status`.

## Platform Plugin Update Workflow

Use this before asking an agent on another platform to rely on Minni
after a code, path, socket, vault, or hook change. The update must refresh both
functionality and configuration:

1. Build from the canonical repo (example): `~/Projects/sovereignMemory`.
2. Copy the current plugin package to the platform's installed plugin/cache
   location.
3. Stamp the platform MCP config with explicit env:
   - `SOVEREIGN_AGENT_ID`
   - `SOVEREIGN_VAULT_PATH`
   - `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock`
   - `SOVEREIGN_WORKSPACE_ID=~/Projects/sovereignMemory` (example)
4. Bootstrap only an empty actual vault tree for that agent if missing.
5. Never copy `wiki/`, `logs/`, `inbox/`, `index.md`, or `log.md` from another
   agent.
6. Verify the platform's config points to the refreshed `dist/server.js`.

Known platform update commands:

```bash
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform codex
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform claude-code
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform kilocode
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform gemini
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform grok-beta
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform grok-build   # Grok Build session hook integration (supports /flush /compact /dream via UserPromptSubmit + scar drafting on PreCompact/Stop)
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform all
```

For a new platform, make the agent id explicit:

```bash
python3 ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform generic --agent <agent-id> --install-root <plugin-root>
```

If the host has its own config format, update the host config after the generic
copy so it launches `<plugin-root>/dist/server.js` with the same explicit env
above. Do not let an unspecified host fall back to Codex defaults.

## Repair Checklist

Use this order:

| Symptom | Repair |
| --- | --- |
| MCP tool says `Transport closed` | Use installed plugin CLI or direct MCP subprocess; restart plugin/session later |
| MCP tool says socket `ENOENT` | Tool is on a stale socket; set or patch `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock` |
| Parse error `Expected HTTP/` over socket | Stale plugin cache is speaking HTTP to JSON-RPC daemon; rebuild/resync cache and restart plugin server |
| Vault appears empty | Resolve `~/.sovereign-memory/<agent>-vault` symlink and compare repo-local vault |
| Agent is using Codex's vault | Repoint to `~/.sovereign-memory/<agent-id>-vault`; do not copy Codex's logs/wiki/inbox |
| Vault root is a symlink | Replace with an actual directory unless the user explicitly asked for a symlink |
| Vault pages exist but are not recalled | Check they live under `wiki/`, have searchable terms, and were written through vault API |
| `agent_api.py --identity` empty | Missing `identity:<agent_id>` whole-document DB rows |
| `agent_api.py --identity` works but daemon `read` omits Layer 1 | Daemon read delivery gap; patch/restart daemon, do not solve with more vault pages |
| Hosted agent envelope includes personality | Strip personality. Keep map, boundaries, precedence, and verification only |
| Index/log missing entries | Rewrite through vault API or repair audit/index explicitly |

## Helper Script

Use `scripts/propagate.py` for local inspection and Codex hosted-envelope
propagation. Run `--help` first.

Common commands:

```bash
python ~/.codex/skills/sm-propagation/scripts/propagate.py status --agent codex
python ~/.codex/skills/sm-propagation/scripts/propagate.py update-plugin --platform codex
python ~/.codex/skills/sm-propagation/scripts/propagate.py seed-hosted --agent codex --workspace ~/Projects/sovereignMemory
python ~/.codex/skills/sm-propagation/scripts/propagate.py verify --agent codex --workspace ~/Projects/sovereignMemory
```

The script may create/update local identity source files and DB identity rows.
It should not write public git files unless explicitly pointed at a tracked
workspace artifact.

## Common Mistakes

- Confusing `wiki/*` recall pages with Layer 1 whole-document delivery.
- Copying Codex's or Claude's vault to bootstrap another agent. Create a fresh
  actual directory instead; agents share the daemon, not a vault.
- Assuming a stronger or newer model will infer the right vault. It will not;
  every platform must be explicitly stamped with its own agent id and vault.
- Giving hosted agents a soul/personality override.
- Trusting a stale MCP transport after cache or daemon changes.
- Treating a missing local `~/.openclaw` as data loss; check cryo storage first.
- Writing vault pages by hand and bypassing audit/index propagation.
- Calling Layer 1 fixed before testing both `agent_api.py --identity` and daemon
  `read`.
