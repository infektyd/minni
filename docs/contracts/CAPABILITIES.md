# Sovereign Memory — JSON-RPC Capabilities Matrix

**Contract version:** 1.1.0
**Last updated:** 2026-06-15

This document lists every JSON-RPC method exposed by the Sovereign Memory daemon
(`engine/minnid.py`; the authoritative dispatch table is `_METHODS`). Methods not
yet implemented are marked `[PLANNED: PR-N]`.

---

## Access Levels

| Level | Meaning |
|-------|---------|
| `agent` | Any connected agent may call this. |
| `operator` | Daemon or human operator only; not exposed over the IPC socket. |

---

## Method Matrix

| Method | Access Level | Side Effects | Notes |
|--------|-------------|-------------|-------|
| `ping` | agent | None | Liveness probe. Returns `"pong"`. |
| `status` | agent | None | Daemon uptime, request count, DB and FAISS health snapshot. |
| `search` | agent | Updates `access_count` and `last_accessed` on matched documents. | Hybrid FTS5 + FAISS + cross-encoder retrieval. Accepts optional `depth` (`headline | snippet | chunk | document`, default `snippet`) and `budget_tokens`. |
| `read` | agent | Updates `access_count` and `last_accessed` on returned documents. | Agent startup context: identity anchor, top documents, recent learnings, recent episodic events. Alias: `recall`. |
| `learn` | agent | Writes a row to `learnings`; optionally appends to `~/.openclaw/MEMORY.md` (dual-write mode). | Stores a durable learning keyed by `agent_id` and `category`. |
| `log_event` | agent | Writes a row to `episodic_events`. | Appends an episodic event. Fields: `event_type`, `content`, `agent_id`, `task_id?`, `thread_id?`. |
| `expand` | agent | Updates `access_count` and `last_accessed` on the expanded document. | Re-fetch a specific result at a deeper depth tier. Accepts `result_id` (chunk_id or doc_id) and `depth`. |
| `recall` | agent | Same as `read`. | Served as an alias of `read` (no distinct `recall` method in `_METHODS`). |
| `health_report` | agent | None | Structured health report including index freshness, decay stats, and schema version. |
| `feedback` | agent | Writes a row to `feedback_events`. | Signal quality of a recall result (thumbs-up / thumbs-down + comment). Used to calibrate confidence scoring. |
| `trace` | agent | None | Retrieve full provenance trace for a `result_id`. Returns the chain of FTS rank, semantic rank, RRF score, cross-encoder score, decay factor. |
| `handoff` | agent | Writes a handoff packet to `wiki/handoffs/` and `inbox/`. | Package current agent context (identity, pending learnings, open questions) for a peer agent. Also reachable as `daemon.handoff`. |
| `compile` | agent | Writes vault pages; updates index.md and log.md. | Synthesize raw notes + learnings into structured wiki pages (entity, concept, decision, etc.). Wired as `daemon.compile`. |
| `endorse` | agent | Updates `review_state` from `candidate` to `accepted` on a vault page. | Peer-agent endorsement of a candidate page. Wired as `daemon.endorse`. |
| `hygiene_report` | agent | None | Report on vault health: orphan pages, pages missing sources, expired pages, supersession chains. |
| `minni_subscribe_contradictions` | agent | None | Return contradiction events for learnings recently read by the calling agent (belief-correction surface). |
| `stage_candidate` | agent | None (no durable write). | G14/G16 candidate pipeline: stage a candidate packet (default learn path) for operator review. |
| `list_candidates` | agent | None | G14/G17: list staged candidates for the stamped principal (console/governance view). |
| `resolve_candidate` | operator | Accept → durable `learn`; reject/redact otherwise. | G15: resolve a staged candidate. Authorization is owner-or-explicit-operator, enforced inside the transaction. |
| `ax_snapshot_store` | agent | Writes an accessibility-tree snapshot (TTL-bounded, default 3600s). | OmniSense: persist an AX/app snapshot (`agent_id`, `app_name`, `tree_json`). Returns `snapshot_id`. |
| `ax_snapshot_get` | agent | None | OmniSense: retrieve a stored AX/app snapshot by `agent_id` / `app_name`. |

---

## Depth Tiers for `search` and `expand`

| Tier | Fields returned | Approximate tokens / result |
|------|----------------|---------------------------|
| `headline` | `wikilink, title, score, confidence, age_days` | ~30 |
| `snippet` | + `text` (~280 chars) | ~120 |
| `chunk` | + full chunk text, heading context, full provenance | ~500 |
| `document` | + full source document (only for `whole_document=1` rows) | variable |

Default tier: `snippet` (matches all existing callers; zero behavior change).

---

## Parameters Quick Reference

### `search`

```json
{
  "query":        "string (required)",
  "agent_id":     "string (optional, default: \"main\")",
  "limit":        "integer (optional, default: 5, max: 20)",
  "depth":        "\"headline\" | \"snippet\" | \"chunk\" | \"document\" (optional, default: \"snippet\")",
  "budget_tokens": "integer (optional) — enables MMR-diverse token-budgeted packing"
}
```

### `expand`

```json
{
  "result_id":  "integer — chunk_id or doc_id from a prior search result",
  "depth":      "\"chunk\" | \"document\" (optional, default: \"chunk\")"
}
```

### `learn`

```json
{
  "content":   "string (required)",
  "agent_id":  "string (optional, default: \"hermes\")",
  "category":  "string (optional, default: \"general\")"
}
```

### `log_event`

```json
{
  "event_type": "string (required)",
  "content":    "string (required)",
  "agent_id":   "string (optional, default: \"hermes\")",
  "task_id":    "string (optional)",
  "thread_id":  "string (optional)"
}
```

### `read`

```json
{
  "agent_id": "string (optional, default: \"hermes\")",
  "limit":    "integer (optional, default: 5, max: 20)"
}
```

---

## Error Codes

| Code | Meaning |
|------|---------|
| `-32700` | Parse error — malformed JSON. |
| `-32601` | Method not found. |
| `-32602` | Invalid params — missing required field. |
| `-32000` | Application error — see `message` for detail. Daemon returned a degraded result or failed entirely. |
