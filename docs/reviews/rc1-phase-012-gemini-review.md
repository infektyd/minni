# External Adversarial Review: Sovereign Memory RC1 (Phase 0, 1, 2)

**Reviewer:** Antigravity (Gemini 2.0 Pro Orchestrator)
**Date:** 2026-05-20
**Scope:** Grok Implementation of Phase 0 (Foundation), Phase 1 (Sovereign Integration), and Phase 2 (Observability & Governance) as merged into `rc1-phase-012` worktree.

## Executive Summary

The Grok-produced implementation of Sovereign Memory (Phases 0-2) demonstrates a high level of architectural alignment with the RC Plan. The core security primitives for identity (G11) and vault binding (G12) are implemented in the `principal.py` module and integrated into the `sovrd.py` daemon. The implementation successfully addresses key requirements such as asynchronous retrieval (RCM-006) and AFM frontmatter forgery protection (RCM-010).

However, a **CRITICAL** discovery was made regarding the test suite integrity. Several high-level feature tests globally monkeypatch the core security resolution logic with a "permissive" bypass, which remains active for subsequent tests. This results in the failure of actual security regression tests when run as part of the full suite. 

Additionally, dependency audits revealed several high-severity vulnerabilities in the Node.js/TypeScript plugin components.

---

## Security Audit (G11/G12/G13)

### [G11] Identity: EffectivePrincipal Server-Stamp Binding
- **Implementation:** `principal.py` correctly implements the `EffectivePrincipal` dataclass and the `resolve_effective_principal` gatekeeper. The logic for synthesizing a local "main" identity on fresh installs (RCM-003) is present and correctly rejects unauthorized `agent_id` claims.
- **Vulnerability:** The test suite bypasses this logic (see Test Suite Integrity).

### [G12] Context: Vault Root Binding
- **Implementation:** `EffectivePrincipal` includes `allowed_vault_roots`, and `allows_vault_root` uses `Path.resolve()` to prevent `..` traversal and symlink escapes (RCM-005/Bug 4).
- **Vulnerability:** Bypassed in tests.

### [G13] Loopback: AFM Transport Lockdown
- **Implementation:** `callAfmJson` in the multi-plugin correctly restricts AFM calls to loopback/allowlisted targets. `afmPrepareUrl` has been removed from model-facing tools, eliminating a key spoofing surface.

---

## Verification of RCMs (Phase 0-2 Highlights)

| RCM | Requirement | Status | Evidence |
| :--- | :--- | :--- | :--- |
| **RCM-003** | Identity Stamping | **IMPLEMENTED (TESTS BROKEN)** | `principal.py:resolve_effective_principal` |
| **RCM-006** | Async Retrieval | **PASSED** | `sovrd.py` uses `asyncio.to_thread` for search. |
| **RCM-010** | AFM Forgery Protection | **PASSED** | `afm_writer.py` uses `safe_dump` and thread-safe queue. |
| **RCM-014** | Internal Audit Log | **PASSED** | `recordAudit` escapes newlines and caps length. |
| **RCM-028** | Hermetic Smoke Test | **PASSED** | `scripts/repro-smoke.sh` verified. |

---

## Test Suite Integrity & Regression Analysis

> [!CAUTION]
> **CRITICAL FINDING: Global Security Bypass in Tests**
> 
> Several test files (`test_pr11_observability.py`, `test_pr5_cache_layers.py`, etc.) perform a **top-level, permanent monkeypatch** of `principal.resolve_effective_principal` with a "permissive" version:
> 
> ```python
> def _permissive_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None):
>     aid = str(supplied_agent_id or "main").strip() or "main"
>     return _EP(agent_id=aid, workspace_id="default", transport=transport, capabilities=["*"])
> _principal_mod.resolve_effective_principal = _permissive_resolve
> ```
> 
> This bypasses all identity verification and vault root restrictions. Because this is done at the module level without teardown, it pollutes the environment for all subsequent tests. In the full `pytest` run, this causes `test_principal_binding.py` and `test_vault_root_binding.py` to **FAIL**, as they detect that the security gates are no longer functional.

### Tool Audit Results
- **Ruff:** 47 violations (unused imports, ambiguous names, etc.).
- **Mypy:** 95 errors (missing library stubs for `faiss`, `tiktoken`, `yaml`, plus several real type-mismatches in `sovrd.py` and `agent_api.py`).
- **NPM Audit:** 4 vulnerabilities in `plugins/sovereign-memory`:
  - `fast-uri <= 3.1.1`: **High** (Path traversal via percent-encoded dots).
  - `hono <= 4.12.17`: **Moderate** (CSS Injection, JWT validation bypass, Cache leakage).
  - `ip-address <= 10.1.0`: **Moderate** (XSS).
- **Bandit/Semgrep:**
  - **SQL Injection Risk:** Several handlers (`agent_api.py`, `retrieval.py`) use f-strings or `.format()` to build dynamic `IN (?)` clauses. While parameterized, the structural interpolation is flagged as a medium risk.
  - **Weak Cryptography:** SHA1 is used for generating deterministic Draft IDs in `engine/afm_passes/`.
  - **Urllib Scheme Audit:** `urllib.request.urlopen` used for local AFM bridge without explicit scheme lockdown.

---

## Adversarial Scenarios & Results

1. **Identity Spoofing:**
   - *Scenario:* Agent A attempts to call `sovrd` claiming to be Agent B.
   - *Result:* **BLOCKED** by implementation in `principal.py`, but **BYPASSED** in current test suite due to pollution.
2. **Vault Escape:**
   - *Scenario:* Agent attempts to read `/etc/passwd` via `..` traversal or symlink.
   - *Result:* **BLOCKED** by `Path.resolve()` in `allows_vault_root`.
3. **AFM Injection:**
   - *Scenario:* Malicious frontmatter injected via `learn()` content.
   - *Result:* **BLOCKED** by `safe_dump` in `afm_writer.py`.

---

## Final Verdict

**Status: REJECTED (PENDING REMEDIATION)**

The core implementation logic for Sovereign Memory Phases 0-2 is technically impressive and correctly addresses the security requirements (G11/G12). However, the **verification integrity is compromised** and the **dependency tree is insecure**.

### Required Remediations:
1. **Fix Test Pollution (CRITICAL):** Remove top-level module monkeypatching in `test_pr11_observability.py`, `test_pr5_cache_layers.py`, `test_pr6_contradictions.py`, and `test_pr9_feedback_trace.py`. All tests must pass in a single `pytest` invocation without bypassing security.
2. **Update Dependencies:** Remediate `fast-uri` (High) and other moderate vulnerabilities in the Node.js plugin.
3. **Type Safety:** Fix the `mypy` errors in `sovrd.py` and `agent_api.py` that indicate missing attributes or union-type index assignment errors.
4. **SQL Hygiene:** Refactor dynamic `IN` clause generation to avoid f-string interpolation of query structure, even if values are parameterized.
