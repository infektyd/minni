# Minni provenance gate: ground-and-resolve findings

Plan: `plan-fdf220b75b172595`
Slice: `ground-and-resolve`
Branch: `feat/minni-provenance-gate`
Date: 2026-06-15

## GO / NO-GO

NO-GO for implementation until claude-code reviews this checkpoint.

The requested daemon `_dispatch` seam is real, but it is not currently universal
for every Minni surface. It is universal for daemon JSON-RPC methods. It is not
universal for many MCP tools, hook-side vault/audit/plan operations, ping
contracts, or slash-command prompts that call those tools. A gate at
`engine/minnid.py::_dispatch` will cover daemon-backed operations, but not all
live Minni behavior unless the bypasses below are deliberately reclassified or
wired through the gate.

## Source documents read

- Plan artifact: `/Users/hansaxelsson/.minni/claudecode-vault/wiki/artifacts/plan-fdf220b75b172595.md:23`
- Ground-and-resolve gate: `/Users/hansaxelsson/.minni/claudecode-vault/wiki/artifacts/plan-fdf220b75b172595.md:39`
- Design doc gate location/fail-loud model: `/Users/hansaxelsson/.minni/claudecode-vault/wiki/sessions/20260615-design-seed-2026-06-15-minni-provenance-gate-fail-loud-two-zone-router-at-dispatch-kills-the-fall-back-to-copy-claude-asymmetry.md:24`
- Design doc authoritative binary split: `/Users/hansaxelsson/.minni/claudecode-vault/wiki/sessions/20260615-design-seed-2026-06-15-minni-provenance-gate-fail-loud-two-zone-router-at-dispatch-kills-the-fall-back-to-copy-claude-asymmetry.md:60`
- Design doc measured scatter count: `/Users/hansaxelsson/.minni/claudecode-vault/wiki/sessions/20260615-design-seed-2026-06-15-minni-provenance-gate-fail-loud-two-zone-router-at-dispatch-kills-the-fall-back-to-copy-claude-asymmetry.md:107`

## Seam completeness

### Proven: daemon RPC methods funnel through `_dispatch`

- `engine/minnid.py:3723` defines `_METHODS`, the daemon method registry.
- `engine/minnid.py:3778` defines `_dispatch(request)`.
- `engine/minnid.py:3784` looks up the method in `_METHODS`.
- `engine/minnid.py:3788` calls coroutine handlers directly or `asyncio.to_thread` for sync handlers.
- `engine/minnid.py:3844` makes the Unix-socket client path call `await _dispatch(request)`.
- `engine/minnid.py:3938` makes the HTTP fallback path call `await _dispatch(request)`.

Verdict: any operation that reaches the daemon as JSON-RPC crosses `_dispatch`.

### Proven: MCP tools are split between daemon-backed and TypeScript-local paths

`plugins/minni/src/server.ts` registers 37 MCP tools (`rg registerTool` count = 37).
The registrations start at:

- `plugins/minni/src/server.ts:88` `minni_prepare_task`
- `plugins/minni/src/server.ts:142` `minni_prepare_outcome`
- `plugins/minni/src/server.ts:224` `minni_team_runtime`
- `plugins/minni/src/server.ts:266` `minni_team_evidence`
- `plugins/minni/src/server.ts:294` `minni_team_promotion`
- `plugins/minni/src/server.ts:328` `minni_status`
- `plugins/minni/src/server.ts:346` `minni_compile_vault`
- `plugins/minni/src/server.ts:378` `minni_route`
- `plugins/minni/src/server.ts:400` `minni_recall`
- `plugins/minni/src/server.ts:462` `minni_drill`
- `plugins/minni/src/server.ts:481` `minni_export_pack`
- `plugins/minni/src/server.ts:507` `minni_learn`
- `plugins/minni/src/server.ts:574` `minni_resolve_candidate`
- `plugins/minni/src/server.ts:612` `minni_learning_quality`
- `plugins/minni/src/server.ts:637` `minni_vault_write`
- `plugins/minni/src/server.ts:670` `minni_audit_report`
- `plugins/minni/src/server.ts:686` `minni_audit_tail`
- `plugins/minni/src/server.ts:702` `minni_negotiate_handoff`
- `plugins/minni/src/server.ts:836` `minni_ping_agent_request`
- `plugins/minni/src/server.ts:871` `minni_ping_agent_inbox`
- `plugins/minni/src/server.ts:887` `minni_ping_agent_decide`
- `plugins/minni/src/server.ts:911` `minni_ping_agent_status`
- `plugins/minni/src/server.ts:927` `minni_ack_handoff`
- `plugins/minni/src/server.ts:952` `minni_list_pending_handoffs`
- `plugins/minni/src/server.ts:967` `minni_await_handoff`
- `plugins/minni/src/server.ts:983` `minni_subscribe_contradictions`
- `plugins/minni/src/server.ts:1008` through `plugins/minni/src/server.ts:1366` `minni_plan_*`

