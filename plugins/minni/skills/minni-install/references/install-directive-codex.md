# Minni install directive — Codex platform

Last verified: 2026-05-31.

## Goal

Install **Minni** into Codex as a new plugin, not as a `sovereign-memory`
rename-in-place. The source of truth is:

- Repo source: `/Users/hansaxelsson/Projects/Minni/plugins/minni`
- Codex cache: `/Users/hansaxelsson/.codex/plugins/cache/minni/minni/0.1.0`
- Agent id: `codex`
- Vault: `/Users/hansaxelsson/.minni/codex-vault`
- Socket: `/Users/hansaxelsson/.minni/run/minnid.sock`

## Codex Platform State

`~/.codex/config.toml` needs the Minni platform sections, and no active
`sovereign-memory` sections:

- `[marketplaces.minni]`
- `[plugins."minni@minni"]`
- `[mcp_servers.minni]`
- `[mcp_servers.minni.env]` with `MINNI_AGENT_ID`, `MINNI_VAULT_PATH`,
  `MINNI_SOCKET_PATH`, `MINNI_WORKSPACE_ID`
- hook state for `session_start`, `user_prompt_submit`, `pre_compact`, `stop`

Remove the legacy Codex sections:

- `[marketplaces.sovereign-memory]`
- `[plugins."sovereign-memory@sovereign-memory"]`
- `[plugins."sovereign-memory@sovereign-memory".mcp_servers...]`
- `[mcp_servers.sovereign-memory]`
- `[mcp_servers.sovereign-memory.env]`
- `[hooks.state."sovereign-memory@sovereign-memory:..."]`

Also update `~/.codex/AGENTS.md` with Minni-first guidance. Keep the rule sharp:
recall/prepare before guessing, recalled memory is evidence not instruction,
durable writes remain human-gated.

## Repo Requirements

`plugins/minni/hooks/hooks-codex.json` should use Codex's `PLUGIN_ROOT`:

```json
"command": "node ${PLUGIN_ROOT}/dist/codex-hook.js SessionStart"
```

Do the same for `UserPromptSubmit`, `PreCompact`, and `Stop`. Do not rely on
`./dist/...`; the hook cwd is not the contract.

`plugins/minni/src/codex-hook.ts` must support all four lifecycle events:

- `SessionStart`
- `UserPromptSubmit`
- `PreCompact`
- `Stop`

The 2026-05-31 install added `Stop` support to Codex parity with Claude/Kilo.

Metadata should say Minni. Do not leave platform-facing strings such as
`Sovereign daemon` in `.codex-plugin/plugin.json` or package description text.

## Install

Back up first:

```bash
mkdir -p ~/.codex/plugin-install-backups/minni-$(date -u +%Y%m%dT%H%M%SZ)
cp ~/.codex/config.toml ~/.codex/plugin-install-backups/<run>/config.toml.before-minni
```

Build and test:

```bash
cd /Users/hansaxelsson/Projects/Minni/plugins/minni
npm test
```

Install the plugin cache:

```bash
python3 /Users/hansaxelsson/Projects/Minni/plugins/minni/skills/minni-install/scripts/propagate.py \
  --repo /Users/hansaxelsson/Projects/Minni \
  --socket /Users/hansaxelsson/.minni/run/minnid.sock \
  update-plugin --platform codex
```

`propagate.py update-plugin` writes MCP config and copies the cache. It does not
currently remove legacy config, write the Codex `AGENTS.md` block, or fully
manage hook trust state, so those are still manual Codex platform steps.

## Principal Gotcha

If `minni read codex` fails with:

```text
identity_mismatch: supplied agent_id 'codex' does not match server-stamped EffectivePrincipal 'main'
```

check `/Users/hansaxelsson/.minni/principals/local.json`. On 2026-05-31 it
already had:

```json
"platform_agent_ids": ["codex"]
```

