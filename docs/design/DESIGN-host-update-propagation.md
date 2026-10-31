# Design: Hermetic Host Update Propagation & Zero-Drift Fleet Reconcile

- **Status:** Draft — Round 2 Rework (Research + Design Spike; zero product-code changes in this commit)
- **Author:** Argon (`gemini-4-argon`)
- **Base Commit:** `af0c04f7` (`main`), amending Round 1 commit `ba0c662a`
- **Supersedes / Extends:** [`docs/design/DESIGN-wire-claude-plugin-adoption.md`](DESIGN-wire-claude-plugin-adoption.md)

---

## Review Response (Round 2 Rework)

Every item from the independent adversarial review (Sonnet) and coordinator live verification (`P1-1` through `P1-6`, all `P2` items, and all `P3` items) is resolved below and integrated into §§1–6.

| Item | Review Finding | Resolution in Round 2 Design | Section(s) Updated |
|---|---|---|---|
| **P1-1** | **Identity Clobber:** Symlinking `~/.agents/plugins/Minni@Minni` and `~/.cursor/plugins/local/minni` to `current` causes both loaders to read the shared payload `.mcp.json` (`MINNI_AGENT_ID=grok-build`, `MINNI_WORKSPACE_ID=workspace-minni`, SHA-pinned `args` and `cwd`), clobbering `agent=gemini` and `agent=cursor`. | **Fixed (Dual Defense):** (1) `~/.agents/plugins/Minni@Minni` and `~/.cursor/plugins/local/minni` remain **real directories** holding generated per-host thin manifests (`.mcp.json` with `MINNI_AGENT_ID=gemini` / `cursor`, `gemini-extension.json`, `.cursor-plugin/plugin.json`) with **no git SHA and no `MINNI_WORKSPACE_ID` or `cwd`**, while only `dist/` (and read-only `skills/`, `commands/`, `hooks/`) resolves through `~/.minni/plugin/current/dist`. (2) `.mcp.json` is removed from the shared `~/.minni/plugin/<version>/` payload altogether (`writers.py:201` / `install.py` stops emitting a shared `.mcp.json`). | §4.1, §4.2, §5 Step 3, §6 |
| **P1-2** | **Unproven Loader Symlink Support & Case-Insensitive APFS:** No evidence on disk that Antigravity/Gemini or Cursor loaders follow a symlinked plugin dir. On case-insensitive APFS, `minni@minni` and `Minni@Minni` share the same inode (`st_ino = 198858508`); a naive `ln -s` collides. | **Fixed:** (1) Top-level plugin directories stay **real directories** (never symlinked at the plugin root). (2) Added a mandatory **Step 2 Pre-Conversion Per-Host Loader Probe** in §5 that tests whether Antigravity/Gemini and Cursor loaders follow a symlinked `dist -> ~/.minni/plugin/current/dist` subtree before converting; if a loader rejects a symlinked `dist/`, the reconciler uses an **Atomic Staged Materialization Fallback** (`stage temp dir -> copy dist/ -> os.replace`). (3) Explicit APFS case-insensitivity guard using `os.path.samestat(path_a, path_b)` (`(st_dev, st_ino)`) + `os.path.realpath` canonical casing: when `Minni@Minni` and `minni@minni` share an inode, they are recognized as one directory and no sibling alias symlink is created. | §4.1, §4.2, §5 Steps 2–3, §6 |
| **P1-3** | **GC vs Long-Lived Servers:** Node resolves main-module realpath at launch and closes the `.js` FD after ESM parsing, so running servers stay on the old versioned dir while `argv` shows `current/...` (`ps` substring scan misses, and `keep_last=2` only survives 1 flip, not 2+). | **Fixed:** (1) At startup, `server.ts` resolves `selfRealpath = fs.realpathSync(fileURLToPath(import.meta.url))` and `versionRoot`, **keeps an open read-only file descriptor (`fs.openSync(selfRealpath, "r")`) for the process lifetime**, acquires a shared kernel lock `flock(LOCK_SH \| LOCK_NB)` on `<versionRoot>/.active-server.lock`, and writes `~/.minni/run/mcp-servers/<pid>.json`. (2) `gc.py` protects version directories by inspecting live processes via `lsof -Fn -p <pids>` (which sees the kept-open FD and any `cwd`), probing `<ver>/.active-server.lock`, validating lease files, and—for pre-upgrade servers—protecting every version directory installed at or before the oldest running `server.js` process's `ps -o lstart=` start timestamp (surviving arbitrary N flips). (3) Every deploy and health check emits a **`running_stale_servers`** report naming **host, PID, PPID, loaded SHA, and target SHA**, and **Behavioral verification** inspects running servers (verifying their backing `version_root` exists on disk and socket lease is healthy), not only fresh spawns. | §4.3, §4.5, §4.7, §6 |
| **P1-4** | **Flip Before Verify, No Rollback:** Round 1 flipped `current` before probing host environments and had no automatic rollback on failure. | **Fixed:** Replaced with a **7-Step Atomic Cutover with Pre-Flip Host-Env Probes, Post-Flip Probes, and Automatic Rollback**: (1) Stage `<new_ver>` & verify manifests; (2) Run `timeout 20` pre-flip behavioral probes for **every** host's exact launcher and `env` against `<new_ver>/dist/...` (abort before flip on any failure); (3) Record `prev_target` and archive any real-dir `current`; (4) Atomically flip `current -> <new_ver>`; (5) Reconcile non-zero-delta host surfaces with pre-write snapshots; (6) Run post-flip behavioral probes of all host configs + running servers; (7) On any Step 5/6 failure, **automatically revert `current -> prev_target` via `os.replace` and restore mutated host configs from snapshots**. Reruns are idempotent. | §4.3, §5, §6 |
| **P1-5** | **Desktop Gate vs Operator Workflow & Write-While-Running:** `Claude.app` is the parent of the `Claude Code` sessions (including the deployer), so "exit 2, quit `Claude.app`" kills the deployer. Also, `Claude.app` writes `claude_desktop_config.json` **while running** (`main.log`: `"Config file written"` at `10:39:49` and `10:40:23`), not only on quit. | **Fixed:** (1) Corrected the Round-1 claim in §1 D1, §2 Row 4, and §4.3 to state that `Claude.app` writes `claude_desktop_config.json` both **while running** (`10:39:49`, `10:40:23` in `main.log`) and on quit, and is the parent process tree (`Claude.app -> disclaimer -> claude`) of active Claude Code sessions. (2) Made `claude-desktop` a **zero-delta host**: `accepted_entries` accepts **both** `~/.agents/plugins/Minni@Minni/dist/server.js` (today's live value, which resolves through `Minni@Minni/dist -> ~/.minni/plugin/current/dist/server.js`) and `~/.minni/plugin/current/dist/server.js`. Under Keyed-Subset diff semantics, today's live `claude_desktop_config.json` has `delta == 0`, so routine deploys **never write `claude_desktop_config.json`** and never ask the operator to quit `Claude.app`. Direct config edits occur only in an explicit operator command (`minni wire claude-desktop --migrate-config`) run from an external terminal when `Claude.app` is stopped. | §1 D1, §2 Row 4, §4.2, §4.3, §5 Step 1, §6 |
| **P1-6** | **`roots/list` Contradicts the Repo & Operator Pin Order:** `plugins/minni/tests/mcp-protocol-version.test.mjs:110-118` forbids `roots/list` and `.listRoots(` (MCP `2026-07-28` deprecates Roots). Also, the operator pin must come **first**, before any heuristic. | **Fixed:** Removed MCP `roots/list` / `.listRoots()` completely. Reordered `resolveSessionWorkspaceId()` so the operator explicit pin (`process.env.MINNI_PINNED_WORKSPACE_ID`) is checked **first** (immediately after explicit per-call `args.workspaceId`), ahead of session hook hints and `deriveWorkspaceIdFromCwd()`. | §4.6, §6 |
| **P2-1** | **Rewrite-on-Run Hosts Beyond Desktop:** Grok `config.toml`, Muse `settings.json` (`.settings.json.lock`), Devin, Cursor `mcp.json`, Antigravity `mcp_config.json` files, and `~/.claude.json` (rewritten continuously via `.claude.json.tmp.<pid>`) all need lock/CAS, refuse-to-write-while-running, and zero-delta stable paths. | **Fixed:** Specified a uniform **Rewrite-on-Run Host Protection Protocol** in §4.3: (1) All hosts use stable paths so routine deploys have `delta == 0` (zero writes). (2) When `delta != 0`, writers check host lockfiles (`.settings.json.lock`, `.claude.json.tmp.*`, `.config.toml.lock`), refuse blind writes while rewrite-on-run daemons/apps hold state in memory, and use advisory locking + byte-level **optimistic compare-and-swap (CAS)** (`(st_ino, st_mtime_ns, sha256)` verified before `os.replace` and 100 ms post-write readback). | §2, §4.2, §4.3, §6 |
| **P2-2** | **PID Walk, H5 Diagnostic Logging, and H4/H6 Scope:** Per-hop `ps` walk is slow, reparents to `PID 1`, matches shared terminal ancestors within TTL, and suffers PID reuse. Add diagnostic logging for `H5` (`minni_learn` filed under `workspace-unknown` in direct-spawn Claude Code session). State explicitly what is and is not solved for `H4` and `H6`. | **Fixed:** (1) Replaced the 4-hop `ps` walk with **zero-intermediate-PID `exec` enforcement** across all wrappers (`mcp-env-run`, `minni-mcp`, `minni-cursor.sh`, `minni-hook.sh`) AND `exec node ...` in `hooks/hooks.json`, plus `process.kill(hostPid, 0)` liveness verification. (2) **Identified the exact mechanism of `H5`:** `hooks/hooks.json` ran `"node ${CLAUDE_PLUGIN_ROOT}/dist/hook.js ..."`, which caused `/bin/sh -c` to fork `node` as a child of `/bin/sh` (`process.ppid == <sh_pid>`), writing `active-session-claude-code-<sh_pid>.json`, whereas `server.js` was spawned directly (`process.ppid == <claude_pid> = 15917`) with `cwd = ${CLAUDE_PLUGIN_ROOT}` (`mcp__plugin_minni_minni`), missing the hint file and falling back to `"workspace-unknown"`. Added structured `workspace_resolution_diag` logging for every miss reason. (3) Added explicit **`H4` and `H6` Scope & Non-Contradiction Subsection** in §4.6. | §3.1 #11, §4.6, §6 |
| **P2-3** | **Desktop Desired-Config Semantics:** Live `claude_desktop_config.json` has `MINNI_SOCKET_PATH` plus `MINNI_AFM_*` env vars omitted by canonical env; without subset semantics, `delta` is never zero. | **Fixed:** Defined formal **Keyed-Subset + Forbidden-Key Diff Semantics** in §4.2: required keys (`MINNI_AGENT_ID`, `MINNI_VAULT_PATH`), allowed optional keys preserved without triggering delta (`MINNI_SOCKET_PATH`, valid `MINNI_AFM_PROVIDER_MODE`, existing `MINNI_AFM_NATIVE_HELPER`, operator-authorized `MINNI_PINNED_WORKSPACE_ID`, non-`MINNI_*` keys), and forbidden keys (`MINNI_WORKSPACE_ID`, `MINNI_<HOST>_WORKSPACE_ID`, `SOVEREIGN_*`, version-root `cwd`). | §4.2, §4.3, §6 |
| **P2-4** | **`installed_plugins.json` Version & SHA Management:** `installed_plugins.json` stores `version` and `gitCommitSha` beside `installPath`. Define how these stay accurate across flips. | **Fixed:** `installed_plugins.json` keeps `installPath: "~/.minni/plugin/current"` while `_register_direct()` updates `version` (`"0.6.0"`) and `gitCommitSha` (`<40-char sha>`) on deploy (idempotently preserving `lastUpdated` when unchanged), and `sync_local_marketplace()` refreshes the real stub directory `~/.claude/local-marketplaces/minni/plugins/minni-<version>/` (which `claude_plugin.py:282` requires to be a real directory, not a symlink). | §2 Rows 2–3, §4.2, §6 |
| **P2-5** | **Antigravity `mcp-env-run` Wrapper:** Antigravity launches go through `~/.agents/bin/mcp-env-run`, which injects `~/.agents/env/global.env`. The registry must preserve the wrapper. | **Fixed:** `HOST_SURFACE_CATALOG` and `wired.json` v2 explicitly record `"command": "~/.agents/bin/mcp-env-run"` and `"args": ["node", "~/.minni/plugin/current/dist/server.js"]` (or `"~/.agents/plugins/Minni@Minni/dist/server.js"`) on Antigravity surfaces. | §4.2, §5, §6 |
| **P2-6** | **`os.replace` onto a Real Directory (`current.legacy-dir.bak`):** `os.replace` fails with `EISDIR`/`EPERM` if `~/.minni/plugin/current` is a real directory (`current.legacy-dir.bak` proves this happened). | **Fixed:** Before `os.replace(tmp_symlink, current_path)`, the cutover checks `if current_path.exists() and not current_path.is_symlink() and current_path.is_dir():` and moves the real directory to `~/.minni/archive/<ts>/current.dir/` via `os.rename` first. | §4.1, §4.3, §5, §6 |
| **P2-7** | **Catalog Gaps & Canonical `.minni/plugin` Scan:** Add `~/.agents/mcp-servers/views/.gemini__*`, `~/.gemini/commands/minni`, and skill copies (`~/.codex/skills/astra-minni`, `~/.claude/skills/`, `~/.agents/skills/`). Add a canonical scan for ANY host file containing `".minni/plugin"`. | **Fixed:** Added rows 26–28 to §2 Inventory and added all three surface groups to `HOST_SURFACE_CATALOG` in §4.2. Added `scan_for_uncatalogued_plugin_refs()` in §4.7 (`check_deployments.py` / `minni doctor`) scanning all host dotdirs for `".minni/plugin"` and failing closed on any uncatalogued or version-pinned reference. | §2 Rows 26–28, §4.2, §4.7, §6 |
| **P3-1** | **Archive-First for Every Removal:** 46 `.minni-backup-*` files, `~/.gemini/extensions/minni`, and Cursor plugin dir's real `node_modules` (269 entries) must be archived before removal. | **Fixed:** Every migration step in §5 and reconciler cleanup moves displaced files/directories into `~/.minni/archive/<YYYYMMDD-HHMMSS>/` (`ARCHIVE-FIRST`) before unlinking or replacing. | §4.4, §5 Steps 1–7 |
| **P3-2** | **Correct Two Inaccurate Round-1 Claims:** (1) `platform.py:35-43` `"AGENT_SPECS grok vs grok-build"` is wrong: no `AGENT_SPECS` exists and `platform_spec("grok")` (`platform.py:169-171`) already uses `grok-build`. (2) `custom_refresh.py:200` only rewrites an existing version-root `cwd`. | **Fixed:** Retracted Round-1 §3.2 #17 and §4.6 #3 regarding `platform.py`. Corrected §1 D2, §3.1 #10, and §4.6 #2 to state that `custom_refresh.py:199-205` only rewrites an already-existing version-root `cwd` (originally stamped by `writers.py:248` / `propagate.py:658`), and specified that `custom_refresh.py` should delete a version-root `cwd` rather than re-pointing it to `new_root`. | §1 D2, §3.1 #10, §3.2 #17, §4.6 #2 |

---

## 1. Problem Statement & Core Invariant

Minni runs across a heterogeneous fleet of local agent hosts: Claude Code (CLI + plugin marketplace), Claude Desktop, Codex, Gemini CLI, Antigravity (IDE, CLI, and Desktop app surfaces), Grok / Grok-build, Cursor, KiloCode, Devin, Windsurf, and Muse.

Today, deploys (`make sync-root` -> `scripts/update_root.sh` and `minni sync` -> `src/minni/fleet_sync.py`) split host management across three partially overlapping mechanisms:
1. `minni wire all --from-repo` (`src/minni/wire/flow.py`), which writes **version-pinned paths** (`~/.minni/plugin/<version>+git.<sha>/dist/server.js`) directly into some host configs while deliberately leaving `~/.minni/plugin/current` unmaintained on `--from-repo` installs (`src/minni/wire/install.py:134`).
2. `custom_refresh.py` (`src/minni/wire/custom_refresh.py`), which rewrites custom JSON bindings recorded in `wired.json` (`muse`, `devin`) to version-pinned paths while leaking `.minni-backup-*` files on every deploy.
3. `propagate.py` (`plugins/minni/skills/minni-install/scripts/propagate.py`), which copies files in place for `antigravity` (`~/.agents/plugins/Minni@Minni`) and `cursor` (`~/.cursor/plugins/local/minni`), and stamps version-pinned paths into `~/.grok/hooks/minni.json`.

This split caused three verified classes of production failure (plus the additional defects cataloged in §3):
- **D1 (Versioned-Path Pin & In-Memory Config Rewrite Hazard, Claude Desktop):** `minni wire-adopt claude-code` (`src/minni/wire/claude_plugin.py:575-641`) rewrote `claude_desktop_config.json` to a SHA-pinned directory `~/.minni/plugin/0.6.0+git.<sha>/dist/server.js` without recording `claude-desktop` in `~/.minni/plugin/wired.json`. Subsequent deploys and GC pruned the old SHA directory, leaving Claude Desktop failing with `MODULE_NOT_FOUND`. Worse, `Claude.app` holds `claude_desktop_config.json` in memory and rewrites it **both while running** (verified in `~/Library/Logs/Claude/main.log`: `"Config file written"` at `10:39:49` and `10:40:23`) **and on quit**, reverting external on-disk edits. Furthermore, `Claude.app` is the parent process tree (`Claude.app -> disclaimer -> claude`) of active Claude Code sessions, so any deploy gate that tells the operator to quit `Claude.app` kills the very Claude Code session running the deploy.
- **D2 (Install-Time Identity Pinning & Hook/Server `ppid` Mismatch):** Both `wire` (`src/minni/wire/flow.py:165`, `writers.py:206,301,327,364,753`, `kilo.py:89`) and `propagate.py` (`propagate.py:620,676,698,721,755,993,1460,1584`) stamp a static `MINNI_WORKSPACE_ID` (typically `"workspace-minni"` or `"workspace-<operator>"`) into host MCP configs at install time, and `writers.py:248,356` / `propagate.py:658,750` stamp `"cwd": "<install_root>"` (which `custom_refresh.py:199-205` then carries forward across every subsequent version flip). At runtime, `DEFAULT_WORKSPACE_ID` (`plugins/minni/src/config.ts:529-535`) prioritizes `process.env.MINNI_WORKSPACE_ID` over `deriveWorkspaceIdFromCwd()`, attributing sessions to `"workspace-minni"` regardless of project. Even when `MINNI_WORKSPACE_ID` is unset (as in the duplicate plugin-spawned `mcp__plugin_minni_minni` server), `hooks/hooks.json` invokes `"node ${CLAUDE_PLUGIN_ROOT}/dist/hook.js ..."`, causing `/bin/sh -c` to fork `node` under an ephemeral `/bin/sh` `ppid` while `server.js` runs directly under `claude`'s PID—so `resolveSessionWorkspaceId()` misses the hint file and files learnings under `"workspace-unknown"` (`H5`).
- **D3 (Dangling-Symlink Trust & Lexicographic SHA Sorting):** `propagate.py:99-100` (`plugin_version_segment()`) checked `current.is_symlink()` before `current.exists()`, trusting a dangling `~/.minni/plugin/current` symlink as authoritative and stamping its dead SHA into `gemini-extension.json`. When `current` is absent, `max_present_version()` (`propagate.py:59-84`) sorts `+git.<sha>` suffixes lexicographically by hex SHA rather than commit/install timestamp, picking arbitrary older builds whose hex SHA sorts higher than `HEAD`.

### The Core Invariant

> **DEPLOY CONVERGENCE INVARIANT:**
> After any Minni deploy (`make sync-root`, `minni sync`, or `minni wire all`), **every connected host surface** (MCP server, lifecycle hook, native bridge, command/skill overlay, and plugin manifest) resolves to and launches the newly deployed build with dynamic per-session workspace identity, **or the deploy fails closed (non-zero exit, with automatic pointer/config rollback)** naming every host surface that could not be updated or verified and reporting any long-lived running MCP servers still holding an older build in memory.

---

## 2. Phase 1 Inventory: Host Surfaces & Code Provenance

The table below captures all 28 host surface groups across the 12 host families discovered on the reference machine (`HEAD` = `0.6.0+git.af0c04f7`). Machine-specific raw probe outputs are stored in `_private/host_probe_results.json` (gitignored); synthetic paths (`~`, `<operator>`, `<user>`) are used below.

| # | Host / Surface | Config File & Key | Server / Hook Target Path | Path Kind Today | Exists & MCP `initialize` Probe (`timeout 20`, scratch cwd) | Code Path That Wrote It (`file:line`) | Refreshed by `make sync-root`? (Proven by call graph) | Hardcoded Identity in `env` / Wrapper | Host Rewrites Config While Running? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Claude Code (MCP)** | `~/.claude.json` -> `mcpServers.minni` | `node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js` | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (157.8 ms) | `wire`: `src/minni/wire/writers.py:286-305` (`update_claude_config`) | **Yes** (`update_root.sh:239` -> `wire all` -> `flow.py:305`) | `MINNI_AGENT_ID=claude-code`, `MINNI_VAULT_PATH=~/.minni/claudecode-vault`, **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG: static pin)** | **Yes (Continuous):** `claude` CLI writes `~/.claude.json.tmp.<pid>` throughout active sessions |
| 2 | **Claude Code (Plugin Registry + Hooks)** | `~/.claude/plugins/installed_plugins.json` -> `plugins["minni@minni"][0].{installPath,version,gitCommitSha}` | `~/.minni/plugin/0.6.0+git.af0c04f7` (loads `.claude-plugin/plugin.json` + `hooks/hooks.json` -> `dist/hook.js` + duplicate MCP `dist/server.js`) | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (157.1 ms) | `wire`: `src/minni/wire/claude_plugin.py:323-360` (`_register_direct`) | **Yes** (`update_root.sh:239` -> `flow.py:585-635`) | Shared `.mcp.json` inside payload has **`MINNI_AGENT_ID=grok-build` and `MINNI_WORKSPACE_ID=workspace-minni` (WRONG)**; `.claude-plugin/plugin.json` spawns duplicate `mcp__plugin_minni_minni__*` with `cwd=${CLAUDE_PLUGIN_ROOT}` (`H5`) | **Partial** (`claude /plugin` commands rewrite `installed_plugins.json`) |
| 3 | **Claude Code (Local Marketplace)** | `~/.claude/local-marketplaces/minni/.claude-plugin/marketplace.json` & `plugins/minni-0.6.0/` | Real directory copy `plugins/minni-0.6.0/` (`claude_plugin.py:282` forbids symlinks here) | `STABLE` (real stub dir) | Exists (real copy of `.claude-plugin`, `hooks`, `dist/hook.js`) | `wire`: `src/minni/wire/claude_plugin.py:238-315` (`sync_local_marketplace`) | **Yes** (`flow.py:585-635`) | `.claude-plugin/plugin.json` embeds duplicate `mcpServers.minni` | No |
| 4 | **Claude Desktop (MCP)** | `~/Library/Application Support/Claude/claude_desktop_config.json` -> `mcpServers.minni` | `/opt/homebrew/bin/node ~/.agents/plugins/Minni@Minni/dist/server.js` (operator hotfix after D1 outage) | `STABLE` (manual hotfix; `wire-adopt` writes `VERSIONED`) | Exists; `rc=0`, `minni@0.6.0` (156.0 ms) | Manual hotfix after `claude_plugin.py:575-641` (`_move_desktop_arg`) left it on pruned `0.6.0+git.de211faf`; `propagate.py:950-1012` also writes here | **No** (`follow_claude_desktop` at `claude_plugin.py:612` skips because arg is not under `~/.minni/plugin`; `propagate` for `claude` is not called by `update_root.sh`) | `MINNI_AGENT_ID=claude-code`, `MINNI_VAULT_PATH=~/.minni/claudecode-vault`, `MINNI_SOCKET_PATH`, `MINNI_AFM_*` (`MINNI_WORKSPACE_ID` manually removed; `propagate.py:993` stamps `workspace-minni`) | **YES (CRITICAL):** `Claude.app` rewrites `claude_desktop_config.json` **both while running** (`main.log` `10:39:49`, `10:40:23`) **and on quit**, and parents `Claude Code` processes |
| 5 | **Codex (MCP)** | `~/.codex/config.toml` -> `[mcp_servers.minni]` | `node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js` | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (144.2 ms) | `wire`: `src/minni/wire/writers.py:706-773` (`update_toml_mcp_config`) | **Yes** (`update_root.sh:239` -> `flow.py:309`) | `MINNI_AGENT_ID=codex`, `MINNI_VAULT_PATH=~/.minni/codex-vault` (no `MINNI_WORKSPACE_ID` today, though `_resolve_codex_env` at `writers.py:267` preserves any stale pin unless `--dynamic-workspace` is passed) | **Partial** (`codex` rewrites `config.toml` when trust/MCP/model settings change) |
| 6 | **Codex (Hooks)** | `~/.codex/hooks.json` -> `hooks.{SessionStart,UserPromptSubmit,PreToolUse,Stop}` | `node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/codex-hook.js <Event>` | `VERSIONED` | Exists (`codex-hook.js` present) | `wire`: `src/minni/wire/writers.py:591-647` (`update_codex_hooks`) | **Yes** (`update_root.sh:239` -> `flow.py:322`) | Reads `MINNI_CODEX_AGENT_ID=codex`, `MINNI_CODEX_VAULT_PATH=~/.minni/codex-vault` | No |
| 7 | **Gemini CLI (`settings.json`)** | `~/.gemini/settings.json` -> `mcpServers.minni` | `node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (in-place copy) | Exists; `rc=0`, `minni@0.6.0` (168.7 ms) | Legacy `propagate.py` / manual (`writers.py` and `propagate.py` no longer touch `~/.gemini/settings.json`) | **Only indirectly** (target `Minni@Minni/dist/server.js` is refreshed by `propagate --platform antigravity`, but `settings.json` itself is never reconciled) | `MINNI_AGENT_ID=gemini`, `MINNI_VAULT_PATH=~/.minni/gemini-vault`, **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG: static pin)** | **Yes** (`gemini` CLI rewrites `settings.json` on setting/extension changes) |
| 8 | **Gemini CLI Extension (`extensions/minni`)** | `~/.gemini/extensions/minni/.gemini-plugin/gemini-extension.json` & `mcp.json` | `node ~/.gemini/extensions/minni/dist/server.js` & `npx tsx ~/Projects/Minni/plugins/minni/src/sovereign.ts` | `STABLE` (orphaned copy) | **STALE / BROKEN TODAY:** `dist/server.js` answers `minni@0.3.0` (`build-manifest.json` `0.5.0`/`c3aaa922`); `mcp.json` `sovereign.ts` does not exist | Legacy `gemini extensions install` / pre-v0.6 copy | **No** (completely untracked by `wire`, `propagate.py`, `check_deployments.py`, and `check_versions.py`) | `MINNI_WORKSPACE_ID=default` / `SOVEREIGN_WORKSPACE_ID=default`, `SOVEREIGN_VAULT_PATH=~/.sovereign-memory/gemini-vault` (**WRONG**) | No |
| 9 | **Antigravity Surface 1 (`~/.gemini/config`)** | `~/.gemini/config/mcp_config.json` (symlink -> `../antigravity/mcp_config.json`) -> `mcpServers.minni` | `~/.agents/bin/mcp-env-run node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (in-place copy) | Exists; `rc=0`, `minni@0.6.0` (159.0 ms) | `propagate.py:911-942` (`update_antigravity_config`) & `writers.py:514-539` | **Yes** (`update_root.sh:314` -> `propagate --platform antigravity`) | `MINNI_AGENT_ID=gemini`, `MINNI_VAULT_PATH=~/.minni/gemini-vault`, **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG)** | **Yes** (Antigravity UI rewrites `mcp_config.json` on MCP toggle) |
| 10 | **Antigravity Surface 2 (`~/.gemini/antigravity`)** | `~/.gemini/antigravity/mcp_config.json` -> `mcpServers.minni` | `~/.agents/bin/mcp-env-run node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (in-place copy) | Exists; `rc=0`, `minni@0.6.0` (156.1 ms) | `propagate.py:911-942` / `writers.py:514-539` | **Yes** (`update_root.sh:314`) | **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG)** | **Yes** |
| 11 | **Antigravity Surface 3 (`~/.gemini/antigravity-ide`)** | `~/.gemini/antigravity-ide/mcp_config.json` -> `mcpServers.minni` | `~/.agents/bin/mcp-env-run node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (in-place copy) | Exists; `rc=0`, `minni@0.6.0` (156.3 ms) | `propagate.py:911-942` / `writers.py:514-539` | **Yes** (`update_root.sh:314`) | **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG)** | **Yes** |
| 12 | **Antigravity Surface 4 (`~/.gemini/antigravity-cli`)** | `~/.gemini/antigravity-cli/plugins/minni/mcp_config.json` -> `mcpServers.minni` | `~/.agents/bin/mcp-env-run node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (in-place copy) | Exists; `rc=0`, `minni@0.6.0` (159.2 ms) | `propagate.py:911-942` / `writers.py:514-539` | **Yes** (`update_root.sh:314`) | **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG)** | **Yes** |
| 13 | **Antigravity Native Hooks** | `~/.gemini/config/plugins/minni/hooks.json` -> `hooks.*` | `node ~/.agents/plugins/minni@minni/dist/gemini-hook.js <Event>` | `STABLE` (via `propagate`; `VERSIONED` if `wire gemini` runs!) | Exists (`gemini-hook.js` present) | `propagate.py:1218-1231` (`update_antigravity_hooks`) or `writers.py:542-588` (`update_agy_plugin_hooks`) | **Yes** (`update_root.sh:314`) | `MINNI_GEMINI_AGENT_ID=gemini`, `MINNI_GEMINI_VAULT_PATH=~/.minni/gemini-vault` | No |
| 14 | **Shared Agents Plugin Tree (`Minni@Minni`)** | `~/.agents/plugins/Minni@Minni/{.mcp.json,gemini-extension.json}` (same APFS inode `198858508` as `minni@minni`) | `node ~/.agents/plugins/Minni@Minni/dist/server.js` (`node_modules` is a symlink into `~/Projects/Minni/plugins/minni/node_modules`) | `STABLE` (real dir, in-place copy) | Exists; `rc=0`, `minni@0.6.0` (142.5 ms); **DRIFT: root `gemini-extension.json` has `"version": "0.6.0+git.6feffaa3"` (D3)** | `propagate.py:610-632, 703-738` | **Partial** (`propagate --platform antigravity` copies files, but `gemini-extension.json` kept `0.6.0+git.6feffaa3` from D3) | `.mcp.json` (`MINNI_AGENT_ID=gemini`) & `gemini-extension.json` have **`MINNI_WORKSPACE_ID=workspace-<operator>` (WRONG)** | No |
| 15 | **Grok / Grok-build (MCP)** | `~/.grok/config.toml` -> `[mcp_servers.minni]` | `node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js` | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (141.4 ms) | `wire`: `src/minni/wire/writers.py:706-773` (`update_toml_mcp_config`) | **Yes** (`update_root.sh:239` -> `flow.py:309`) | `MINNI_AGENT_ID=grok-build`, `MINNI_VAULT_PATH=~/.minni/grok-build-vault`, **`MINNI_WORKSPACE_ID=workspace-<operator>` (WRONG: preserved forever by `writers.py:680`)** | **Yes** (`grok` CLI locks and rewrites `config.toml` on trust/setting changes) |
| 16 | **Grok / Grok-build (Hooks Overlay 1)** | `~/.grok/hooks/minni.json` -> `hooks.*` | `node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/grok-hook.js <Event>` | `VERSIONED` | Exists (`grok-hook.js` present) | `propagate.py:1147-1160` (`update_grok_hooks`) called by `update_root.sh:369` / `fleet_sync.py:336` | **Yes** (`update_root.sh:369`, unless `minni sync --wire-only` is used) | `MINNI_GROK_AGENT_ID=grok-build`, `MINNI_GROK_VAULT_PATH=~/.minni/grok-build-vault` | No |
| 17 | **Grok / Grok-build (Top-Level `hooks.json`)** | `~/.grok/hooks.json` -> `hooks.{SessionStart,UserPromptSubmit,PreToolUse,PreCompact,Stop}` | `node ~/.minni/plugin/current/dist/grok-hook.js <Event>` | `STABLE` (dangling target!) | **BROKEN TODAY (`MODULE_NOT_FOUND`):** `~/.minni/plugin/current` does not exist! | Legacy `propagate.py` / manual install before `hooks/minni.json` split | **No** (`propagate.py:1018` only writes `~/.grok/hooks/minni.json`, leaving `~/.grok/hooks.json` pointing at missing `current`) | `MINNI_GROK_AGENT_ID=grok-build`, `MINNI_GROK_VAULT_PATH=~/.minni/grok-build-vault` | No |
| 18 | **Grok / Grok-build (Rules)** | `~/.grok/rules/minni.md` | Static markdown (invokes `node ~/.minni/plugin/current/dist/cli.js` in fallback docs) | `STABLE` | Exists | `propagate.py:1163-1182` (`write_grok_rules`) | **Yes** (`update_root.sh:370`) | `agent=grok-build` | No |
| 19 | **Cursor (MCP Launcher & Lanes)** | `~/.cursor/mcp.json` -> `mcpServers.{minni,minni-w5,minni-swift,minni-widget,minni-cos,minni-shop,minni-agent,minni-inbox}` | `~/.local/bin/minni-mcp --lane <lane>` -> `exec node ~/.cursor/plugins/local/minni/dist/server.js` | `STABLE` (wrapper -> in-place copy) | Exists; `rc=0`, `minni@0.6.0` (210.6 ms) | Operator wrapper `~/.local/bin/minni-mcp` (not managed by `wire` or `propagate.py`) | **Indirectly** (`propagate --platform cursor` updates `~/.cursor/plugins/local/minni`, which `minni-mcp` execs) | **`minni-mcp` sets `MINNI_WORKSPACE_ID=${MINNI_WORKSPACE_ID:-workspace-<user>}` (or `default`) and `cd ~/.cursor/plugins/local/minni` (WRONG: pins workspace and clobbers `cwd`)** | **Yes** (Cursor Settings UI rewrites `~/.cursor/mcp.json` when MCP servers are toggled) |
| 20 | **Cursor (Local Plugin & Hooks)** | `~/.cursor/plugins/local/minni/{.mcp.json,.cursor-plugin/plugin.json}`, `~/.cursor/hooks.json`, `~/.cursor/hooks/minni-cursor.sh` | `./hooks/minni-cursor.sh <event>` -> `exec node ~/.cursor/plugins/local/minni/dist/cursor-hook.js` | `STABLE` (real dir with 269-entry real `node_modules`) | Exists; `rc=0`, `minni@0.6.0` (141.7 ms) | `propagate.py:1234-1291` (`update_cursor_hooks`, `deploy_cursor_wrapper`) | **Partial** (`minni-cursor.sh` is **never updated** under `--existing-only` due to early return at `propagate.py:1066-1074`!) | `.mcp.json` (`MINNI_AGENT_ID=cursor`) stamps **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG)**; `minni-cursor.sh` sets `MINNI_CURSOR_AGENT_ID=cursor` | No |
| 21 | **KiloCode (MCP)** | `~/.config/kilo/kilo.json` (`kilo.jsonc`) -> `mcp.minni` | `["node", "~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js"]` | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (139.1 ms) | `wire`: `src/minni/wire/writers.py:308-334` (`update_kilo_config`) | **Yes** (`update_root.sh:239` -> `flow.py:307`) | `MINNI_AGENT_ID=kilocode`, `MINNI_VAULT_PATH=~/.minni/kilocode-vault`, **`MINNI_WORKSPACE_ID=workspace-minni` (WRONG: static pin)** | **Partial** (Kilo CLI/UI rewrites `kilo.json` on MCP edits) |
| 22 | **KiloCode (Native Hook Bridge)** | `~/.config/kilo/plugin/minni.js` -> `__MINNI_KILO_HOOK_SCRIPT__` | `~/.minni/plugin/0.6.0+git.af0c04f7/dist/kilocode-hook.js` | `VERSIONED` | Exists (`kilocode-hook.js` present) | `wire`: `src/minni/wire/kilo.py:72-121` (`install_kilo_bridge`) | **Conditional** (`flow.py:260-288` skips if `minni.js` is absent or lacks marker during `wire all`) | **`MINNI_KILOCODE_WORKSPACE_ID=workspace-minni` (WRONG: static pin at `kilo.py:89`)** | No |
| 23 | **Devin (MCP)** | `~/.config/devin/mcp_config.json` -> `mcpServers.minni` (`cwd` + `args`) | `/opt/homebrew/bin/node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js` | `VERSIONED` | Exists; `rc=0`, `minni@0.6.0` (141.6 ms); **23 `.minni-backup-*` files leaked in `~/.config/devin/`** | `custom_refresh.py:173-242` (`_refresh`) | **Yes** (`update_root.sh:275` -> `refresh_custom_wires`) | `MINNI_AGENT_ID=devin`, `MINNI_VAULT_PATH=~/.minni/devin-vault`, **`MINNI_WORKSPACE_ID=workspace-praxis` (WRONG: conflicts with `wired.json` `workspace-playbook-engineering`), plus `cwd` pinned to `install_root`!** | **Yes** (`devin mcp` rewrites `mcp_config.json`) |
| 24 | **Devin (Lifecycle Hooks)** | `~/.config/devin/config.json` -> `hooks.{SessionStart,UserPromptSubmit,PreToolUse,PostCompaction,Stop}` -> `~/.config/devin/scripts/minni-hook.sh` | `exec /opt/homebrew/bin/node ~/.minni/plugin/devin/dist/hook.js "$1"` | `STABLE` (dangling target!) | **BROKEN TODAY (`MODULE_NOT_FOUND`):** `~/.minni/plugin/devin` does not exist! Also sets `MINNI_AFM_NATIVE_HELPER=~/Projects/Minni/src/minni/native_afm_helper` (missing) | Manual / legacy setup script | **No** (completely unknown to `wire`, `custom_refresh.py`, `propagate.py`, and `check_deployments.py`) | **`project_dir` defaults to `~/Projects/playbook-engineering` and sets `MINNI_CLAUDECODE_WORKSPACE_ID=workspace-playbook-engineering` (WRONG)** | **Yes** (`devin` CLI dynamically appends `Exec(...)` rules to `~/.config/devin/config.json`) |
| 25 | **Muse (MCP & Settings)** | `~/.muse/mcp.json` -> `mcpServers.minni` vs `~/.config/muse/settings.json` -> `mcpServers` | `/opt/homebrew/bin/node ~/.minni/plugin/0.6.0+git.af0c04f7/dist/server.js` (in `~/.muse/mcp.json`; **absent from `~/.config/muse/settings.json`!**) | `VERSIONED` | `~/.muse/mcp.json`: `rc=0`, `minni@0.6.0` (141.4 ms); **`~/.config/muse/settings.json` has NO `minni` entry**; **23 `.minni-backup-*` files leaked in `~/.muse/`** | `custom_refresh.py:173-242` (`_refresh`) | **Partial** (`~/.muse/mcp.json` is refreshed, but `~/.config/muse/settings.json` — which Muse CLI actually reads — has no `minni` server!) | **`MINNI_WORKSPACE_ID=workspace-minni` and `cwd` pinned to `install_root` (WRONG)** | **Yes** (Muse locks `.settings.json.lock` and rewrites `~/.config/muse/settings.json` from memory) |
| 26 | **Agents MCP View Mirror Files (`~/.agents/mcp-servers/views/`)** | `~/.agents/mcp-servers/views/.gemini__{config,antigravity,antigravity-ide,antigravity-cli__plugins__minni}__mcp_config.json.json` (+ `.claude__mcp.json.json`, `.cursor__mcp.json.json`) | `~/.agents/bin/mcp-env-run node ~/.agents/plugins/minni@minni/dist/server.js` | `STABLE` (unlinked regular files) | Exists; drifted from `~/.gemini/antigravity*/mcp_config.json` (regular files instead of symlinks) | `writers.py:514-539` / `~/.agents` view sync | **No** (`writers.py:520` skips views that are not symlinks into `~/.agents/mcp-servers/views/`) | **`MINNI_WORKSPACE_ID=workspace-<operator>` (WRONG)** | No |
| 27 | **Gemini CLI Slash Commands (`~/.gemini/commands/minni`)** | `~/.gemini/commands/minni/*.toml` (11 command TOML files: `status.toml`, `recall.toml`, `learn.toml`, etc.) | Static TOML prompt wrappers invoking `minni_*` MCP tools | `STABLE` (stale copies from June 5) | Exists (11 `.toml` files) | Legacy `propagate.py` / manual install | **No** (not refreshed by `wire` or `propagate --platform antigravity`) | None | No |
| 28 | **Host Skill Overlays (`~/.codex/skills/`, `~/.claude/skills/`, `~/.agents/skills/`)** | `~/.codex/skills/astra-minni/SKILL.md` & skill trees under `~/.agents/skills/` / `~/.claude/skills/` | Markdown skill instructions referencing Minni MCP tools & CLI | `STABLE` | Exists | Operator / `propagate.py` skill sync | **Partial** (`propagate.py` syncs plugin `skills/` into `Minni@Minni/skills`, but `~/.codex/skills/astra-minni` is untracked by `check_deployments.py`) | None | No |

*(Note on **Windsurf**: `~/.codeium/windsurf/mcp_config.json` exists on disk with `typesafe`, `persona-hub`, and `axpress`, and has no `minni` binding today. Under `host_discovery.py`, Windsurf is not registered as a separate platform spec.)*

---

## 3. Adversarial Sweep of `src/`, `scripts/`, and `plugins/minni/` (`NEW` Findings)

Every site that writes a SHA-pinned path, stamps an install-time workspace ID, trusts an unvalidated symlink, or fails to detect host drift is listed below, triaged with **Blockers Today** (a host or invariant is actively broken on the live machine today) before **Latent Defects**.

### 3.1 Blockers Today

1. `NEW: ~/.grok/hooks.json & plugins/minni/skills/minni-install/scripts/propagate.py:1018,1147-1160 · propagate.py only updates ~/.grok/hooks/minni.json while ~/.grok/hooks.json remains pointed at nonexistent ~/.minni/plugin/current/dist/grok-hook.js · grok / grok-build`
   - Live `~/.grok/hooks.json` invokes `node ~/.minni/plugin/current/dist/grok-hook.js <Event>` across all 5 hook events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PreCompact`, `Stop`), failing with `Cannot find module` because `~/.minni/plugin/current` is absent on `--from-repo` machines.

2. `NEW: ~/.config/devin/config.json:57-113 & ~/.config/devin/scripts/minni-hook.sh:7-20 · Devin lifecycle hooks execute ~/.minni/plugin/devin/dist/hook.js which does not exist on disk and hardcodes playbook-engineering workspace · devin`
   - All 5 Devin lifecycle hooks fail because `~/.minni/plugin/devin` does not exist; neither `wire`, `custom_refresh.py`, nor `check_deployments.py` knows about `~/.config/devin/config.json` or `~/.config/devin/scripts/minni-hook.sh`.

3. `NEW: ~/.gemini/extensions/minni/.gemini-plugin/gemini-extension.json & scripts/check_deployments.py:52-65 · Orphaned ~/.gemini/extensions/minni deployment runs stale 0.3.0/0.5.0 server.js and broken sovereign.ts mcp.json while invisible to all checkers · gemini`
   - Answering MCP `initialize` today with `serverInfo: {"name": "minni", "version": "0.3.0"}`; completely omitted from `DEPLOYMENT_GLOBS` in `check_deployments.py` and `check_versions.py`.

4. `NEW: ~/.config/muse/settings.json:69-85 & src/minni/wire/custom_refresh.py:173-242 · custom_refresh updates ~/.muse/mcp.json while Muse's active ~/.config/muse/settings.json mcpServers lacks minni · muse`
   - `wired.json` tracks `~/.muse/mcp.json`, whereas Muse's runtime configuration at `~/.config/muse/settings.json` only registers `typesafe`, `workbench`, and `xcode`.

5. `NEW: plugins/minni/skills/minni-install/scripts/propagate.py:59-84 · max_present_version() sorts +git.<sha> local version segments lexicographically by random hex SHA instead of commit/install time · all propagate callers`
   - `_version_sort_key` returns `(tuple(release), 1 if local else 0, local)`. Because `local` is `"git.<sha>"`, `max()` picks whichever version directory has the highest hex SHA (e.g., `git.de211faf` > `git.af0c04f7`), stamping stale versions into manifests whenever `~/.minni/plugin/current` is absent.

6. `NEW: plugins/minni/src/config.ts:526-535 · DEFAULT_WORKSPACE_ID prioritizes process.env.MINNI_WORKSPACE_ID over deriveWorkspaceIdFromCwd(), which is always populated by wire/propagate install-time stamping · all hosts`
   - Because `wire` and `propagate.py` stamp `MINNI_WORKSPACE_ID` into every host config's `env`, `process.env.MINNI_WORKSPACE_ID` is always defined, rendering the `?? deriveWorkspaceIdFromCwd()` fallback in `DEFAULT_WORKSPACE_ID` unreachable dead code.

7. `NEW: src/minni/wire/flow.py:165 & src/minni/wire/writers.py:206-207,301,327,364,680,753 · wire stamps static MINNI_WORKSPACE_ID ("workspace-minni") into .mcp.json, ~/.claude.json, kilo.json, Antigravity views, and ~/.grok/config.toml · claude-code, kilocode, grok, antigravity, gemini, codex`
   - `flow.py:165` defaults `stamp_workspace` to `~/Projects/Minni` for every non-Codex host, and `writers.py:680` (`replace_toml_sections`) preserves any previously stamped `MINNI_WORKSPACE_ID` (`workspace-<user>`) forever on Grok and Codex.

8. `NEW: plugins/minni/skills/minni-install/scripts/propagate.py:620,676,698,721,755,993,1460-1466,1584 · propagate.py defaults stamp_workspace to args.repo and overwrites MINNI_WORKSPACE_ID across all propagated configs · cursor, antigravity, gemini, claude-desktop, kilocode, grok`
   - `cmd_update_plugin` (line 1584) sets `stamp_workspace = Path(getattr(args, "workspace", None) or args.repo)` and unconditionally stamps `MINNI_WORKSPACE_ID` into `.mcp.json`, `~/.claude.json`, `kilo.json`, `gemini-extension.json`, Antigravity views, and `claude_desktop_config.json`.

9. `NEW: src/minni/wire/kilo.py:86-93 & plugins/minni/src/config.ts:589-593 · install_kilo_bridge() stamps static MINNI_KILOCODE_WORKSPACE_ID ("workspace-minni") into ~/.config/kilo/plugin/minni.js · kilocode`
   - Defeats `deriveWorkspaceIdFromCwd()` in `KILOCODE_WORKSPACE_ID` whenever a Kilo hook event payload omits `cwd`.

10. `NEW: src/minni/wire/writers.py:248,356, plugins/minni/skills/minni-install/scripts/propagate.py:658,750 & src/minni/wire/custom_refresh.py:199-205 · Initial writers stamp MCP entry["cwd"] = str(install_root) and custom_refresh.py carries existing version-root cwd forward to new_root (while ~/.local/bin/minni-mcp runs cd ~/.cursor/plugins/local/minni), corrupting deriveWorkspaceIdFromCwd() · muse, devin, antigravity, cursor, shared .mcp.json`
    - *(Corrected in Round 2 per P3-2: `custom_refresh.py:199-205` only rewrites an already-existing version-root `cwd` to `new_root`; the initial version-root `cwd` was stamped by `writers.py:248` / `propagate.py:658`, and `custom_refresh.py` perpetuates it across flips instead of deleting it.)* Setting the MCP server's working directory to the plugin installation directory causes `deriveWorkspaceIdFromCwd()` to resolve to the plugin folder (`workspace-minni` or `workspace-unknown`) instead of the user's project directory.

11. `NEW: plugins/minni/hooks/hooks.json:6-68 & plugins/minni/src/config.ts:77-80,149-229 · Non-exec hook invocations ("node ${CLAUDE_PLUGIN_ROOT}/dist/hook.js") run under /bin/sh -c so hook.js records /bin/sh's ephemeral PID while server.js checks claude's PID, causing H5 workspace-unknown fallback · claude-code, antigravity, cursor`
    - Verified root cause of `H5`: when Claude Code runs a hook command containing `${CLAUDE_PLUGIN_ROOT}`, `/bin/sh -c` forks `node` as a child of `/bin/sh`, so `hook.js` writes `active-session-claude-code-<sh_pid>.json` (e.g., PIDs `14376`, `1906`, `3721`). Meanwhile `server.js` is spawned directly by `claude` (`process.ppid == 15917`). When the duplicate plugin-spawned server (`mcp__plugin_minni_minni`, whose `cwd` is `${CLAUDE_PLUGIN_ROOT}` and lacks `MINNI_WORKSPACE_ID`) handles `minni_learn`, `active-session-claude-code-15917.json` does not exist and `deriveWorkspaceIdFromCwd()` returns `"workspace-unknown"`, with zero diagnostic logging explaining why the hint missed.

12. `NEW: src/minni/wire/claude_plugin.py:554-641 & src/minni/wire/flow.py:585-656 · follow_claude_desktop() refuses to update paths outside ~/.minni/plugin, writes version-pinned paths while Claude.app is running, and flow.py swallows all exceptions · claude-desktop`
    - Line 612 (`_is_under(a, base)`) makes `follow_claude_desktop()` a permanent no-op once Desktop points to `~/.agents/plugins/Minni@Minni/dist/server.js` or `~/.claude/plugins/cache/...`; when it does write, it writes a SHA-pinned path without checking if `Claude.app` is running.

13. `NEW: plugins/minni/skills/minni-install/scripts/propagate.py:1066-1074,1274-1275 · _native_hook_plan("cursor") returns early when minni-cursor.sh is already referenced, so --existing-only never updates or repairs ~/.cursor/hooks/minni-cursor.sh · cursor`
    - Under `make sync-root` (`--existing-only`), `deploy_cursor_wrapper()` is never reached if `hooks.json` already contains `./hooks/minni-cursor.sh`.

14. `NEW: scripts/check_deployments.py:232-363,495-500 & scripts/check_versions.py:242-375 · Verification gates skip Muse and Devin as "unsupported platform", ignore Claude Desktop, Grok hooks, Kilo bridge, Cursor MCP/hooks, ~/.gemini/commands/minni, and 3 of 4 Antigravity views, and unconditionally skip ~/.minni/plugin/current · all hosts`
    - `check_wire_launch_bindings` catches `ValueError` on `muse` and `devin` (`check_deployments.py:274-277`) and logs a non-failing note; neither checker inspects hook manifests, wrapper scripts, `claude_desktop_config.json`, or live running `server.js` processes.

15. `NEW: src/minni/minni_cli.py:254-277,606-635 & src/minni/minnid_runtime/deploy_honesty.py:127-238 · minni status ignores daemon["deploy"], and minni doctor calls deploy_status() in-process inside the short-lived CLI (making started_sha == current_sha a tautology) and exits 0 on stale fleet · minni CLI / doctor`
    - `minni doctor` cannot detect that the running `minnid` daemon is on an old commit, and does not increment `failures` or `warns` even when `plugin_dist.stale` is `True`.

### 3.2 Latent Defects

16. `NEW: src/minni/wire/platform.py:200-229 & src/minni/wire/gc.py:49-165 · default_config_scan_paths() omits ~/.grok/hooks/minni.json, ~/.grok/hooks.json, ~/.gemini/config/plugins/minni/hooks.json, ~/.muse/mcp.json, ~/.config/devin/mcp_config.json, and ~/.claude/local-marketplaces/minni, and gc.py does not inspect open FDs/realpaths of long-lived server.js processes · grok, antigravity, muse, devin, claude-code, gc`
    - Because Node resolves `import.meta.url` realpath at startup and closes the `.js` FD after ESM parsing, a long-lived `server.js` process continues importing lazy modules or reading assets from its launch `<version>` directory while `gc.py` only keeps `keep_last=2` and scans static config substrings—pruning the running server's directory on the second deploy after launch.

17. *(Retracted in Round 2 per P3-2: Round 1 claimed `src/minni/wire/platform.py:35-43` had an `AGENT_SPECS` dict using `agent_id="grok"`. In fact, `platform.py` has no `AGENT_SPECS` dict and `platform_spec("grok")` at `src/minni/wire/platform.py:169-171` already specifies `agent_id="grok-build"`. However, `~/.minni/plugin/<version>/.mcp.json` written by `writers.py:201` on the last `wire grok` call leaves `MINNI_AGENT_ID=grok-build` inside the shared payload directory, which is why §4.1 removes `.mcp.json` from the shared payload.)*

18. `NEW: src/minni/wire/flow.py:260-288 · Bulk wire (minni wire all) silently sets refresh_bridge=False when ~/.config/kilo/plugin/minni.js is missing or lacks the managed marker · kilocode`
    - Leaves KiloCode without hooks or on a stale hook script while reporting `status="wired"`.

19. `NEW: src/minni/wire/writers.py:514-539 & src/minni/wire/flow.py:290-303 · update_antigravity_config() silently skips non-symlink view files in ~/.agents/mcp-servers/views/.gemini__* and records only views_written[0] in wired.json · antigravity`
    - `~/.agents/mcp-servers/views/.gemini__*` and `~/.gemini/antigravity*/mcp_config.json` have drifted into separate regular files rather than symlinks, leaving `~/.agents/mcp-servers/views/.gemini__*` unrefreshed.

20. `NEW: scripts/update_root.sh:364-365, src/minni/fleet_sync.py:286-291, plugins/minni/skills/minni-install/scripts/propagate.py:144-155 & src/minni/wire/active_roots.py:126-147 · Fallback readers trust ~/.minni/plugin/current without verifying payload-manifest.json SHA or target containment, and install.py does not guard os.replace against a real directory at current · grok, cli, status, install`
    - Additionally, `current.legacy-dir.bak` on disk proves `~/.minni/plugin/current` was previously materialized as a real directory, which causes `os.replace(tmp_symlink, current)` to raise `IsADirectoryError` (`EISDIR`).

21. `NEW: src/minni/wire/custom_refresh.py:229 & src/minni/wire/install.py:83 · Unbounded accumulation of .minni-backup-* files in ~/.config/devin/ and ~/.muse/ and .install-<version>.lock files in ~/.minni/plugin/ · devin, muse, wire`
    - Every deploy creates new `mkstemp(prefix=f"{config_path.name}.minni-backup-")` files (46 files on disk today) and `.install-<version>.lock` files (24 files on disk today) that are never cleaned up.

22. `NEW: plugins/minni/.claude-plugin/plugin.json:18-25 & src/minni/wire/claude_plugin.py:203-216 · Payload .claude-plugin/plugin.json embeds mcpServers.minni alongside ~/.claude.json mcpServers.minni, spawning duplicate server.js processes per Claude Code session · claude-code`
    - Registers both `mcp__minni__*` and `mcp__plugin_minni_minni__*` (`Issue #5` in the operator issue ledger), doubling MCP process count and creating the unanchored `mcp__plugin_minni_minni` server that triggered `H5`.

