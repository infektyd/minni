# Minni Canonical Paths

This folder is the canonical home for Minni on this machine:

`<repo-root>`

It is also the canonical Git working tree for
`https://github.com/infektyd/minni`. The live layout is intentional:
runtime integrations use `src/minni/`, `openclaw-extension/`, and
`plugins/minni/` directly.

Use this page to avoid guessing between older root-level, OpenClaw, Hermes, and
downloaded paths.

Older notes may still spell this path using retired home-root or temp-main
names. Treat those as legacy references, not active roots.

## Active Core

- `src/minni/` - Minni Python package.
- `openclaw-extension/` - OpenClaw extension bridge.
- `plugins/minni/` - `minni-multi-plugin` multi-host plugin package (Codex, Claude Code, Gemini, KiloCode surfaces sharing one MCP server and daemon).
- `session-extracts/` - extracted handoff/session notes.

## Active Runtime

- `~/.minni/minni.db` - active Minni database.
- `~/.minni/faiss/` - active FAISS cache.
- `~/.minni/run/minnid.sock` - active daemon socket.
- `~/.minni/codex-vault` - Codex-owned Obsidian vault.
- `~/.minni/claudecode-vault` - Claude Code-owned Obsidian vault.
- `~/.minni/kilocode-vault` - KiloCode-owned Obsidian vault.

Agent vault roots must be actual directories. Do not point a new agent at
Codex's vault and do not bootstrap a new agent by copying another agent's
`wiki/`, `logs/`, `inbox/`, `index.md`, or `log.md`.

## Organized Supporting Material

- `docs/plans/` - planning documents and prompt plans.
- `docs/research/` - related research notes.
- `logs/openclaw/` - preserved OpenClaw audit logs.
- `archives/downloads/` - old downloaded bundles and one-off prototypes.
- `_archive/` - previous repo/workspace archives.

## Retired Legacy Roots

Legacy home-root copies, repo-local vault copies, `.openclaw` state, and old
archives belong in private offsite/cryo storage, outside the public repository.

Do not recreate compatibility symlinks for those paths unless the user
explicitly asks for one.

## Still External On Purpose

Do not casually move these folders wholesale. They are live application state
for other systems and may contain secrets, sessions, local databases, or runtime
locks:

- Agent-host runtime roots such as Hermes/OpenClaw state.
- Private AFM training, adapter, or model artifact storage.

If they need cleanup later, move only specific non-runtime artifacts and leave
symlinks where the owning app expects stable paths.

## Sync-Root Avoidance

Minni's "local-first" guarantee assumes the vault and database are
not under any third-party sync root. The daemon (`src/minni/minnid.py`,
`_warn_if_sync_root`) emits a startup warning — non-fatal, best-effort —
when any daemon-managed path resolves under one of these prefixes:

- `~/Library/Mobile Documents/` (iCloud Drive)
- `~/Dropbox`
- `~/Google Drive`
- `~/OneDrive`

The check runs at startup for the socket, DB, and vault paths. If such a
warning fires, move the affected path out of the sync root before relying
on local-first claims. The daemon warns only; it does not refuse to start.