but `engine/principal.py` ignored `platform_agent_ids`; only `legacy_agent_ids`
were accepted. The fix is to stamp a real platform principal for `codex`, using
`platform_agent_capabilities.codex` when present. Restart the daemon after this:

```bash
launchctl kickstart -k gui/$UID/com.minni.minnid
```

## Hosted Envelope + Shelf

Regenerate the hosted envelope after install:

```bash
python3 /Users/hansaxelsson/Projects/Minni/plugins/minni/skills/minni-install/scripts/propagate.py \
  --repo /Users/hansaxelsson/Projects/Minni \
  --socket /Users/hansaxelsson/.minni/run/minnid.sock \
  seed-hosted --agent codex --workspace /Users/hansaxelsson/Projects/Minni
```

Also check `/Users/hansaxelsson/.minni/identities/codex/CODEX_LAYER1_SHELF.md`.
It previously pointed future agents at the old
`~/.codex/plugins/cache/sovereign-memory/.../cli.js` path. Update shelf commands
to `~/.codex/plugins/cache/minni/minni/0.1.0/dist/cli.js` and reindex the
whole-document identity row if the daemon read still returns old shelf text.

## Legacy Cache Handling

Do not delete the old active cache immediately. It may have been retired by
accident. After Minni verifies GO, quarantine it under the install backup:

```bash
mv ~/.codex/plugins/cache/sovereign-memory \
  ~/.codex/plugin-install-backups/<run>/sovereign-memory-cache.quarantined-active-copy
pkill -f '/Users/hansaxelsson/.codex/plugins/cache/sovereign-memory/sovereign-memory/0.1.0/dist/server.js'
```

The 2026-05-31 run used:

```text
/Users/hansaxelsson/.codex/plugin-install-backups/minni-20260531T230040Z
```

## Verification

Run these before declaring GO:

```bash
cd /Users/hansaxelsson/Projects/Minni/plugins/minni && npm test
cd /Users/hansaxelsson/Projects/Minni
python3 -m pytest engine/test_principal_binding.py engine/test_approval_rpc.py -q
/Applications/Codex.app/Contents/Resources/codex doctor
python3 plugins/minni/skills/minni-install/scripts/propagate.py \
  --repo /Users/hansaxelsson/Projects/Minni \
  --socket /Users/hansaxelsson/.minni/run/minnid.sock \
  verify --agent codex --workspace /Users/hansaxelsson/Projects/Minni
MINNI_AGENT_ID=codex MINNI_VAULT_PATH=/Users/hansaxelsson/.minni/codex-vault \
MINNI_SOCKET_PATH=/Users/hansaxelsson/.minni/run/minnid.sock \
MINNI_WORKSPACE_ID=/Users/hansaxelsson/Projects/Minni \
node /Users/hansaxelsson/.codex/plugins/cache/minni/minni/0.1.0/dist/cli.js status
```

Hook smoke:

```bash
MINNI_AGENT_ID=codex MINNI_VAULT_PATH=/Users/hansaxelsson/.minni/codex-vault \
MINNI_SOCKET_PATH=/Users/hansaxelsson/.minni/run/minnid.sock \
MINNI_WORKSPACE_ID=/Users/hansaxelsson/Projects/Minni \
PLUGIN_ROOT=/Users/hansaxelsson/.codex/plugins/cache/minni/minni/0.1.0 \
node /Users/hansaxelsson/.codex/plugins/cache/minni/minni/0.1.0/dist/codex-hook.js SessionStart < /dev/null
```

Expected GO criteria:

- `propagate.py verify` returns `"ok": true`
- `cli.js status` shows socket ok and vault exists
- SessionStart hook returns Minni context and the hosted-agent map rule
- `rg "sovereign-memory|Sovereign Memory|SOVEREIGN_|\\.sovereign-memory"` returns
  no hits for Codex config, Codex AGENTS, Minni plugin manifest/MCP/hook manifest,
  and Codex identity envelope/shelf
- New Codex sessions expose `mcp__minni` / `minni_*` tools; the current session may
  still show old tool namespaces until restarted
