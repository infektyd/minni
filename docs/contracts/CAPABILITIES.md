# Minni — Capabilities Matrix (12 MCP Tools & Daemon JSON-RPC)

**Contract version:** 2.0.0 (Minni v1 surface)
**Last updated:** 2026-11-12

This contract defines both capability boundaries in Minni:

1. **The 12-tool MCP surface** exposed to coding agents by `plugins/minni/src/server.ts`.
2. **The internal JSON-RPC `_METHODS` table** exposed over the local Unix socket by `src/minni/minnid.py` (enforced via `RPC_CAPABILITY_REQUIREMENTS` and `RECOVERY_ALLOWED_METHODS` in `src/minni/minnid_runtime/provenance.py`).

---

## 1. 12-Tool MCP Surface (`plugins/minni/src/server.ts`)

Model-facing identity parameters (`agentId`, `vaultPath`) are omitted from MCP schemas; the plugin stamps the operator-configured runtime identity (`MINNI_AGENT_ID`, `MINNI_VAULT_PATH`) and the daemon resolves `EffectivePrincipal` server-side.

| Group | MCP Tool | Backing Daemon RPC / Shared Gate | Required Principal Capability | Notes |
|---|---|---|---|---|
| **Core Memory (6)** | `minni_recall` | `read` / `search` | `read`, `search` | Repo-First hybrid recall (`workspace_id` + `global` boosted first, strict post-rerank floor, offline local vault fallback only when daemon is unreachable). |
| | `minni_drill` | `sm_drill` / `expand` | `read` | Expands `resultIds`, `chunkIds`, `m:<8hex>` session handles, `d:<src>:<id>` refs, or `wikilink` (including repo-map path wikilinks) with privacy/workspace authz and typed `memory_links`. |
| | `minni_learn` | `learn` / `stage_candidate` | `learn` | Runs `assessLearningQualityAsync` (blocks credentials and `< 15` char / `< 4` token chatter), stages a workspace-tagged candidate (or auto-accepts when `auto_accept_own` is configured), and writes a personal vault note. |
| | `minni_list_candidates` | `gate.shared("candidates.list")` + `list_candidates` | `learn` | Lists caller's own `status="proposed"` candidates (POLICY §2 redacted, `total`/`has_more` pagination). |
| | `minni_resolve_candidate` | `gate.shared("candidates.resolve")` + `resolve_candidate` | Owner (non-accept) or `resolve_candidate` / `govern` (accept) | `accept` / `learn` commits a shared learning, wires typed graph edges (`memory_links`), refreshes `~/.minni/TODAY.md`, and indexes synchronously. |
| | `minni_status` | `gate.shared("audit.status")` + `status` | Valid stamped principal (`status` itself is recovery-allowed) | Daemon/AFM health, vault state, recent audit summary, and 7-day recall-to-use telemetry. |
| **Handoff (2)** | `minni_handoff_send` | `gate.shared("handoff.send")` + `handoff` | `handoff` | Packages task, recall citations, and scars for the next session or a named peer agent (`< 1 KB` lease receipt). |
| | `minni_handoff_inbox` | `gate.shared("handoff.inbox")` + `minni_list_pending_handoffs` / `minni_ack_handoff` | `handoff` | Previews waiting handoffs (`summary`, `files`, `next_steps`, `open_questions`) filtered to workspace context; acknowledges by `lease_id` or `ack: true`. |
| **Thread (4)** | `minni_thread_create` | `gate.shared("plan.create")` + locked vault write | Valid stamped principal | Creates `plan-*.md` + per-session active pointer (`< 2 KB` compact receipt). |
| | `minni_thread_status` | `gate.shared("plan.status")` + vault read | Valid stamped principal | Compact `< 2 KB` progress, `next_action`, capped scars, and optional shelf hash drift check. |
| | `minni_thread_update` | `gate.shared("plan.update")` + `withThreadPlanLock` | Valid stamped principal | Updates slice status (`evidence` required for `done`, `depends_on` enforced) and/or per-session `activate` (`< 2 KB` compact response). |
| | `minni_thread_scar` | `gate.shared("plan.scar")` + `withThreadPlanLock` | Valid stamped principal | Appends `failed_command`, `dead_end`, or `rejected_hypothesis` (`< 1 KB` receipt) and promotes workspace scar context. |

> **MCP surface scope:** Only the 12 `minni_*` tools in the table above are registered in `plugins/minni/src/server.ts`. Pre-v1 dry-run task/outcome, route/export, direct vault-write/compile, team-profile, ping, audit, and multi-lease worker-claim MCP tools were retired in v1; internal daemon JSON-RPC methods that remain in `_METHODS` to power the 12 MCP tools, hooks, and the `minni` CLI are documented in Section 2 below.