---

## 4. Architecture & Design Decisions

### 4.1 Decision 1: Stable Launch Pointer (`~/.minni/plugin/current`) & Real Per-Host Plugin Containers

No host config file, hook manifest, wrapper script, plugin manifest, or native bridge may ever contain a SHA-pinned version segment (`0.6.0+git.<sha>`). We evaluated three options:

| Option | Description | Verdict & One-Line Reason |
|---|---|---|
| **Option A (Chosen Primary): Atomic Validated Symlink `~/.minni/plugin/current` + Real Per-Host Plugin Containers (`Minni@Minni`, `local/minni`)** | `minni wire` stages the immutable versioned directory `~/.minni/plugin/<version>/` (**without** a shared `.mcp.json`), runs pre-flip behavioral probes for every host against `<version>/dist/...`, and atomically flips `~/.minni/plugin/current -> <version>`. Direct MCP/hook hosts reference `~/.minni/plugin/current/dist/...`. Plugin-loader hosts (`~/.agents/plugins/Minni@Minni` and `~/.cursor/plugins/local/minni`) remain **real directories** containing generated thin host-specific manifests (`.mcp.json`, `gemini-extension.json`, `.cursor-plugin/plugin.json`) with **no SHA**, while their `dist/` (and read-only `skills/`, `commands/`, `hooks/`) subtrees symlink to `~/.minni/plugin/current/<subtree>` (with atomic staged copy fallback if a loader probe rejects symlinked `dist/`). | **CHOSEN:** Eliminates cross-host `.mcp.json` identity clobber (`P1-1`), avoids loader rejection of symlinked top-level plugin directories and APFS case-collision (`P1-2`), and gives zero-delta atomic cutover across all hosts without touching running host config files (`P1-5`). |
| **Option B (Rejected): Symlinking Whole Plugin Directories (`Minni@Minni -> current`, `local/minni -> current`)** | Replace `~/.agents/plugins/Minni@Minni` and `~/.cursor/plugins/local/minni` with whole-directory symlinks to `~/.minni/plugin/current`. | **REJECTED (`P1-1`, `P1-2`):** Causes both Antigravity/Gemini and Cursor loaders to read the same `.mcp.json` in `current` (clobbering `agent_id=gemini` and `agent_id=cursor` with `grok-build`), and breaks any plugin loader whose `fs.readdir(..., {withFileTypes: true})` checks `dirent.isDirectory()` (which is `false` for symlinks). |
| **Option C (Rejected as primary): Shell Wrapper Script (`~/.local/bin/minni-mcp`)** | Point host MCP configs at a shell script that resolves the newest version directory at launch time. | **REJECTED as primary:** Does not cover plugin manifests, skills, or non-MCP hooks, and non-`exec` shell wrappers corrupt `process.cwd()` or insert an extra parent PID that breaks `process.ppid` session-workspace matching. |

