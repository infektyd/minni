# Dimension 3: Security & Privilege Boundaries (Model B - Claude 4.6 Sonnet)

## Security & Privilege Boundary Analysis

### 1. Unauthenticated JSON-RPC Surfaces
Sovereign Memory uses Unix Domain Sockets (UDS) for IPC, restricting network-level access. However, because multiple processes on the same host may connect to the socket, all endpoints must validate the caller's identity.
Currently, the `status` and `trace` endpoints in `engine/sovrd.py` completely bypass the `resolve_effective_principal` authentication gate. This allows any local client to dump the daemon status (including absolute paths) and read ephemeral process-local traces of other agents' queries by trace ID.

### 2. Default-Allow Synthesis in Non-Strict Mode
Until an operator explicitly creates principal profiles under `principals/`, the daemon runs in a non-strict compatibility mode. Under this mode, any supplied `agent_id` is automatically synthesized as an `EffectivePrincipal` with administrative capabilities (`capabilities=["*"]`). This default-allow fallback opens a significant vulnerability if a deployment is left in non-strict mode.

---

## Findings

### [P1] [Auth Bypass] Unauthenticated trace and status endpoints
* **File:** [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py#L1866)
* **Summary:** The JSON-RPC handlers `_handle_status` and `_handle_trace` do not authenticate callers using `resolve_effective_principal()`. They allow any local agent/process to inspect configuration paths, database metrics, and search traces belonging to other agents.
* **Evidence:** `engine/sovrd.py` lines 1866 (`_handle_status`) and 1088 (`_handle_trace`).
* **Recommendation:** Wrap `_handle_status` and `_handle_trace` with `resolve_effective_principal` check. For `_handle_trace`, verify that the principal requesting the trace matches the agent who generated the trace.

### [P2] [Privilege Escalation] Administrative Capability Auto-Synthesis
* **File:** [engine/principal.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/principal.py#L323-L331)
* **Summary:** In non-strict mode, when the caller supplies any `agent_id`, the engine synthesizes a principal with wildcard capabilities (`capabilities=["*"]`). This lets any client claim a privileged ID or request any action without authentication.
* **Evidence:** `engine/principal.py` lines 325-331.
* **Recommendation:** Restrict synthesized principals in non-strict mode to a baseline of minimum permissions (e.g., read/write to their own vault only) rather than granting wildcard capabilities.
