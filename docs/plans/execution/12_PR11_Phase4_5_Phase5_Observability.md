# PR-11: Phase 4.5 + Phase 5 — Provenance Edges + Observability + Hygiene

> **Scope:** Cross-agent provenance edges, memory health endpoint, daemon stats histograms, backend-aware formatting, vault/wiki hygiene report.
>
> **Depends on:** PR-2 (envelope), PR-6 (contradiction detection for hygiene).
>
> **Behavior change:** New JSON-RPC endpoints `health_report()`, `hygiene_report()`. Enhanced `status()`. Incremental — ship as appetite allows.

---

## 4.5 — Cross-Agent Provenance Edges

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/writeback.py` | When `learn()` includes `evidence_doc_ids`, add `link_type='derived_from'` edges in existing `memory_links` table. |
| **NO MIGRATION** | — | Reuses existing `memory_links` table. |
| **NO CHANGE** | `engine/graph_export.py` | Already emits whatever `link_type` it finds — new edges appear automatically. |

---

## 5.1 — Memory Health Endpoint

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.health_report()` |

### Response

```json
{
  "stale_docs": 42,
  "never_recalled": 15,
  "contradicting_learnings": [...],
  "vector_backend_lag": {"faiss-disk": 0, "qdrant": 128},
  "faiss_cache_age_seconds": 3600
}
```

- `stale_docs`: count where `decay < 0.2`
- `never_recalled`: count where `access_count = 0`
- `contradicting_learnings`: list from contradiction detection
- `vector_backend_lag`: per-backend rowid delta vs SQLite
- `faiss_cache_age_seconds`: seconds since last FAISS persist

---

## 5.2 — Daemon Stats Histograms

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/sovrd.py` | Extend `status()` with rolling-window p50/p95 latencies for: `search`, `learn`, `read`, embedding calls, cross-encoder calls. |

Implementation: tiny in-memory ring per method (last 100 call durations). Compute percentiles on request.

---

## 5.3 — Backend-Aware Result Formatting

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/sovrd.py` | Python-side `formatRecall()` adds backend badge: `[faiss-disk+qdrant]` etc. |
| **MODIFY** | `plugins/sovereign-memory/src/server.ts` | TS-side `formatRecall()` updated to render backend badge. Optional — JSON envelope always carries it. |

---

## 5.4 — Vault/Wiki Hygiene Report

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/hygiene.py` | Core hygiene checks. Outputs `logs/hygiene-YYYY-MM-DD.md` + JSON summary. |
| **MODIFY** | `engine/sovrd.py` | New JSON-RPC: `daemon.hygiene_report()`. |
| **MODIFY** | `engine/sovereign_memory.py` | CLI: `python -m engine.sovereign_memory hygiene --vault <path>`. |

### Checks

| Check | Severity | Description |
|-------|----------|-------------|
| Broken wikilinks | `warn` | Pages cite `[[wiki/...]]` that don't resolve |
| Missing sources | `warn` | `candidate`/`accepted` pages without `sources:` frontmatter |
| Status drift | `warn` | `superseded_by` pointing at non-existent/also-superseded page; expired pages still `accepted` |
| Orphan pages | `info` | No wikilinks to them, not in `index.md` |
| Frontmatter violations | `block` | Missing required keys, unknown type/status/privacy values |
| Privacy mismatches | `block` | `safe` pages with redaction-trigger patterns; `blocked` pages in vector index |
| Contradictions | `warn` | Embedding cosine > 0.85 with opposite assertion (heuristic) |
| Index/log drift | `info` | Pages on disk not in `index.md`; entries pointing at missing files |

### Output Format

Markdown report grouped by severity (`block` → `warn` → `info`). JSON summary feeds `health_report()`.

### Constraint: Hygiene reports are read-only. Fixes go through the file-back workflow (WORKFLOWS.md).

### Verification

```bash
# Run against a known-clean vault
python -m engine.sovereign_memory hygiene --vault <path>
# Expect zero block-severity findings

# Run against vault with known issues
# Expect issues reported correctly
```

---

## PR-11 Completion Checklist

- [ ] `derived_from` edges written to `memory_links` when `evidence_doc_ids` provided
- [ ] `graph_export.py` shows new edges automatically
- [ ] `daemon.health_report()` returns all specified fields
- [ ] `status()` includes p50/p95 latencies for search, learn, read, embedding, CE
- [ ] Backend badge rendered in formatRecall (Python + TS)
- [ ] `engine/hygiene.py` runs all 8 checks
- [ ] `hygiene --vault` CLI produces Markdown report
- [ ] `daemon.hygiene_report()` returns JSON summary
- [ ] Clean vault → zero `block` findings
- [ ] All existing tests pass

---

## Next Steps

→ [13_PR12_Phase6A_AFM_Session_Distill.md](./13_PR12_Phase6A_AFM_Session_Distill.md)
