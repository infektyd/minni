# Install & troubleshooting

## PyPI install (daemon + CLI)

Since v0.2 the daemon and CLI install from PyPI — no checkout, no Node:

```bash
pipx install minni     # or: uv tool install minni
minni up
minni doctor
```

This gives you the `minni` and `minnid` commands and the full engine.

## Docs honesty vs next-PR goals

Operator docs must not lie about **present** behavior. Accuracy work must
**not** erase wanted capabilities by only deleting overclaims — see
[docs/ops/docs-truth-policy.md](ops/docs-truth-policy.md) (honesty now +
`goal_next_pr` / PARTIAL with a pointer).

## Keep every agent host current (`minni sync`)

After you upgrade the package **or** pull a newer `main` in a dogfood
checkout, the **daemon can move while each agent host still points at last
week's plugin tree**. That is a real failure mode, not a power-user tip.

```bash
minni sync              # redeploy plugin payload to all wire-primary hosts
                        # + refresh cursor/antigravity (D7 partition)
minni doctor            # WARN fleet if still stale; always names `minni sync`
```

| You installed via… | After the install moves… |
|---|---|
| **PyPI / pipx** | `pipx upgrade minni` then **`minni sync`** |
| **Editable checkout** (contributor dogfood) | `git pull` (clean `main`) then **`minni sync`**, or **`minni sync --full`** for pull + rebuild + redeploy (same as `make sync-root`) |

`minni sync` always force-reinstalls the active version dir so a same-version
payload rebuild cannot leave hosts on old hashes. Restart agent apps after a
successful sync so they reload MCP/`server.js`.

### Optional: unattended checkout sync (macOS)

If this machine dogfoods an editable checkout of Minni and you want
`origin/main` to redeploy itself without remembering:

```bash
minni sync --install-auto    # launchd every 6h → update_root.sh
minni sync --auto-status
minni sync --uninstall-auto
```

Unattended runs **refuse** a dirty or diverged tree (they never discard your
work). Prefer a clean dedicated checkout for timers. Details:
[deploy/README.md](../deploy/README.md).

## Wire agent runtimes (`minni wire`)

Agents reach the daemon through the MCP plugin, which `minni wire <platform>`
installs to a versioned dir under `~/.minni/plugin/` and registers with the
runtime's own config (MCP server entry, per-agent vault path, hook
entrypoints). Node >= 20 must be on PATH — the preflight checks and tells you
if it isn't.

```bash
minni wire claude-code        # or: codex, kilocode, grok, generic, all
```

- Wheels from **v0.3** bundle the plugin payload, so this works straight from
  `pipx install minni`. On a v0.2 wheel (or an editable install) there is no
  bundled payload; wire from a source checkout instead:
  `minni wire <platform> --from-repo /path/to/minni` (builds with Node, then
  runs the identical install + verify path, versioned as
  `<version>+git.<sha>`).
- `all` wires codex, claude-code, kilocode, and grok (`ALL_EXPANSION_V03`).
  Cursor and antigravity are fleet-known but skipped from bulk wire
  (cursor is propagate-managed; antigravity shares the Gemini tree and is
  explicit-only). **Gemini family:** prefer `minni wire antigravity` or
  `propagate --platform antigravity` (also covered by `make sync-root`); pure
  `gemini` remains provisional-skip on wire (`gemini-provisional`), not the
  day-to-day install path. `generic` requires `--agent` and `--install-root`.
- Attempted wiring ends with verification probes (MCP handshake, hook dry-run,
  config readback). Re-run `minni wire <platform>` to repeat them; `minni
  doctor` never substitutes for wire verify (doctor is interpreter / socket /
  status / recall / models only). Output is a single JSON document on stdout
  with per-platform results; exit code 0 = all attempted platforms wired, 1 =
  a failed attempt or an all-skipped run, 2 = preflight/usage error before any change.
