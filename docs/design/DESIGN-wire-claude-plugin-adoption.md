# Wire-pipeline adoption for the Claude Code plugin surface

Status: accepted, 2026-08-01
Scope: make `minni wire claude-code` own Claude Code's plugin surface (hooks,
skills, commands, dist) from `~/.minni/plugin/<version>`, and retire the
orphaned marketplace/cache install.

## Problem

Claude Code loaded Minni's plugin surface from a tree nothing owned.

- `~/.claude/plugins/installed_plugins.json` pinned `minni@minni` to
  `~/.claude/plugins/cache/minni/minni/0.3.0` — a dir last written 2026-07-11 by
  a marketplace install and since overwritten in place by `propagate.py`'s
  `rsync -a --delete`. It is content-stale against HEAD (older `dist/server.js`,
  missing the `threads` command and `readme-audit` skill, older skill bodies).
- `~/.claude/plugins/known_marketplaces.json` pointed the `minni` marketplace at
  `~/Projects/minni-worktrees/cursor-hooks`, an unrelated
  stale worktree whose `hooks.json` carries `timeout: 10/20/30`. Any
  `/plugin update` would refresh the cache from *that*, silently reverting the
  hook timeouts the wire tree gets right (`timeout: 60`).
- Meanwhile `minni wire` already installs a verified, hash-manifested payload to
  `~/.minni/plugin/<version>` and wires all four platforms, including Claude
  Code's MCP server in `~/.claude.json`. Only the *plugin* surface was excluded.

So one host had two Minni trees with two different lifecycles, and the one
Claude Code read hooks from was the unmanaged one.

## Q1 — What path does `installed_plugins.json` reference?

**Decision: the versioned install root, `~/.minni/plugin/<version>`. Not a
`current` symlink.**

The obvious alternative — point the registration at a stable
`~/.minni/plugin/current` symlink so it never needs rewriting — was rejected for
three independent reasons.

1. **`current` is deliberately release-only.** `update_current_symlink`
   (`src/minni/wire/install.py`) early-returns for PEP440-local versions, and
   that is not an oversight: `tests/test_wire_hardening.py` asserts it three
   ways (`test_current_symlink_created_for_release`,
   `test_current_symlink_never_moved_by_dev_build`,
   `test_current_symlink_absent_when_only_dev_installed`), under a module
   docstring that names the "release-only `current` symlink gate" as a hardening
   outcome. A dev build must not be able to move the pointer a released install
   depends on. Registering against `current` would have required either breaking
   that invariant or leaving every `--from-repo` machine unregistered.
2. **GC does not understand symlinks.** `gc.py::_config_references_dir` matches
   the *literal versioned path string* in config files, and `_all_version_dirs`
   skips symlinks outright. A config naming `.../plugin/current` matches no
   version dir, so the symlink's target would be unprotected — GC would prune
   the tree out from under a live registration and leave a dangling link that GC
   itself never cleans.
3. **Claude Code's symlink handling in `installPath` is not a documented
   contract**, and there are open upstream issues about stale path resolution
   after plugin updates. Depending on it is unnecessary risk.

Versioned paths are safe here because **Claude Code reads plugin manifests once
per session at startup** (mid-session changes need `/reload-plugins`). Version
churn in the registration is therefore invisible: the next session reads the
current file and gets the current tree. Wire rewrites the entry on every wire
run, so registration and payload move together atomically from the session's
point of view.

Consequence accepted: `installed_plugins.json` will carry PEP440-local versions
like `0.4.0+git.afad904` on dev machines. Claude Code treats `version` as an
opaque string (`playwright@claude-plugins-official` currently reads `"unknown"`).

## Q2 — How does `minni wire claude-code` register, and is a marketplace needed?

**Decision: registration is a normal step of `minni wire claude-code`, performed
*after* verification passes; no marketplace entry is created or required.**

### No marketplace

Claude Code loads a plugin's hooks/skills/commands from `installPath` alone. The
`@marketplace` suffix in the `minni@minni` key is a namespacing convention, not
a lookup — the marketplace source is consulted only during install/update. The
live machine already proves this: `minni@minni` loads from a cache path while
its marketplace source points at an unrelated worktree.

Registering a marketplace would therefore buy nothing and re-arm the
`/plugin update` foot-gun. Wire creates no marketplace entry, and the cutover
removes the stale one.

