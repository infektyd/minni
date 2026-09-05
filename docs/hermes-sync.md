# Existing Hermes source bindings during sync

`minni sync` and `minni sync --full` inspect an existing enabled Minni entry in `~/.hermes/config.yaml`, even without a wire registry record. This is a read-only compatibility check: it does not install Hermes, activate a missing or disabled binding, rewrite YAML, change identity/environment/workspace, or reconnect sessions.

The supported binding uses `command: node` (or `nodejs`), a single absolute argument pointing to the checkout's `plugins/minni/dist/server.js`, and an optional string-to-string `env` mapping. Environment assignments belong in `env`, not after a script argument named `--env`. Custom launchers and other checkout paths are preserved and reported incomplete.

After an editable checkout build, the check requires clean source/build metadata at the same commit and source server bytes matching the verified installer payload. A missing installer target cannot establish this comparison. Dry runs report that artifacts have not been validated. Packaged sync cannot update a source-checkout binding and reports it incomplete; build and sync the corresponding checkout instead. No automatic migration to a versioned payload occurs.

A verified artifact is reported separately from loaded state. Existing Hermes sessions still need their native `/reload-mcp` action; new sessions load the current artifact. Rebuilding a stable server path does not change Hermes configuration and therefore does not trigger its configuration watcher. This check does not claim that a session is running or that its reload completed. Native reload may reconnect other MCP servers too.

The YAML reader rejects duplicate keys and malformed supported fields. Configuration symlinks are preserved and reported incomplete. An absent host executable, absent configuration/binding, or explicitly disabled entry is skipped. Parser unavailability or validation errors on an existing configuration are reported without emitting environment values. Neither this check nor a successful daemon restart establishes that external gateway or other host sessions have reloaded.
