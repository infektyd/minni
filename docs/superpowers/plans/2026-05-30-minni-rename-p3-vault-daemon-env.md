# Minni Rename — P3 (Vault + Daemon + Env) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Precondition:** Operator commits/stashes their `engine/` + `src/` WIP so the working tree is clean before the code sweep (Tasks 2–4). The daemon stays DOWN through all phases (`com.openclaw.sovrd` booted out) — no bring-up until after P4 + P5.

**Goal:** Make `minni` the canonical runtime identity at the vault/daemon/env layer: rename `SOVEREIGN_*` env → `MINNI_*`, `~/.sovereign-memory/` paths → `~/.minni/`, the `sovereign_*` JSON-RPC methods → `minni_*` (TS + Python together), `sovereign_memory.db` → `minni.db`, migrate the vault data + frontmatter, and rewrite the launchd plist. Clean direct rename, no aliases.

**Architecture:** Daemon down → all of this is offline. Order: rename CODE + CONFIG to point at `minni` first, THEN move the DATA to match, so the moved vault and the code agree at bring-up. Backup already taken (`~/.minni-migration-backup-20260530-083521`, 4244 files, DB checksum verified).

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md` (§3, §3b, §3c, P3 obligations)

**Out of scope:** platform manifest env/paths → P4; skills → P5; the actual daemon restart/verify → bring-up.

---

## Task 0: Preconditions (controller, before dispatch)
- [ ] Confirm `git status --short` shows a CLEAN working tree (operator committed/stashed WIP). If not, STOP and wait.
- [ ] Confirm backup exists: `ls -d ~/.minni-migration-backup-* ` and daemon down: `launchctl list | grep sovrd` → empty.

## Task 1: Enumerate the rename surface (no edits)
- [ ] `rg -oh "SOVEREIGN_[A-Z_]+" plugins/minni/src engine | sort -u` → the full env-var list (target: each `SOVEREIGN_X` → `MINNI_X`).
- [ ] `rg -n "\.sovereign-memory\b|sovereign_memory\.db" plugins/minni/src engine` → path/DB refs.
- [ ] `rg -n "sovereign_[a-z_]+" engine --glob '!*.md'` and `rg -n "\"sovereign_[a-z_]+\"" plugins/minni/src/sovereign.ts` → RPC method strings + Python dispatch handlers. Record the exact set; these must change in lockstep.

## Task 2: Rename `SOVEREIGN_*` env → `MINNI_*` and `.sovereign-memory` → `.minni` in code (TS + Python)
**Files:** the 3 TS + 6 Python files from Task 1 (e.g. `plugins/minni/src/{config,vault,ui-server,task,sovereign}.ts`, `engine/sovrd.py` + 5 others).
- [ ] Per file, targeted edits (NO blanket sed across repo): each `process.env.SOVEREIGN_X` / `os.environ["SOVEREIGN_X"]` → `MINNI_X`; each `~/.sovereign-memory` / `.sovereign-memory` path literal → `~/.minni` / `.minni`; `sovereign_memory.db` → `minni.db`.
- [ ] Update any default-path constants (e.g. `DEFAULT_VAULT_PATH`, `SOVEREIGN_HOME` resolution) to the `~/.minni` root.
- [ ] Gate: `cd plugins/minni && npm run build:server` exit 0. Python import check: `python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]" engine/sovrd.py engine/agent_api.py engine/config.py engine/principal.py` (syntax OK).
- [ ] Commit TS files and Python files (explicit paths) — may be one commit or split TS/Python: "refactor(minni-p3): rename SOVEREIGN_* env + .sovereign-memory paths -> MINNI_*/.minni in code" + trailer.

