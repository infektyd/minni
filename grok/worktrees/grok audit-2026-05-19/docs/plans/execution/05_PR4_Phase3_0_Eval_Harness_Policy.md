# PR-4: Phase 3.0 — Eval Harness, Policy Docs, Workflows

> **Scope:** Recall evaluation harness, policy/privacy docs, threat model, documented vault workflows. **Gates all subsequent retrieval tuning (PR-7, PR-8, PR-15).**
>
> **Depends on:** PR-2 (envelope fields needed for eval metrics).
>
> **Behavior change:** None. Adds offline tooling and documentation.

---

## 3.0.a — Recall Eval Harness

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/eval/__init__.py` | Package init. |
| **CREATE** | `engine/eval/harness.py` | Offline harness: loads `eval/queries.jsonl`, runs `search()` under each config, reports recall@K (K=1,3,5,10), MRR, cross-encoder calibration error. Outputs JSON + Markdown to `eval/reports/`. |
| **CREATE** | `eval/queries.jsonl` | Seed dataset: ~50 hand-curated `{query, expected_doc_ids, notes}` pairs from existing task logs and audit history. |
| **CREATE** | `eval/reports/.gitkeep` | Directory for generated reports. |

### CLI

```bash
python -m engine.eval.harness run --config baseline,with-expand,with-hyde
```

Produces a comparison table across configs.

### Record Mode

The harness includes a `record` mode that captures live queries + agent's later feedback as eval pairs:

```bash
python -m engine.eval.harness record --query "auth migration" --expected-ids 8412,8413
```

### Gate Rule

**A feature flips its default only after the harness shows ≥+5% recall@5 on the seed set with no regression on any class.** This gates PR-7 (query expansion), PR-8 (HyDE), and PR-15 (quantized embeddings).

---

## 3.0.b — Policy and Privacy Documentation

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `docs/contracts/POLICY.md` | Default privacy posture, redaction rules, retention rules, cross-agent rules. |
| **CREATE** | `docs/contracts/THREAT_MODEL.md` | Plain-English threat model with control references. |

### POLICY.md Sections

1. **Default privacy posture.** Vaults local-only by default. Cross-agent recall shared but privacy-tagged.
2. **Redaction rules.** Local paths, secrets patterns (`api_key`, `token`, `password`, `private key`), adapter/launchd filenames — redacted in any envelope crossing process boundary.
3. **Retention rules.** Episodic events: 7-day TTL. Raw session notes: immutable, can be `expired`. Learnings: forever unless `expires` set.
4. **Cross-agent rules.** Read via shared recall (with `agent_origin` provenance). No writing into another agent's vault. Handoff packets are the cross-agent write channel.

### THREAT_MODEL.md Threats

- Prompt injection via recalled content → `instruction_like` flag + memory-as-evidence rule
- Daemon socket access → local-only binding
- Vault path traversal → path validation in vault.ts
- AFM bridge tampering → trace_id provenance
- Vector-backend leakage → privacy_level filtering

---

## 3.0.c — Vault Workflows

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `docs/contracts/WORKFLOWS.md` | Four documented, repeatable workflows. |

### Workflows

1. **Ingest.** `raw/` source → indexer chunks/embeds → optional `synthesis` draft → review → `accepted`.
2. **Query.** Agent receives task → routes → recalls (depth=snippet) → expands chosen to chunk → builds task packet → cites in output.
3. **File-back.** Agent learns → drafts wiki page (status `candidate`) → `learning_quality` check → `learn` writes → index/log updated.
4. **Lint.** Hygiene runs nightly/on-demand → reports broken wikilinks, missing sources, drift, orphans, contradictions → outputs to `logs/hygiene-YYYY-MM-DD.md` → agents review and remediate via file-back.

---

## PR-4 Completion Checklist

- [ ] `engine/eval/harness.py` exists and runs against live daemon or frozen DB
- [ ] `eval/queries.jsonl` seeded with ~50 curated queries
- [ ] `harness run --config baseline` produces JSON + Markdown report
- [ ] `harness record` mode works for capturing new eval pairs
- [ ] `docs/contracts/POLICY.md` covers privacy, redaction, retention, cross-agent
- [ ] `docs/contracts/THREAT_MODEL.md` covers 5 threat categories with controls
- [ ] `docs/contracts/WORKFLOWS.md` documents ingest, query, file-back, lint workflows
- [ ] All existing tests pass

---

## Next Steps

→ [06_PR5_Phase3_1_2_Cache_Layers.md](./06_PR5_Phase3_1_2_Cache_Layers.md)