```mermaid
flowchart LR
  subgraph Build["1. Immutable Versioned Payload (No Shared .mcp.json)"]
    VNew["~/.minni/plugin/0.6.0+git.new/\n(payload-manifest.json + dist/ + skills/ + hooks/)"]
  end

  subgraph Pointer["2. Validated Pointer & Per-Host Real Plugin Dirs"]
    Cur["~/.minni/plugin/current\n(atomic os.replace symlink)"]
    AgDir["~/.agents/plugins/Minni@Minni/ (REAL dir)\n.mcp.json (agent=gemini, no SHA, no workspace)\ngemini-extension.json (v0.6.0, no SHA)\ndist -> ~/.minni/plugin/current/dist"]
    CuDir["~/.cursor/plugins/local/minni/ (REAL dir)\n.mcp.json (agent=cursor, no SHA, no workspace)\n.cursor-plugin/plugin.json (v0.6.0, no SHA)\ndist -> ~/.minni/plugin/current/dist"]
  end

  subgraph Hosts["3. Stable Host Configs (Zero Routine Edits After Convergence)"]
    CD["Claude Desktop (zero-delta)\naccepts Minni@Minni/dist/server.js OR current/dist/server.js"]
    CC["Claude Code\n~/.claude.json + installed_plugins.json"]
    CX["Codex\n~/.codex/config.toml + hooks.json"]
    GK["Grok-build\n~/.grok/config.toml + hooks.json"]
    AG["Gemini / Antigravity\nmcp-env-run node .../dist/server.js"]
    KL["KiloCode\nkilo.json + plugin/minni.js"]
    DV["Devin\nmcp_config.json + scripts/minni-hook.sh"]
    MU["Muse\n~/.config/muse/settings.json + ~/.muse/mcp.json"]
    CU["Cursor\n~/.cursor/mcp.json + hooks.json"]
  end

  VNew <-- "os.replace after pre-flip host probes" --- Cur
  AgDir -- "dist/ symlink" --> Cur
  CuDir -- "dist/ symlink" --> Cur
  CD --> AgDir
  AG --> AgDir
  CU --> CuDir
  CC & CX & GK & KL & DV & MU --> Cur
```