Daemon-backed examples:

- `minni_recall`: stamps `DEFAULT_AGENT_ID` at `plugins/minni/src/server.ts:436`; calls `recallMemory`, which sends daemon `search` at `plugins/minni/src/sovereign.ts:172`.
- `minni_compile_vault`: calls `compileVault` at `plugins/minni/src/server.ts:369`; `compileVault` sends daemon `daemon.compile` at `plugins/minni/src/sovereign.ts:468`.
- `minni_drill`: calls `drillMemory` at `plugins/minni/src/server.ts:476`; `drillMemory` sends daemon `sm_drill` at `plugins/minni/src/sovereign.ts:319`.
- `minni_export_pack`: calls `exportContextPack` at `plugins/minni/src/server.ts:496`; `exportContextPack` sends daemon `sm_export_pack` at `plugins/minni/src/sovereign.ts:337`.
- `minni_learn`: calls daemon `learn` via `learnMemory` at `plugins/minni/src/server.ts:542` and `plugins/minni/src/sovereign.ts:195`; it also writes a vault note outside the daemon at `plugins/minni/src/server.ts:548`.
- `minni_resolve_candidate`: sends daemon `resolve_candidate` at `plugins/minni/src/server.ts:602`.
- `minni_ack_handoff`, `minni_list_pending_handoffs`, `minni_await_handoff`, and `minni_subscribe_contradictions` call daemon methods through `plugins/minni/src/sovereign.ts:361`, `plugins/minni/src/sovereign.ts:373`, `plugins/minni/src/sovereign.ts:382`, and `plugins/minni/src/sovereign.ts:392`.

Bypass or partially-bypass examples:

- `minni_prepare_task` uses TypeScript vault search and audit directly at `plugins/minni/src/task.ts:760`, `plugins/minni/src/task.ts:770`, and only the recall leg reaches daemon via `plugins/minni/src/task.ts:783`.
- `minni_prepare_outcome` is a TypeScript packet/dry-run path at `plugins/minni/src/task.ts:968`; no daemon gate is required for its candidate drafting unless the design says all vault reads/writes must route through the gate.
- `minni_team_evidence` is local summary construction at `plugins/minni/src/server.ts:288`.
- `minni_team_promotion` is local packet construction at `plugins/minni/src/server.ts:317`.
- `minni_route` records audit directly at `plugins/minni/src/server.ts:391`.
- `minni_learning_quality` records audit directly at `plugins/minni/src/server.ts:628`.
- `minni_vault_write` writes a vault page directly at `plugins/minni/src/server.ts:659`.
- `minni_audit_report` and `minni_audit_tail` read vault audit logs directly at `plugins/minni/src/server.ts:681` and `plugins/minni/src/server.ts:697`.
- `minni_ping_agent_request`, `minni_ping_agent_inbox`, `minni_ping_agent_decide`, and `minni_ping_agent_status` operate through TypeScript file-backed contracts at `plugins/minni/src/agent_ping.ts:276`, `plugins/minni/src/agent_ping.ts:332`, `plugins/minni/src/agent_ping.ts:367`, and `plugins/minni/src/agent_ping.ts:458`.
- `minni_plan_create` and all update/status/history/diff/restore/activate/deactivate tools persist and read plan artifacts through TypeScript vault helpers at `plugins/minni/src/server.ts:1029`, `plugins/minni/src/server.ts:1094`, `plugins/minni/src/server.ts:1184`, `plugins/minni/src/server.ts:1248`, `plugins/minni/src/server.ts:1275`, `plugins/minni/src/server.ts:1308`, `plugins/minni/src/server.ts:1334`, and `plugins/minni/src/server.ts:1361`.

