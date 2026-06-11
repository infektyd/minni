# slice-4-plugin-hygiene

## Status

✅ Proven / tested

## Red Test

Command run from `plugins/minni/` after adding tests and before implementation:

```bash
PATH=/Users/hansaxelsson/Projects/Minni/plugins/minni/node_modules/.bin:$PATH npm run build:server && node --test --import ./tests/setup-env.mjs tests/config.test.mjs
```

Expected RED result:

```text
2 failed, 2 passed
```

The failures proved:

- no-env defaults still resolved to `DEFAULT_AGENT_ID="codex"`
- the Codex MCP manifest had no explicit `MINNI_AGENT_ID` env block

## Green Tests

Focused command from `plugins/minni/`:

```bash
PATH=/Users/hansaxelsson/Projects/Minni/plugins/minni/node_modules/.bin:$PATH npm run build:server && node --test --import ./tests/setup-env.mjs tests/config.test.mjs
```

Result:

```text
4 passed, 0 failed
```

Plugin build gate from `plugins/minni/`:

```bash
PATH=/Users/hansaxelsson/Projects/Minni/plugins/minni/node_modules/.bin:$PATH npm run build
```

Result:

```text
tsc && vite build
✓ built in 92ms
```

## Implementation Evidence

✅ Proven / tested: `plugins/minni/.mcp.json` now pins the Codex MCP server env:

- `MINNI_AGENT_ID=codex`
- `MINNI_VAULT_PATH=~/.minni/codex-vault`
- `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`

✅ Proven / tested: `plugins/minni/src/config.ts` now fails safe when no env is present:

- `DEFAULT_AGENT_ID="unknown-agent"`
- `DEFAULT_VAULT_PATH=~/.minni/unknown-vault`

✅ Proven / tested: generic `MINNI_*` env still overrides Codex-specific env, and Codex-specific env remains a compatibility fallback.

## Notes

The worktree did not have local `node_modules`; I used the already-installed main-checkout plugin dependencies by temporarily symlinking `plugins/minni/node_modules` inside this worktree for verification, then removed the symlink before committing. No package install or network call was run.

## Diff

See `evidence/slice-4-plugin-hygiene/diff.patch`.