#### Rules for Shared Payload vs Per-Host Real Plugin Containers (`P1-1`, `P1-2`, `P2-6`)
1. **No `.mcp.json` in `~/.minni/plugin/<version>/`:** `src/minni/wire/writers.py:201` (`write_plugin_mcp_json`) is removed from the shared payload staging path. `~/.minni/plugin/<version>/` contains code (`dist/`, `node_modules/`), read-only assets (`skills/`, `commands/`, `hooks/`, `frontend/`), `.claude-plugin/plugin.json` (with `mcpServers` removed so Claude Code does not spawn a duplicate MCP server), and provenance manifests (`payload-manifest.json`, `dist/build-manifest.json`).
2. **Real Per-Host Plugin Directories with Thin SHA-Free Manifests:**
   - `~/.agents/plugins/Minni@Minni/` is a **real directory** containing:
     - `.mcp.json`: `{"mcpServers": {"minni": {"command": "node", "args": ["~/.agents/plugins/Minni@Minni/dist/server.js"], "env": {"MINNI_AGENT_ID": "gemini", "MINNI_VAULT_PATH": "~/.minni/gemini-vault"}}}}` (no `MINNI_WORKSPACE_ID`, no `cwd`, no SHA).
     - `gemini-extension.json` and `.gemini-plugin/gemini-extension.json`: `"version": "0.6.0"` (base semver only; **no `+git.<sha>`**), `"env": {"MINNI_AGENT_ID": "gemini", "MINNI_VAULT_PATH": "~/.minni/gemini-vault"}`.
     - Subtree symlinks: `dist -> ~/.minni/plugin/current/dist`, `node_modules -> ~/.minni/plugin/current/node_modules`, `skills -> ~/.minni/plugin/current/skills`, `commands -> ~/.minni/plugin/current/commands`, `hooks -> ~/.minni/plugin/current/hooks`.
   - `~/.cursor/plugins/local/minni/` is a **real directory** containing:
     - `.mcp.json`: `{"mcpServers": {"minni": {"command": "node", "args": ["~/.cursor/plugins/local/minni/dist/server.js"], "env": {"MINNI_AGENT_ID": "cursor", "MINNI_VAULT_PATH": "~/.minni/cursor-vault"}}}}` (no `MINNI_WORKSPACE_ID`, no `cwd`, no SHA).
     - `.cursor-plugin/plugin.json`: `"version": "0.6.0"` (base semver only; no SHA).
     - Subtree symlinks: `dist -> ~/.minni/plugin/current/dist`, `node_modules -> ~/.minni/plugin/current/node_modules`, `skills -> ~/.minni/plugin/current/skills`, `commands -> ~/.minni/plugin/current/commands`, `hooks -> ~/.minni/plugin/current/hooks`.