Verdict: a daemon `_dispatch` gate alone will miss TypeScript-local MCP side
effects unless those tools are moved behind daemon methods or explicitly treated
as trusted interface-layer operations.

### Proven: hooks also mix daemon-backed calls with direct vault operations

Claude-specific hook:

- `plugins/minni/src/hook.ts:55` starts `handleSessionStart`.
- `plugins/minni/src/hook.ts:65` calls daemon-backed recall.
- `plugins/minni/src/hook.ts:75` calls daemon-backed read.
- `plugins/minni/src/hook.ts:81` reads handoff context directly from the vault.
- `plugins/minni/src/hook.ts:97` resolves active plan directly from the vault.
- `plugins/minni/src/hook.ts:190` records audit directly.
- `plugins/minni/src/hook.ts:224` does direct vault search and daemon recall in parallel.
- `plugins/minni/src/hook.ts:303` starts `PreCompact`; `plugins/minni/src/hook.ts:321` fetches daemon stale-beliefs, while `plugins/minni/src/hook.ts:323` and `plugins/minni/src/hook.ts:331` write vault/audit directly.
- `plugins/minni/src/hook.ts:346` starts `Stop`; `plugins/minni/src/hook.ts:351`, `plugins/minni/src/hook.ts:358`, and `plugins/minni/src/hook.ts:366` perform outcome/vault/audit work outside daemon `_dispatch`.

Shared Codex/Grok/Kilo hook factory:

- `plugins/minni/src/hook-handlers.ts:138` starts `handleSessionStart`.
- `plugins/minni/src/hook-handlers.ts:146` mixes daemon status/read/recall/contradictions with direct vault/audit/inbox reads.
- `plugins/minni/src/hook-handlers.ts:188` resolves active plan directly.
- `plugins/minni/src/hook-handlers.ts:293` records audit directly.
- `plugins/minni/src/hook-handlers.ts:329` does direct vault search plus daemon recall.
- `plugins/minni/src/hook-handlers.ts:402` starts `PreCompact`; `plugins/minni/src/hook-handlers.ts:422` writes inbox directly and `plugins/minni/src/hook-handlers.ts:462` records audit directly.
- `plugins/minni/src/hook-handlers.ts:479` starts `Stop`; `plugins/minni/src/hook-handlers.ts:485`, `plugins/minni/src/hook-handlers.ts:500`, and `plugins/minni/src/hook-handlers.ts:511` perform outcome/vault/audit work outside daemon `_dispatch`.

Thin runtime hook entrypoints:

- `plugins/minni/src/codex-hook.ts:13` passes Codex constants into the shared factory.
- `plugins/minni/src/grok-hook.ts:13` passes Grok constants into the shared factory.
- `plugins/minni/src/kilocode-hook.ts:16` passes KiloCode constants into the shared factory.

Verdict: hook event handling does not fully funnel through daemon `_dispatch`.
Only daemon-backed legs do. Hook-side vault/audit/inbox/plan operations bypass it.

### Proven: slash commands inherit the MCP split

The command files are prompts that tell the host to call MCP tools, not independent
daemon clients.

- `plugins/minni/commands/audit.md:5` calls `minni_audit_tail`/`minni_audit_report`, both TypeScript-local audit readers.
- `plugins/minni/commands/learn.md:13` calls `minni_learn`, which is mixed daemon + direct vault write.
- `plugins/minni/commands/plan.md:8` and `plugins/minni/commands/plan.md:23` call `minni_plan_*`, which is TypeScript-local plan persistence.
- `plugins/minni/commands/prepare-task.md:6` calls `minni_prepare_task`, mixed direct vault search + daemon recall.
- `plugins/minni/commands/recall.md:5` calls `minni_recall`, mixed direct vault search/audit + daemon search.
- `plugins/minni/commands/status.md:5` calls `minni_status`, mixed direct vault/audit + daemon status.

Verdict: commands do not add a new bypass mechanism, but they route into MCP
tools that may bypass `_dispatch`.

## Provenance-site enumeration

