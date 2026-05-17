# Sovereign Memory Canonical Paths

This folder is the canonical home for Sovereign Memory on this machine:

`/Users/hansaxelsson/sovereignMemory`

It is also the canonical Git working tree for
`https://github.com/infektyd/sovereign-memory`. The live layout is intentional:
runtime integrations use `engine/`, `openclaw-extension/`, and
`plugins/sovereign-memory/` directly.

Use this page to avoid guessing between older root-level, OpenClaw, Hermes, and
downloaded paths.

Older notes may still spell this path as `/Users/hansaxelsson/SovereignMemory`.
On this macOS volume that resolves to the same directory, but new docs and
symlink targets should use `/Users/hansaxelsson/sovereignMemory`.

## Active Core

- `engine/` - Sovereign Memory Python engine.
- `openclaw-extension/` - OpenClaw extension bridge.
- `plugins/sovereign-memory/` - Codex plugin package.
- `codex-vault/` - Codex-owned Obsidian vault.
- `sovereign_memory.db` - active Sovereign Memory database.
- `session-extracts/` - extracted handoff/session notes.

## Organized Supporting Material

- `docs/plans/` - planning documents and prompt plans.
- `docs/decisions/` - decision records.
- `docs/research/` - related research notes.
- `assets/hermes/` - Hermes/OpenClaw visual assets.
- `logs/openclaw/` - preserved OpenClaw audit logs.
- `archives/downloads/` - old downloaded bundles and one-off prototypes.
- `archives/legacy-vaults/` - preserved old vault copies before symlink cleanup.
- `_archive/` - previous repo/workspace archives.

## Compatibility Symlinks

These paths are intentionally kept as symlinks for tools or older agent notes
that still reference the legacy locations:

- `/Users/hansaxelsson/.openclaw/sovereign-memory-v3.1` -> `engine/`
- `/Users/hansaxelsson/.openclaw/sovereign_memory.db` -> `sovereign_memory.db`
- `/Users/hansaxelsson/.openclaw/extensions/sovereign-memory` -> `openclaw-extension/`
- `/Users/hansaxelsson/.sovereign-memory/codex-vault` -> `codex-vault/`

Legacy home-root and project-root symlinks that only duplicated docs, assets,
or engine source should be retired into `_cleanup-quarantine/` rather than kept
as active integration points. The 2026-04-26 cleanup moved those duplicate
links, plus archived duplicate `.git` directories, into
`_cleanup-quarantine/20260426-081508/` for reversible review.

## Still External On Purpose

Do not casually move these folders wholesale. They are live application state
for other systems and may contain secrets, sessions, local databases, or runtime
locks:

- `/Users/hansaxelsson/.openclaw`
- `/Users/hansaxelsson/.hermes`
- `/Users/hansaxelsson/.sovereign`
- `/Volumes/Macmini/06_Development/AppleFoundationModels/...`

If they need cleanup later, move only specific non-runtime artifacts and leave
symlinks where the owning app expects stable paths.

## Sync-Root Avoidance

Sovereign Memory's "local-first" guarantee assumes the vault and database are
not under any third-party sync root. The daemon emits a startup warning if its
vault path or DB path resolves under any of these prefixes:

- `~/Library/Mobile Documents/` (iCloud Drive)
- `~/Dropbox`
- `~/Google Drive`
- `~/OneDrive`

If a warning fires, move the affected path out of the sync root before relying
on local-first claims. The actual startup-warning code is being implemented in
Wave 2; this section documents the contract the daemon will enforce.