3. **Case-Insensitive APFS Handling (`P1-2`):**
   - On macOS case-insensitive APFS, `~/.agents/plugins/Minni@Minni` and `~/.agents/plugins/minni@minni` resolve to the **exact same inode (`st_ino = 198858508`)**.
   - Before touching any path variant, the reconciler checks `os.path.exists(lower_path) and os.path.exists(camel_path) and os.path.samestat(lower_path, camel_path)`. When `samestat` is `True`, both spellings already resolve through the kernel VFS to the same physical directory; the reconciler operates on the canonical on-disk path (`Path(camel_path).resolve()`) and **never runs `ln -s` between case variants**. Only on a case-sensitive filesystem (`not lower_path.exists()` while `camel_path.exists()`) does it create a `minni@minni -> Minni@Minni` symlink.
4. **Pre-Existing Real Directory at `~/.minni/plugin/current` (`P2-6`):**
   - Because `os.replace(tmp_symlink, current_path)` fails with `IsADirectoryError` (`EISDIR` / `EPERM`) if `current_path` is an existing real directory (evidenced by `current.legacy-dir.bak` on disk), the pointer-flip function first checks `if current_path.exists() and not current_path.is_symlink() and current_path.is_dir():` and moves it via `os.rename(current_path, archive_dir / f"current.realdir.{ts}")` before executing `os.replace(tmp_symlink, current_path)`.
5. **Strict Symlink Validation Rule (Fixes D3 & Fallback Trust):**
   - Every Python and TypeScript reader of `~/.minni/plugin/current` calls `resolve_verified_plugin_root(link_path, expected_base=~/.minni/plugin)`:
     1. `link_path.is_symlink()` is `True` AND `link_path.exists()` (`stat()` following link) is `True` (dangling symlinks fail immediately).
     2. `target = link_path.resolve(strict=True)` is a directory strictly inside `~/.minni/plugin/`.
     3. `(target / "payload-manifest.json").is_file()` and `(target / "dist" / "server.js").is_file()` hold, and `server.js` SHA-256 matches `payload-manifest.json`.
     4. `propagate.py`'s `max_present_version()` hex-SHA sort is deleted.

---

### 4.2 Decision 2: Canonical Host Registry (`~/.minni/plugin/wired.json` Schema v2) & Keyed-Subset Diff Semantics

We upgrade `~/.minni/plugin/wired.json` to **Schema v2**, backed by a static declarative `HOST_SURFACE_CATALOG` in `src/minni/wire/platform.py` covering all 28 surface groups across the fleet:

```json
{
  "schema_version": 2,
  "generation": 520,
  "active_version": "0.6.0+git.af0c04f7",
  "base_semver": "0.6.0",
  "git_commit_sha": "af0c04f7...",
  "active_root": "~/.minni/plugin/0.6.0+git.af0c04f7",
  "stable_root": "~/.minni/plugin/current",
  "updated_at": "2026-10-31T16:00:00Z",
  "hosts": {
    "claude-code": {
      "agent_id": "claude-code",
      "vault_path": "~/.minni/claudecode-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "continuous_cas",
      "lock_glob": "~/.claude.json.tmp.*",
      "surfaces": [
        {"kind": "mcp_json", "path": "~/.claude.json", "key": "mcpServers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "claude_plugin_registry", "path": "~/.claude/plugins/installed_plugins.json", "key": "plugins.minni@minni", "entry": "~/.minni/plugin/current", "tracks_version_and_sha": true},
        {"kind": "claude_marketplace", "path": "~/.claude/local-marketplaces/minni/.claude-plugin/marketplace.json", "entry": "~/.claude/local-marketplaces/minni/plugins/minni-0.6.0"}
      ]
    },
    "claude-desktop": {
      "agent_id": "claude-code",
      "vault_path": "~/.minni/claudecode-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "rewrite_while_running_and_quit_zero_delta",
      "process_match": {"bundle_id": "com.anthropic.claudefordesktop", "process_names": ["Claude"]},
      "surfaces": [
        {
          "kind": "mcp_json",
          "path": "~/Library/Application Support/Claude/claude_desktop_config.json",
          "key": "mcpServers.minni",
          "entry": "~/.minni/plugin/current/dist/server.js",
          "accepted_entries": [
            "~/.minni/plugin/current/dist/server.js",
            "~/.agents/plugins/Minni@Minni/dist/server.js"
          ]
        }
      ]
    },
    "codex": {
      "agent_id": "codex",
      "vault_path": "~/.minni/codex-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "cas_on_delta",
      "surfaces": [
        {"kind": "mcp_toml", "path": "~/.codex/config.toml", "key": "mcp_servers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "hooks_json", "path": "~/.codex/hooks.json", "entry": "~/.minni/plugin/current/dist/codex-hook.js", "use_exec": true},
        {"kind": "skill_copy", "path": "~/.codex/skills/astra-minni/SKILL.md", "entry": "~/.codex/skills/astra-minni/SKILL.md"}
      ]
    },
    "grok-build": {
      "agent_id": "grok-build",
      "vault_path": "~/.minni/grok-build-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "lock_and_cas_on_delta",
      "surfaces": [
        {"kind": "mcp_toml", "path": "~/.grok/config.toml", "key": "mcp_servers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "hooks_json", "path": "~/.grok/hooks/minni.json", "entry": "~/.minni/plugin/current/dist/grok-hook.js", "use_exec": true},
        {"kind": "hooks_json", "path": "~/.grok/hooks.json", "entry": "~/.minni/plugin/current/dist/grok-hook.js", "use_exec": true},
        {"kind": "rules_md", "path": "~/.grok/rules/minni.md", "entry": "~/.minni/plugin/current/dist/cli.js"}
      ]
    },
    "antigravity": {
      "agent_id": "gemini",
      "vault_path": "~/.minni/gemini-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "cas_on_delta",
      "surfaces": [
        {"kind": "plugin_container", "path": "~/.agents/plugins/Minni@Minni", "dist_symlink": "~/.minni/plugin/current/dist"},
        {"kind": "mcp_json", "path": "~/.gemini/settings.json", "key": "mcpServers.minni", "command": "node", "entry": "~/.agents/plugins/Minni@Minni/dist/server.js", "accepted_entries": ["~/.agents/plugins/Minni@Minni/dist/server.js", "~/.agents/plugins/minni@minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "mcp_json", "path": "~/.gemini/config/mcp_config.json", "key": "mcpServers.minni", "command": "~/.agents/bin/mcp-env-run", "args_prefix": ["node"], "entry": "~/.agents/plugins/Minni@Minni/dist/server.js", "accepted_entries": ["~/.agents/plugins/Minni@Minni/dist/server.js", "~/.agents/plugins/minni@minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "mcp_json", "path": "~/.gemini/antigravity/mcp_config.json", "key": "mcpServers.minni", "command": "~/.agents/bin/mcp-env-run", "args_prefix": ["node"], "entry": "~/.agents/plugins/Minni@Minni/dist/server.js", "accepted_entries": ["~/.agents/plugins/Minni@Minni/dist/server.js", "~/.agents/plugins/minni@minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "mcp_json", "path": "~/.gemini/antigravity-ide/mcp_config.json", "key": "mcpServers.minni", "command": "~/.agents/bin/mcp-env-run", "args_prefix": ["node"], "entry": "~/.agents/plugins/Minni@Minni/dist/server.js", "accepted_entries": ["~/.agents/plugins/Minni@Minni/dist/server.js", "~/.agents/plugins/minni@minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "mcp_json", "path": "~/.gemini/antigravity-cli/plugins/minni/mcp_config.json", "key": "mcpServers.minni", "command": "~/.agents/bin/mcp-env-run", "args_prefix": ["node"], "entry": "~/.agents/plugins/Minni@Minni/dist/server.js", "accepted_entries": ["~/.agents/plugins/Minni@Minni/dist/server.js", "~/.agents/plugins/minni@minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "mcp_view_mirrors", "paths": [
          "~/.agents/mcp-servers/views/.gemini__config__mcp_config.json.json",
          "~/.agents/mcp-servers/views/.gemini__antigravity__mcp_config.json.json",
          "~/.agents/mcp-servers/views/.gemini__antigravity-ide__mcp_config.json.json",
          "~/.agents/mcp-servers/views/.gemini__antigravity-cli__plugins__minni__mcp_config.json.json"
        ], "command": "~/.agents/bin/mcp-env-run", "args_prefix": ["node"], "entry": "~/.agents/plugins/Minni@Minni/dist/server.js"},
        {"kind": "hooks_json", "path": "~/.gemini/config/plugins/minni/hooks.json", "entry": "~/.agents/plugins/Minni@Minni/dist/gemini-hook.js", "use_exec": true},
        {"kind": "commands_dir", "path": "~/.gemini/commands/minni", "entry": "~/.minni/plugin/current/commands"}
      ]
    },
    "cursor": {
      "agent_id": "cursor",
      "vault_path": "~/.minni/cursor-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "cas_on_delta",
      "surfaces": [
        {"kind": "plugin_container", "path": "~/.cursor/plugins/local/minni", "dist_symlink": "~/.minni/plugin/current/dist"},
        {"kind": "mcp_json", "path": "~/.cursor/mcp.json", "key": "mcpServers.minni", "entry": "~/.local/bin/minni-mcp", "accepted_entries": ["~/.local/bin/minni-mcp", "~/.cursor/plugins/local/minni/dist/server.js", "~/.minni/plugin/current/dist/server.js"]},
        {"kind": "launcher_wrapper", "path": "~/.local/bin/minni-mcp", "entry": "~/.cursor/plugins/local/minni/dist/server.js", "use_exec": true},
        {"kind": "hooks_json", "path": "~/.cursor/hooks.json", "entry": "./hooks/minni-cursor.sh"},
        {"kind": "hook_wrapper", "path": "~/.cursor/hooks/minni-cursor.sh", "entry": "~/.cursor/plugins/local/minni/dist/cursor-hook.js", "use_exec": true}
      ]
    },
    "kilocode": {
      "agent_id": "kilocode",
      "vault_path": "~/.minni/kilocode-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "cas_on_delta",
      "surfaces": [
        {"kind": "mcp_json", "path": "~/.config/kilo/kilo.json", "key": "mcp.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "js_bridge", "path": "~/.config/kilo/plugin/minni.js", "entry": "~/.minni/plugin/current/dist/kilocode-hook.js"}
      ]
    },
    "devin": {
      "agent_id": "devin",
      "vault_path": "~/.minni/devin-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "cas_on_delta",
      "surfaces": [
        {"kind": "mcp_json", "path": "~/.config/devin/mcp_config.json", "key": "mcpServers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "hooks_json", "path": "~/.config/devin/config.json", "entry": "~/.config/devin/scripts/minni-hook.sh"},
        {"kind": "hook_wrapper", "path": "~/.config/devin/scripts/minni-hook.sh", "entry": "~/.minni/plugin/current/dist/hook.js", "use_exec": true}
      ]
    },
    "muse": {
      "agent_id": "muse",
      "vault_path": "~/.minni/muse-vault",
      "status": "wired",
      "rewrite_on_run_hazard": "lockfile_and_cas_on_delta",
      "lock_glob": "~/.config/muse/.settings.json.lock",
      "surfaces": [
        {"kind": "mcp_json", "path": "~/.config/muse/settings.json", "key": "mcpServers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"},
        {"kind": "mcp_json", "path": "~/.muse/mcp.json", "key": "mcpServers.minni", "command": "node", "entry": "~/.minni/plugin/current/dist/server.js"}
      ]
    }
  }
}
```