### Engine resolver and principal root

- `engine/principal.py:73` defines canonical operator principal file names: `local`, `default`, `operator`, `main`.
- `engine/principal.py:237` defines `from_local_transport`.
- `engine/principal.py:253` loops canonical principal files and returns the first present file.
- `engine/principal.py:259` synthesizes `agent_id="main"` when no operator principal file exists.
- `engine/principal.py:365` defines `resolve_effective_principal`.
- `engine/principal.py:390` turns on strict mode if any principal JSON exists.
- `engine/principal.py:392` stamps the base local principal via `from_local_transport`.
- `engine/principal.py:410` blocks reserved `main`/`operator` ids without `operator_context`.
- `engine/principal.py:426` lets a matching per-agent `principals/<agent>.json` win.
- `engine/principal.py:443` starts platform-agent reconciliation.
- `engine/principal.py:448` reads `platform_agent_ids`.
- `engine/principal.py:452` reads per-platform capabilities.
- `engine/principal.py:466` returns an `EffectivePrincipal(agent_id=supplied, ...)` for platform-hosted agents.
- `engine/principal.py:475` starts legacy alias reconciliation.
- `engine/principal.py:483` reads `legacy_agent_ids` or `aliases`.
- `engine/principal.py:487` returns the stamped canonical principal for legacy aliases.
- `engine/principal.py:497` defines `_default_deny_principal`.
- `engine/principal.py:532` defines `is_operator_principal`.
- `engine/principal.py:551` defines read authorization `can_read_document`.

### Engine call sites for `resolve_effective_principal`

Live `rg` count in non-test engine source: 19 call sites in `engine/minnid.py`.

- `engine/minnid.py:113` `_guard_vault_root`
- `engine/minnid.py:664` `_handle_daemon_handoff`
- `engine/minnid.py:953` `_handle_ack_handoff`
- `engine/minnid.py:1008` `_handle_list_pending_handoffs`
- `engine/minnid.py:1189` `_handle_search`
- `engine/minnid.py:1380` `_handle_feedback`
- `engine/minnid.py:1408` `_handle_trace`
- `engine/minnid.py:1476` `_handle_expand`
- `engine/minnid.py:1740` `_handle_sm_drill`
- `engine/minnid.py:1764` `_handle_sm_drill` fallback branch for reference-based drill
- `engine/minnid.py:1832` `_handle_sm_export_pack`
- `engine/minnid.py:1928` `_handle_read`
- `engine/minnid.py:2122` `_handle_learn`
- `engine/minnid.py:2348` `_handle_resolve_contradiction`
- `engine/minnid.py:2493` `_handle_subscribe_contradictions`
- `engine/minnid.py:2657` `_handle_log_event`
- `engine/minnid.py:3391` `_stage_candidate`
- `engine/minnid.py:3455` `_list_candidates`
- `engine/minnid.py:3543` `_resolve_candidate`

Notable non-resolved daemon methods:

- `engine/minnid.py:3725` `ping`
- `engine/minnid.py:3750` `status`
- `engine/minnid.py:3751` `health_report`
- `engine/minnid.py:3752` `hygiene_report`
- `engine/minnid.py:3742` `daemon.compile` only resolves identity indirectly via `_guard_vault_root` at `engine/minnid.py:3164`.
- `engine/minnid.py:3743` `daemon.endorse` only resolves identity indirectly via `_guard_vault_root` at `engine/minnid.py:3368`.
- `engine/minnid.py:3748` `ax_snapshot_store` and `engine/minnid.py:3749` `ax_snapshot_get` take raw `agent_id` at `engine/minnid.py:3682` and `engine/minnid.py:3707`; they currently bypass `resolve_effective_principal`.

### TypeScript default stamping/fallback sites

Root defaults:

- `plugins/minni/src/config.ts:49` defines `DEFAULT_VAULT_PATH`, falling back to `~/.minni/unknown-vault`.
- `plugins/minni/src/config.ts:236` defines `DEFAULT_AGENT_ID`, falling back to `"unknown-agent"`.
- `plugins/minni/src/config.ts:241` defines `DEFAULT_WORKSPACE_ID`, falling back to `"workspace-unknown"`.
- `plugins/minni/src/config.ts:256` defines Claude defaults separately.