## Task 3: Rename the JSON-RPC method strings `sovereign_*` → `minni_*` (TS + Python lockstep)
**Files:** `plugins/minni/src/sovereign.ts` (4 call sites: ack_handoff, list_pending_handoffs, await_handoff, subscribe_contradictions); the Python dispatch handlers in `engine/` that register/serve those method names; affected `engine/test_pr*.py` assertions.
- [ ] Rename the method strings on BOTH sides to `minni_*` so the wire protocol still matches.
- [ ] Update Python test assertions that reference the old method names (e.g. `engine/test_pr10_handoff.py`, `test_pr6_contradictions.py`).
- [ ] Gate: TS build exit 0; run the relevant Python tests: `cd engine && python3 -m pytest test_pr10_handoff.py test_pr6_contradictions.py -q` (or the repo's runner) → pass.
- [ ] Commit: "refactor(minni-p3): rename sovereign_* JSON-RPC methods -> minni_* (TS + Python lockstep)" + trailer.

## Task 4: Rewrite the launchd plist(s)
**Files:** `~/Library/LaunchAgents/com.openclaw.sovrd.plist` (and `com.openclaw.sovereign-console.plist` if it references sovereign paths/env).
- [ ] `SOVEREIGN_*` keys → `MINNI_*`; `SOVEREIGN_WORKSPACE_ID=~/Projects/sovereignMemory` → `~/Projects/minni`; `SOVEREIGN_DB_PATH` → `~/.minni/minni.db`; `SOVEREIGN_SOCKET_PATH` → `~/.minni/run/minnid.sock`; `SOVEREIGN_VAULT_PATH` (currently `~/wiki`) → keep or move per operator; ProgramArguments python+script paths `~/Projects/sovereignMemory/...` → `~/Projects/minni/...` (NOTE: the symlink resolves, but use the canonical anchor); optionally rename the job label `com.openclaw.sovrd` → `com.openclaw.minnid` and the plist filename.
- [ ] Do NOT `launchctl bootstrap` (load) it — daemon stays down until bring-up. Just rewrite the file. These live outside the repo; back up the original plist first (`cp … …​.bak`).

## Task 5: Migrate the vault DATA (late, after code+config point at minni)
- [ ] Re-verify backup exists and daemon down.
- [ ] `mv ~/.sovereign-memory ~/.minni` (clean rename; backup is the safety net — no symlink-back under clean rename).
- [ ] Rename DB + sidecars: `~/.minni/sovereign_memory.db` → `minni.db` (and `.db-wal`, `.db-shm` if present). Rename the run dir socket name expectations if needed (socket is recreated on bring-up).
- [ ] Migrate frontmatter in the ~32 notes: for each `~/.minni/*/wiki/**.md` containing `sovereign_learning:`, rename the key to `minni_learning:` (targeted; verify count before/after).
- [ ] Update vault-internal stale path refs in identity envelopes (`~/.minni/identities/*/**ENVELOPE.md`): `vault_path: …/.sovereign-memory/…` → `…/.minni/…`, `workspace: …/Projects/sovereignMemory` → `…/Projects/minni`.
- [ ] Gate: `ls ~/.minni/minni.db` exists; `grep -rl "sovereign_learning:" ~/.minni | wc -l` → 0; old `~/.sovereign-memory` gone.

## Task 6: Verification
- [ ] `rg -n "SOVEREIGN_|\.sovereign-memory|sovereign_memory\.db|\"sovereign_[a-z]" plugins/minni/src engine --glob '!*.md' --glob '!dist'` → EMPTY (code fully on minni).
- [ ] TS build exit 0; full TS tests fail 0; Python handoff/contradiction tests pass.
- [ ] `~/.minni/` is canonical; `~/.sovereign-memory` absent; backup intact.
- [ ] Working tree: only the operator's (now-committed) history + P3 commits; no stray bundling.

## NOT in P3 (sequenced after)
- P4: platform manifests (`.codex/.gemini/.kilocode` mcpServers `env` blocks set `SOVEREIGN_VAULT_PATH=~/.sovereign-memory/<a>-vault` → `MINNI_VAULT_PATH=~/.minni/<a>-vault`); grok `.mcp.json` + identity workspace anchor fix.
- P5: skills keep/merge/retire audit.
- **Bring-up (operator present):** rewrite-load plist (`launchctl bootstrap`), restart daemon as `minnid` on `~/.minni`, reinstall plugins (`minni@minni`), verify recall/learn across all platforms.