Note the distinction: this retires the **machine-side** `known_marketplaces.json`
entry. It does **not** delete the repo's `.claude-plugin/marketplace.json`, which
is a publication artifact for other users installing Minni the ordinary way.
Whether that publication should continue is a separate decision (and
`scripts/check_versions.py` does not currently cover `marketplace.json` — an
adjacent finding, not fixed here).

### Idempotence

`register_claude_plugin()` upserts only the `scope: "user"` entry under the
`minni@minni` key:

- every other plugin key, every other-scope entry, and every unknown top-level
  field is preserved verbatim;
- `installedAt` is preserved from an existing entry (first registration stamps
  it);
- if `installPath`, `version` and `gitCommitSha` all already match, the function
  is a **no-op — `lastUpdated` is not touched**, so re-wiring an unchanged
  version leaves the file byte-identical. This is what makes `--dry-run`
  honest and repeat wires free of churn.

The write is atomic (`tempfile` + `os.replace`) because the target is a live
Claude Code config. A corrupt existing file raises rather than being
overwritten — it holds other plugins' registrations, and clobbering them to
recover our own entry is not a trade we get to make. **No exception is
swallowed in this writer.**

### Ordering: after verify, not before

`flow.py` writes host configs inside `_wire_platform` *before* `run_verify`, and
on verification failure it `continue`s with the config already stamped and no
`upsert_wire` — leaving a config pointing at a tree that `wired.json` does not
protect. The registration deliberately does **not** join that pattern: it runs
in `run_wire` immediately alongside `upsert_wire`, only on the path where
verification has passed. A failed wire leaves `installed_plugins.json` untouched.

## Q3 — Migration / cutover

**Decision: a dedicated `minni wire-adopt claude-code` subcommand, dry-run by
default, `--apply` to execute.** It is a one-time destructive repoint, so it does
not ride along with ordinary wiring.

`--apply` performs, in order:

1. **Register** the plugin against the currently wired claude-code install root
   (read from `wired.json`), if not already registered there.
2. **Repoint Claude Desktop.** `~/Library/Application Support/Claude/claude_desktop_config.json`
   currently launches `mcpServers.minni` from the 0.3.0 cache. Step 4 deletes
   that dir, so the repoint is not optional — skipping it would knowingly break
   a live surface. Merge-only: other servers and unrelated top-level keys are
   preserved. The rewrite targets *the argument that points into the legacy
   cache*, not `args[0]`, so an entry like `["--inspect", "<cache>/server.js"]`
   keeps its flag; when no argument points into the cache the step is a no-op
   and step 4's scan decides whether deletion is still safe. A `command` living
   inside the cache aborts the cutover rather than being guessed at.
3. **Retire the stale marketplace entry** (`minni` →
   `~/Projects/minni-worktrees/cursor-hooks`) from `known_marketplaces.json`.
4. **Remove the legacy cache tree** `~/.claude/plugins/cache/minni/minni`
   (skippable with `--keep-legacy-cache`).

Each step reports `changed: true|false` and is individually idempotent, so a
re-run after a partial failure completes the remainder rather than compounding.
**This PR ships the code; it is not executed against the live machine.**

#### What step 4 guarantees

The deletion is an `rmtree` on a live machine, so it carries two explicit
properties rather than relying on the earlier steps having done their job:

- **It refuses while anything still points into the tree.** Before deleting,
  every string in `installed_plugins.json` (all plugins, all scopes),
  `~/.claude.json`, `~/.claude/settings{,.local}.json` and the Claude Desktop
  config is checked for a path under `~/.claude/plugins/cache/minni`. Any hit
  aborts the cutover and names the offending file and field. A config that
  cannot be parsed counts as a hit: an unreadable file is not evidence that
  nothing references the tree. `known_marketplaces.json` is deliberately not
  scanned — its `minni` entry is the one reference step 3 retires itself.

  The check is two-layer, applied to each string leaf. Structured matching
  (`Path.relative_to`) recognises a string that *is* a lexically-normal absolute
  path and reports `field -> path`. Claude Code's hook entries are shell command
  strings, though — `"node <path>/dist/hook.js SessionStart"` — so the cache
  path routinely appears embedded in a larger string, and a boundary-anchored
  substring match over the same leaf catches those.

  Four details that gate got wrong before review, each of which turned it into a
  no-op, a permanent blocker, or a leak:

  - **Report the field, never the value.** This scan fires exactly where people
    inline `FOO_TOKEN=...`, and over `~/.claude.json` the surrounding text is
    verbatim prompt history. Earlier versions quoted 120 characters of context,
    then echoed the matched value — which `Path()` happily accepts with
    arguments glued on. Neither is emitted now: the message is the dotted trail
    (`hooks.SessionStart[0].hooks[0].command mentions <root>`), which is both
    leak-free and a better pointer than an excerpt of a multi-megabyte file.
  - **Scan keys, not just values.** `~/.claude.json` keys its `projects` map by
    directory, so a path can appear only as a key.
  - **`normpath` before comparing.** A detour above the root
    (`.../plugins/../plugins/cache/minni/...`) is a live reference the kernel
    resolves back into the tree, but `relative_to` compares components
    literally and would call it unrelated. Collapsing can only add matches, so
    the failure direction stays conservative.
  - **Anchor the needle.** A bare substring reintroduces exactly the bug
    `_is_under` avoids: `.../cache/minni` is a prefix of
    `.../cache/minni-tools`, so an unrelated marketplace would block the
    cutover. Only `[A-Za-z0-9_.-]` suppresses a match, and a genuine reference
    is always followed by `/` or a string terminator.
  - **Normalize to NFC.** macOS treats NFC and NFD spellings of an accented
    path as the same file; Python string comparison does not. Without this a
    config written from a differently-normalized source reads as "not a
    reference" and clears the deletion.
  - **The literal `~/.claude/plugins/cache/minni` is not a needle.**
    `~/.claude.json` persists prompt history, and this repo's own docs and
    `--help` text contain that string, so any session discussing the migration
    would poison the file permanently and leave `--keep-legacy-cache` as the
    only exit — the cutover could never complete on the machine it was written
    for. Claude Code does not expand `~` in these configs, so the on-disk risk
    it would cover is not real.

  Residual gaps are accepted and bounded: a tilde-spelled path, `$HOME`-style
  indirection, a reference reached through some unrelated symlink alias, a `..`
  detour *inside a command string* (the structured layer normalizes, the
  substring layer cannot), and a path split across two fields. The structured
  scan catches every lexically-normal absolute path, which is the shape these
  configs actually take, and the worst case for a miss is a dangling pointer
  into a cache the operator was retiring — recoverable, and avoidable up front
  with `--keep-legacy-cache`.

- **It reports everything it deletes.** The target is the plugin dir
  `<cache>/minni/minni`, not the whole `<cache>/minni` marketplace dir. A
  sibling plugin cached under the same marketplace survives and is listed in
  `siblings_kept`; the marketplace dir is removed only once it is empty.
  `removed_versions` lists every direct child of the target, not just
  version-shaped dirs, because `rmtree` takes loose files and symlinks too.

Because step 1 and step 2 rewrite registrations that themselves point into the
cache, the scan runs against the documents those steps are *about to write*, not
the ones currently on disk. A dry run therefore answers exactly the question
`--apply` will answer, instead of refusing on every machine that has not adopted
yet.

### Cutover runbook (operator, after merge)

Nothing below was run against the live machine by the change that introduced it.

```sh
# 1. Wire the current payload. This installs ~/.minni/plugin/<version>,
#    verifies it, records wired.json, and registers the plugin surface.
minni wire claude-code --from-repo ~/Projects/Minni

# 2. Inspect the cutover without writing anything.
minni wire-adopt claude-code

# 3. Perform it.
minni wire-adopt claude-code --apply

# 4. Confirm, then restart Claude Code (manifests are read at session start).
minni wire-adopt claude-code            # every step should report changed: false
python3 scripts/check_deployments.py    # the wire tree should not be stale
```

After the restart, `/plugin` should list `minni@minni` at the `~/.minni/plugin/<version>`
path, and the hook timeout should be the payload's `60`, not the retired
worktree's `30`.

Rollback, should it be needed: `installed_plugins.json` is the only thing that
decides which tree loads. Re-pointing its `minni@minni` user-scope `installPath`
at a restored cache dir and restarting reverts the plugin surface; the MCP server
in `~/.claude.json` is independent of it.

## GC reference tracking