MCP server stamp sites:

- `plugins/minni/src/server.ts:131` `minni_prepare_task` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:132` `minni_prepare_task` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:178` `minni_prepare_outcome` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:256` `minni_team_runtime` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:341` `minni_status` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:371` `minni_compile_vault` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:391` `minni_route` stamps `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:420` `minni_recall` sets `effectiveVaultPath = DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:436` `minni_recall` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:452` records stamped `DEFAULT_AGENT_ID` in audit details.
- `plugins/minni/src/server.ts:500` `minni_export_pack` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:526` `minni_learn` audit uses `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:545` `minni_learn` stamps daemon `agentId`.
- `plugins/minni/src/server.ts:549` `minni_learn` writes to `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:554` `minni_learn` writes vault note with `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:628` `minni_learning_quality` audit uses `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:660` `minni_vault_write` writes to `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:681` `minni_audit_report` reads `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:697` `minni_audit_tail` reads `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:725` `minni_negotiate_handoff` sets `effectiveVaultPath = DEFAULT_VAULT_PATH`.
- `plugins/minni/src/server.ts:726` `minni_negotiate_handoff` stamps `fromAgent = DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:729` records runtime agent as `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:882` `minni_ping_agent_inbox` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:947` `minni_ack_handoff` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:962` `minni_list_pending_handoffs` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:995` `minni_subscribe_contradictions` stamps `DEFAULT_AGENT_ID`.
- `plugins/minni/src/server.ts:1023`, `1086`, `1151`, `1180`, `1211`, `1270`, `1295`, `1324`, `1356`, and `1374` stamp plan operations to `DEFAULT_VAULT_PATH`.

Other TS default/fallback sites:

- `plugins/minni/src/task.ts:761` and `plugins/minni/src/task.ts:762` default prepare-task `vaultPath` and `agentId`.
- `plugins/minni/src/task.ts:938` and `plugins/minni/src/task.ts:939` default handoff packet `vaultPath` and `agentId`.
- `plugins/minni/src/agent_ping.ts:115` defines `resolveAgentVaultPath`.
- `plugins/minni/src/agent_ping.ts:116` maps the runtime default agent to `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/agent_ping.ts:276`, `332`, `367`, and `458` default ping actor/list/status functions to `DEFAULT_AGENT_ID`.
- `plugins/minni/src/plan.ts:369` defaults plan vault path to `DEFAULT_VAULT_PATH`.
- `plugins/minni/src/codex-hook.ts:14` and `plugins/minni/src/codex-hook.ts:15` stamp Codex hooks with `DEFAULT_AGENT_ID`/`DEFAULT_VAULT_PATH`.
- `plugins/minni/src/hook-handlers.ts:58` describes the stamped agent identity contract for shared hooks.
- `plugins/minni/src/hook-handlers.ts:200` injects the configured agent id into the hook envelope.

## Cached identity root cause

### Verified current behavior

Live daemon check:

```text
read(agent_id="codex") -> result.agent_id == "codex"
```

Live resolver check with current `/Users/hansaxelsson/.minni/principals/local.json`:

```text
None -> main
codex -> codex
gemini -> gemini
grok-build -> grok-build
claude-code -> claude-code
kilocode -> kilocode
grok -> main
main -> main with no capabilities
```

Interpretation: the current daemon is not presently reproducing "codex resolves
to main". Platform agents in `platform_agent_ids` resolve to their own stamped
principal in current code.

### Confirmed stale-cache bug

There is a real principal-scope cache that stays stale until explicitly cleared
or the process restarts:

- `engine/principal.py:323` decorates `_agent_scope_for_cached` with `@lru_cache(maxsize=128)`.
- `engine/principal.py:331` reads canonical principal files inside that cached function.
- `engine/principal.py:337` reads `platform_agent_ids`.
- `engine/principal.py:359` returns the cached result from `agent_scope_for`.
- `engine/principal.py:362` exposes `agent_scope_for.cache_clear`.
- `engine/minnid.py:4113` installs a SIGHUP reload handler.
- `engine/minnid.py:4114` logs `config reload (no-op for now)`.

Repro command:

```text
PYTHONPATH=engine python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import json, os
from principal import resolve_effective_principal, agent_scope_for
with TemporaryDirectory() as td:
    d=Path(td)
    f=d/'local.json'
    f.write_text(json.dumps({'agent_id':'main','legacy_agent_ids':['old'],'platform_agent_ids':['codex']}))
    os.chmod(f,0o600)
    print(agent_scope_for('codex', d))
    f.write_text(json.dumps({'agent_id':'main','legacy_agent_ids':['old','new'],'platform_agent_ids':['codex']}))
    os.chmod(f,0o600)
    print(agent_scope_for('codex', d))
    agent_scope_for.cache_clear()
    print(agent_scope_for('codex', d))
    print(resolve_effective_principal(supplied_agent_id='codex', transport='uds', principals_dir=d).agent_id)
