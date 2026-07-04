# Install & troubleshooting

## PyPI install (daemon + CLI)

Since v0.2 the daemon and CLI install from PyPI — no checkout, no Node:

```bash
pipx install minni     # or: uv tool install minni
minni up
minni doctor
```

This gives you the `minni` and `minnid` commands and the full engine.

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
- `all` wires codex, claude-code, kilocode, and grok. Gemini wiring is
  provisional (skipped with a warning; use the checkout's
  `propagate.py update-plugin --platform gemini` for now), and
  antigravity/`generic` are always explicit single-platform wires. `generic`
  requires `--agent` and `--install-root`.
- Every wire ends with verification probes (MCP handshake, hook dry-run,
  config readback); the same probes run in `minni doctor`. Output is a single
  JSON document on stdout with per-platform results; exit code 0 = all
  attempted platforms wired, 1 = at least one failed, 2 = preflight/usage
  error before any change.
- Old version dirs are pruned only when no runtime's config references them
  (`--prune` / `--no-prune`; prompts are skipped when stdin isn't a TTY).
  `--use-version <ver>` re-stamps a platform's config against an
  already-installed version — rollback without touching the Python package.
- The agent-driven `minni-install` skill handles first-time identity and
  vault seeding after the wire.

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

`minni doctor` runs the same probes CI's hermetic smoke runs on every push:
interpreter floor, socket presence and permissions, `status` RPC shape
(`daemon` + `engine`), a recall round-trip, and model-cache presence. If
doctor passes, the daemon is up and answering recalls. It does not exercise
the background AFM consolidation pass
(see [#119](https://github.com/infektyd/minni/issues/119) for that path's
history), so doctor stays green regardless of that path's health.

For a login-persistent daemon on macOS, a launchd template ships at
`src/minni/launchd/com.minni.minnid.plist.example` (restart with
`launchctl kickstart -k gui/$UID/com.minni.minnid`, stop with
`launchctl bootout gui/$UID/com.minni.minnid`).

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
demo/eval channel — the supported day-to-day install is the source checkout
above, because Minni's value is vaults living on your machine next to your
editors and agent runtimes.

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
   (`propagate.py update-plugin --platform <yours>` still works and remains
   the path for gemini while its wiring is provisional.)
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
