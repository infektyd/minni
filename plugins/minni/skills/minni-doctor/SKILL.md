---
name: minni-doctor
description: Use when a Minni install misbehaves or its health is unknown — diagnose daemon/socket/DB, vault, plugin/MCP wiring, per-platform hooks, identity Layer 1, inbox lifecycle, correction re-injection, model providers, and plan injection. Doctor diagnoses and routes; minni-install repairs.
---

# Minni Doctor

## Overview

Diagnose the health of the Minni installation on this machine and say exactly
what is wrong, with evidence, before anything is changed. The doctor never
repairs silently:

> `minni-install` is the installer (the "how").
> `minni-doctor` is the diagnostician (the "what is wrong, and where to fix it").

Run the checks below top-to-bottom — they are ordered by dependency. Stop at
the first failing layer; everything below it will look broken too.

## Quick Triage (60 seconds)

1. Call `minni_status` (or `/minni:status`). Read it strictly:
   - `daemon_ok: true` — the daemon answered on the socket. If false, nothing
     else matters; go to Layer 1 below.
   - `afm_ok: true` — means a **verified one-token completion** succeeded, not
     merely "a process responded". (Honest-health contract, PR #69. If an older
     install reports ok without a completion, that itself is a finding.)
   - `vault` — must be THIS agent's vault (`~/.minni/<agent-id>-vault`), an
     actual directory, not a symlink, not another agent's path.
2. Call `minni_recall` with a term you know exists. Empty-but-healthy recall
   on a populated DB is a delivery gap, not "no data".
3. Check the session started with a Minni context envelope (SessionStart hook).
   No envelope = hooks not registered for this platform (Layer 4).

## Layer 1 — Daemon / Socket / DB

| Check | Healthy looks like | If not |
|---|---|---|
| Socket exists | `~/.minni/run/minnid.sock` | Daemon not running; check launchd agent `com.minni.minnid` |
| Daemon answers | `status` RPC returns JSON | Stale socket or crashed daemon; restart, then re-check |
| DB is the canonical one | `~/.minni/minni.db`, hundreds+ of documents on an established install | Wrong `MINNI_*` env in platform config — route to minni-install |
| Transport sanity | JSON-RPC over Unix socket | `Expected HTTP/` parse error = stale plugin cache speaking HTTP; rebuild plugin cache |

## Layer 2 — Vault

| Check | Healthy | Finding if not |
|---|---|---|
| Vault path | Real directory owned by this agent | Symlink or another agent's vault = config drift (repair via minni-install) |
| Structure | `wiki/ raw/ inbox/ outbox/ schema/ index.md log.md` | Bootstrap incomplete |
| Writes recallable | A page written via vault API appears in recall | Pages written by hand bypass index/audit — re-write through the API |

## Layer 3 — Plugin / MCP wiring

- The platform config must launch the installed plugin's `dist/server.js` with
  explicit env: `MINNI_AGENT_ID`, `MINNI_VAULT_PATH`,
  `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`, `MINNI_WORKSPACE_ID`.
- Missing env = the platform may silently fall back to another agent's
  defaults. That is an identity-boundary failure, not a cosmetic one.
- Tool count sanity: a current server registers **37 `minni_*` tools**
  (including the 11 `minni_plan_*` tools). Far fewer visible = stale plugin
  build or cache.

## Layer 4 — Hooks (per platform, shared semantics)

Every platform registers its OWN compiled hook (`hook.js`, `codex-hook.js`,
`grok-hook.js`, `kilocode-hook.js`); Minni semantics live in one shared factory.

| Check | Healthy | Finding |
|---|---|---|
| SessionStart envelope | `<minni:context event="SessionStart">` with identity, audit tail, pending_learnings | Hook not registered in THIS platform's native config |
| Per-turn recall | UserPromptSubmit envelope with ranked results | Recall channel dead; check daemon first, then hook registration |
| Stop drafts | Candidate learnings appear in vault `inbox/` after sessions | Stop hook missing or writing kind-less files (pre-factory bug class) |
| Correction re-assert | Post-compaction and SessionStart re-inject stored corrections; `stale_beliefs` fires when a prompt contradicts a stored correction | If `stale_beliefs` is silently empty while a relevant correction exists, that is the C1 failure class — report it loudly |
| Cross-platform honesty | Each platform has its own hook | One platform borrowing another's hooks (e.g. via compat scanning) is drift |

## Layer 5 — Identity (Layer 1 delivery)

- `documents.agent = identity:<agent_id>` with `whole_document = 1` must exist.
- `python engine/agent_api.py <agent_id> --identity` shows it; daemon `read`
  must deliver it BEFORE other context. Identity present in the DB but absent
  from `read` is a daemon delivery gap — do not paper over it with vault pages.
- Hosted agents get an envelope/map (subordinate to the host runtime), never a
  personality. An envelope containing personality is a finding.

## Layer 6 — Inbox lifecycle

Healthy inboxes drain. PR #69 semantics:

- Handoffs carry lease-aware TTLs and drain on resolution.
- `derived_from` correspondence is verified — one agent cannot forge work into
  another's lane.
- **Dead-letter check:** count files in `~/.minni/<agent>-vault/inbox/`. A
  large, monotonically growing count with old timestamps (the historical
  failure was 1,500+ unprocessed files) means the drain loop is not running.
- A handoff that re-appears as pending at every session boot is stuck — report
  its path and age.

## Layer 7 — Model providers

- `~/.minni/providers.json` configures the provider chain. Validation findings:
  inline `apiKey` is forbidden (env/key-file only) and disables that provider;
  invalid JSON falls back to the default AFM-only chain — silently weaker, so
  surface it.
- Health = verified completion. Any "healthy" provider that has never
  completed a token is lying; say so.
- `retrieval` operation policy defaults to `localOnly: true`. If a config has
  flipped it, confirm the human did that on purpose.
- Cloud/mlx/ollama entries parse but have no transports yet (planned tier) —
  configured-but-skipped is expected, not a bug.

## Layer 8 — Plans

- An active plan (`minni_plan_*`) injects into SessionStart/UserPromptSubmit on
  all four platforms and survives compaction. Active plan set but absent from
  envelopes = injection gap.
- Plan updates must go through `minni_plan_update` (journaled); a plan note
  edited by hand without journal events is drift.

## Test suites as health instruments

On a source checkout, these are the ground truth (counts as of 2026-06-11):

```bash
cd engine && PYTHONPATH=. pytest -q          # expect ~560 passed
cd plugins/minni && npm run build && npm test # expect ~327 passed
bash scripts/repro-smoke.sh                   # hermetic daemon smoke
```

The VectorBackend conformance suite
(`engine/test_vector_backend_conformance.py`) certifies any vector backend —
FAISS today; any future Lance/Qdrant adapter must pass the same file.

## Reporting discipline

- Report findings layer-by-layer with evidence (paths, RPC output, counts) —
  never "looks fine" without a check actually run.
- Diagnose first, then route: repairs and re-installs go through
  `minni-install`; engine-internal issues go to the human with the failing
  layer named.
- Degraded is a state, not an excuse: partial recall (`{text, source, heading,
  score}`) still works and should be reported as degraded, not broken.
- The doctor itself stays read-only. Any state change is a recommendation
  unless the human says go.
