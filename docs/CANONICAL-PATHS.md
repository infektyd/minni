# Minni Canonical Paths

This folder is the canonical home for Minni on this machine:

`<repo-root>`

It is also the canonical Git working tree for
`https://github.com/infektyd/sovereign-memory`. The live layout is intentional:
runtime integrations use `engine/`, `openclaw-extension/`, and
`plugins/minni/` directly.

Use this page to avoid guessing between older root-level, OpenClaw, Hermes, and
downloaded paths.

Older notes may still spell this path using retired home-root or temp-main
names. Treat those as legacy references, not active roots.

## Active Core

- `engine/` - Minni Python engine.
- `openclaw-extension/` - OpenClaw extension bridge.
- `plugins/minni/` - Codex plugin package.
- `session-extracts/` - extracted handoff/session notes.

## Active Runtime

- `~/.sovereign-memory/sovereign_memory.db` - active Minni database.
- `~/.sovereign-memory/faiss/` - active FAISS cache.
- `~/.sovereign-memory/run/sovrd.sock` - active daemon socket.
- `~/.sovereign-memory/codex-vault` - Codex-owned Obsidian vault.
- `~/.sovereign-memory/claudecode-vault` - Claude Code-owned Obsidian vault.
- `~/.sovereign-memory/kilocode-vault` - KiloCode-owned Obsidian vault.

Agent vault roots must be actual directories. Do not point a new agent at
Codex's vault and do not bootstrap a new agent by copying another agent's
`wiki/`, `logs/`, `inbox/`, `index.md`, or `log.md`.

## Organized Supporting Material

- `docs/plans/` - planning documents and prompt plans.
- `docs/decisions/` - decision records.
- `docs/research/` - related research notes.
- `assets/hermes/` - Hermes/OpenClaw visual assets.
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
not under any third-party sync root. The daemon emits a startup warning if its
vault path or DB path resolves under any of these prefixes:

- `~/Library/Mobile Documents/` (iCloud Drive)
- `~/Dropbox`
- `~/Google Drive`
- `~/OneDrive`

If a warning fires, move the affected path out of the sync root before relying
on local-first claims. The actual startup-warning code is being implemented in
Wave 2; this section documents the contract the daemon will enforce.