PY
```

Observed:

```text
scope1 ['codex', 'old']
scope2_no_clear ['codex', 'old']
scope3_after_clear ['codex', 'old', 'new']
resolve_after_edit codex
```

Root cause: principal-file dependent scope is cached in-process and the daemon
has no reload path that clears it. This can leave recall scope/alias visibility
stale until restart. The hot identity resolver itself re-reads principal files
for platform-agent stamping; the stale cache is specifically `agent_scope_for`,
not `resolve_effective_principal` in current code.

### Why "main" still appears

`main` remains the canonical local/operator fallback:

- `engine/principal.py:253` prefers canonical local/default/operator/main files.
- `engine/principal.py:259` synthesizes `main` on fresh installs with no principal files.
- Current live `local.json` has `agent_id: main`, `legacy_agent_ids: ["grok"]`, and `platform_agent_ids` for Codex/Gemini/Grok-Build/Claude/Kilo.
- `engine/principal.py:475` legacy alias reconciliation returns the stamped canonical principal for legacy aliases.

So `grok` resolving to `main` is explained by the live legacy alias setup. Platform
ids resolve to their own id in current code.

## Open decisions before implementation

1. Decide whether TypeScript-local MCP/hook/plan/ping/vault operations are trusted interface-layer surfaces or must be moved behind daemon `_dispatch`.
2. Decide whether the provenance gate lives only at daemon `_dispatch`, or whether a second TypeScript-side gate is needed before direct vault/plan/ping operations.
3. Define `recover()` behavior for direct TypeScript-local paths, because those paths currently have no daemon `_dispatch` return channel.
4. Define reload semantics: at minimum, SIGHUP or the future gate should clear `agent_scope_for.cache_clear()` and any related per-vault/principal caches.

## Commands run

```text
git status --short --branch
git switch -c feat/minni-provenance-gate main
rg --files /Users/hansaxelsson/.minni/codex-vault /Users/hansaxelsson/.minni/claudecode-vault | rg 'plan-fdf220b75b172595|20260615-design-seed'
nl -ba /Users/hansaxelsson/.minni/claudecode-vault/wiki/artifacts/plan-fdf220b75b172595.md
nl -ba /Users/hansaxelsson/.minni/claudecode-vault/wiki/sessions/20260615-design-seed-2026-06-15-minni-provenance-gate-fail-loud-two-zone-router-at-dispatch-kills-the-fall-back-to-copy-claude-asymmetry.md
rg -n "resolve_effective_principal\\(" engine/*.py engine/afm_passes/*.py engine/tools/*.py
rg -n "DEFAULT_AGENT_ID|DEFAULT_VAULT_PATH" plugins/minni/src/server.ts plugins/minni/src/config.ts plugins/minni/src/task.ts plugins/minni/src/agent_ping.ts plugins/minni/src/plan.ts plugins/minni/src/codex-hook.ts plugins/minni/src/hook-handlers.ts
rg -n "platform_agent_ids|legacy_agent_ids|aliases|agent_scope_for|from_local_transport|_default_deny_principal|OPERATOR_RESERVED_AGENT_IDS|CANONICAL_PRINCIPAL_NAMES|lru_cache|cache_clear|SIGHUP|reload" engine/principal.py engine/minnid.py engine/test_principal_binding.py
rg -n "registerTool\\(" plugins/minni/src/server.ts
node -e "... JSON-RPC read(agent_id:'codex') ..."
PYTHONPATH=engine python3 - <<'PY' ...
```
