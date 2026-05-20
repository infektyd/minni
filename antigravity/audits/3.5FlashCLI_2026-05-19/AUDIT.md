# Sovereign Memory Release Audit (2026-05-19)

## Dimension 1: Architecture & Separations

### Summary
The system architecture shows a strong foundation with the `EffectivePrincipal` identity resolution, but suffers from "leaky abstractions" where agent-specific hardcoding and internal implementation details (absolute paths, DB IDs) are exposed through public APIs.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| ARCH-001 | P1 | `sovrd.py:287` | Hardcoded agent aliases in vault resolution. | [sovrd.py:L287](file:///Users/hansaxelsson/projects/sovereignMemory/engine/sovrd.py#L287) | Externalize to config/principals. |
| ARCH-002 | P1 | `principal.py:376` | Hardcoded governance status for "main"/"operator". | [principal.py:L376](file:///Users/hansaxelsson/projects/sovereignMemory/engine/principal.py#L376) | Use capability-based checks in JSON. |
| ARCH-003 | P1 | `plugins/hook.ts` | Agent-specific constants in plugin contract. | Hardcoded `CLAUDECODE_AGENT_ID`. | Refactor to environment-aware registry. |
| ARCH-004 | P2 | `sovrd.py` | Absolute file paths leaked in public APIs. | Status/Recall return full OS paths. | Sanitize to vault-relative paths. |
| ARCH-005 | P2 | `server.ts` | Hardcoded agent ID in MCP tools. | `agentId: DEFAULT_AGENT_ID`. | Resolve from `EffectivePrincipal`. |
| ARCH-006 | P1 | `sovrd.py` | Lack of single source of truth for API schemas. | Overlap between RPC, MCP, and Plugins. | Centralize schema authority. |


### Cross-Model Disagreements
- **Governance Hardcoding**:
    - **Agent A (Gemini)**: Did not flag the hardcoded "main"/"operator" identities in `principal.py`.
    - **Agent B (Claude)**: Identified this as a **P1** risk for multi-tenant scalability (ARC-03).
    - **Resolution**: Included as **ARCH-002 (P1)**.
- **Severity of Path Leaks**:
    - **Agent A (Gemini)**: Categorized as a minor implementation leak (P3).
    - **Agent B (Claude)**: Categorized as a significant security-enabling leak (P2).
    - **Resolution**: Upgraded to **P2** (ARCH-004) as absolute paths aid in sandbox escapes.
- **Root Cause Focus**:
    - **Agent A (Gemini)**: Focused on individual hardcoded instances in `sovrd.py`.
    - **Agent B (Claude)**: Identified the *lack of a unified schema authority* as the P1 root cause.
    - **Resolution**: Included as **ARCH-006 (P1)**.


### What Looks Good
- **Identity Layer**: The `EffectivePrincipal` implementation in [principal.py](file:///Users/hansaxelsson/projects/sovereignMemory/engine/principal.py) is a robust "stamped" identity system that correctly ignores caller-supplied IDs.
- **Handoff Lifecycle**: The acknowledge/lease flow for cross-agent handoffs is stable and well-modeled.

## Dimension 2: Scope Creep & Dead Code

### Summary
The repository suffers from significant "documentation drift" where alpha/untested features (Team Mode) are promoted as primary workflows in agent skills. Additionally, multiple "ghost" versions of core logic (redaction, daemon, migrations) create a high-maintenance, high-risk environment for the release candidate.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SCOPE-001 | P1 | `SKILL.md:31` | **Documentation Drift**: Untested Team Mode promoted as primary. | `SKILL.md` vs `README.md` (Alpha/Untested). | Downgrade `SKILL.md` to experimental. |
| SCOPE-002 | P1 | `engine/migrations/` | **Migration Collision**: Duplicate ID 007. | `007_handoff_*.sql` and `007_candidate_*.sql`. | Re-sequence migrations. |
| SCOPE-003 | P1 | Multiple | **Security Logic Duplication**: Redaction 4x. | Duplicated in `sovrd.py`, `team-harvest.ts`, `agent_ping.ts`. | Centralize to shared utility. |
| SCOPE-004 | P2 | `openclaw-extension/` | **Dead Code**: Deprecated HTTP bridge. | Superseded by JSON-RPC daemon. | Remove directory. |
| SCOPE-005 | P2 | `engine/db.py` | **Redundant Schema**: Code duplicates migrations. | `CREATE TABLE` in Python vs SQL files. | Rely on SQL migrations. |
| SCOPE-006 | P2 | `engine/sovrd.py:188` | **Legacy Support**: Dual-write feature. | writes to `MEMORY.md` for legacy Hermes support. | Deprecate and remove. |
| SCOPE-007 | P3 | `_archive/` | **Stale Artifacts**: Old backups from April. | `_archive/` and `_cleanup-quarantine/` present. | Purge old backups. |

### Cross-Model Agreements
- **Migration Duplication**: Unanimous P1 finding across all agents.
- **Redundancy**: High consensus on removing the `openclaw-extension` folder and centralizing redaction logic.
- **Documentation Drift**: Identified by Agent A as a P1 "silent failure" risk for agents.

## Dimension 3: Security (Deep Pass)

### Summary
The security audit revealed a significant regression in the **AFM Loop**. While the core `writeback.py` is well-defended, the newer `afm_writer.py` lacks critical guards against **frontmatter forgery** and **YAML injection**. Additionally, raw secrets are currently leaking into the database via episodic events and subsequently into AFM prompts.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SEC-001 | P1 | `afm_writer.py:133` | **Missing SEC-018 Guard**: Model can forge frontmatter. | Lacks check for `---` in model-generated body. | Port `_contains_forged_frontmatter` from `writeback.py`. |
| SEC-002 | P1 | `afm_writer.py:78` | **YAML Injection**: Unescaped title/tags. | Injects new keys (e.g. `privacy: safe`) via newlines. | Use YAML dumper for frontmatter. |
| SEC-003 | P2 | `episodic.py:82` | **Secret Leakage**: Raw secrets in database. | Raw episodic events passed to AFM prompts without redaction. | Implement redaction in `add_event`. |
| SEC-004 | P3 | `afm_provider.py:211` | **SSRF Vector**: Unvalidated `urlopen`. | Used in bridge client without strict host allowlist. | Restrict to `localhost`. |

### Cross-Model Agreements
- **AFM Regression**: Unanimous P1 finding. The mismatch between `writeback.py` and `afm_writer.py` is the primary security blocker for RC.
- **Leakage Path**: Both agents confirmed that the distillation process in `session_distillation.py` acts as a data exfiltration path for unredacted episodic events.
- **UDS Robustness**: All models agreed that the Unix Domain Socket permission logic (`SEC-001`) and principal stamping are correctly implemented.

---

## Dimension 4: Performance & Footprint

### Summary
The performance audit identified a **P0 Architectural Bottleneck**: the `sovrd` daemon handles search requests synchronously on the main `asyncio` event loop. This blocks all other clients during heavy CPU-bound re-ranking tasks. Additionally, two **P1 scaling issues** were found in the FAISS build process and neighborhood retrieval that will cause degradation as the vault grows.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| PERF-001 | P0 | `sovrd.py:2550` | **Synchronous IPC Blocking**. Main loop blocks on `_dispatch`. | Event loop cannot process new UDS connections during search. | Move engine calls to `run_in_executor`. |
| PERF-002 | P0 | `sovrd.py:832` | **Blocking Event Loop (sleep)**. `_handle_await_handoff` uses `time.sleep`. | Blocks ALL daemon traffic while waiting for a handoff. | Replace with `await asyncio.sleep`. |
| PERF-003 | P1 | `retrieval.py:1231` | **Algorithmic Inefficiency**. Leading wildcard `LIKE %path` scan. | Forces full table scan of `documents` table for every link. | Add `filename` column or use FTS for path lookups. |
| PERF-004 | P1 | `faiss_index.py:44` | **Redundant Memory Bloat**. Stores raw vectors in Python list. | Consumes 1.5GB+ per 1M vectors in addition to FAISS index. | Remove `_vectors` and retrieve from FAISS. |
| PERF-005 | P1 | `retrieval.py:356` | **Memory Allocation Spike**. O(N) list-to-numpy conversion. | Duplicates vector data in memory before FAISS handoff. | Use `np.fromiter` or batch loading. |
| PERF-006 | P2 | `sovrd.py:140` | **Cold Start Latency**. Lazy model loading on first request. | First search can hang for >5s during weight load. | Implement background warmup on daemon start. |
| PERF-007 | P2 | `db.py:316` | **Disk Fragmentation**. Missing FTS optimization. | `episodic_fts` deletions leave ghost pages without `VACUUM`. | Add periodic `FTS optimize` and `VACUUM`. |

### Cross-Model Agreements
- **UDS Bottleneck**: Both models identified the synchronous nature of the daemon as the single most critical performance risk for multi-agent concurrency.
- **Memory Growth**: Consensus that the current `List[np.ndarray]` approach for FAISS rebuilding will crash on low-RAM devices (e.g., Raspberry Pi) once the vault exceeds ~50k chunks.

---

## Dimension 5: Code Quality & Syntax

### Summary
The code quality audit revealed significant **Architectural Debt** in the core retrieval path. The primary concern is a massive "God Method" in `retrieval.py` that violates the Single Responsibility Principle (SRP). Secondary concerns include widespread **Type Safety Gaps** and inconsistent error handling that favors raw strings over structured exceptions.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| QUAL-001 | P0 | `retrieval.py:1276` | **God Method Complexity**. `retrieve()` exceeds 500 lines. | Complexity ~28; handles FTS, Semantic, RRF, HyDE, and Auth. | Refactor into a Pipeline class or strategy pattern. |
| QUAL-002 | P1 | `engine/*.py` | **Widespread Type Gaps**. Missing return types and arg hints. | `sovrd.py` and `db.py` rely on generic `dict` returns. | Enforce strict typing with MyPy/Ruff. |
| QUAL-003 | P1 | `sovrd.py:2000+` | **File Bloat**. `sovrd.py` and `retrieval.py` are >2000 lines. | Maintainability is degrading; logic is bleeding between layers. | Split into sub-modules (e.g., `handlers/`, `pipeline/`). |
| QUAL-004 | P2 | `principal.py:255` | **Mutable Default Argument**. `roots=[]` in constructor. | Classic Python footgun for shared state. | Replace with `None` and initialize inside `__init__`. |
| QUAL-005 | P2 | `engine/*.py` | **Raw String Exceptions**. Widespread `raise ValueError("msg")`. | Prevents programmatic error handling by callers. | Implement `SovereignError` hierarchy. |

### Cross-Model Agreements
- **Complexity**: Both agents independently flagged `RetrievalEngine.retrieve` as the single most critical maintainability risk in the codebase.
- **Type Safety**: Strong consensus that the current "loosely typed" approach will lead to regressions during the next major feature addition.

---

## Dimension 6: CI/CD & Pipeline

### Summary
The CI/CD audit revealed a **P0 Infrastructure Gap**: the project currently lacks any automated CI/CD infrastructure. Verification of the "454 passing tests" claim is impossible for external contributors, and the manual deployment process is fragile and macOS-centric.

### Findings Table
| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| CI-001 | P0 | `.github/workflows/` | **Total Absence of CI Automation**. No GHA workflows found. | Grep for `on: push` returned 0 results. | Initialize `.github/workflows/ci.yml`. |
| CI-002 | P1 | `engine/requirements.txt` | **Unpinned Python Dependencies**. Core libs lack upper bounds. | `sentence-transformers>=2.2.0` (unbounded). | Pin exact versions and implement a lockfile. |
| CI-003 | P1 | Project Root | **Missing Security Scanning**. No SAST/CodeQL/Dependabot. | No security workflow configs present. | Enable CodeQL and Dependabot. |
| CI-004 | P2 | `engine/launchd/` | **Fragile Manual Deployment**. Relies on manual `.plist` edits. | No automated deploy/rollback logic. | Create a `deploy.sh` script with dry-run support. |
| CI-005 | P2 | Project Root | **Dev/Prod Parity Gaps**. Platform verification is macOS-only. | No Linux/Windows test matrix. | Implement a Docker-based reproduction environment. |

### Cross-Model Agreements
- **Automation Gap**: Both agents flagged the total lack of automated CI as the most significant barrier to a stable release-candidate.
- **Dependency Risk**: Consensus that unpinned Python dependencies will cause breakage as upstream ML libraries release breaking changes.

---

## Final Audit Conclusion
The Sovereign Memory codebase is functionally advanced but architecturally "unpolished" for a production release candidate.

**Critical Paths for Release Readiness:**
1. **Performance**: Solve the P0 blocking event loop in `sovrd.py`.
2. **Quality**: Decompose the P0 "God Method" `RetrievalEngine.retrieve`.
3. **Security**: Finalize SEC-018 guards against forgery in `afm_writer.py`.
4. **CI/CD**: Bootstrap an automated testing pipeline to verify claims and protect against regressions.
