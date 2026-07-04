# Hardened Design: Wheel-Shipped Plugin Payload for `minni wire <platform>` (issue #142, v0.3)

Status: hardened design, not implemented. macOS-first (operator decision 2026-07-03, per issue #142).

## 1. Goal

Ship the Minni plugin payload (compiled hook entrypoints, MCP server, hooks manifests,
minni-install skill) **inside the Python wheel**, so that `pipx install minni && minni wire
<platform>` wires any supported agent platform without a repo checkout and — critically —
**without a Node toolchain at wheel-build or install time**. The payload installs to the
canonical path `~/.minni/plugin/<version>/`, with all absolute paths resolved at wire time on
the target machine ("resolve, never predict"), replacing today's repo-checkout-based
`propagate.py update-plugin` flow (propagate.py:868-904).

## 2. Grounding: what is true today

- Build backend is setuptools>=77, src-layout rooted at `where=["src"]`; non-code assets
  already ship via `[tool.setuptools.package-data]` under key `minni` (pyproject.toml:63-73).
  That mechanism only picks up files under `src/minni/`. No MANIFEST.in exists.
- `plugins/minni/dist/` is **not tracked in git** (`.gitignore:84` = `plugins/*/dist/`); it is
  pure `tsc` output (`rootDir: ./src`, `outDir: ./dist`, plugins/minni/tsconfig.json) plus
  `vite build` for the frontend. Nothing bridges the npm/tsc build and the Python wheel build:
  `make build` runs `npm run build`, `make setup` runs `pip install -e .` — two disjoint paths
  (Makefile:1-40).
- The npm `files` allowlist (plugins/minni/package.json) defines today's authoritative payload:
  `dist`, `.claude-plugin`, `.codex-plugin`, `.gemini-plugin`, `.kilocode-plugin`, `.mcp.json`,
  `commands`, `hooks`, `skills`, `README.md`. `engines.node >= 20`.
- **Verified during hardening (2026-07-04):** grepping all 30 files in `plugins/minni/src/` for
  non-relative, non-`node:` imports shows external imports **only in `server.ts`**
  (`@modelcontextprotocol/sdk/server/mcp.js`, `.../stdio.js`, `zod`). Hook entrypoints
  (`hook.ts`, `codex-hook.ts`, `gemini-hook.ts`, `grok-hook.ts`, `kilocode-hook.ts`) and
  `cli.ts`, `ui-server.ts` import only relative modules and `node:` builtins. So issue #142's
  "dependency-free tsc output" claim is **true for hooks and cli, false for server.js**.
  `react`/`react-dom` in `dependencies` are consumed only by the vite-bundled frontend.
  Notably, propagate.py's `copy_tree` already excludes `node_modules` (propagate.py:297,301)
  while pointing MCP configs at `<install_root>/dist/server.js` — meaning server.js in copied
  installs depends on Node's upward `node_modules` resolution finding *something*, which is
  fragile and must not be carried into the wheel design.
- Version drift is live, not hypothetical: pyproject.toml and plugins/minni/package.json say
  0.2.0; all four per-platform manifests (.claude-plugin/plugin.json, .codex-plugin,
  .gemini-plugin/gemini-extension.json, .kilocode-plugin) say 0.1.0. And the hardcoded
  stale version-pinned literal is a repeated anti-pattern in propagate.py, not a one-off:
  `DEFAULT_PLUGIN_CLI = ~/.codex/plugins/cache/minni/minni/0.1.0/dist/cli.js`
  (propagate.py:50-52); the codex and claude-code install roots in `platform_spec()`,
  both `.../cache/minni/minni/0.1.0` (propagate.py:816, 822); and
  `update_gemini_manifest`'s literal `"version": "0.1.0"` written into every
  gemini-extension.json it produces (propagate.py:469). All four are remediated in
  Phase 1 (§7).
- Host schema drift is an observed incident: Gemini/Antigravity plugin dir moved from
  `~/.gemini/antigravity-cli/plugins/` to `~/.gemini/config/plugins/` on 2026-07-03, stranding
  a hand-installed plugin (issue #142).

## 3. Decision summary

| Question | Decision |
|---|---|
| How does dist/ get into the wheel? | **Release-time staging copy** into `src/minni/plugin_payload/` (gitignored), performed by a `make stage-payload` step that runs *before* `python -m build`. The sdist/wheel then carry the pre-built payload as ordinary package-data. No committed vendored copy; no custom setuptools build hook that shells out to npm. |
| Does the wheel build need Node? | **No.** Node is needed only on the release machine (or release CI job) that runs `make stage-payload`. `python -m build` itself only globs files already present. |
| How does server.js become dependency-free? | **Bundle it**: staging runs esbuild to produce a single-file `dist/server.js` (platform=node, `node:*` external, MCP SDK + zod inlined). Hooks/cli stay as plain tsc output — verified dependency-free. No `node_modules` ships in the wheel or the payload. |
| Canonical version source | `pyproject.toml [project] version`, read at runtime via `importlib.metadata.version("minni")`. All other version fields become **derived, stamped at staging time**. |
| Install path | `~/.minni/plugin/<version>/` + a `current` symlink, written by `minni wire`. |
| Staleness guarantee | A generated `payload-manifest.json` (version, git SHA, build timestamp, per-file SHA-256) is the gate: staging refuses to stage a mismatched version; `minni wire` refuses to install a payload whose manifest version differs from the installed Python package version. |

### Why staging-copy over the alternatives

- **Committed synced copy under src/minni/** (option B in the issue): would be a net-new
  mechanism (nothing like it exists today), puts ~generated JS in git, and *creates* a second
  drift axis (TS source vs committed JS) that a CI check would have to police forever. The
  repo's own history (0.1.0 manifests, `DEFAULT_PLUGIN_CLI`) shows committed derived artifacts
  rot. Rejected.
- **Custom setuptools build hook invoking npm/tsc**: violates the hard constraint that wheel
  builds must not require a Node toolchain, and would break `pip install` from sdist on
  machines without Node. Rejected.
- **Staging copy**: reuses the existing, working package-data *mechanism*
  (pyproject.toml:63-73) — files under `src/minni/` shipped via
  `[tool.setuptools.package-data]` — and moves the Node requirement to the one place it
  already exists (the release workflow, which already runs `npm run build` via `make
  build`). One caveat the precedent does **not** cover: every existing entry is a flat,
  single-depth glob, while the payload is a nested tree. The design therefore declares
  explicit per-directory flat globs rather than a recursive `**` pattern (see §4.2 step 5
  for why, and §9.3 for the completeness test).

## 4. Mechanism, step by step

### 4.1 Payload contents (what ships)

Derived from the npm `files` allowlist, minus dev/UI-only material:

```
src/minni/plugin_payload/
├── payload-manifest.json        # generated: {version, git_sha, built_at, files: {path: sha256}}
├── dist/                        # tsc output for hooks + cli, esbuild bundle for server
│   ├── hook.js, codex-hook.js, gemini-hook.js, grok-hook.js, kilocode-hook.js
│   ├── cli.js
│   ├── server.js                # single-file esbuild bundle (MCP SDK + zod inlined)
│   └── (supporting *.js modules the hooks import — plain tsc output)
├── hooks/                       # hook manifests
├── .claude-plugin/ .codex-plugin/ .gemini-plugin/ .kilocode-plugin/
├── .mcp.json                    # template; concrete values stamped at wire time
├── commands/
├── skills/                      # ALL seven skill dirs — see below
│   ├── minni/                   # the primary agent-facing orientation skill
│   ├── minni-consolidation/     # (+ scripts/)
│   ├── minni-doctor/
│   ├── minni-engine/
│   ├── minni-health-check/
│   ├── minni-ingestion/
│   └── minni-install/           # (+ references/, scripts/)
└── README.md
```

**Skills ship whole, not just minni-install.** The npm `files` allowlist ships all of
`skills/` and propagate.py's `copy_tree` copies the whole tree — the skills are
agent-facing product content (skills/minni is the primary "Portable Delivery Layer"
orientation skill; minni-doctor is the diagnostic entrypoint the §4.4 step 6 / §6.4
doctor checks complement), not dev/UI material. A wheel-wired agent must get the same
skill set as a propagate.py-wired one, or `minni wire` is functionally incomplete
relative to the flow it replaces (§1, §7 parity goal). The seven directories verified
2026-07-04: minni, minni-consolidation (+scripts/), minni-doctor, minni-engine,
minni-health-check, minni-ingestion, minni-install (+references/, scripts/).

Excluded: `frontend-src/`, `src/*.ts`, `tests/`, `node_modules/`, vite frontend output unless
the UI is explicitly promoted into scope (open question 5), `package.json`/lockfiles. This is
deliberately narrower than propagate.py's whole-tree `copy_tree` (propagate.py:291-301) —
but only by those dev/UI items; no skill directory is excluded. See §7 for the parity plan.

### 4.2 Staging (`make stage-payload`, release machine / release CI only)

1. `cd plugins/minni && npm ci && npm run build:server` (tsc) — existing scripts.
2. esbuild bundles `dist/server.js` → single file (`--bundle --platform=node
   --format=esm --external:node:*`). esbuild is added as a devDependency; it is never needed
   at wheel-build or user-install time.
3. Read canonical version from `pyproject.toml`. **Stamp** it into: the four per-platform
   manifest `version` fields inside the payload, and `payload-manifest.json`. (No
   `package.json` is stamped because none ships — §4.1 excludes it; nothing at wire time
   reads it, and the `engines.node` constraint it would otherwise carry is recorded in
   `payload-manifest.json.node_engine` instead. This makes the payload internally
   consistent even while the source-tree manifests drift; a separate `make check-versions`
   CI lint pushes the source tree back into sync — see §8.)
4. Copy the §4.1 file set into `src/minni/plugin_payload/`, compute per-file SHA-256s, write
   `payload-manifest.json`.
5. `src/minni/plugin_payload/` is **gitignored**; pyproject gains an **explicit, flat
   per-directory enumeration** under `[tool.setuptools.package-data] minni` — one
   single-level glob per payload subdirectory, mirroring the style of the existing
   precedent (which is all flat globs, no `**`):

   The enumeration below was derived by walking the real payload tree
   (`find plugins/minni/<payload dirs> -type d`, verified 2026-07-04) — not by assuming
   single-depth. The tree is up to three levels deep today
   (`.kilocode-plugin/skills/sovereign-memory/`), and one glob line exists per real
   directory:

   ```
   "plugin_payload/*",                                  # payload-manifest.json, .mcp.json, README.md
   "plugin_payload/dist/*",
   "plugin_payload/hooks/*",
   "plugin_payload/.claude-plugin/*",
   "plugin_payload/.codex-plugin/*",
   "plugin_payload/.gemini-plugin/*",
   "plugin_payload/.gemini-plugin/skills/sovereign-memory/*",
   "plugin_payload/.kilocode-plugin/*",
   "plugin_payload/.kilocode-plugin/commands/*",
   "plugin_payload/.kilocode-plugin/hooks/*",
   "plugin_payload/.kilocode-plugin/skills/sovereign-memory/*",
   "plugin_payload/commands/*",
   "plugin_payload/skills/minni/*",
   "plugin_payload/skills/minni-consolidation/*",
   "plugin_payload/skills/minni-consolidation/scripts/*",
   "plugin_payload/skills/minni-doctor/*",
   "plugin_payload/skills/minni-engine/*",
   "plugin_payload/skills/minni-health-check/*",
   "plugin_payload/skills/minni-ingestion/*",
   "plugin_payload/skills/minni-install/*",
   "plugin_payload/skills/minni-install/references/*",
   "plugin_payload/skills/minni-install/scripts/*",
   ```

   (Note there is no `plugin_payload/.gemini-plugin/skills/*` line: `skills/` there
   contains only the `sovereign-memory/` subdirectory, no files, and setuptools globs
   match files. Staging additionally **excludes junk** present in the source tree today —
   `.DS_Store`, `__pycache__/`, `.pytest_cache/` — so they neither ship nor trip the
   completeness check.)

   This list is a snapshot, not a contract: the authoritative guard is the fail-hard
   staging check below plus §9.3's manifest-vs-wheel round-trip test, both of which fail
   the release if the tree gains a directory with no matching glob. A CI unit test
   (§9.3) additionally walks the source payload dirs and asserts every directory has a
   corresponding glob line — catching the omission at PR time rather than release time.

   A single recursive `plugin_payload/**` is deliberately **not** used: setuptools has a
   documented history of recursive package-data globs silently dropping nested
   subdirectories from wheels (pypa/setuptools#1806, #4402; pypa/pip#12735 shows it can
   also surface at pip-install time), and the existing pyproject precedent
   (pyproject.toml:63-73) establishes only flat single-depth globs, not recursion. If the
   payload layout ever gains a new subdirectory, staging fails hard: `make stage-payload`
   ends by checking that every file it copied matches at least one of the declared
   package-data patterns, so an unlisted directory breaks the release build instead of
   silently shipping an incomplete wheel. Independently, §9.3's wheel test asserts the full
   file set round-trips. Because package-data is included in both sdist and wheel by
   setuptools, an sdist produced after staging builds into a wheel on any machine with
   **zero Node dependency**.

### 4.3 Release build

`make release-wheel` = `make stage-payload && python -m build`. It **fails hard** if
`plugin_payload/payload-manifest.json` is absent or its version ≠ pyproject version — this is
the guarantee that no wheel ever ships stale or missing payload. (A plain `python -m build`
without staging produces a wheel with no payload; the release lane never does this, and
`minni wire` detects it at runtime, §6.1.)

### 4.4 `minni wire <platform>` runtime flow

New subcommand registered in `src/minni/minni_cli.py` (`[project.scripts]` already routes
`minni` there; no `wire` exists today). Platforms mirror propagate.py's set: codex,
claude-code, kilocode, gemini, antigravity, grok, generic, all (valid `--platform` values
per propagate.py:1328-1334).

**`all` expansion is an explicit contract, not a loop over every valid value.**
propagate.py's dispatcher expands `all` to
`["codex", "claude-code", "kilocode", "gemini", "grok"]` (propagate.py:946) —
**antigravity and generic are deliberately excluded** (generic requires
`--install-root` and has nothing to expand to; antigravity was never in the set).
`minni wire all` adopts the same base contract, with one v0.3 refinement: while
gemini/antigravity wiring is provisional (step 5 carve-out, open question 8),
`all` **skips gemini with a printed warning** ("gemini wiring is provisional; run
`minni wire gemini` explicitly to attempt it") rather than silently attempting an
unverified mechanism or failing the whole run. So in v0.3,
`minni wire all` = codex, claude-code, kilocode, grok; gemini rejoins the expansion
(and the warning is removed) when open question 8 is resolved and its wiring ships
non-provisional. Antigravity and generic are never part of `all`; wiring them is
always an explicit per-platform invocation. A skipped platform does not fail the
run; any wired platform's failure does. §9.4 tests this expansion.

1. **Locate payload**: `importlib.resources.files("minni") / "plugin_payload"`. Never a
   hardcoded path; never a version-pinned literal (the anti-pattern of
   `DEFAULT_PLUGIN_CLI`, propagate.py:50-52).
2. **Integrity + version gate**: read `payload-manifest.json`; require
   `manifest.version == importlib.metadata.version("minni")`; optionally verify file hashes
   (`--verify-payload`). Mismatch → hard error naming both versions. In `--use-version`
   rollback mode, exactly steps 1, 2, and 4 are skipped (payload-locate, package-version
   gate, payload extraction); **step 3 (preflight) and steps 5–7 still run** — rollback
   targets an already-installed version dir and carries its own checks, but it still
   writes configs pointing at `dist/server.js`, so the Node preflight must run to preserve
   §6.3's "no config written when Node is missing/<20" guarantee on the rollback path too.
   See §5 for the exact semantics of that documented bypass.
3. **Preflight**: `node --version` present and >= 20 (from `engines.node`); target platform's
   config root exists (with the Gemini-style probe of §6.4). Fail with actionable messages
   before touching anything.
4. **Install payload**: copy payload → staging dir `~/.minni/plugin/.staging-<version>-<pid>/`,
   fsync, then atomic rename to `~/.minni/plugin/<version>/`.

   **Concurrency**: the exists/hash-match check below is check-then-act, and `os.rename`
   onto an existing non-empty directory raises `ENOTEMPTY`/`EEXIST` on POSIX rather than
   atomically merging — so two concurrent wires targeting the same not-yet-installed
   version (e.g. two platforms wired in parallel right after a pipx upgrade) would both
   see "target absent", both stage, and the second rename would crash on a path none of
   the three branches covers. Step 4 therefore runs under an exclusive advisory lock:
   `fcntl.flock` on `~/.minni/plugin/.install-<version>.lock` (macOS-first per §1;
   `flock` is fine on APFS/local filesystems), acquired **before** the existence check
   and held through the rename, so the second process re-checks after acquiring the lock
   and falls into the hash-match idempotent-skip branch. Belt-and-braces: if the rename
   still raises `ENOTEMPTY`/`EEXIST` (e.g. a foreign tool created the dir mid-flight),
   wire catches it and re-derives through the same three branches against the
   now-existing dir instead of surfacing a raw traceback. Lock files are tiny, never
   deleted (deleting an flock target is racy), and ignored by GC.

   Three explicit cases when the target already exists — this is what enforces §5's "immutable once installed" invariant
   rather than merely asserting it:
   - **Exists, hashes match** the incoming payload's manifest: skip (idempotent no-op).
   - **Exists, hashes mismatch** (corrupted prior install, manual edit, or two builds that
     stamped the same version with different content): **hard error**, never a silent skip
     or overwrite. The error names the version dir, the mismatching files, and requires
     `--force-reinstall`, which moves the existing dir aside to
     `~/.minni/plugin/.quarantine-<version>-<timestamp>/` (never deletes it) before the
     atomic rename installs the fresh payload. Quarantine dirs are reported by `--prune`
     and swept by GC like orphaned staging dirs.
   - **Collision prevention for dev builds**: `--from-repo` payloads are versioned as
     `<version>+git.<shortsha>[.dirty]` (a PEP 440 local-version suffix) for both the dir
     name and the stamped manifest `version`, so a dev build can never collide with — or
     silently overwrite — a released wheel's version dir carrying the same pyproject
     version string. For `--from-repo` installs the §4.4 step 2 gate is a
     **manifest self-check only** (see §4.5 step 3 for the single authoritative
     definition): it never compares against `importlib.metadata.version("minni")`.

   Update symlink `~/.minni/plugin/current` → `<version>` atomically
   (`os.symlink` + `os.replace`) — **release payloads only**. Local-versioned installs
   (any version with a PEP 440 local segment, i.e. every `--from-repo` build's
   `+git.<shortsha>[.dirty]` suffix) never move `current`: PEP 440 orders
   `0.3.0+git.abc1234` *above* `0.3.0` (verified with `packaging.version.Version`), so
   without this exclusion every routine dev build would silently outrank and hijack
   `current` from the actual installed release. `current` means "newest installed
   *release* payload"; dev builds are reachable only by their explicit versioned path
   and their wired.json records.
5. **Wire configs**: compute `server_path = ~/.minni/plugin/<version>/dist/server.js` and
   hook paths, resolved **now, on this machine**. This "stamp a resolved absolute path"
   convention is what propagate.py uses for **most** platforms (propagate.py:903-904:
   configs point at `<install_root>/dist/server.js`) — but **not all of them**: Gemini's
   `update_gemini_manifest` (propagate.py:464-484) writes a *host-resolved template*
   (`"args": ["${extensionPath}${/}dist${/}server.js"], "cwd": "${extensionPath}"`), never
   an absolute path, and that template only resolves inside a directory Gemini's own
   extension loader discovers — which `~/.minni/plugin/<version>/` is not. See the
   Gemini/Antigravity carve-out below and open question 8; the writers must **not** be
   ported "as today's convention, uniformly." Port the per-platform writers
   (`mcp_json`, `update_claude_config`, `update_kilo_config`, `update_toml_mcp_config`,
   the gemini-family writers `update_gemini_manifest`, `update_antigravity_config`, and
   `update_agy_plugin_hooks` — propagate.py:382-560, 638-665, 673-745) into
   `src/minni/wire/` so the wheel is self-contained. `update_agy_plugin_hooks` is a
   distinct third gemini-family writer, easy to lose track of: it registers the
   deny-capable PreToolUse guard hook via the external `agy` CLI
   (`agy plugin install` / `agy plugin enable`, writing under
   `~/.gemini/config/plugins/minni/hooks.json`) and is invoked for **both** the
   gemini-manifest and antigravity config kinds alongside the MCP-config writers
   (propagate.py:926-927). It is in-scope for the port (same graceful-degradation
   contract as today: if `agy` is absent from PATH, hook registration is skipped with a
   recorded reason, never a hard failure), and step 3's preflight gains an `agy`-presence
   probe for the gemini/antigravity platforms — informational only, since the writer
   degrades gracefully, but surfaced up front so "MCP wired but guard hook not
   registered" is a stated outcome, not a surprise. In Phase 1 this is a **port (copy + adapt), not a refactor**:
   propagate.py keeps its own implementations unchanged (§7 Phase 1), and the §7/§9.5
   parity test is the guard against drift between the two copies during the transition.
   propagate.py is refactored into a thin caller of `src/minni/wire` only in Phase 2 (§7). **Claude Code, v0.3, decided**: `minni wire claude-code`
   writes an MCP-server entry with a stamped absolute `args: [<server_path>]` into
   `~/.claude.json`, exactly as propagate.py's `update_claude_config` does today
   (propagate.py:428-443, `config_kind == 'claude-json'`). It does **not** attempt Claude
   Code native-plugin registration: `${CLAUDE_PLUGIN_ROOT}` (.claude-plugin/plugin.json:16-27)
   is resolved by Claude Code's own plugin loader only for plugin directories it discovers
   through its marketplace/extension mechanism — writing `.claude-plugin/plugin.json` under
   `~/.minni/plugin/<version>/` does nothing to make Claude Code treat that path as a
   registered plugin root, and this design defines no registration step. The
   `.claude-plugin/` dir still ships in the payload (it is consumed when the payload is
   installed through Claude Code's own plugin channel, out of scope here), but wire never
   relies on it. Native-plugin registration from a wheel install is deferred to open
   question 7. This decision is also what makes the §7 parity test well-defined: both
   propagate.py and `minni wire` produce a concrete `~/.claude.json` MCP entry that can be
   diffed directly. **Gemini/Antigravity carve-out** — these are not one symmetric
   unresolved problem; the wiring decomposes into three writers with different
   resolution states:
   - **Antigravity surface views (`update_antigravity_config`,
     propagate.py:638-665): resolved and portable, with one launcher caveat.** It
     already writes absolute `server_path` MCP entries into the four
     `GEMINI_SURFACE_CONFIGS` (propagate.py:62-67) via
     `write_view_entry`/`gemini_minni_entry` — shipped, exercised code matching this
     design's stamped-absolute-path convention. The path-resolution question is
     settled; what is **not** free is the launcher: `gemini_minni_entry` sets
     `command` to the external `~/.agents/bin/mcp-env-run` wrapper
     (propagate.py:60, 488-516), an out-of-repo convention Minni neither ships nor
     installs (docs/design/DESIGN-sovereign-delivery-layer.md:32) — unlike the other
     writers, whose `command: node` is covered by step 3's Node preflight. The ported
     writer therefore does both: step 3's preflight for gemini/antigravity gains an
     `mcp-env-run`-presence probe (informational, like the `agy` probe), and when the
     wrapper is absent the writer **falls back to `command: node,
     args: [<server_path>]` directly** and records that choice in the wire output —
     never silently stamping a launcher path that doesn't exist, which would wire a
     config that fails only at MCP-launch time (the failure class step 3 exists to
     prevent).
   - **The gemini extension manifest (`update_gemini_manifest`)**: must **not** be
     ported verbatim — its `${extensionPath}` template resolves only inside directories
     Gemini's extension loader discovers, never `~/.minni/plugin/<version>/`. This is
     the actual open problem; see open question 8.
   - **agy guard-hook registration (`update_agy_plugin_hooks`)**: ported with its
     existing graceful-degradation contract (above); its dependency is the external
     `agy` binary, not a path-templating question.

   Consequently `minni wire antigravity` in v0.3 is provisional **only** because its
   flow today also invokes `update_gemini_manifest` (propagate.py:918, to keep the
   gemini-cli extension manifest in sync) — the surface-view and agy-hook legs are
   ready. `minni wire gemini` (standalone) is provisional pending open question 8; the
   presumptive mechanism is a settings-level MCP entry with a stamped absolute
   `server_path` (`gemini_minni_entry`, which already accepts one), verified against a
   real install.
6. **Verify**: end with post-wire verification probes (issue #142 requirement). These are
   **net-new probes this design adds** — no existing surface covers them: `minni doctor`
   today (src/minni/minni_cli.py:253-318) checks Python version, socket permissions,
   daemon RPC round-trips, and the embedding-model cache — nothing MCP-server- or
   plugin-config-aware — and propagate.py's `verify` subcommand (propagate.py:1259-1300)
   checks agent-identity strings and a daemon `read` RPC, likewise unrelated. The new
   probes are: (a) `node <version>/dist/server.js` MCP handshake smoke (spawn, send
   `initialize`, expect a well-formed response, kill); (b) hook dry-run — invoke the
   platform's hook entrypoint with a sample stdin event and assert clean exit; (c)
   config-file readback — re-read the just-written config and confirm the stamped paths
   resolve. The same three probes are also added to `minni doctor` as a persistent
   plugin-wiring check (alongside the dangling-path check of §6.4), so post-install
   verification and later health checks share one implementation.
7. **Record + GC (reference-aware)**: after a successful wire+verify, record the wire in
   `~/.minni/plugin/wired.json`. The record is keyed by **(platform, config_path)**, not by
   platform alone — because `--install-root`/`--workspace` (and `generic`, which *requires*
   `--install-root`) let the same platform name be wired to N independently-located
   configs, and a platform-keyed dict would let the second wire silently clobber the
   first's GC-protection record:

   ```json
   {"schema": 1, "wires": [
     {"platform": "claude-code", "config_path": "~/.claude.json",
      "install_root": "~/.minni/plugin/0.3.0", "version": "0.3.0",
      "workspace": null, "wired_at": "..."}
   ]}
   ```

   Updated atomically — and "atomically" covers the **whole read-modify-write cycle**,
   not just the final temp-file + rename: without that, two concurrent wires (e.g. a
   script wiring claude-code and codex in parallel, or CI wiring several platforms)
   would both read the same snapshot and the second writer's rename would silently drop
   the first writer's just-added entry — a lost update that unpins that wire from GC
   protection and reintroduces exactly the silent-deletion failure mode wired.json
   exists to prevent. Mechanism: the upsert runs under an exclusive `fcntl.flock` on
   `~/.minni/plugin/wired.lock` (same style as step 4's install lock), held from the
   read through the atomic replace — the read always happens **after** lock
   acquisition, so no lock-respecting writer can ever act on a stale snapshot and no
   retry logic is needed among them. As defense in depth against writers that do
   **not** take the lock (foreign or legacy tooling editing wired.json directly),
   wired.json carries a `"generation"` counter incremented on every write: a
   generation that moved while the lock was held proves an out-of-band write, which
   wire surfaces as a warning; and `minni doctor` flags a config whose stamped path
   has no wired.json entry (§6.4), so even a lost update caused by such a writer is
   detected rather than silent. A wire **upserts** the entry matching its
   (platform, config_path) pair and leaves all other entries untouched.
   `install_root` is recorded explicitly for
   every wire, so GC's reference set is complete even for wires the fallback scan below
   could never rediscover (arbitrary `--install-root` targets, and `generic`, which has no
   default candidate locations at all — for these, wired.json is the *only* protection, and
   the docs for `--install-root` say so).

   GC then removes old version dirs under `~/.minni/plugin/`, subject to **all** of:
   (a) never delete a version listed in *any* wired.json entry; (b) as a belt-and-braces
   check, before deleting a candidate dir, scan the known per-platform default config
   locations (the same ordered candidate lists §6.4 probes) for any config whose stamped
   path resolves under that dir, and skip it if found — this protects platforms wired by
   older tooling that predates `wired.json` (it cannot protect arbitrary-`--install-root`
   wires; those rely on wired.json per the previous paragraph); (c) beyond the in-use set,
   keep the current version plus one previous version, computed **over release version
   dirs only** — local-suffixed dev dirs (`+git.*`, step 4) are excluded from the
   candidate set *before* ranking, so they can be neither "current" nor "previous" for
   retention purposes (excluding them only from "previous" would let a dev build that
   PEP 440-outranks the newest release displace it from the "current" slot, leaving a
   real release protected by nothing but wired.json/config-scan). Among the release
   dirs, "current" is the highest and "previous" the **second-highest version by
   PEP 440 ordering** — never mtime or install order, which diverge from version order
   after a rollback-and-reinstall sequence (§6.7) and would make retention
   nondeterministic. Dev dirs are GC-protected solely by rules (a)/(b) while wired, and
   are otherwise prunable like orphaned staging dirs.

   **Version-comparison implementation**: PEP 440 ordering (pre/post/dev releases,
   local segments) is nontrivial and load-bearing here; the implementation is
   `packaging.version.Version`, and **`packaging` is added to `[project] dependencies`
   in pyproject.toml** as part of this feature — it is not a declared runtime dependency
   today (§2's dependency list) and must not be assumed present just because setuptools
   uses it at build time. No hand-rolled version comparison anywhere in wire/GC.

   **Prompting and non-interactive behavior**: GC prompts before removing anything.
   Explicit flags override: `--prune` = yes without prompting, `--no-prune` = skip GC
   entirely. When stdin is **not a TTY** (CI, scripts, headless wiring) and neither flag is
   given, GC is a **no-op**: it deletes nothing, prints what it would have pruned, and the
   wire still succeeds — an interactive prompt must never block or fail a scripted
   `pipx install minni && minni wire ...` (§1's stated goal). Never GC on failure.

   Rationale: `~/.minni/plugin/` is shared across all platforms, but wiring is
   per-platform at different times — a version-count-only policy could delete a dir still
   referenced by a never-re-wired platform's stamped absolute path (e.g. platform B stuck
   at 0.2.0 while platform A is upgraded twice), producing exactly the silent breakage the
   versioned-path decision exists to prevent. `--prune` reports any dirs it retained
   because they are still referenced, so operators know which platforms need re-wiring
   before space is reclaimed.

### 4.5 `minni wire <platform> --from-repo PATH` build flow (dev escape hatch)

The steps above describe the wheel-payload path. `--from-repo` is a distinct control
path — the one every developer exercises before a wheel is cut — and is specified here
rather than implied by cross-references:

1. **Build**: `cd <repo>/plugins/minni && npm run build` (requires Node ≥ 20 — checked
   first, same probe as §4.4 step 3; a missing toolchain fails here with the same
   actionable message). This produces plain tsc `dist/` output. The esbuild server bundle
   step (§4.2 step 2) also runs, so `--from-repo` installs get the same dependency-free
   `server.js` as wheels — dev and release payloads differ only in provenance, not shape.
2. **Version + manifest**: read the public version from the repo's `pyproject.toml`,
   compute `git rev-parse --short HEAD` and the dirty flag (`git status --porcelain`
   non-empty), and derive the dev version `<version>+git.<shortsha>[.dirty]`. Assemble
   the §4.1 file set from the repo tree (same junk exclusions as §4.2), stamp the four
   platform manifests, hash every file, and write a `payload-manifest.json` with the dev
   version, git SHA, dirty flag, `built_at`, and per-file SHA-256s — structurally
   identical to a staging-produced manifest (§5 schema), so every downstream consumer
   (step 4's hash branches, `--verify-payload`, `minni doctor`, GC) works unmodified.
3. **Continue at §4.4 step 3**: preflight, then steps 4–7 run as for a wheel payload,
   with the deltas below. Step 1 (importlib.resources locate) is replaced by the repo
   assembly above. **Step 2's gate semantics for `--from-repo` — the authoritative
   definition**: the gate is a *manifest self-check only* — the freshly written
   manifest must parse, carry the expected schema, and its `version` must equal the
   dev version derived in step 2 of this section (trivially true for a clean build,
   but it keeps the code path single). It deliberately does **not** compare anything
   against `importlib.metadata.version("minni")`: for an editable install
   (`pip install -e .`, the flow that reaches `--from-repo` per §6.1),
   importlib.metadata reflects the version frozen at the last `pip install -e .`, not
   a live read of pyproject.toml — so a routine pre-release version bump in
   pyproject.toml would otherwise hard-fail the exact workflow this escape hatch
   exists for. The public-version prefix of the dev version is read live from the
   repo's pyproject.toml (§4.5 step 2) and is authoritative for `--from-repo`.
   Remaining delta: the version-dir name is the dev version (collision prevention,
   §4.4 step 4), and dev installs never move `current` (§4.4 step 4).

For a `.dirty` build, repeated wires of the same short SHA can produce different content
under the same dev version string; step 4's hash-mismatch branch catches this and
`--force-reinstall` is the documented answer (quarantine + reinstall), matching the
release-payload semantics — no special dev-mode overwrite exists.

## 5. Data & interfaces

### payload-manifest.json (new)

```json
{
  "schema": 1,
  "version": "0.3.0",
  "git_sha": "<40-hex>",
  "built_at": "2026-07-04T00:00:00Z",
  "node_engine": ">=20",
  "files": { "dist/server.js": "sha256:...", "dist/hook.js": "sha256:..." }
}
```

Written only by staging; read by `minni wire` (gate) and `minni doctor` (staleness report).
`schema` allows forward evolution.

### CLI

```
minni wire <platform> [--agent NAME] [--workspace PATH] [--install-root PATH]
                      [--dry-run] [--verify-payload] [--prune | --no-prune]
                      [--force-reinstall] [--from-repo PATH] [--use-version VER]
```

`--agent/--workspace/--install-root` keep parity with `propagate.py update-plugin`
(propagate.py:945-956).

**The `generic` platform has a distinct argument and config contract** (propagate.py's
`platform_spec()` at 855-863 and `update_one_plugin` at 881-882, preserved verbatim in
`minni wire`): `--install-root` is **mandatory** (there are no default candidate
locations to probe; absent → hard error, same as propagate.py's SystemExit) and
`--agent` is **mandatory** (an unnamed generic wire would silently inherit another
agent's vault — the exact bug propagate.py's check exists to prevent; absent → hard
error). Its config kind is **mcp-json-only**: no per-platform config writer runs at
all — only the shared `.mcp.json` write (`mcp_json`). Every other platform's
required-argument set is unchanged; `generic` is called out because it is the one
platform whose omitted-argument behavior is a hard error rather than a default.

Every wire — including `--install-root` overrides and `generic` —
is recorded in `wired.json` keyed by (platform, config_path) (§4.4 step 7); for
non-default install roots that record is the *only* GC protection, and the `--install-root`
help text says so. `--force-reinstall` is the explicit override for a hash-mismatched
existing version dir (§4.4 step 4); `--prune`/`--no-prune` control GC without prompting,
and GC is a no-op when stdin is not a TTY and neither is given (§4.4 step 7).
`--from-repo` is the escape hatch for developers: wire from a repo checkout (requires
Node to build) instead of the wheel payload — its full build-manifest-install sequence
is specified in §4.5, not left implicit. There is deliberately **no `--no-build`**: the
wheel payload is prebuilt by definition.

`--use-version VER` is the **rollback/re-stamp mode** (§6.7). (Named `--use-version`,
not `--version`: the latter conventionally means "print the tool's version and exit",
and argparse users typing `minni wire --version` expecting that would silently trigger
a rollback instead.) Semantics:

- It selects an **already-installed** version dir `~/.minni/plugin/VER/` and re-runs
  steps 3 and 5–7 of §4.4 (Node/config-root preflight, config stamping, verify, record+GC)
  against it. It performs **no payload extraction** — steps 1, 2, and 4 (payload-locate,
  package-version gate, install) are skipped, so the
  `manifest.version == importlib.metadata.version("minni")` gate does not apply. Step 3
  is deliberately **not** skipped: a rollback still stamps configs pointing at
  `dist/server.js`, so the Node-version preflight must fail before any filesystem change
  exactly as in a normal wire (§6.3) — otherwise a rollback on a machine where Node has
  since been removed or downgraded would silently write a broken config. That gate exists to prevent installing a *stale payload from the current
  package*; `--use-version` is by definition targeting a *different, previously verified*
  install, so it is the one documented, explicit bypass of that gate.
- In place of the package-version gate, `--use-version` requires: the dir exists, contains a
  `payload-manifest.json` whose `version` field equals VER, and (with `--verify-payload`,
  recommended for rollback) its file hashes verify against that dir's own manifest.
- `--use-version` is mutually exclusive with `--from-repo`. `--dry-run` composes with it
  normally (shows the configs that would be re-stamped). A `--use-version` wire updates
  `wired.json` like any other, so GC (§4.4 step 7) will protect the rolled-back-to dir.
- **`current` skew after rollback is expected and inert.** Because `--use-version` skips step 4,
  the `current` symlink is deliberately **not** moved: it keeps pointing at the newest
  installed version while the rolled-back platform's config points at VER (and other
  platforms may still point at the newer version). This is well-defined because `current`
  is **advisory-only**: no consumer — not `minni doctor`'s wiring/drift check (§6.4, which
  compares stamped paths against wired.json only), not GC (which uses wired.json + the
  config scan), not the wire flow itself — ever compares a config against `current` or
  treats config-vs-`current` disagreement as a fault. `current` exists solely for humans
  and ad-hoc tooling to find "the newest installed payload".

### Output and exit-code contract (`minni wire`)

The minni-install skill and any wrapping script invoke `minni wire` programmatically
(§7), so its output is a contract specified with the same rigor as
payload-manifest.json — matching the precedent of propagate.py's machine-parseable
`{"status": "updated", "results": [...]}` JSON (propagate.py:955, 929-940):

- **stdout is a single JSON document** (human-oriented progress/warnings go to stderr):

  ```json
  {
    "schema": 1,
    "status": "ok | partial | failed | dry-run",
    "payload_version": "0.3.0",
    "install_root": "~/.minni/plugin/0.3.0",
    "results": [
      {"platform": "claude-code", "status": "wired | skipped | failed",
       "config_path": "~/.claude.json", "server_path": ".../dist/server.js",
       "agent": "...", "workspace": null,
       "verify": {"handshake": true, "hook_dry_run": true, "config_readback": true},
       "reason": null}
    ],
    "gc": {"pruned": [], "retained_in_use": [], "skipped_no_tty": false}
  }
  ```

  `results` has one entry per attempted platform (so `minni wire all` reports the
  gemini skip as `status: "skipped"` with a `reason`, per §4.4 intro). Degraded
  sub-steps that don't fail the wire (agy hook skipped, mcp-env-run fallback, §4.4
  step 5) are recorded in that platform's entry, never dropped.
- **Exit codes**: `0` = every attempted platform wired and verified (skips with a
  printed reason do not fail the run — matching §4.4 intro's `all` contract; a
  `--dry-run` that found no blocking condition also exits 0); `1` = at least one
  attempted platform failed (status `partial` if others succeeded, `failed` if none
  did); `2` = usage/preflight error before any filesystem change (unknown platform,
  missing mandatory `--agent`/`--install-root` for `generic`, Node missing/<20,
  version-gate mismatch). §9.4 asserts the exit code and parses stdout JSON in each
  scenario it tests.

### Filesystem layout on target

```
~/.minni/plugin/
├── 0.3.0/            # immutable once installed (enforced by §4.4 step 4's mismatch branch)
├── 0.2.x/            # previous, kept for rollback
├── wired.json        # wire records keyed by (platform, config_path) (§4.4 step 7)
└── current -> 0.3.0  # atomic symlink; ADVISORY ONLY — see open question 3 and §5 --use-version
```

Default: platform configs are stamped with the **versioned** path (not `current`), because a
config pointing at a concrete, hash-verified directory can never be silently retargeted; the
symlink exists for human/tooling convenience and rollback. The versioned-path default has a
corollary the design must honor: version dirs referenced by any platform's stamped config are
**pinned against GC** (`wired.json` + config-scan cross-check, §4.4 step 7) — otherwise a
platform wired once and never re-wired would be silently broken by a later platform's
upgrade, which is strictly worse than the retargeting this default was chosen to prevent.

## 6. Failure handling

1. **Payload missing from installed package** (editable install via `make setup`, or a wheel
   built without staging): `minni wire` detects absent `plugin_payload/` and errors:
   "no bundled plugin payload in this install; use `--from-repo ~/Projects/minni` (requires
   Node) or install a released wheel." Editable-dev flow keeps working via `--from-repo`.
2. **Version mismatch** (manifest ≠ importlib.metadata): hard error, no partial writes. This
   is structurally hard to hit because staging stamps from pyproject, but guards against
   stale `plugin_payload/` leftovers in a dev tree being picked up by a local build.
3. **Node missing / < 20**: preflight failure before any filesystem change, with install hint
   (macOS-first: `brew install node`).
4. **Host schema drift** (the real 2026-07-03 Gemini/Antigravity dir move): wire's platform
   spec probes an ordered candidate list of config roots per platform and refuses to guess
   when none exist (explicit error naming the probed paths + `--install-root` override).
   `minni doctor` gains a check that flags configs pointing at paths that no longer exist,
   or whose stamped `~/.minni/plugin/<v>` path disagrees with the version recorded for that
   (platform, config_path) in `wired.json` (or is absent from `wired.json` entirely) —
   turning the "stranded stale plugin" incident into a detected condition instead of silent
   breakage. The check deliberately compares against **wired.json, not the installed
   package version and not the `current` symlink**: an intentional `--use-version` rollback
   (§5, §6.7) leaves a config stamped at an older version than the installed package by
   design, and records that in wired.json — comparing against the package version or
   `current` would false-positive on every doctor run after a rollback. A wired.json-
   recorded older version is healthy; only a stamped path that wired.json doesn't account
   for is drift. (Configs predating wired.json are reported once as "unrecorded — re-wire
   to adopt", not as breakage.)
5. **Interrupted install**: staging-dir + atomic rename means `~/.minni/plugin/<version>/`
   either fully exists or doesn't; orphaned `.staging-*` dirs are swept on next run.
6. **server.js runtime deps**: eliminated by the esbuild bundle. A staging-time smoke test
   (`node dist/server.js` in an empty temp dir with no `node_modules` anywhere above it must
   reach MCP handshake) enforces "dependency-free" as a tested property, not a claim — this
   directly closes the verified gap between issue #142's sketch and reality (§2), and also
   fixes the latent fragility in today's copy_tree-without-node_modules installs.
7. **Rollback**: `minni wire <platform> --use-version <prev>` (defined in §5) re-stamps configs
   at the retained previous version dir. Because `--use-version` skips payload extraction and
   the package-version-equality gate (§5 defines this as the gate's one explicit,
   documented bypass), rolling back does not require downgrading the Python package —
   though reinstalling the previous wheel and re-running a plain wire is the equivalent
   full-downgrade path. No un-wire needed because old version dirs are retained and GC is
   reference-aware (§4.4 step 7): a version still stamped in any platform's config is
   never pruned.

## 7. Rollout / rollback plan

- **Phase 1 (v0.3.0)**: land staging + `minni wire` + writers ported (copied + adapted)
  into `src/minni/wire/` — the port list is §4.4 step 5's, and explicitly includes all
  three gemini-family writers (`update_gemini_manifest` deferred behind open question 8,
  `update_antigravity_config` and `update_agy_plugin_hooks` ported as-is). propagate.py is **not refactored in Phase 1** — its writer
  implementations stay in place as the documented dev path, and the parity test below is
  the explicit guard against the two copies drifting during the transition (this
  supersedes any "thin caller in Phase 1" reading of §4.4 step 5; the thin-caller refactor
  is Phase 2 work). One exception: Phase 1's PR **does** fix every hardcoded stale
  version-pinned literal in propagate.py, of which `DEFAULT_PLUGIN_CLI` is only one of
  (at least) four:
  1. `DEFAULT_PLUGIN_CLI` (propagate.py:50-52) — resolve dynamically from the wired
     install root instead of a pinned `0.1.0` path;
  2. `platform_spec()`'s codex install root, `~/.codex/plugins/cache/minni/minni/0.1.0`
     (propagate.py:816) — derive the version segment from the package/manifest version;
  3. `platform_spec()`'s claude-code install root,
     `~/.claude/plugins/cache/minni/minni/0.1.0` (propagate.py:822) — same fix;
  4. `update_gemini_manifest`'s literal `"version": "0.1.0"` (propagate.py:469), which
     re-injects a stale version into the written config on **every** wire, i.e. at
     runtime — outside the reach of §8's source-manifest lint. Fixed to take the real
     version; correspondingly, the **ported** gemini writer in `src/minni/wire/` takes its
     `version` field from `payload-manifest.json`, never a literal.
  A grep-based CI check (`make check-versions` extension, §8) asserts no
  `\b0\.\d+\.\d+\b`-style literal survives in propagate.py path/version constants, so new
  instances of the anti-pattern fail CI instead of rotting. A parity test asserts
  propagate.py and `minni wire` produce equivalent MCP config entries for the same inputs —
  reconciling the payload-width divergence (whole-tree vs. §4.1 subset) explicitly: the
  test compares *configs*, not trees, and docs state the wheel payload is intentionally
  narrower.
- **Phase 2 (v0.3.x)**: propagate.py's `update-plugin` is refactored into a **thin caller**
  that imports and delegates to `src/minni/wire/` (the repo dev environment always has the
  `minni` package importable via `make setup`'s editable install), eliminating the dual
  implementations the Phase-1 parity test was policing; the parity test then becomes
  trivially green and is retired or kept as a smoke test. minni-install skill instructs
  agents to prefer `minni wire`; propagate.py `update-plugin` prints a deprecation pointer.
- **Rollback of the feature itself**: `minni wire` is additive; reverting to v0.2 behavior is
  "keep using propagate.py". No existing flow is removed in v0.3.

## 8. Version-sync remediation (pre-existing drift)

Because drift is already live (0.2.0 vs 0.1.0 manifests, §2), the design includes a one-time
fix plus a guard:

1. One-time: bump the four platform manifests + confirm package.json to match pyproject.
2. Guard: `make check-versions` (run in CI on every PR) asserts pyproject.toml,
   plugins/minni/package.json, and the four platform manifests agree, **and** greps
   propagate.py for version-pinned path/version literals (the §7 Phase-1 anti-pattern
   list: propagate.py:51, 469, 816, 822) so the pattern cannot be reintroduced. This is a
   *lint on source*, independent of the staging-time stamping (§4.2 step 3), which
   guarantees the *shipped* payload is consistent even if the lint is ever bypassed —
   noting the lint alone was never sufficient for propagate.py:469, which stamps its stale
   version at *runtime* into the target machine's config; that one required the code fix
   in §7.

Single source of truth: pyproject.toml. Everything else is checked or stamped.

## 9. Testing

1. **Import-graph guard** (unit, fast): script asserts no file in `plugins/minni/src/` other
   than `server.ts` has non-relative, non-`node:` imports — pins the verified property that
   hooks/cli are dependency-free, and fails loudly if a future PR adds an external import to
   a hook.
2. **Bundle smoke** (staging): `node dist/server.js` MCP handshake + one hook invocation with
   sample stdin JSON, executed in a temp dir with no reachable `node_modules` (§6.6). Note:
   the handshake and hook-dry-run harnesses are **new code** written for this feature (see
   §4.4 step 6 — nothing existing performs either check); they are shared between this
   staging test, wire's post-install verify, and the new `minni doctor` plugin check.
3. **No-Node wheel build + payload-completeness** (CI): from an sdist produced by `make
   stage-payload && python -m build --sdist`, build the wheel in a container/venv with Node
   absent from PATH; assert success. Then, rather than spot-checking two files, parse
   `payload-manifest.json` out of the wheel and assert that **every path in its `files`
   map is present in the wheel's file listing** (and, for full strength, that its SHA-256
   matches). This makes the deepest nesting (`skills/minni-install/scripts/*.py`)
   round-trip a tested property — a spot-check would pass even if setuptools silently
   dropped an entire nested tree (the known failure mode §4.2 step 5 guards against).
   A companion **glob-coverage unit test** (runs on every PR, no staging needed) derives
   its expected set from the **npm `files` allowlist in plugins/minni/package.json**
   (today's authoritative payload, §2) minus the explicit §4.1 exclusion list
   (frontend-src/, *.ts sources, tests/, node_modules/, vite output, package.json/
   lockfiles) and the §4.2 junk exclusions — **not** from §4.1's tree diagram. It walks
   every remaining source directory containing files and asserts each maps to a declared
   package-data glob line. Deriving from the npm allowlist rather than this document's
   own tree is deliberate: it makes the test catch scope-narrowing bugs in the design
   itself (a §4.1 tree that silently drops a shipped directory fails the test), not just
   missing glob lines — so a new subdirectory fails CI at PR time, before the
   release-time fail-hard check in §4.2 step 5 would catch it. Any *intentional* future
   exclusion must be added to the test's explicit exclusion list, making the decision
   reviewable in the diff.
4. **Wire integration** (macOS CI or local): install wheel into fresh venv, `HOME=$(mktemp
   -d) minni wire claude-code --dry-run` and real run; assert `~/.minni/plugin/<version>/`
   layout, `current` symlink, config contents, idempotency (second run = no-op), and the
   version-mismatch and missing-node error paths. Also covered: the hash-mismatch hard
   error + `--force-reinstall` quarantine path (§4.4 step 4); GC no-op when stdin is not a
   TTY (§4.4 step 7); wired.json upsert keyed by (platform, config_path) — two wires of
   the same platform to different `--install-root`s must both survive in wired.json and
   both be GC-protected; **concurrency**: (a) two `minni wire` processes run in parallel
   for different platforms — both wired.json entries must survive (no lost update; §4.4
   step 7 lock), and (b) two parallel wires targeting the same not-yet-installed version
   must both succeed with exactly one install and one idempotent skip, no
   `ENOTEMPTY`/`EEXIST` traceback (§4.4 step 4 lock); **`all` expansion** (§4.4 intro):
   `minni wire all` in v0.3 wires exactly codex, claude-code, kilocode, grok, prints the
   gemini-provisional skip warning, and never touches antigravity or generic; `--use-version`
   rollback on a PATH without Node must fail at preflight with no config written (§4.4
   step 2 / §5 / §6.3); and doctor's no-false-positive-after-`--use-version`-rollback
   property (§6.4).
5. **Parity test** (transition): propagate.py vs `minni wire` config-entry equivalence (§7).
6. **check-versions lint** in CI (§8).

## 10. Explicit open questions

1. **esbuild vs. dependency reclassification**: bundling server.js is the recommendation; the
   lighter alternative is moving `react`/`react-dom` to devDependencies and vendoring a pruned
   prod `node_modules` (MCP SDK + zod + transitives) into the payload. Bundle chosen for size
   and the "no node_modules anywhere" guarantee, but if esbuild output breaks an MCP SDK
   dynamic-import pattern, vendoring is the fallback. Needs a spike.
2. **Payload breadth**: do `commands/` and all four platform manifest dirs ship in v0.3, or
   only the manifests for platforms `minni wire` supports at launch? Shipping all (matching
   the npm `files` list) is the default here; trimming is a size optimization only.
3. **`current` symlink in configs**: stamping versioned paths (chosen) means every upgrade
   rewrites configs; stamping `current` means upgrades are symlink-flips but configs can be
   silently retargeted. Chosen versioned-path default should be revisited if config-rewrite
   churn proves painful across many platforms.
4. **sdist-from-git consumers**: someone running `pip install git+https://…` gets no payload
   (staging never ran). Declared unsupported-with-clear-error in this design (§6.1); is that
   acceptable, or should a PEP 517 build hook *optionally* run staging when Node is present?
5. **UI (`ui-server.ts` + vite frontend)**: verified to have no external imports server-side,
   but the frontend bundle is a vite artifact not in scope of issue #142's payload list. Ship
   in v0.3 payload or keep repo-only? Default here: excluded.
6. **propagate.py end state**: after `minni wire` reaches parity, does propagate.py's
   `update-plugin` get removed in v0.4, or retained permanently as the dev/from-source path
   behind `minni wire --from-repo`?
7. **Claude Code native-plugin registration** (deferred from §4.4 step 5): v0.3 wires Claude
   Code via an MCP-server entry in `~/.claude.json` only. If native-plugin loading (hooks,
   commands, skills via `${CLAUDE_PLUGIN_ROOT}`) is wanted for wheel installs, someone must
   determine the concrete registration mechanism: either install the payload into a
   directory Claude Code's plugin loader actually scans, or use whatever registration
   command/config Claude Code exposes for non-marketplace plugin roots — and reconcile that
   with the §7 parity test (which compares `~/.claude.json` entries and has no counterpart
   for host-resolved template paths). Until that mechanism is specified and verified against
   a real Claude Code install, native-plugin registration is out of scope.
8. **Gemini `${extensionPath}` extension-manifest templating** (carved out from §4.4
   step 5; deliberately **scoped down** during hardening): the same
   host-resolved-template problem as open question 7, for the platform whose drift
   incident motivated this design. What is **not** open: antigravity's surface-view
   writer (`update_antigravity_config`) already stamps absolute paths and ports now
   (§4.4 step 5 carve-out), and `update_agy_plugin_hooks` ports with its existing
   graceful-degradation contract. What **is** open is only `update_gemini_manifest`
   (propagate.py:464-484): it does not stamp an absolute path at all — it writes
   `${extensionPath}`-relative `args`/`cwd` that Gemini's extension loader resolves only
   for extension directories it discovers (today `~/.gemini/extensions/`; post-drift
   possibly `~/.gemini/config/plugins/`, per §2). A `gemini-extension.json` dropped under
   `~/.minni/plugin/<version>/` resolves to nothing — porting that writer verbatim would
   silently fail to wire Gemini, reproducing exactly the stranded-plugin failure this
   design exists to close. Two candidate mechanisms, to be verified against a real
   Gemini install before `minni wire gemini` ships as non-provisional (and before
   antigravity's flow re-adds its extension-manifest sync leg, propagate.py:918): (a) a
   settings-level MCP entry with a stamped absolute `server_path` (propagate.py's
   `gemini_minni_entry`, propagate.py:490+, already takes a concrete `server_path` —
   this matches the design's convention and is the presumptive answer); (b)
   install/symlink the payload into the directory Gemini's extension loader actually
   scans, keeping the `${extensionPath}` template. Whichever is chosen, the §7 parity
   test for gemini compares the concrete config the mechanism writes, not the
   extension-manifest template, and the ported writer takes its `version` field from
   payload-manifest.json, not a literal (§7 remediation list). While this remains open,
   `minni wire all` excludes gemini with a warning (§4.4 intro).