---

## 2. Daemon JSON-RPC Access Levels & `_METHODS` Matrix

### Access Levels

| Level | Meaning |
|---|---|
| `recovery` | Listed in `RECOVERY_ALLOWED_METHODS` (`ping`, `status`, `health_report`, `hygiene_report`). Available before identity registration so broken or fresh runtimes can be diagnosed; never grants governed writes or private reads. |
| `agent` | Requires a resolved `EffectivePrincipal` and the method's capability in `RPC_CAPABILITY_REQUIREMENTS`; per-row ownership, workspace, and `privacy_level` checks still apply. |
| `operator` | Requires operator/governance authority (`govern`, `resolve_candidate`, `*`, or zero-config local `main` when no `principals/*.json` files exist). |

### JSON-RPC Method Matrix (`src/minni/minnid.py`)

| JSON-RPC Method | Required Capability / Level | Side Effects | Notes |
|---|---|---|---|
| `ping` | `recovery` | None | Liveness probe; returns `"pong"`. |
| `status` | `recovery` | None | Daemon uptime, request count, latencies, DB/FAISS/AFM snapshot. |
| `health_report` | `recovery` | None | Index freshness, decay coverage, lifecycle queues, contradiction counts (non-operator callers receive aggregate counts only). |
| `hygiene_report` | `recovery` | None | Vault hygiene report: orphan pages, missing sources, expired pages, supersession chains. |
| `gate.shared` | Stamped principal | None | Resolves caller provenance (`EffectivePrincipal`) for plugin-side gated operations; attribution/provenance check, not vault-write execution. |
| `search` | `search` (`agent`) | Updates `access_count` / `last_accessed`; optional recall trace | Repo-First hybrid Tier 1 Trie + Tier 2 FTS5 BM25 + FAISS + Cross-Encoder reranking with strict confidence floor and typed graph expansion. |
| `read` | `read` (`agent`) | Updates `access_count` / `last_accessed` | Boot/session context: identity anchor, authorized documents (enforcing `can_read_document` & `privacy_level`), recent workspace learnings, episodic events. |
| `expand` | `read` (`agent`) | Updates `access_count` / `last_accessed` | Re-fetches a single `result_id` at `snippet`, `chunk`, or `document` depth under read authorization. |
| `sm_drill` | `read` (`agent`) | Updates `access_count` / `last_accessed` | Batch expansion backing `minni_drill` (`result_ids`, `chunk_ids`, `d:<src>:<id>`, repo-map path wikilinks, and typed `memory_links`). |
| `sm_export_pack` | `export` (`agent`) | None | Internal daemon RPC for deterministic token-budgeted anchor/snippet packs (not exposed as an MCP tool). |
| `list_events` | `read` (`agent`) | None | Read-only episodic event query backing `minni watch` (cross-agent listing follows cross-agent read gates). |
| `trace` | `read` (`agent`) | None | Ephemeral in-memory retrieval leg timing lookup owned by the requesting principal (`not_found` after expiry/restart). |
| `feedback` | `feedback` (`agent`) | Inserts `feedback_events` row | Recall quality signal for confidence calibration. |
| `minni_subscribe_contradictions` | `read` (`agent`) | None | **Internal daemon JSON-RPC method** (not an MCP tool): returns authorized contradiction events for learnings read by the caller. |
| `learn` | `learn` (`agent`) | Stages candidate (default) or commits shared learning (`auto_accept_own` / operator `force=true`) | Preserves `workspace_id`, runs typed graph edge classification + supersession on durable commit, and refreshes `TODAY.md`. |
| `stage_candidate` | `learn` (`agent`) | Inserts `candidate_packets` row | Candidate staging pipeline (`privacy=review` clamp for learn-only callers; strips forged inbox provenance). |
| `list_candidates` | `learn` (`agent`) | None | Lists caller's `proposed` candidates (or cross-principal queue for operators omitting `agent_id`), POLICY §2 redacted. |
| `resolve_candidate` | Owner or `resolve_candidate` / `govern` | `accept`/`learn` commits shared learning + purges superseded projections | Enforces transactional authorization and `supersede_ids` ownership/existence checks (`H8`). |
| `resolve_contradiction` | `learn` (`agent`, own rows) | Inserts superseding learning + marks prior rows `superseded` | Atomic contradiction resolution; `instruction_like` content additionally requires `accept_flagged`. |
| `log_event` | `log_event` (`agent`) | Inserts `episodic_events` row | Appends an episodic event (`event_type`, `content`, `agent_id`, `task_id?`, `thread_id?`). |
| `vault_index_doc` | `learn` (`agent`) | Indexes single vault file into `.index/` | Synchronous single-file personal/map indexing. |
| `repo_map` | `learn` (`agent`) | Writes `<minni_home>/vault/maps/<workspace>/` + `repo_map_cards` + FAISS/Trie | Synthesizes file-and-SHA-pinned Living Repo Map subsystem cards; backs `minni map [<repo>]` and background staleness refresh. |
| `handoff` / `daemon.handoff` | `handoff` (`agent`) | Writes `handoff_leases` + inbox/outbox packets | Internal daemon RPC backing `minni_handoff_send`. |
| `minni_list_pending_handoffs` | `handoff` (`agent`) | None | Lists pending non-expired handoff leases for the caller; backs `minni_handoff_inbox`. |
| `minni_ack_handoff` | `handoff` (`agent`) | CAS status transition on `handoff_leases` | Enforces pending/non-expired CAS acknowledgement (`H9`); backs `minni_handoff_inbox`. |
| `minni_await_handoff` | `handoff` (`agent`) | None | Internal lease-wait helper RPC (not an MCP tool). |
| `daemon.compile` | `read` (`agent`) | Writes synthesis drafts | Internal wiki synthesis RPC. |
| `daemon.endorse` | `govern` (`operator`) | Updates `review_state` to `accepted` | Operator/governance endorsement RPC. |
| `cache_reload` | `govern` (`operator`) | Flushes `agent_scope_for` & `vault_retrieval` caches | Mutating admin cache reload (deliberately excluded from `RECOVERY_ALLOWED_METHODS`). |
| `recall` | — | n/a | **Not a JSON-RPC method.** `_METHODS` has no `recall` entry (`-32601 Method not found`); `minni_recall` and `minni recall` call `search` and `read`. |

