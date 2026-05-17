# Sovereign Memory — JSON-RPC Capabilities Matrix

**Contract version:** 1.0.0
**Last updated:** 2026-04-26

This document lists every JSON-RPC method exposed by the Sovereign Memory daemon
(`engine/sovrd.py`). Methods not yet implemented are marked `[PLANNED: PR-N]`.

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
| `recall` | agent | Same as `read`. | Alias for `read`. [PLANNED: PR-2] |
| `health_report` | agent | None | Structured health report including index freshness, decay stats, and schema version. [PLANNED: PR-2] |
| `feedback` | agent | Writes a row to `feedback_events`. | Signal quality of a recall result (thumbs-up / thumbs-down + comment). Used to calibrate confidence scoring. [PLANNED: PR-9] |
| `trace` | agent | None | Retrieve full provenance trace for a `result_id`. Returns the chain of FTS rank, semantic rank, RRF score, cross-encoder score, decay factor. [PLANNED: PR-9] |
| `handoff` | agent | Writes a handoff packet to `wiki/handoffs/` and `inbox/`. | Package current agent context (identity, pending learnings, open questions) for a peer agent. [PLANNED: PR-10] |
| `compile` | agent | Writes vault pages; updates index.md and log.md. | Synthesize raw notes + learnings into structured wiki pages (entity, concept, decision, etc.). [PLANNED: PR-13] |
| `endorse` | agent | Updates `review_state` from `candidate` to `accepted` on a vault page. | Peer-agent endorsement of a candidate page. [PLANNED: PR-13] |
| `hygiene_report` | agent | None | Report on vault health: orphan pages, pages missing sources, expired pages, supersession chains. [PLANNED: PR-11] |

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
