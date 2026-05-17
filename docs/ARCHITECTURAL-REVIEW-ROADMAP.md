# Sovereign Memory Architectural Review and Staged Roadmap

**Generated based on repo inspection (commit ~4f7e1fb).**

## SECTION 1 — CURRENT STATE ASSESSMENT

**Architectural Strengths:**
- Strong local-first philosophy with SQLite as single source of truth (WAL mode, migrations via db.py).
- Hybrid retrieval (FTS5 + FAISS + rerank) in retrieval.py.
- Layered memory model (identity whole, knowledge chunked) clearly articulated in docs.
- Daemon (sovrd.py) with JSON-RPC, plugin MCP surface for multiple agents.
- Human-readable Obsidian vaults as derived surface.
- Dry-run AFM passes for self-organization.
- Detailed security planning and engineering review docs.

**Risks and Gaps:**
- Security findings (SECURITY_PLAN.md): incomplete principal binding, caller-controlled params, prompt-injection fencing not fully enforced, socket permissions.
- Human approval workflow appears more conceptual than fully backend-implemented (dry-run exists, full review UI/console limited).
- Redaction/privacy boundaries partial.
- Evaluation harness present but needs expansion for regression on governance flows.

(Continuing with other sections...)

## Memory Lifecycle (Mermaid)
```mermaid
flowchart TD
    A[Source Ingestion / Observation] --> B[Memory Candidate Generation]
    B --> C{Dry-Run Learning Review}
    C -->|Approve| D[Persist to SQLite as Durable Memory]
    C -->|Log Only| E[Ephemeral / Audit Log]
    C -->|Reject| F[Discard or Mark Rejected]
    D --> G[Evidence Linked]
    G --> H[Retrieval / Recall]
```

## Recommended Next Steps
See full roadmap in subsequent sections.