---

## 3. Depth Tiers for `search`, `expand`, and `sm_drill`

| Tier | Fields returned | Approximate tokens / result |
|---|---|---|
| `headline` | `wikilink`, `title`, `score`, `confidence`, `age_days`, `chunk_id`, `decay_factor` | ~30 |
| `snippet` | + truncated `text` (`<= 280` chars) and `<EVIDENCE>` framing | ~120 |
| `chunk` | + full chunk text, `heading_context`, full `provenance`, and typed `memory_links` (on drill) | ~500 |
| `document` | + full source document (for `whole_document=1` / authorized vault & map pages) | variable |

Default search tier: `snippet`.

---

## 4. Parameters Quick Reference

### `search`

```json
{
  "query": "string (required)",
  "agent_id": "string (optional; verified against stamped principal)",
  "workspace_id": "string (optional; Repo-First boost for current workspace + global)",
  "mode": "\"hybrid\" | \"fast\" (optional; fast uses <10ms Trie + FTS5 BM25)",
  "limit": "integer (optional, default: 5, max: 20)",
  "depth": "\"headline\" | \"snippet\" | \"chunk\" | \"document\" (optional, default: \"snippet\")",
  "budget_tokens": "integer (optional) — enables MMR-diverse token-budgeted packing"
}
```

### `repo_map`

```json
{
  "repo_path": "string (required — path to repository root)",
  "workspace_id": "string (optional — defaults to workspace-<repo>)",
  "refresh": "boolean (optional, default: false)"
}
```

### `learn`

```json
{
  "content": "string (required)",
  "agent_id": "string (optional; use the registered runtime principal)",
  "workspace_id": "string (optional; e.g. workspace-minni or global)",
  "category": "string (optional, default: \"general\")",
  "force": "boolean (optional; operator-only audited durable write)"
}
```

### `sm_drill` / `expand`

```json
{
  "result_ids": [101, 102],
  "depth": "\"snippet\" | \"chunk\" | \"document\" (optional, default: \"chunk\")"
}
```

---

## 5. JSON-RPC Error Codes

| Code | Meaning |
|---|---|
| `-32700` | Parse error — malformed JSON. |
| `-32600` | Invalid request — e.g. line exceeds 1 MiB socket body limit. |
| `-32601` | Method not found. |
| `-32602` | Invalid params — missing or mistyped required field. |
| `-32000` | Application error — see `message` for detail. |
| `-32004` | Provenance or capability denial (`unknown_identity`, `reserved_agent_id`, `identity_mismatch`, or `capability_denied`). |
