# Grok

Wire Grok to a running Minni daemon from your checkout:

```bash
.venv/bin/minni up   # if the daemon isn't already running
minni wire grok                                   # v0.3+ wheel (bundled payload)
.venv/bin/minni wire grok --from-repo .           # v0.2 wheel / source checkout
```

(`propagate.py update-plugin --platform grok` from the checkout still works as
the legacy path.)

Grok uses the standard `minni@minni` plugin install: the plugin lands under
`~/.agents/plugins/minni@minni`, is wired via `~/.grok/config.toml`, and gets
a Grok-specific hook entrypoint (`plugins/minni/src/grok-hook.ts` /
`plugins/minni/hooks/hooks-grok.json`).

Like every wired runtime, Grok shares the daemon's memory pool under its own
agent identity — recall is shared (scope-governed), durable writes go through
the propose→approve gate, and cross-agent work moves via handoffs.

Verify: from a Grok session, call `minni_status` and check `socket.ok` and the
vault path.
