# Dimension 1: Architecture & Separations (Model B - Claude 4.6 Sonnet)

## Conceptual Architecture & Privilege Boundaries

### Privilege Separation: Daemon vs. Client Plugin
Sovereign Memory establishes a local privilege model. The daemon (`sovrd.py`) acts as the reference monitor, managing SQLite and FAISS data. The client plugin (`plugins/sovereign-memory`) acts as the agent-facing translation layer.
However, because both run on the same host and communicate via Unix Domain Sockets, the daemon implicitly trusts UDS transport as the local operator. This creates a privilege boundary collapse: if the client plugin exposes operator tools (like `sovereign_resolve_candidate`) to the agent, the agent can invoke these tools to approve its own candidates, bypassing the operator check.

---

## Findings

### [P0] [Vulnerability] Privilege Gating Bypass via MCP Tool Exposure
* **File:** [plugins/sovereign-memory/src/server.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/server.ts#L504-L540)
* **Summary:** The MCP tool `sovereign_resolve_candidate` is exposed directly to the LLM agent. When the agent invokes this tool, the MCP server forwards the request to the daemon over UDS without supplying an explicit `agent_id`. The daemon treats the connection as trusted (stamping it with the local operator identity), thereby allowing the agent to approve its own staged memory candidates without human operator intervention.
* **Evidence:** `server.ts` line 536 (`agentId omitted → server stamps DEFAULT / local operator`).
* **Recommendation:** Remove `sovereign_resolve_candidate` from the model-facing MCP tool schema. It should only be accessible through an explicit operator CLI or direct console UI.

### [P1] [Design Gap] Lack of API Versioning and Capability Negotiation
* **File:** [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py) and [plugins/sovereign-memory/src/sovereign.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/sovereign.ts)
* **Summary:** The JSON-RPC connection has no version handshake or capability negotiation. If the daemon and plugin versions drift (e.g., during partial upgrades), the plugin may send unsupported parameters or call deprecated methods, leading to unhandled JSON-RPC errors.
* **Evidence:** The `ping` and `status` RPC methods do not return supported capability lists, nor does the plugin verify the daemon version on startup.
* **Recommendation:** Implement a simple version handshake upon socket connection, denying startup if there is a major version mismatch between the daemon and client.