`gc.py` discovers references two ways: `wired_install_roots()` from
`wired.json`, plus a literal-string scan of `default_config_scan_paths()`, which
covered only the four MCP configs. `installed_plugins.json` was invisible to it.

`default_config_scan_paths()` now includes a `claude-code-plugins` entry for
`~/.claude/plugins/installed_plugins.json`. Belt and braces alongside
`wired.json`: even a registration written out of band, or one left behind by a
wire run that failed verification before `upsert_wire`, protects its own tree
from collection. This mirrors the existing rationale in `_protected_versions`
for dev dirs referenced by stamped configs.

## propagate.py

`propagate.py` predates wire and still resolves Claude Code's install dir
itself. Post-adoption it must not write to a tree nobody reads.

- **claude-code fails loud.** `platform_spec("claude-code")` raises with a
  pointer to `minni wire claude-code`. It can no longer reach
  `update_claude_config`, whose wholesale `mcpServers.minni` rewrite drops
  `MINNI_WORKSPACE_ID`. `claude-code` is removed from the `--platform all`
  expansion (with a loud notice) so the remaining platforms keep working rather
  than the whole run aborting. No parallel fix path is built.
- **Claude Desktop moves to the cutover, then rides ordinary wiring.** Its only
  writer was reachable solely via the claude-code platform. The unreachable call
  site is removed; `update_claude_desktop_config` stays (still directly tested)
  and `minni wire-adopt` is what first moves Desktop onto the wire tree.

  Adopt alone would not have been enough. Desktop records a *versioned* path, so
  the next `minni wire` that installs a new version strands it on the old one,
  which GC then prunes out from under it — a one-shot writer for a path that
  changes every wire. So `minni wire claude-code` also runs
  `follow_claude_desktop`, which moves an argument already inside
  `~/.minni/plugin` onto the freshly wired root and does nothing otherwise (a
  no-op before adoption, and on hosts with no Desktop). Only the `server.js`
  entrypoint moves: a sibling path under the same tree is not a second server
  pointer, and rewriting it would be silent corruption.

  Belt and braces, `default_config_scan_paths()` gains the Desktop config, so GC
  treats it as a reference like any other. `test_gc_retains_a_version_only_
  claude_desktop_references` fails without that line, which is the point — the
  scan is load-bearing, not decorative.

  Deliberately *not* adopted: `update_claude_desktop_config`'s env stamping
  (workspace/AFM). Desktop is a separate product wire does not otherwise own,
  and widening this from "keep the server path valid" to "own Desktop's
  environment" is a bigger claim than this change needs.
- **codex resolves from what exists.** `plugin_version_segment()` no longer falls
  through to pip metadata as its primary answer on machines without `current` —
  it prefers `current`, then takes the **PEP440-maximum over version dirs that
  actually exist on disk**, and only then pip metadata. Codex resolves against
  its own tree (`~/.codex/plugins/cache/minni/minni`) rather than inheriting the
  wire tree's version, and the silent literal `"current"` path segment
  substitution is gone: if nothing can be resolved, it raises.

Note: wire's `ALL_EXPANSION_V03` (4 platforms) and propagate's `all` list
disagree in both directions, before and after this change. Removing claude-code
from propagate's list narrows the gap by one but does not close it; reconciling
the two lists is deliberately left out of scope.

## Deliberately not addressed

- **Duplicate MCP registration.** The payload's `.claude-plugin/plugin.json`
  declares its own `mcpServers.minni`, so a loaded plugin registers a second
  Minni server (`mcp__plugin_minni_minni__*`) beside the richer wire-stamped one
  in `~/.claude.json` (`mcp__minni__*`). Both are live today; this change moves
  which tree the plugin half loads from but does not deduplicate. The wire entry
  is strictly better (agent id, workspace id, AFM env), so the follow-up is to
  drop `mcpServers` from the Claude payload manifest — that affects the public
  marketplace install path and is its own decision.
- `scripts/check_deployments.py` gained the wire tree (`~/.minni/plugin/*/dist`)
  so the registered tree is health-checked rather than invisible. Its
  `hooks/`/`skills/` subtree drift coverage is still absent and unfixed here.
- `marketplace.json` version coverage in `check_versions.py`; the
  `except Exception: pass` env-preservation swallow in
  `writers.py::replace_toml_sections`.
