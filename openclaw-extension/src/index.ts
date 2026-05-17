/**
 * index.ts — Plugin entry point for sovereign-memory
 *
 * OpenClaw loads this via the openclaw.extensions field in package.json.
 * The default export registers the plugin with the OpenClaw plugin API.
 * Named exports remain for internal use (bridge, manager, etc.).
 */

export {
  SovereignMemoryManager,
  getMemorySearchManager,
  type MemorySearchResult,
  type MemorySource,
  type MemoryEmbeddingProbeResult,
  type MemoryProviderStatus,
  type MemorySyncProgressUpdate,
  type LearnResult,
} from "./sovereign-manager.js";

// Direct import for internal use (re-export above makes it available to consumers)
import { SovereignMemoryManager } from "./sovereign-manager.js";

export {
  health,
  recall,
  learn,
  read,
  identity,
  full,
  isRunning,
} from "./bridge.js";

export {
  start,
  stop,
  restart,
  ensureRunning,
  getStatus,
} from "./bridge-process.js";

// Default export: OpenClaw plugin registration object
// OpenClaw 2026.3.23 resolves the extensions entry and calls register() if present.
// Phase 2: Reads workspaceId and agentId from process.env for per-agent DB scoping.

/**
 * Resolve workspace ID from environment or fallback.
 * OpenClaw sets these when spawning agent sessions.
 */
function resolveWorkspaceId(): string {
  const envWorkspace = process.env.OPENCLAW_WORKSPACE_ID || process.env.WORKSPACE_ID;
  if (envWorkspace) return envWorkspace;
  const agentId = process.env.OPENCLAW_AGENT_ID || process.env.AGENT_ID;
  if (agentId) return `workspace-${agentId}`;
  return "workspace-default";
}

/**
 * Resolve agent ID from environment or fallback.
 */
function resolveAgentId(): string {
  return process.env.OPENCLAW_AGENT_ID
    || process.env.AGENT_ID
    || "hermes";
}

const plugin = {
  id: "sovereign-memory",
  name: "Sovereign Memory",
  description: "Local-first semantic memory — FAISS+FTS5 hybrid retrieval for OpenClaw agents.",
  register(api: { registerTool?: (t: unknown) => void; on?: (event: string, cb: unknown) => void; agentId?: string; workspaceId?: string }) {
    // Read workspace context from OpenClaw's register API or process.env.
    const agentId = api?.agentId || resolveAgentId();
    const workspaceId = api?.workspaceId || resolveWorkspaceId();

    // Instantiate SovereignMemoryManager with Phase 2 workspaceId scoping.
    // This is used by OpenClaw's memory subsystem when it calls
    // getMemorySearchManager() at agent startup.
    const manager = new SovereignMemoryManager(agentId, workspaceId);

    // Log workspace bridge wiring at plugin load time.
    console.log(`[sovereign-memory] Phase 2 bridge: agent=${agentId}, workspace=${workspaceId}`);

    // Plugin loaded — sovereign memory capabilities are exposed via the bridge
    // functions (recall, learn, etc.) already exported as named exports above.
    // No tools registered at the plugin level; agents consume via the Python
    // daemon at /tmp/sovereign.sock directly through their tool schemas.
    if (typeof api?.on === "function") {
      api.on("before_agent_start", async () => {
        // Daemon health check on agent start — non-blocking, fire and forget
        try {
          const { isRunning } = await import("./bridge.js");
          const running = await isRunning();
          if (!running) {
            console.warn("[sovereign-memory] sovrd daemon not running at /tmp/sovereign.sock — start it with: python3 ~/.openclaw/extensions/sovereign-memory/sovrd.py");
          }
        } catch {
          // Silently skip — daemon may not be needed for this agent run
        }
      });
    }
  },
};

export default plugin;
