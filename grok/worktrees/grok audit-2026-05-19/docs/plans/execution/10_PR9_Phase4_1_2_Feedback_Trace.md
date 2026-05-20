# PR-9: Phase 4.1 + 4.2 — Negative Feedback + Per-Query Trace

> **Scope:** Feedback table and JSON-RPC method for agents to report useful/not-useful results. Per-query trace ring buffer with trace endpoint. Highest observability lift for nearly zero cost.
>
> **Depends on:** PR-2 (envelope with `chunk_id`, `doc_id`).
>
> **Behavior change:** New JSON-RPC methods `feedback()` and `trace()`. Small per-result demotion from negative feedback.

---

## 4.1 — Negative Feedback Hook

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/migrations/006_feedback.sql` | `feedback(id INTEGER PRIMARY KEY, query_hash TEXT, query_text TEXT, doc_id INTEGER, chunk_id INTEGER, agent_id TEXT, useful BOOLEAN, created_at INTEGER)` |
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.feedback(query, result_id, useful, agent_id)`. Stores row. |
| **MODIFY** | `engine/retrieval.py` | Read recent feedback in per-process cache (refreshed every 60s). Apply small per-result demotion: max -0.3 cumulative across negative votes for `(agent, query_class, doc_id)`. |
| **MODIFY** | `engine/config.py` | Add `feedback_enabled: bool = True`. |

### Constraints

- Tiny demotion ceiling (-0.3 max) prevents runaway.
- Off via `feedback_enabled=False` if needed.
- Cache refreshed every 60s — not real-time but sufficient.

---

## 4.2 — Per-Query Trace Endpoint

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/trace.py` | `TraceRing`: keeps last 100 queries' full breakdown in memory. Ring buffer, ~5MB max. |
| **MODIFY** | `engine/retrieval.py` | Every `search()` writes a trace entry. Returns `trace_id` in response. |
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.trace(trace_id)` returns full trace JSON. |

### Trace Entry Contents

- Query text and variants (if expanded)
- FTS hits with BM25 scores
- Semantic hits with cosine scores
- RRF math (per-stream ranks, merged scores)
- Cross-encoder scores
- Decay factors
- Final ordering
- HyDE pass details (if triggered)
- Backend(s) used
- Timing breakdown (FTS ms, embedding ms, CE ms, total ms)

### Constraints

- Memory bounded by ring buffer (last 100 queries, ~5MB max).
- `trace_id` is a short random string (e.g., `t8f2a1b3`).
- Trace data is ephemeral — lost on daemon restart. Not persisted.

### Verification

```bash
# Feedback round-trip
python -c "
from engine.sovrd_client import search, feedback
results = search('auth migration')
feedback(query='auth migration', result_id=results[0]['provenance']['chunk_id'], useful=False, agent_id='test')
print('Feedback stored')
"

# Trace round-trip
python -c "
from engine.sovrd_client import search, trace
results = search('auth migration')
trace_id = results[0].get('trace_id') or 'unknown'
t = trace(trace_id)
print(f'Trace keys: {list(t.keys())}')
assert 'fts_hits' in t or 'timing' in t
print('PASS')
"
```

---

## PR-9 Completion Checklist

- [x] `006_feedback.sql` creates feedback table
- [x] `daemon.feedback()` JSON-RPC stores rows
- [x] Retrieval applies per-result demotion from negative feedback (max -0.3)
- [x] `feedback_enabled` config toggle works
- [x] `engine/trace.py` TraceRing with capacity 100
- [x] Every `search()` returns `trace_id`
- [x] `daemon.trace(trace_id)` returns full breakdown
- [x] Ring buffer bounded at ~5MB
- [x] All existing tests pass

---

## Next Steps

→ [11_PR10_Phase4_6_Inbox_Handoff.md](./11_PR10_Phase4_6_Inbox_Handoff.md)
