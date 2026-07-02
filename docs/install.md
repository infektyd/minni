# Install & troubleshooting

## Supported install (source checkout)

Requirements: `git`, `make`, Node >= 20 (`.nvmrc`). Python 3.14 is required by
the engine (`.python-version`) but you do not have to install it yourself: if
your system `python3` is older and [uv](https://docs.astral.sh/uv/) is on your
PATH, `make setup` provisions a uv-managed Python 3.14 automatically.

```bash
git clone https://github.com/infektyd/minni.git && cd minni
make setup
```

`make setup` builds `engine/.venv` (from the pinned `engine/requirements.lock`),
installs the `minni` CLI into the venv, runs `npm ci` for the plugin, and
enables the repo's git hooks. First daemon use downloads ~320 MB of embedding
models into your HuggingFace cache — this is announced, happens once, and is
the main reason the first run takes a few minutes.

## Daemon lifecycle

```bash
engine/.venv/bin/minni up        # start in the background (PID + logs under ~/.minni)
engine/.venv/bin/minni status    # plain-language daemon + engine health
engine/.venv/bin/minni doctor    # verify the install end to end
engine/.venv/bin/minni down      # stop
```

Equivalents: `make daemon` runs the daemon in the foreground; `make doctor`
wraps the doctor. The daemon listens on a Unix socket at
`~/.minni/run/minnid.sock` (0600, in a 0700 run dir) — no TCP port by default.

`minni doctor` runs the same probes CI's hermetic smoke runs on every push:
interpreter floor, socket presence and permissions, `status` RPC shape
(`daemon` + `engine`), a recall round-trip, and model-cache presence. If
doctor passes, the install works.

For a login-persistent daemon on macOS, a launchd template ships at
`engine/launchd/com.minni.minnid.plist.example` (restart with
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
engine/.venv/bin/python engine/tools/author_principals.py            # dry-run (default)
engine/.venv/bin/python engine/tools/author_principals.py --apply    # write principals/*.json (0600)
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
(re)built manually from `engine/`:

```bash
engine/.venv/bin/python engine/index_all.py --vault-ingest-all            # from the repo root
engine/.venv/bin/python engine/index_all.py --vault-ingest-all --dry-run
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