#### Keyed-Subset + Forbidden-Key Diff Semantics (`P2-3`)
To ensure hosts with optional runtime environment keys (such as Claude Desktop's `MINNI_SOCKET_PATH` and `MINNI_AFM_*`) evaluate to **`delta == 0`** on routine deploys without stripping valid operator customizations:
1. **Command & Entry Equivalence:** The surface's configured entrypoint matches `entry` or any member of `accepted_entries`, and—for Antigravity surfaces (`P2-5`)—preserves `command: "~/.agents/bin/mcp-env-run"` with `args: ["node", "<entry>"]`.
2. **Required `env` Keys (Must Equal Canonical):**
   - `MINNI_AGENT_ID == host.agent_id`
   - `MINNI_VAULT_PATH == host.vault_path`
3. **Allowed Optional `env` Keys (Ignored in Diff; Preserved Verbatim on Write):**
   - `MINNI_SOCKET_PATH` (if valid socket path string)
   - `MINNI_AFM_PROVIDER_MODE` (`"native"` | `"bridge"` | `"auto"` | `"off"`)
   - `MINNI_AFM_NATIVE_HELPER` (allowed if the target binary exists on disk; flagged as `delta != 0` only if pointing to a nonexistent file, such as Devin's broken `~/Projects/Minni/src/minni/native_afm_helper`)
   - `MINNI_PINNED_WORKSPACE_ID` (allowed when `"workspace_pinned_by_operator": true` in `wired.json`)
   - Any non-`MINNI_*` / non-`SOVEREIGN_*` environment key set by the operator.
4. **Forbidden Keys (Presence Forces `delta != 0` and Removal on Reconcile):**
   - `MINNI_WORKSPACE_ID`, `MINNI_CODEX_WORKSPACE_ID`, `MINNI_KILOCODE_WORKSPACE_ID`, `MINNI_CURSOR_WORKSPACE_ID`, `MINNI_CLAUDECODE_WORKSPACE_ID`, `SOVEREIGN_*`
   - Any `"cwd"` key on the MCP entry whose path resolves inside `~/.minni/plugin` or `~/.cursor/plugins` or `~/.agents/plugins`.

#### Keeping `installed_plugins.json` and `local-marketplaces/minni` Accurate Across Flips (`P2-4`)
- `~/.claude/plugins/installed_plugins.json` stores `installPath: "~/.minni/plugin/current"`, `version: "<base_semver>"` (e.g. `"0.6.0"`), and `gitCommitSha: "<40_char_git_sha>"`. During Step 5 of cutover, `_register_direct()` (`src/minni/wire/claude_plugin.py:183-200`) updates `version` and `gitCommitSha` whenever `HEAD` changes while keeping `installPath` at `~/.minni/plugin/current` (and preserving `lastUpdated` when `version` and `gitCommitSha` are unchanged).
- Because `sync_local_marketplace()` (`claude_plugin.py:282-312`) explicitly enforces that `~/.claude/local-marketplaces/minni/plugins/minni-<version>/` is a **real directory copy** (`if target.is_symlink(): raise ClaudePluginError`), the reconciler refreshes that stub directory copy on deploy with `.claude-plugin/plugin.json` stripped of its duplicate `mcpServers.minni` block.

---

### 4.3 Decision 3: 7-Step Atomic Cutover with Pre-Flip Probes, Automatic Rollback & Rewrite-on-Run Host Safety (`P1-4`, `P1-5`, `P2-1`)

`make sync-root` (`scripts/update_root.sh`) and `minni sync` (`src/minni/fleet_sync.py`) execute a 7-step atomic cutover in Python (`minni wire reconcile-all --from-repo <repo>`):

```mermaid
sequenceDiagram
    participant Op as Operator / make sync-root
    participant Rec as Reconciler (wire reconcile-all)
    participant FS as ~/.minni/plugin/ & Containers
    participant Host as Host Configs
    participant Probe as Behavioral Verifier (timeout 20)

    Op->>Rec: Run deploy (HEAD = <ver>+git.<sha>)
    Rec->>FS: 1. Stage ~/.minni/plugin/<ver>+git.<sha>/ (no .mcp.json) & verify SHA-256 manifests
    Rec->>Probe: 2. Pre-flip probes: test EVERY host's exact command+env against <ver>+git.<sha>/dist/...
    alt Any pre-flip host probe fails or times out (>20s)
        Probe-->>Op: ABORT BEFORE FLIP (exit 1): current & host configs untouched
    end
    Rec->>FS: 3. Record prev_target = readlink(current); archive real-dir current if present
    Rec->>FS: 4. Atomic flip: os.replace(tmp_symlink, ~/.minni/plugin/current -> <ver>+git.<sha>)
    Rec->>FS: Ensure Minni@Minni/dist & local/minni/dist -> ~/.minni/plugin/current/dist
    Rec->>Host: 5. Diff all surfaces (Keyed-Subset semantics). Snapshot & CAS-write only delta != 0 surfaces
    Rec->>Probe: 6. Post-flip probes: test EVERY host's live stable entrypoint + inspect running server.js PIDs
    alt Any Step 5 write or Step 6 post-flip probe fails
        Rec->>FS: 7a. AUTOMATIC ROLLBACK: os.replace(current -> prev_target) & restore config snapshots
        Rec-->>Op: FAIL-CLOSED (exit 1): Rolled back to <prev_target>; name failing surface
    else All post-flip probes pass
        Rec->>FS: 7b. Safe GC (protect current, wired, open FDs/lsof, flock, process lstart floor, keep_last)
        Rec-->>Op: DEPLOY GREEN + running_stale_servers report (host, PID, loaded SHA -> target SHA)
    end
```

#### Numbered Atomic Cutover Sequence (`P1-4`)
1. **Stage & Verify Immutable Payload:** Stage `~/.minni/plugin/<ver>+git.<sha>/` under `~/.minni/plugin/.install.lock` (without a shared `.mcp.json`) and verify `payload-manifest.json` + `dist/build-manifest.json` SHA-256 digests.
2. **Pre-Flip Behavioral Probes Against `<new_ver>` (Every Host's Exact Launcher & Env):** For every host in `wired.json` v2, run a `timeout 20` MCP `initialize` probe (and hook syntax/smoke check) in a scratch `mkdtemp` using that host's **exact configured launcher (`~/.agents/bin/mcp-env-run`, `node`, etc.) and exact `env`**, substituting `~/.minni/plugin/<ver>+git.<sha>/dist/...` as the script target. If **any** host fails pre-flip verification, abort immediately (`exit 1`) before touching `~/.minni/plugin/current` or any host config.
3. **Record Rollback State & Handle Real-Dir `current` (`P2-6`):** Record `prev_target = os.readlink(~/.minni/plugin/current)` if `current` is a symlink. If `~/.minni/plugin/current` is a real directory, move it to `~/.minni/archive/<ts>/current.dir/` via `os.rename`.
4. **Atomic Pointer Flip:** Create `~/.minni/plugin/.current.tmp.<pid> -> <ver>+git.<sha>` and execute `os.replace(tmp, ~/.minni/plugin/current)`. Verify `Minni@Minni/dist` and `local/minni/dist` resolve through `~/.minni/plugin/current/dist`.
5. **Zero-Delta Diff & Lock/CAS-Guarded Surface Reconcile:** Diff every surface in `HOST_SURFACE_CATALOG` using Keyed-Subset + Forbidden-Key semantics.
   - Surfaces with `delta == 0` (which is **all** MCP and hook configs on routine deploys once converged, including `claude-desktop`) are **skipped with zero file writes**.
   - Surfaces with `delta != 0` record an in-memory pre-write byte snapshot and apply the Rewrite-on-Run Host Protection Protocol below.
6. **Post-Flip Behavioral Probes (Configs + Live Running Processes):**
   - Spawn each host's actual configured command (`~/.minni/plugin/current/dist/server.js`, `~/.agents/plugins/Minni@Minni/dist/server.js`, `~/.local/bin/minni-mcp`) under `timeout 20`, send MCP `initialize`, and verify `serverInfo.version == "0.6.0"` and resolved `build-manifest.json` `git_sha == <sha>`.
   - Inspect all currently running `server.js` processes on the machine (`P1-3`): verify each running server's resolved `version_root` still exists on disk and emit the `running_stale_servers` table naming `(host, pid, ppid, loaded_sha, target_sha)`.
7. **Automatic Rollback on Any Failure (`P1-4`):**
   - If any Step 5 write or Step 6 post-flip probe fails, atomically restore `~/.minni/plugin/current -> prev_target` via `os.replace`, restore every config mutated in Step 5 from its byte snapshot, and exit non-zero (`exit 1`) naming the failing host surface and rollback target.
   - Reruns are strictly idempotent.

#### Rewrite-on-Run Host Protection Protocol (`P1-5`, `P2-1`)
1. **Claude Desktop (`claude_desktop_config.json`) — Zero-Delta Host (`P1-5`):**
   - `Claude.app` rewrites `claude_desktop_config.json` **both while running** (`main.log`: `"Config file written"` at `10:39:49` and `10:40:23`) **and on quit**, and `Claude.app` is the parent process tree (`Claude.app -> disclaimer -> claude`) of active Claude Code sessions.
   - Because `claude-desktop`'s `accepted_entries` includes both `~/.agents/plugins/Minni@Minni/dist/server.js` (today's live path, which resolves through `Minni@Minni/dist -> ~/.minni/plugin/current/dist/server.js`) and `~/.minni/plugin/current/dist/server.js`, and Keyed-Subset semantics allow `MINNI_SOCKET_PATH` and `MINNI_AFM_*`, **today's live `claude_desktop_config.json` already has `delta == 0`**.
   - Routine deploys (`make sync-root`, `minni sync`) **never write `claude_desktop_config.json`** and never ask the operator to quit `Claude.app`.
   - If `claude_desktop_config.json` ever has `delta != 0` while `Claude.app` is running, `reconcile-all` does **not** mutate `claude_desktop_config.json`; if the current path still resolves to the new build via `Minni@Minni/dist`, it logs `[WARN] claude-desktop config has non-canonical env; run 'minni wire claude-desktop --migrate-config' from an external terminal after quitting Claude.app`. Only if Desktop's entrypoint is broken/stale does it fail closed.
2. **Continuous/Locked Rewrite Hosts (`~/.claude.json`, `~/.config/muse/settings.json`, `~/.grok/config.toml`, `~/.config/devin/*`, `~/.cursor/mcp.json`, Antigravity `mcp_config.json`) (`P2-1`):**
   - **Primary Defense (Zero-Delta):** Once converged to `~/.minni/plugin/current/dist/...` (or `Minni@Minni/dist/...` / `local/minni/dist/...`), `delta == 0` on every deploy, so zero writes occur while these hosts run.
   - **When `delta != 0` (Initial Migration or Drift Repair):**
     - **Lockfile Check:** Refuse to write if an active host lock or temp file exists (`~/.config/muse/.settings.json.lock`, `~/.claude.json.tmp.*`, `~/.grok/.config.toml.lock`), retrying with backoff up to 2 s before failing closed.
     - **Running-Host Guard for In-Memory Overwriters:** For hosts that hold the entire config in memory and overwrite on state changes (`muse` daemon, `grok` CLI session, `devin` CLI session), if `pgrep` detects a running process holding the config open (`lsof <config_path>`) while `delta != 0`, refuse blind mutation unless `--force-cas` / `--migrate-config` is passed when the host is quiescent.
     - **Optimistic Compare-and-Swap (CAS) for `~/.claude.json` and JSON/TOML Configs:** Record `pre_stat = (st_ino, st_mtime_ns, sha256(raw_bytes))`, stage `<path>.minni-tmp.<pid>`, re-verify `stat` + `sha256` is identical and no `.tmp.*` sibling exists immediately before `os.replace`, and re-read 100 ms after `os.replace` to confirm the written bytes were not clobbered by a concurrent flush.

---

### 4.4 Decision 4: Idempotent Deploys & Archive-First Backup/Lock Hygiene (`P3-1`)

1. **Read-Compare-Skip Before Write:** Every config writer parses the existing file, computes the target AST/JSON/TOML under Keyed-Subset semantics, and returns `changed=False` without touching disk if `delta == 0`.
2. **Archive-First Before Any Mutation or Removal (`P3-1`):** Before modifying any host config (`delta != 0`) or removing any legacy artifact, copy/move the prior state into `~/.minni/archive/<YYYYMMDD-HHMMSS>/<host>/` (replacing the unbounded `tempfile.mkstemp(prefix=f"{config_path.name}.minni-backup-")` leak at `custom_refresh.py:229`).
3. **Single Lock File:** Use a single `~/.minni/plugin/.install.lock` file instead of per-version `.install-<version>.lock` files (`install.py:83`), archiving the 24 existing `.install-*.lock` files to `~/.minni/archive/<ts>/backups-and-locks/`.

---

### 4.5 Decision 5: Retention & Safe Garbage Collection vs Long-Lived Servers (`P1-3`)

Node resolves `import.meta.url` to the realpath `~/.minni/plugin/<ver>/dist/server.js` at launch and closes the `.js` file descriptor immediately after parsing ESM (`lsof` on live PID `15929` shows zero open FDs under `~/.minni/plugin/` when `cwd` is `$HOME`), while `ps` `argv` shows `~/.minni/plugin/current/dist/server.js`. Therefore, a naive `ps` substring scan and `keep_last=2` fail to protect a long-lived server across 2+ deploy flips.

We harden `server.ts` and `collect_garbage()` (`src/minni/wire/gc.py`) with four interlocking protections:
1. **Kernel FD Pin + Shared Flock + Runtime Lease in `server.ts` (`P1-3`):**
   - At the top of `plugins/minni/src/server.ts`, resolve:
     ```ts
     const selfRealpath = fs.realpathSync(fileURLToPath(import.meta.url));
     const versionRoot = path.dirname(path.dirname(selfRealpath));
     ```
   - Open and retain a read-only file descriptor `const pinFd = fs.openSync(selfRealpath, "r")` for the entire lifetime of the process (stored on `globalThis.__minniSelfPinFd`), and acquire a shared non-blocking kernel lock `flock(pinFd, LOCK_SH | LOCK_NB)` (plus `<versionRoot>/.active-server.lock`).
   - Because `pinFd` remains open in the macOS kernel file descriptor table until the process exits (even on `SIGKILL`), `lsof -Fn -p <pid>` **deterministically lists `~/.minni/plugin/<ver>/dist/server.js`** for every live server process regardless of `argv` or `cwd`!
   - Also write `~/.minni/run/mcp-servers/<pid>.json` containing `{pid, ppid, agent_id, realpath: selfRealpath, version_root: versionRoot, git_sha, started_at}` (unlinked on `exit`/`SIGTERM`/`SIGINT`).
2. **Pre-Upgrade Process Start-Time Floor in `gc.py`:**
   - For any pre-upgrade `server.js` process started before the `pinFd` change (where `lsof` only shows `cwd = $HOME`), `gc.py` queries `ps -o pid=,lstart= -p <server_pids>` and protects **every** version directory in `~/.minni/plugin/` whose install timestamp (`payload-manifest.json` `built_at` / directory `mtime`) is at or before the oldest running unpinned `server.js` process's start time (plus the version immediately preceding each subsequent flip).
3. **Full Protected Set in `collect_garbage()`:**
   - A version directory `~/.minni/plugin/<ver>` is **PROTECTED** if it matches ANY of:
     - `os.readlink(~/.minni/plugin/current)` (and `prev_target` rollback target);
     - Any version directory referenced by an open FD or `cwd` in `lsof -Fn -p <all_node_pids>`;
     - Any version directory where a non-blocking exclusive flock probe on `<ver>/dist/server.js` or `<ver>/.active-server.lock` returns `EWOULDBLOCK`;
     - Any version directory recorded in a live `~/.minni/run/mcp-servers/<pid>.json` lease where `os.kill(pid, 0)` succeeds;
     - Any version directory protected by the pre-upgrade process `lstart` floor;
     - Any path substring matched across the full `HOST_SURFACE_CATALOG`;
     - The `keep_last = 2` most recently built version directories sorted by `payload-manifest.json` `built_at` timestamp (never hex SHA).
4. **Deploy Report of `running_stale_servers`:**
   - At the end of every deploy (`reconcile-all`), `minni status`, and `minni doctor`, Minni enumerates all live `server.js` PIDs, resolves each PID's loaded `version_root` via `lsof` / lease file and its parent host (`ppid` -> process name), and prints:
     ```text
     RUNNING STALE SERVERS (protected from GC until host reloads MCP):
       - host=claude-code (parent=claude pid=15917) server_pid=15929 loaded=0.6.0+git.af0c04f7 target=0.6.0+git.<new_sha>
       - host=claude-desktop (parent=Claude pid=15673) server_pid=76961 loaded=0.6.0+git.af0c04f7 target=0.6.0+git.<new_sha>
     ```

---

### 4.6 Decision 6: Dynamic Per-Session Identity, `exec`-Only Wrappers, H5 Diagnostics & H4/H6 Boundaries (`P1-6`, `P2-2`, `P3-2`)

1. **Forbid Install-Time `MINNI_WORKSPACE_ID` Stamping:**
   - Remove `MINNI_WORKSPACE_ID`, `MINNI_CODEX_WORKSPACE_ID`, `MINNI_KILOCODE_WORKSPACE_ID`, `MINNI_CURSOR_WORKSPACE_ID`, and `MINNI_CLAUDECODE_WORKSPACE_ID` from all config writers (`writers.py`, `kilo.py`, `custom_refresh.py`, `propagate.py`, `minni-mcp`, `minni-cursor.sh`, `minni-hook.sh`).
   - During reconcile, any existing `MINNI_WORKSPACE_ID` or `MINNI_<HOST>_WORKSPACE_ID` is **stripped** unless the host entry in `wired.json` carries `"workspace_pinned_by_operator": true` (which sets `MINNI_PINNED_WORKSPACE_ID`, never `MINNI_WORKSPACE_ID`).
2. **Never Stamp or Carry Forward a Plugin-Root `cwd` (`P3-2`):**
   - Remove `"cwd": str(install_root)` from `writers.py:248,356` and `propagate.py:658,750`, update `custom_refresh.py:199-205` to **delete** `entry["cwd"]` whenever an existing `cwd` points inside `~/.minni/plugin` (rather than rewriting it to `new_root`), and remove `cd "$HOME/.cursor/plugins/local/minni"` from `~/.local/bin/minni-mcp` and `cd "$project_dir"` from `~/.config/devin/scripts/minni-hook.sh`.
3. **Replace the Multi-Hop `ps` Walk with Zero-Intermediate-PID `exec` Enforcement (`P2-2`):**
   - *Why the Round-1 4-hop `ps` walk is rejected:* Spawning `ps` up to 4 times at startup adds subprocess latency, breaks if an intermediate wrapper exits (`ppid` reparents to `PID 1`), can false-match a shared terminal/IDE ancestor (`Terminal.app`, `tmux`, `Claude.app`) across unrelated sessions within the 30-min TTL, and is vulnerable to OS PID reuse.
   - *Sound Replacement:*
     1. **Mandatory `exec` in all wrappers and hook commands:**
        - `~/.agents/bin/mcp-env-run` already uses `os.execvpe(command, args, os.environ)`.
        - `~/.local/bin/minni-mcp`, `~/.cursor/hooks/minni-cursor.sh`, and `~/.config/devin/scripts/minni-hook.sh` use `exec node ...`.
        - All `hooks.json` files (`plugins/minni/hooks/hooks.json`, `~/.codex/hooks.json`, `~/.grok/hooks/minni.json`, `~/.grok/hooks.json`, `~/.gemini/config/plugins/minni/hooks.json`) prefix commands with `exec node ...` (e.g., `"command": "exec node ${CLAUDE_PLUGIN_ROOT}/dist/hook.js UserPromptSubmit"`). Under POSIX `/bin/sh -c "exec node ..."`, `/bin/sh` replaces its own process image with `node` without forking, so `process.ppid` inside `hook.js` is **always** the host binary PID (`claude`, `codex`, `grok`, `agy`), never an ephemeral `/bin/sh` child!
     2. **Liveness & Clock-Skew Verification on `active-session-<agent>-<hostPid>.json`:**
        - Before trusting `active-session-<agent>-<hostPid>.json` (where `hostPid = Number(process.env.MINNI_HOST_PID) || process.ppid`), `resolveSessionWorkspaceId()` checks `process.kill(hostPid, 0)` (confirming `hostPid` is still alive), verifies `age_ms <= 30 * 60 * 1000`, and verifies `updated_at <= Date.now() + 5000`.
4. **Runtime Workspace Resolution Order (`P1-6` — No `roots/list`, Operator Pin First):**
   - Because `plugins/minni/tests/mcp-protocol-version.test.mjs:110-118` forbids `roots/list` and `.listRoots(` under MCP `2026-07-28`, MCP Roots is **not used**.
   - `resolveSessionWorkspaceId()` resolves in this strict priority order:
     1. **Explicit tool call argument:** `args.workspaceId` / `args.workspace_id` when passed by the caller.
     2. **Operator explicit pin (`process.env.MINNI_PINNED_WORKSPACE_ID`):** Checked **FIRST** before any session hook file or `cwd` heuristic when set via `--pin-workspace <id>`.
     3. **Active session hook hint (`.runtime/active-session-<agent>-<hostPid>.json`):** Validated against `agent_id`, `process.kill(hostPid, 0)` liveness, and 30-min TTL.
     4. **Project root from `deriveWorkspaceIdFromCwd(process.cwd())`:** Walks up from `process.cwd()` looking for `.git`, stopping strictly before `$HOME`, `~/.minni`, `~/.agents`, `~/.cursor`, and `~/.claude`. If no `.git` ancestor exists (e.g., `process.cwd()` is `$HOME` or `/` or `~/.minni/plugin/...`), returns `undefined` (never `"workspace-<operator>"` or `"workspace-minni"`). Note: `findProjectRoot()` in `hook-utils.ts:167-178` is also fixed to return `undefined` when `curr === home` has no `.git`, instead of returning `base = $HOME`.
     5. **Fallback:** `"workspace-unknown"`.
5. **Structured Diagnostic Logging for `H5` (`P2-2`):**
   - Whenever `resolveSessionWorkspaceId()` misses step 3 (session hint) or resolves to `"workspace-unknown"`, it logs a structured JSON diagnostic line to stderr and attaches `workspace_resolution_diag` to the tool call's audit record:
     ```json
     {
       "resolved_workspace_id": "workspace-unknown",
       "resolved_via": "fallback_unknown",
       "server_pid": 15933,
       "server_ppid": 15917,
       "expected_hint_file": "active-session-claude-code-15917.json",
       "hint_miss_reason": "hint_file_missing",
       "fresh_hint_pids_in_dir": [14376, 1906, 3721],
       "cwd": "~/.minni/plugin/0.6.0+git.af0c04f7"
     }
     ```
   - Supported `hint_miss_reason` enum values: `hint_file_missing`, `hint_pid_not_alive`, `hint_agent_mismatch`, `hint_expired_ttl`, `hint_clock_skew`, `hint_workspace_invalid`, `hint_parse_error`.
6. **Explicit Scope Statement for `H4` and `H6` (`P2-2`):**
   - **What this design DOES solve:**
     - Eliminates duplicate MCP server instances inside a single Claude Code session (`mcp__plugin_minni_minni` vs `mcp__minni`, fixing Issue #5 in `minni-issues-2026-10-31.md` by removing `mcpServers` from `.claude-plugin/plugin.json`).
     - Eliminates install-time `MINNI_WORKSPACE_ID` static pinning across all hosts (`H2`).
     - Fixes the `/bin/sh -c` hook `ppid` mismatch (`exec node ...` in `hooks.json`) and `$HOME` non-git root fallback in `findProjectRoot()`, and adds structured diagnostic telemetry for `H5`.
     - Converges all host surfaces to the same deployed build (`H1`, `H3`).
   - **What this design DOES NOT solve (deferred to the dedicated `H6` Multi-Session Principal Spike):**
     - **`H4` (One Active Plan Per Vault):** When two concurrent sessions of the same principal (e.g., two `claude-code` terminals) run in parallel and Session B calls `minni_thread_create`, the daemon's vault-scoped thread table deactivates Session A's active thread (`UPDATE plan_threads SET status='paused' WHERE status='active'`).
     - **`H6` (Multi-Session Principal Collisions):** Concurrent sessions sharing the same `agent_id` still share vault-global `.runtime/recall-state.json` (`session_turn` counter and `current_topic`), `agent_id`-keyed thread relay cursors, and single-active-thread pointers.
   - **Non-Contradiction Guarantee for `H6`:** Nothing in `wired.json` v2, `~/.minni/plugin/current`, or the runtime workspace resolver assumes a 1:1 mapping between `agent_id` and a single live OS process. Runtime server leases (`~/.minni/run/mcp-servers/<pid>.json`) are keyed by `pid`, session hints (`active-session-<agent>-<hostPid>.json`) are keyed by `(agent_id, host_pid)` and carry `session_id` when supplied by the host hook, and host configs carry no static session or workspace pins. Consequently, the `H6` spike can partition `.runtime/` state, relay cursors, and active threads by `(agent_id, session_id)` or `(agent_id, workspace_id)` without altering host wiring or launch configs.

---

### 4.7 Decision 7: Unified Drift Health Check & Canonical `.minni/plugin` Scanner (`P1-3`, `P2-7`)

All four health surfaces (`scripts/check_deployments.py`, `scripts/check_versions.py`, `minni status`, `minni doctor`) share `src/minni/wire/health.py` driven by `HOST_SURFACE_CATALOG`:
1. **Complete Catalog Inspection:** Inspects all 28 surface groups in `HOST_SURFACE_CATALOG`, including `claude-desktop`, `muse` (`settings.json` + `~/.muse/mcp.json`), `devin` (`mcp_config.json` + `config.json` + `minni-hook.sh`), `grok` (`config.toml` + `hooks/minni.json` + `hooks.json` + `rules/minni.md`), `kilocode` (`kilo.json` + `plugin/minni.js`), `cursor` (`mcp.json` + `minni-mcp` + `hooks.json` + `minni-cursor.sh` + `local/minni`), `antigravity` (`Minni@Minni` + `gemini/settings.json` + all 4 `mcp_config.json` files + `~/.agents/mcp-servers/views/.gemini__*` + `hooks.json` + `~/.gemini/commands/minni`), and skill copies (`~/.codex/skills/astra-minni`).
2. **Canonical Uncatalogued `.minni/plugin` Drift Scanner (`P2-7`):**
   - `scan_for_uncatalogued_plugin_refs()` walks all host configuration roots (`~/.claude*`, `~/Library/Application Support/Claude`, `~/.codex`, `~/.gemini` excluding session logs `brain/` and `tmp/`, `~/.agents`, `~/.grok`, `~/.cursor`, `~/.config/{kilo,devin,muse}`, `~/.muse`, `~/.local/bin`) for any config/script file containing `".minni/plugin"` (or `"Minni@Minni"` / `"minni@minni"` / `"sovereign-memory"`).
   - Any file matching `".minni/plugin"` that is **not** registered in `HOST_SURFACE_CATALOG`—or any registered file containing a version-pinned `.minni/plugin/<ver>+git.<sha>` path—fails the check with `UNCATALOGUED_OR_VERSIONED_BINDING`.
3. **Live Running Server Inspection (`P1-3`):**
   - Enumerates all live `server.js` processes via `ps` + `lsof` + `~/.minni/run/mcp-servers/*.json`, verifies each process's `version_root` exists on disk (failing `RED` if a running server's directory was deleted), and reports any `running_stale_servers` naming `(host, pid, loaded_sha, target_sha)`.
4. **`minni status` & `minni doctor` Daemon Honesty:**
   - `minni status` prints `daemon["deploy"]` and `running_stale_servers`.
   - `minni doctor` reads `status["daemon"]["deploy"]` directly from the running `minnid` RPC response (instead of calling `deploy_status()` in-process inside the CLI) and exits non-zero whenever `daemon` lags `HEAD`, `~/.minni/plugin/current` is missing/invalid, or any host surface fails verification.

---

## 5. Migration Plan (Archive-First, For Coordinator Execution)

> **NOTE:** Per spike rules, zero live host files were modified during this research round. Every removal or replacement step below is **ARCHIVE-FIRST** into `ARCHIVE_DIR=~/.minni/archive/$(date -u +%Y%m%d-%H%M%S)`.

1. **Step 1 — Create Timestamped Archive Root & Archive Legacy Backups, Locks, and `current.legacy-dir.bak` (`ARCHIVE-FIRST`, `P2-6`, `P3-1`):**
   - Create `ARCHIVE_DIR=~/.minni/archive/<YYYYMMDD-HHMMSS>/`.
   - **ARCHIVE-FIRST:** Move all 23 `~/.config/devin/mcp_config.json.minni-backup-*` files, all 23 `~/.muse/mcp.json.minni-backup-*` files, all 24 `~/.minni/plugin/.install-*.lock` files, and `~/.minni/plugin/current.legacy-dir.bak` (plus `~/.minni/plugin/current` if it is a real directory) into `$ARCHIVE_DIR/backups-and-locks/`.
   - Note (`P1-5`): Do **not** quit `Claude.app` during migration; `claude_desktop_config.json` already points to `~/.agents/plugins/Minni@Minni/dist/server.js` with no `MINNI_WORKSPACE_ID` (`delta == 0`), so converging `Minni@Minni/dist -> ~/.minni/plugin/current/dist` updates Claude Desktop with zero edits to `claude_desktop_config.json`.
2. **Step 2 — Stage Canonical Payload, Flip `~/.minni/plugin/current`, and Run Pre-Conversion Per-Host Loader Probe (`ARCHIVE-FIRST`, `P1-2`, `P1-4`):**
   - Stage `~/.minni/plugin/<version>+git.<sha>/` (with no shared `.mcp.json` and no duplicate `mcpServers.minni` in `.claude-plugin/plugin.json`), run pre-flip `timeout 20` MCP `initialize` probes for every host's exact `env`, and atomically flip `~/.minni/plugin/current -> ~/.minni/plugin/<version>+git.<sha>`.
   - **Per-Host Loader Probe (`P1-2`):** Before converting `~/.agents/plugins/Minni@Minni/dist` or `~/.cursor/plugins/local/minni/dist` to symlinks, back up both directories to `$ARCHIVE_DIR/pre-probe-containers/` (`ARCHIVE-FIRST`), link `dist -> ~/.minni/plugin/current/dist`, and execute `timeout 20` loader/MCP probes (`~/.agents/bin/mcp-env-run node ~/.agents/plugins/Minni@Minni/dist/server.js` + `gemini` extension discovery + `~/.local/bin/minni-mcp` + `~/.cursor/hooks/minni-cursor.sh`). If both loaders follow `dist -> ~/.minni/plugin/current/dist` (expected), keep the symlinked `dist/`; if either loader rejects a symlinked `dist/`, fall back to atomic staged directory materialization (`stage temp -> copy dist/ -> os.replace`).
3. **Step 3 — Converge Real Per-Host Plugin Containers (`~/.agents/plugins/Minni@Minni` and `~/.cursor/plugins/local/minni`) (`ARCHIVE-FIRST`, `P1-1`, `P1-2`, `P3-1`):**
   - **ARCHIVE-FIRST:** Move `~/.agents/plugins/Minni@Minni/node_modules` (worktree symlink), old `dist/`, and SHA-pinned `gemini-extension.json` (`0.6.0+git.6feffaa3`) into `$ARCHIVE_DIR/agents-Minni@Minni/`. Move `~/.cursor/plugins/local/minni/node_modules` (real directory with 269 entries) and old `dist/` into `$ARCHIVE_DIR/cursor-local-minni/`.
   - Keep `~/.agents/plugins/Minni@Minni` (recognizing `minni@minni` as the same APFS inode `198858508` via `os.path.samestat` — no sibling symlink created) and `~/.cursor/plugins/local/minni` as **real directories**.
   - Write thin SHA-free host-specific manifests inside each real directory (`Minni@Minni/.mcp.json` with `MINNI_AGENT_ID=gemini`, `Minni@Minni/gemini-extension.json` with `"version": "0.6.0"`, `local/minni/.mcp.json` with `MINNI_AGENT_ID=cursor`, `local/minni/.cursor-plugin/plugin.json` with `"version": "0.6.0"`—all with `MINNI_WORKSPACE_ID` and `cwd` omitted) and symlink `dist`, `node_modules`, `skills`, `commands`, and `hooks` into `~/.minni/plugin/current/<subtree>`.
4. **Step 4 — Archive & Retire Orphaned `~/.gemini/extensions/minni` and Repair Broken Hook/Wrapper Surfaces (`ARCHIVE-FIRST`, `P3-1`):**
   - **ARCHIVE-FIRST:** Move the entire orphaned `~/.gemini/extensions/minni` directory (`0.3.0`/`0.5.0` server + broken `sovereign.ts`) into `$ARCHIVE_DIR/gemini-extensions-minni/`.
   - **ARCHIVE-FIRST:** Copy `~/.grok/hooks.json`, `~/.grok/hooks/minni.json`, `~/.config/devin/config.json`, `~/.config/devin/scripts/minni-hook.sh`, `~/.local/bin/minni-mcp`, `~/.cursor/hooks/minni-cursor.sh`, and `~/.gemini/commands/minni/` into `$ARCHIVE_DIR/hooks-and-wrappers/` before updating:
     - Converge both `~/.grok/hooks.json` and `~/.grok/hooks/minni.json` to `exec node ~/.minni/plugin/current/dist/grok-hook.js <Event>`.
     - Update `~/.config/devin/scripts/minni-hook.sh` to `exec /opt/homebrew/bin/node ~/.minni/plugin/current/dist/hook.js "$1"`, point `MINNI_AFM_NATIVE_HELPER` to `~/.minni/bin/native_afm_helper` (or omit if absent), and remove the hardcoded `playbook-engineering` `project_dir` / `MINNI_CLAUDECODE_WORKSPACE_ID` fallback.
     - Update `~/.local/bin/minni-mcp` and `~/.cursor/hooks/minni-cursor.sh` to use `exec node ...`, removing `cd "$HOME/.cursor/plugins/local/minni"` and removing `MINNI_WORKSPACE_ID="${MINNI_WORKSPACE_ID:-workspace-...}"`.
     - Refresh `~/.gemini/commands/minni/*.toml` from `~/.minni/plugin/current/commands/`.
5. **Step 5 — Reconcile Host Configs (`delta != 0` Surfaces Only) with Archive-First Snapshots & CAS (`ARCHIVE-FIRST`, `P2-1`, `P2-4`, `P2-5`, `P2-7`):**
   - **ARCHIVE-FIRST:** Copy every host config with `delta != 0` (`~/.claude.json`, `~/.claude/plugins/installed_plugins.json`, `~/.codex/config.toml`, `~/.codex/hooks.json`, `~/.grok/config.toml`, `~/.gemini/settings.json`, all 4 Antigravity `mcp_config.json` files, `~/.agents/mcp-servers/views/.gemini__*`, `~/.gemini/config/plugins/minni/hooks.json`, `~/.config/kilo/kilo.json`, `~/.config/kilo/plugin/minni.js`, `~/.config/devin/mcp_config.json`, `~/.config/muse/settings.json`, `~/.muse/mcp.json`) into `$ARCHIVE_DIR/host-configs/` before CAS write.
   - Reconcile each file to stable paths (`~/.minni/plugin/current/dist/...` or `Minni@Minni/dist/...` preserving `~/.agents/bin/mcp-env-run` on Antigravity), strip static `MINNI_WORKSPACE_ID` / `MINNI_KILOCODE_WORKSPACE_ID` and version-root `"cwd"`, register `mcpServers.minni` in `~/.config/muse/settings.json` (respecting `.settings.json.lock`), update `installPath: "~/.minni/plugin/current"` + `version` + `gitCommitSha` in `installed_plugins.json`, and refresh `~/.claude/local-marketplaces/minni/plugins/minni-0.6.0/`.
6. **Step 6 — Post-Flip Behavioral Verification & Rollback Check (`P1-3`, `P1-4`, `P2-7`):**
   - Run `python3 scripts/check_deployments.py --strict` (including the canonical `.minni/plugin` uncatalogued-file scan and `timeout 20` behavioral `initialize` probes across all hosts), `python3 scripts/check_versions.py`, and `minni doctor`.
   - Inspect the `running_stale_servers` output to confirm every pre-existing `server.js` PID still has its backing `<version_root>` intact on disk.

---

## 6. Regression Test Plan (RED-Before-GREEN & Call-Site Pinning)

Every test below must fail (`RED`) on `af0c04f7` before the implementation fix and pass (`GREEN`) after:

| Defect / Invariant | Test File | RED Assertion on `af0c04f7` -> GREEN Behavior |
|---|---|---|
| **P1-1 / P1-2: Per-host real plugin containers, no shared `.mcp.json`, APFS `samestat` guard** | `tests/test_plugin_containers_and_apfs.py` | `test_shared_payload_omits_mcp_json_and_host_containers_keep_distinct_principals`: Verify `install_plugin_payload()` never writes `.mcp.json` into `~/.minni/plugin/<ver>/`, `Minni@Minni/.mcp.json` (`agent=gemini`) and `local/minni/.mcp.json` (`agent=cursor`) remain distinct real directories with zero git SHAs and `dist -> ~/.minni/plugin/current/dist`, and `samestat(Minni@Minni, minni@minni)` skips alias symlink creation on case-insensitive APFS. |
| **P1-3: GC protection of long-lived `server.js` across 3+ flips & `running_stale_servers` report** | `tests/test_gc_long_lived_servers.py` | `test_gc_protects_running_server_across_three_flips_and_reports_stale_pid`: Spawn a real `server.js` from `v1`, then stage and flip `current` through `v2`, `v3`, and `v4` with `keep_last=2` and run `collect_garbage()`. On `af0c04f7`, `v1` is deleted while the `v1` server is still running! Post-fix: `v1` is retained via open FD / `lsof` / flock / `lstart` floor until the `v1` PID exits, and `reconcile-all` reports `v1` in `running_stale_servers` naming host and PID. |
| **P1-4 / P2-6: 7-Step Atomic cutover, pre-flip probe abort, post-flip rollback, real-dir `current`** | `tests/test_atomic_cutover_and_rollback.py` | `test_cutover_aborts_before_flip_on_preflip_failure_and_reverts_current_on_postflip_failure`: (1) Inject pre-flip failure on one host env; assert `current` is never flipped. (2) Inject post-flip failure on one host; assert `current` automatically reverts to `prev_target` and mutated host configs are restored from snapshots. (3) Seed a real directory at `~/.minni/plugin/current`; assert it is archived to `~/.minni/archive/` before `os.replace` succeeds. |
| **P1-5 / P2-3: Claude Desktop zero-delta semantics & no deployer self-kill** | `tests/test_wire_claude_desktop.py` | `test_claude_desktop_accepts_minni_at_minni_and_subset_env_with_zero_writes`: Seed `claude_desktop_config.json` with today's live entry (`~/.agents/plugins/Minni@Minni/dist/server.js` + `MINNI_SOCKET_PATH` + `MINNI_AFM_*`) while simulating a running `Claude.app` parent process. On `af0c04f7`, `follow_claude_desktop()` either ignores Desktop or writes a SHA-pinned path. Post-fix: `delta == 0`, zero writes occur, deploy succeeds without asking to quit `Claude.app`. |
| **P1-6 / P2-2: Workspace resolution order (no `roots/list`, pin first), `exec` hooks (`H5`), & diagnostics** | `plugins/minni/tests/workspace-resolution.test.mjs` | `test_workspace_resolution_order_and_h5_diagnostics`: (1) Assert `server.ts` contains no `roots/list` or `.listRoots(`. (2) Assert `MINNI_PINNED_WORKSPACE_ID` beats session hint and `cwd`. (3) Assert `hooks/hooks.json` uses `exec node ...` so `hook.js` records the host PID (`server.ppid`) rather than `/bin/sh`. (4) Assert `findProjectRoot($HOME)` returns `undefined` when `$HOME/.git` is absent. (5) Assert missing/expired/mismatched hints emit `workspace_resolution_diag` with exact `hint_miss_reason`. |
| **P2-1 / P3-1 / P3-2: Rewrite-on-run CAS/locks, archive-first removals, version-root `cwd` deletion** | `tests/test_rewrite_on_run_and_archive.py` | `test_cas_aborts_on_concurrent_mutation_and_custom_refresh_deletes_plugin_cwd`: Verify writers respect `.settings.json.lock` and `.claude.json.tmp.*`, abort CAS if target `mtime`/`sha256` changes mid-write, archive displaced files into `~/.minni/archive/<ts>/` (leaving zero `.minni-backup-*` files), and `custom_refresh.py` deletes any existing version-root `"cwd"`. |
| **P2-4 / P2-5 / P2-7: `installed_plugins.json` SHA sync, `mcp-env-run` preservation, canonical `.minni/plugin` scan** | `tests/test_catalog_and_uncatalogued_scan.py` | `test_installed_plugins_tracks_sha_antigravity_keeps_wrapper_and_scanner_catches_rogue_file`: Assert `installed_plugins.json` keeps `installPath: "~/.minni/plugin/current"` while updating `version` and `gitCommitSha`, Antigravity configs keep `~/.agents/bin/mcp-env-run`, and planting an uncatalogued file containing `".minni/plugin/0.6.0+git.deadbeef"` causes `check_deployments.py --strict` to fail with `UNCATALOGUED_OR_VERSIONED_BINDING`. |
| **Call-Site Pinning** | `tests/test_deploy_callsite_pinning.py` | `test_update_root_and_fleet_sync_invoke_atomic_cutover_and_behavioral_probe`: Parse/trace `scripts/update_root.sh` and `src/minni/fleet_sync.py`; fail if either deploy entrypoint stops invoking `reconcile_all_hosts()` + pre/post-flip behavioral probes or omits any host in `HOST_SURFACE_CATALOG`. |