- Old version dirs are pruned only when no runtime's config references them
  (`--prune` / `--no-prune`; prompts are skipped when stdin isn't a TTY).
  Fleet `minni sync` and `sync --full` defer garbage collection and retain old
  payloads because native hooks or custom MCP bindings may deliberately remain
  on them. Sync refreshes existing enabled Muse and Devin JSON MCP bindings
  when their executables are available; unsupported custom formats are reported
  as skipped. This does not install native hooks or verify those hosts are running.
  If every canonical host is skipped, wire produces no new payload target;
  custom-only refresh then requires an explicitly verified installed root through
  `python -m minni.wire.custom_refresh --new-root <versioned-payload-root>`.
  `--use-version <ver>` re-stamps a platform's config against an
  already-installed version — rollback without touching the Python package.
- Host availability is checked before builds or configuration changes. A leftover
  config directory does not establish that a host is installed. Unavailable
  hosts skip; unreadable or malformed configuration is a visible failure.
  Bulk refresh requires an existing, enabled Minni binding. Use an explicit
  named wire command for initial setup on an installed host.
- `minni sync`, `make sync-root`, and bulk propagation preserve disabled bindings
  and MCP-only setups. They refresh recognized existing native hooks without
  adding missing event subscriptions or enabling host plugins. Standalone
  propagation accepts `--existing-only` for the same policy. An all-skipped
  propagation run exits zero with `status: skipped`; it did not update anything.
- A successful MCP probe and hook dry-run do not establish delivery by an already
  running host. Reload the integration in that host and check a real event when
  lifecycle delivery matters. Antigravity bulk refresh preserves its existing
  registration state and reports that registration has not been verified.
- The agent-driven `minni-install` skill handles first-time identity and
  vault seeding after the wire.
- **After wire adoption**, do **not** re-run
  `propagate.py update-plugin --platform codex|kilocode|grok` (or bulk
  `propagate --platform all` expecting those hosts). Propagate's `all` expands
  only to antigravity + cursor; explicit codex/kilocode/grok propagate still
  rewrites MCP onto legacy cache/agents trees. Prefer `minni wire <platform>`
  (or `make sync-root` from a checkout) to refresh wire-primary hosts. See
  [deploy/README.md](../deploy/README.md).

## Keep a live install current (checkout operators)

If you dogfood from a source checkout (editable install + wire-managed plugin
tree), the day-to-day refresh path is **`make sync-root`**, not a hand-rolled
mix of pull / wire / propagate. It fast-forwards to `origin/main`, refreshes
the editable install, rebuilds the plugin, redeploys with the D7 fleet
partition (`minni wire all --from-repo` then propagate for antigravity +
cursor only), restarts the daemon when launchd is loaded, and verifies with
`check_versions` / `check_deployments --strict`.

```bash
make sync-root            # do it
make sync-root DRY_RUN=1  # plan only
minni status              # look for the deploy block: deploy.stale / plugin_dist
```

`minni status` (and the daemon's status RPC) expose a `deploy` block
(`deploy_honesty.py`) that reports when the running process or the deployed
plugin dist lags the checkout — including nested `plugin_dist.stale`, which
rolls up into top-level `deploy.stale`. Details:
[deploy/README.md](../deploy/README.md).

## Source install (contributors + `--from-repo` wiring)

Requirements: `git`, `make`, Node >= 20 (`.nvmrc`). Python 3.14 is required by
the engine (`.python-version`) but you do not have to install it yourself: if
your system `python3` is older and [uv](https://docs.astral.sh/uv/) is on your
PATH, `make setup` provisions a uv-managed Python 3.14 automatically.

```bash
git clone https://github.com/infektyd/minni.git && cd minni
make setup
```

`make setup` builds `.venv` (from the pinned `requirements.lock`),
installs the `minni` CLI into the venv, runs `npm ci` for the plugin, and
enables the repo's git hooks. First daemon use downloads ~320 MB of embedding
models into your HuggingFace cache — this is announced, happens once, and is
the main reason the first run takes a few minutes.

## Daemon lifecycle

```bash
.venv/bin/minni up        # start in the background (PID + logs under ~/.minni)
.venv/bin/minni status    # plain-language daemon + engine health
.venv/bin/minni doctor    # verify the install end to end
.venv/bin/minni down      # stop
```

Equivalents: `make daemon` runs the daemon in the foreground; `make doctor`
wraps the doctor. The daemon listens on a Unix socket at
`~/.minni/run/minnid.sock` (0600, in a 0700 run dir) — no TCP port by default.

`minni doctor` is the local install subset: interpreter floor, socket
presence and permissions, `status` RPC shape (`daemon` + `engine`), a recall
round-trip, and model-cache presence. CI's hermetic smoke
(`scripts/repro-smoke.sh` / `make smoke`) additionally proves status + recall
under a throwaway `MINNI_HOME` (home isolation) — doctor does **not** assert
that. Doctor also does **not** run wire verify probes (MCP handshake, hook
dry-run, config readback). If doctor passes, the daemon is up and answering
recalls. It does **not** fully wet-exercise the background AFM consolidation
loop (`MINNI_AFM_LOOP`, default off; functional since
[#119](https://github.com/infektyd/minni/issues/119) closed), so doctor stays
green whether that opt-in path is healthy or not.

For a login-persistent daemon on macOS, a launchd template ships at
`src/minni/launchd/com.minni.minnid.plist.example` (restart with
`launchctl kickstart -k gui/$UID/com.minni.minnid`, stop with
`launchctl bootout gui/$UID/com.minni.minnid`).

The daemon raises its own file-descriptor soft limit at startup
(`_raise_fd_ceiling` in `src/minni/minnid.py`), but launchd's default soft
limit (256) is low enough to starve the window before that runs, so the
plist template also sets `SoftResourceLimits`/`NumberOfFiles`. If you have a
live plist without that key, add it and reload with a full
`launchctl bootout` + `bootstrap` — `kickstart` alone does not re-read plist
changes. See the comments in
`src/minni/launchd/com.minni.minnid.plist.example` for details, and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#daemon-hits-the-file-descriptor-ceiling-under-sustained-load)
if you're chasing `EMFILE`/`EPIPE` symptoms.

Logging knobs: `MINNI_LOG_LEVEL` (`DEBUG`/`INFO`/…) and `MINNI_LOG_FORMAT`
(`text` default, `json` for structured output).

## Provision agent identities (principals)

The daemon fail-closes any **named** caller it cannot attribute: an agent that
supplies an `agent_id` (the shipped plugins always do — `claude-code`, `codex`,
…) needs a matching operator-owned `~/.minni/principals/<agent>.json` before
gated tools and handoffs work. Without it, gated calls return a structured
`recovery_required` route (reason `unknown_identity`) telling you exactly this.
Author the shipped agents' files from the repo root:

```bash
.venv/bin/python -m minni.tools.author_principals            # dry-run (default)
.venv/bin/python -m minni.tools.author_principals --apply    # write principals/*.json (0600)
```

For an unlisted agent, hand-author `~/.minni/principals/<agent>.json` (for
example `{"agent_id": "myagent", "capabilities": ["search", "read", "learn",
"handoff"]}`) and `chmod 600` it. Either way, `kill -HUP` the daemon (or
restart it) so identity caches reload.

Only the anonymous caller — one that omits `agent_id` entirely — gets the
zero-config operator synthesis on a fresh install. Explicitly claiming the
reserved ids `main`/`operator` over the wire is always denied (with a
`reserved_agent_id` diagnostic) unless the daemon itself runs with
`MINNI_LOCAL_OPERATOR=1`. See the strict-mode caveat in
[concepts.md](concepts.md#delegating-approval) before authoring your first
principal file.

## Docker eval image

To evaluate the daemon without any local Python/Node setup:

```bash
docker run --rm -it -v minni-data:/home/minni ghcr.io/infektyd/minni:latest
```

The image is engine-only, runs as a non-root user, downloads models lazily at
runtime (announced), and persists memory in the `minni-data` volume. It is the
demo/eval channel — the supported day-to-day install is **`pipx install minni`**
plus **`minni wire <platform>`** (vaults and agent wiring live on your
machine). Source checkout is for contributors, `--from-repo` wiring when the
wheel payload is missing or you are dogfooding `main`, and
`make sync-root` / fleet redeploy — not the default operator path.

## Manual vault indexing

Personal vault indexes are built by the `vault_ingest` pass, and can be
(re)built manually from the repo root:

```bash
.venv/bin/python -m minni.index_all --vault-ingest-all            # from the repo root
.venv/bin/python -m minni.index_all --vault-ingest-all --dry-run
```

## Development checks

```bash
make check    # fast gate: lint + typecheck + plugin build/test + scoped engine pytest
make test     # full suites (heavy: loads embedding/FAISS models)
make smoke    # hermetic daemon smoke in a throwaway MINNI_HOME
```

Both the smoke and the engine pytest suite force a throwaway `MINNI_HOME`, so
they cannot create or mutate your live `~/.minni`. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for the full workflow.

## Migrating a v0.1 checkout

If you installed Minni before the v0.2 package restructure (flat `engine/`
layout), bring your checkout current:

1. Pull the latest changes.
2. Run `make setup` (rebuilds the venv at root `.venv`).
3. Re-wire your platforms to re-stamp configs:
   ```bash
   .venv/bin/minni wire <yours> --from-repo .
   ```
   Prefer `minni wire` for codex/claude-code/kilocode/grok after wire adoption;
   `propagate.py update-plugin` remains the path for cursor and for the Gemini
   family via **antigravity** (pure `gemini` is provisional). Do not
   bulk-propagate wire-primary platforms expecting a fleet refresh — see
   [deploy/README.md](../deploy/README.md).
4. For launchd users: update the plist's three paths — python interpreter →
   `/path/to/repo/.venv/bin/python`, script args → `-m minni.minnid`,
   `WorkingDirectory` → repo root — then run:
   ```bash
   launchctl bootout gui/$UID/com.minni.minnid && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.minni.minnid.plist
   ```
5. The old `engine/.venv` can be deleted afterwards.

## Troubleshooting

- **`Socket not found`** — the daemon isn't running: `minni up` (or
  `make daemon`), then retry. A stale socket left by a crash is removed
  automatically on the next daemon start.
- **First recall hangs for minutes** — it's the one-time model download; the
  daemon announces it with sizes. Subsequent starts are fast.
- **`Python 3.14+ is required`** — install Python 3.14, or install uv and
  re-run `make setup` (uv downloads the interpreter for you).
- **A daemon answers but `minni down` refuses** — the daemon wasn't started by
  `minni up` (no PID file); stop it where it was started (the `make daemon`
  shell, or launchd).

Deeper operational issues: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
