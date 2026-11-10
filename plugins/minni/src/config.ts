import { createHash, randomBytes } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const PLUGIN_ROOT = path.resolve(__dirname, "..");

/**
 * Expand a leading `~` to the home dir. Plugin manifests pass env values like
 * `~/.minni/run/minnid.sock` literally; Node does NOT expand `~`, so an
 * unexpanded value reaches net.createConnection()/existsSync() and the daemon
 * socket + vault lookups fail. Expand centrally so every consumer gets an
 * absolute path.
 */
export function expandTilde(p: string): string {
  return p.replace(/^~(?=$|\/)/, os.homedir());
}

/**
 * Normalize workspace_id to canonical form 'workspace-<basename>'.
 * - If already 'workspace-*', lowercase and return it.
 * - If a filesystem path, extract basename, lowercase, prepend 'workspace-'.
 * - Empty/undefined returns empty string.
 */
export function normalizeWorkspaceId(value: string | undefined): string {
  if (!value) return "";
  value = value.trim();
  if (!value) return "";
  if (value.startsWith("workspace-")) {
    return "workspace-" + value.slice("workspace-".length).toLowerCase();
  }
  const basename = path.basename(value.replace(/\/$/, ""));
  if (!basename) return "";
  return "workspace-" + basename.toLowerCase();
}

export const ACTIVE_SESSION_TTL_MS = 30 * 60 * 1000;
export const ACTIVE_SESSION_PRUNE_AGE_MS = 24 * 60 * 60 * 1000;

export type HintMissReason =
  | "hint_file_missing"
  | "hint_pid_not_alive"
  | "hint_agent_mismatch"
  | "hint_expired_ttl"
  | "hint_clock_skew"
  | "hint_workspace_invalid"
  | "hint_parse_error";

export interface WorkspaceResolutionDiag {
  event: "workspace_resolution_diag";
  agent_id: string;
  host_pid: number;
  resolved_workspace_id: string;
  source: "explicit_or_pinned" | "active_session_hint" | "cwd_git_walk" | "unknown_fallback";
  hint_miss_reason: HintMissReason | null;
  candidate_other_pids?: number[];
  timestamp: string;
}

export interface ActiveSessionWorkspaceOptions {
  agentId?: string;
  hostPid?: number;
  nowMs?: number;
  sessionId?: string;
  sessionKey?: string;
}

export interface ResolveSessionWorkspaceOptions {
  agentId?: string;
  hostPid?: number;
  ttlMs?: number;
  nowMs?: number;
  checkHostPidAlive?: boolean;
  onDiagnostic?: (diag: WorkspaceResolutionDiag) => void;
}

export interface McpSessionIdentity {
  sessionId?: string;
  sessionKey: string;
  hostPid: number;
  matchedHintPid?: number;
}

export interface ResolveMcpSessionIdentityOptions {
  agentId?: string;
  hostPid?: number;
  sessionId?: string;
  ttlMs?: number;
  nowMs?: number;
  checkHostPidAlive?: boolean;
  parentPidResolver?: (pid: number) => number | undefined;
}

function deriveSessionKeyFromId(sessionId: string | undefined): string | undefined {
  if (typeof sessionId !== "string") return undefined;
  const trimmed = sessionId.trim();
  if (!trimmed || trimmed === "session") return undefined;
  return createHash("sha256").update(trimmed).digest("hex").slice(0, 16);
}

function defaultParentPidResolver(pid: number): number | undefined {
  if (!Number.isInteger(pid) || pid <= 1) return undefined;
  try {
    const procStat = `/proc/${pid}/stat`;
    if (existsSync(procStat)) {
      const raw = readFileSync(procStat, "utf8");
      const closeParen = raw.lastIndexOf(")");
      if (closeParen >= 0) {
        const fields = raw
          .slice(closeParen + 1)
          .trim()
          .split(/\s+/);
        const ppid = Number(fields[1]);
        if (Number.isInteger(ppid) && ppid > 1 && ppid !== pid) return ppid;
      }
    }
  } catch {
    // Fall back to ps below.
  }
  try {
    const out = execFileSync("ps", ["-o", "ppid=", "-p", String(pid)], {
      encoding: "utf8",
      timeout: 500,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    const ppid = Number(out);
    if (Number.isInteger(ppid) && ppid > 1 && ppid !== pid) return ppid;
  } catch {
    // Ignore ps failure.
  }
  return undefined;
}

function isPidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    return code === "EPERM";
  }
}

function isDisallowedHintPath(rawInput: string): boolean {
  const trimmed = rawInput.trim();
  if (!trimmed || trimmed.startsWith("workspace-")) return false;
  const expanded = expandTilde(trimmed);
  if (!path.isAbsolute(expanded)) return false;
  const resolved = path.resolve(expanded);
  const home = os.homedir();
  if (home) {
    const pluginPrefixes = [
      path.join(home, ".minni", "plugin"),
      path.join(home, ".cursor", "plugins"),
      path.join(home, ".agents", "plugins"),
    ];
    for (const prefix of pluginPrefixes) {
      if (resolved === prefix || resolved.startsWith(prefix + path.sep)) {
        return true;
      }
    }
    if (resolved === path.resolve(home) && !existsSync(path.join(resolved, ".git"))) {
      return true;
    }
  }
  return resolved.includes(`${path.sep}.minni${path.sep}plugin`);
}

function sanitizeSessionKeyPart(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, "-") || "unknown-agent";
}

function activeSessionFilename(agentId: string, hostPid: number): string {
  return `active-session-${sanitizeSessionKeyPart(agentId)}-${hostPid}.json`;
}

const AGENT_SESSION_ENV_VARS: Record<string, readonly string[]> = {
  "claude-code": ["CLAUDE_CODE_SESSION_ID"],
  codex: ["CODEX_SESSION_ID"],
  gemini: ["GEMINI_SESSION_ID", "ANTIGRAVITY_SESSION_ID"],
  "grok-build": ["GROK_SESSION_ID"],
  cursor: ["CURSOR_SESSION_ID"],
  kilocode: ["KILOCODE_SESSION_ID"],
};

const SINGLE_LIVE_SESSION_ALLOWLIST: ReadonlySet<string> = new Set(["cursor"]);

function resolveAgentScopedEnvSessionId(agentId: string): string | undefined {
  const generic = process.env.MINNI_SESSION_ID?.trim();
  if (generic && generic !== "session") return generic;
  const allowedVars = AGENT_SESSION_ENV_VARS[agentId] ?? [];
  for (const envName of allowedVars) {
    const val = process.env[envName]?.trim();
    if (val && val !== "session") return val;
  }
  return undefined;
}

export function recordActiveSessionWorkspace(
  vaultPath: string | undefined,
  workspaceIdOrPath: string | undefined,
  agentIdOrOptions?: string | ActiveSessionWorkspaceOptions,
  options?: ActiveSessionWorkspaceOptions,
): void {
  if (!vaultPath) return;
  const opts: ActiveSessionWorkspaceOptions =
    typeof agentIdOrOptions === "object" && agentIdOrOptions !== null
      ? agentIdOrOptions
      : { ...options, ...(typeof agentIdOrOptions === "string" ? { agentId: agentIdOrOptions } : {}) };
  const hasValidWorkspaceInput = Boolean(
    workspaceIdOrPath && !isDisallowedHintPath(workspaceIdOrPath),
  );
  const normalized = hasValidWorkspaceInput ? normalizeWorkspaceId(workspaceIdOrPath) : "";
  const validWorkspace =
    normalized && normalized !== "workspace-unknown" ? normalized : undefined;
  const hasExplicitSession =
    (typeof opts.sessionId === "string" &&
      opts.sessionId.trim() !== "" &&
      opts.sessionId.trim() !== "session") ||
    (typeof opts.sessionKey === "string" && /^[0-9a-f]{16}$/.test(opts.sessionKey));
  // R1 (§9): write the session record whenever a valid workspace OR a session
  // identity is present; omit workspace_id when the workspace is unknown/disallowed.
  if (!validWorkspace && !hasExplicitSession) return;
  const resolvedAgentId = (opts.agentId ?? DEFAULT_AGENT_ID).trim() || DEFAULT_AGENT_ID;
  const resolvedHostPid =
    typeof opts.hostPid === "number" && Number.isInteger(opts.hostPid) && opts.hostPid > 0
      ? opts.hostPid
      : process.ppid;
  const nowMs = typeof opts.nowMs === "number" && Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  try {
    const resolvedVault = expandTilde(vaultPath);
    const runtimeDir = path.join(resolvedVault, ".runtime");
    mkdirSync(runtimeDir, { recursive: true });
    const realVault = realpathSync(resolvedVault);
    const realRuntime = realpathSync(runtimeDir);
    const relRuntime = path.relative(realVault, realRuntime);
    if (relRuntime.startsWith("..") || path.isAbsolute(relRuntime)) return;
    const targetName = activeSessionFilename(resolvedAgentId, resolvedHostPid);
    const target = path.join(runtimeDir, targetName);
    let existingSessionId: string | undefined;
    let existingSessionKey: string | undefined;
    let existingWorkspaceId: string | undefined;
    try {
      const prior = JSON.parse(readFileSync(target, "utf8")) as Record<string, unknown>;
      if (typeof prior?.session_id === "string" && prior.session_id.trim()) {
        existingSessionId = prior.session_id.trim();
      }
      if (typeof prior?.session_key === "string" && /^[0-9a-f]{16}$/.test(prior.session_key)) {
        existingSessionKey = prior.session_key;
      }
      if (typeof prior?.workspace_id === "string") {
        const priorWs = normalizeWorkspaceId(prior.workspace_id);
        if (priorWs && priorWs !== "workspace-unknown") {
          existingWorkspaceId = priorWs;
        }
      }
    } catch {
      // No existing hint file or unreadable.
    }
    const rawSessionId =
      typeof opts.sessionId === "string" && opts.sessionId.trim() && opts.sessionId.trim() !== "session"
        ? opts.sessionId.trim()
        : existingSessionId;
    const resolvedSessionKey =
      typeof opts.sessionKey === "string" && /^[0-9a-f]{16}$/.test(opts.sessionKey)
        ? opts.sessionKey
        : deriveSessionKeyFromId(rawSessionId) ?? existingSessionKey;
    const effectiveWorkspace = validWorkspace ?? existingWorkspaceId;
    const tmp = `${target}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`;
    writeFileSync(
      tmp,
      `${JSON.stringify(
        {
          ...(effectiveWorkspace ? { workspace_id: effectiveWorkspace } : {}),
          agent_id: resolvedAgentId,
          host_pid: resolvedHostPid,
          ...(rawSessionId ? { session_id: rawSessionId } : {}),
          ...(resolvedSessionKey ? { session_key: resolvedSessionKey } : {}),
          updated_at: new Date(nowMs).toISOString(),
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    renameSync(tmp, target);
    try {
      const prefix = `active-session-${sanitizeSessionKeyPart(resolvedAgentId)}-`;
      for (const entry of readdirSync(runtimeDir)) {
        if (entry === targetName || !entry.startsWith(prefix) || !entry.endsWith(".json")) {
          continue;
        }
        const pidPart = entry.slice(prefix.length, -".json".length);
        if (!/^\d+$/.test(pidPart)) continue;
        const candidatePath = path.join(runtimeDir, entry);
        try {
          let updatedAtMs = Number.NaN;
          try {
            const parsed = JSON.parse(readFileSync(candidatePath, "utf8")) as Record<string, unknown>;
            if (typeof parsed?.agent_id === "string" && parsed.agent_id !== resolvedAgentId) {
              continue;
            }
            if (typeof parsed?.updated_at === "string") {
              updatedAtMs = Date.parse(parsed.updated_at);
            }
          } catch {
            // Corrupt JSON falls back to mtime check.
          }
          const mtimeMs = statSync(candidatePath).mtimeMs;
          const isStaleByUpdatedAt =
            Number.isFinite(updatedAtMs) && nowMs - updatedAtMs > ACTIVE_SESSION_PRUNE_AGE_MS;
          const isStaleByMtime =
            (!Number.isFinite(updatedAtMs) || updatedAtMs - mtimeMs > ACTIVE_SESSION_PRUNE_AGE_MS) &&
            nowMs - mtimeMs > ACTIVE_SESSION_PRUNE_AGE_MS;
          if (isStaleByUpdatedAt || isStaleByMtime) {
            unlinkSync(candidatePath);
          }
        } catch {
          // Best-effort pruning; never fail a hook.
        }
      }
    } catch {
      // Best-effort pruning; never fail a hook.
    }
  } catch {
    // Best-effort runtime hint; never fail a hook.
  }
}

export const writeActiveSessionHint = recordActiveSessionWorkspace;

export function resolveMcpSessionIdentity(
  vaultPath: string = DEFAULT_VAULT_PATH,
  options: ResolveMcpSessionIdentityOptions = {},
): McpSessionIdentity {
  const resolvedAgentId = (options.agentId ?? DEFAULT_AGENT_ID).trim() || DEFAULT_AGENT_ID;
  const resolvedHostPid =
    typeof options.hostPid === "number" && Number.isInteger(options.hostPid) && options.hostPid > 0
      ? options.hostPid
      : process.ppid;
  const explicitSessionId =
    typeof options.sessionId === "string" &&
    options.sessionId.trim() &&
    options.sessionId.trim() !== "session"
      ? options.sessionId.trim()
      : resolveAgentScopedEnvSessionId(resolvedAgentId);
  if (explicitSessionId) {
    const key = deriveSessionKeyFromId(explicitSessionId);
    if (key) {
      return {
        sessionId: explicitSessionId,
        sessionKey: key,
        hostPid: resolvedHostPid,
      };
    }
  }

  const ttlMs =
    typeof options.ttlMs === "number" && Number.isFinite(options.ttlMs) && options.ttlMs > 0
      ? options.ttlMs
      : ACTIVE_SESSION_TTL_MS;
  const nowMs =
    typeof options.nowMs === "number" && Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const checkAlive = options.checkHostPidAlive !== false;
  const resolveParent = options.parentPidResolver ?? defaultParentPidResolver;

  if (vaultPath) {
    try {
      const resolvedVault = expandTilde(vaultPath);
      const runtimeDir = path.join(resolvedVault, ".runtime");
      if (existsSync(runtimeDir)) {
        const realVault = realpathSync(resolvedVault);
        const realRuntime = realpathSync(runtimeDir);
        const relRuntime = path.relative(realVault, realRuntime);
        if (!relRuntime.startsWith("..") && !path.isAbsolute(relRuntime)) {
          const MAX_ANCESTOR_HOPS = 3;
          let candidatePid: number | undefined = resolvedHostPid;
          for (
            let hop = 0;
            hop <= MAX_ANCESTOR_HOPS && candidatePid && candidatePid > 1;
            hop++
          ) {
            const candidateHintPaths: string[] = [
              path.join(runtimeDir, activeSessionFilename(resolvedAgentId, candidatePid)),
            ];
            if (resolvedAgentId === "unknown-agent") {
              try {
                const suffix = `-${candidatePid}.json`;
                for (const entry of readdirSync(runtimeDir)) {
                  if (entry.startsWith("active-session-") && entry.endsWith(suffix)) {
                    const p = path.join(runtimeDir, entry);
                    if (!candidateHintPaths.includes(p)) candidateHintPaths.push(p);
                  }
                }
              } catch {
                // Ignore readdir failure.
              }
            }
            for (const hintPath of candidateHintPaths) {
              if (!existsSync(hintPath)) continue;
              try {
                const parsed = JSON.parse(readFileSync(hintPath, "utf8")) as Record<string, unknown>;
                const sameAgent =
                  resolvedAgentId === "unknown-agent" ||
                  typeof parsed?.agent_id !== "string" ||
                  parsed.agent_id === resolvedAgentId;
                const samePid =
                  typeof parsed?.host_pid !== "number" || parsed.host_pid === candidatePid;
                const aliveOk = !checkAlive || isPidAlive(candidatePid);
                const updatedAtMs =
                  typeof parsed?.updated_at === "string"
                    ? Date.parse(parsed.updated_at)
                    : Number.NaN;
                const freshOk =
                  !Number.isFinite(updatedAtMs) ||
                  (nowMs - updatedAtMs <= ttlMs && updatedAtMs - nowMs <= 60_000);
                if (sameAgent && samePid && aliveOk && freshOk) {
                  const sid =
                    typeof parsed?.session_id === "string" &&
                    parsed.session_id.trim() &&
                    parsed.session_id.trim() !== "session"
                      ? parsed.session_id.trim()
                      : undefined;
                  const skey =
                    typeof parsed?.session_key === "string" &&
                    /^[0-9a-f]{16}$/.test(parsed.session_key)
                      ? parsed.session_key
                      : deriveSessionKeyFromId(sid);
                  if (skey) {
                    return {
                      ...(sid ? { sessionId: sid } : {}),
                      sessionKey: skey,
                      hostPid: resolvedHostPid,
                      matchedHintPid: candidatePid,
                    };
                  }
                }
              } catch {
                // Ignore corrupt hint file and continue ancestor walk.
              }
            }
            if (hop < MAX_ANCESTOR_HOPS) {
              candidatePid = resolveParent(candidatePid);
            }
          }

          // Priority 3 (R1 item 2): single-live-session fallback is allowed ONLY
          // for hosts on SINGLE_LIVE_SESSION_ALLOWLIST (today: Cursor). Every other
          // host falls straight through to Priority 4 (ppid_derived) so it never
          // borrows another live session's key.
          if (SINGLE_LIVE_SESSION_ALLOWLIST.has(resolvedAgentId)) {
            const prefix = `active-session-${sanitizeSessionKeyPart(resolvedAgentId)}-`;
            const liveMatches: Array<{ sid?: string; skey: string; pid: number }> = [];
            const seenKeys = new Set<string>();
            for (const entry of readdirSync(runtimeDir)) {
              if (!entry.startsWith(prefix) || !entry.endsWith(".json")) continue;
              const pidPart = entry.slice(prefix.length, -".json".length);
              if (!/^\d+$/.test(pidPart)) continue;
              const pid = Number(pidPart);
              if (checkAlive && !isPidAlive(pid)) continue;
              try {
                const parsed = JSON.parse(
                  readFileSync(path.join(runtimeDir, entry), "utf8"),
                ) as Record<string, unknown>;
                const updatedAtMs =
                  typeof parsed?.updated_at === "string"
                    ? Date.parse(parsed.updated_at)
                    : Number.NaN;
                const freshOk =
                  !Number.isFinite(updatedAtMs) ||
                  (nowMs - updatedAtMs <= ttlMs && updatedAtMs - nowMs <= 60_000);
                if (!freshOk) continue;
                const sid =
                  typeof parsed?.session_id === "string" &&
                  parsed.session_id.trim() &&
                  parsed.session_id.trim() !== "session"
                    ? parsed.session_id.trim()
                    : undefined;
                const skey =
                  typeof parsed?.session_key === "string" &&
                  /^[0-9a-f]{16}$/.test(parsed.session_key)
                    ? parsed.session_key
                    : deriveSessionKeyFromId(sid);
                if (skey && !seenKeys.has(skey)) {
                  seenKeys.add(skey);
                  liveMatches.push({ sid, skey, pid });
                }
              } catch {
                // Ignore unreadable entry.
              }
            }
            if (liveMatches.length === 1) {
              return {
                ...(liveMatches[0].sid ? { sessionId: liveMatches[0].sid } : {}),
                sessionKey: liveMatches[0].skey,
                hostPid: resolvedHostPid,
                matchedHintPid: liveMatches[0].pid,
              };
            }
          }
        }
      }
    } catch {
      // Best-effort hint resolution; fall back to synthetic pid key below.
    }
  }

  const syntheticSessionId = `pid-${resolvedHostPid}`;
  const syntheticKey =
    deriveSessionKeyFromId(syntheticSessionId) ??
    createHash("sha256").update(syntheticSessionId).digest("hex").slice(0, 16);
  return {
    sessionId: syntheticSessionId,
    sessionKey: syntheticKey,
    hostPid: resolvedHostPid,
  };
}

let warnedSessionPpidMismatch = false;

export function resolveSessionWorkspaceWithDiag(
  vaultPath: string = DEFAULT_VAULT_PATH,
  explicitWorkspaceId?: string,
  options: ResolveSessionWorkspaceOptions = {},
): {
  workspace_id: string;
  source: "explicit_or_pinned" | "active_session_hint" | "cwd_git_walk" | "unknown_fallback";
  hint_miss_reason: HintMissReason | null;
  diag?: WorkspaceResolutionDiag;
} {
  // Step 1: Explicit caller argument or operator pin (MINNI_PINNED_WORKSPACE_ID)
  const pinnedRaw =
    typeof explicitWorkspaceId === "string" && explicitWorkspaceId.trim()
      ? explicitWorkspaceId.trim()
      : typeof process.env.MINNI_PINNED_WORKSPACE_ID === "string" &&
          process.env.MINNI_PINNED_WORKSPACE_ID.trim()
        ? process.env.MINNI_PINNED_WORKSPACE_ID.trim()
        : "";
  if (pinnedRaw) {
    const ws = normalizeWorkspaceId(pinnedRaw) || DEFAULT_WORKSPACE_ID;
    return {
      workspace_id: ws,
      source: "explicit_or_pinned",
      hint_miss_reason: null,
    };
  }

  const resolvedAgentId = (options.agentId ?? DEFAULT_AGENT_ID).trim() || DEFAULT_AGENT_ID;
  const resolvedHostPid =
    typeof options.hostPid === "number" && Number.isInteger(options.hostPid) && options.hostPid > 0
      ? options.hostPid
      : process.ppid;
  const ttlMs =
    typeof options.ttlMs === "number" && Number.isFinite(options.ttlMs) && options.ttlMs > 0
      ? options.ttlMs
      : ACTIVE_SESSION_TTL_MS;
  const nowMs = typeof options.nowMs === "number" && Number.isFinite(options.nowMs) ? options.nowMs : Date.now();

  let missReason: HintMissReason = "hint_file_missing";
  const otherFreshPids: number[] = [];

  if (vaultPath && typeof vaultPath === "string") {
    try {
      const runtimeDir = path.join(expandTilde(vaultPath), ".runtime");
      const targetName = activeSessionFilename(resolvedAgentId, resolvedHostPid);
      const target = path.join(runtimeDir, targetName);
      if (existsSync(target)) {
        let parsed: Record<string, unknown> | null = null;
        try {
          const rawObj: unknown = JSON.parse(readFileSync(target, "utf8"));
          if (rawObj !== null && typeof rawObj === "object" && !Array.isArray(rawObj)) {
            parsed = rawObj as Record<string, unknown>;
          } else {
            missReason = "hint_parse_error";
          }
        } catch {
          missReason = "hint_parse_error";
        }

        if (parsed !== null) {
          if (parsed.agent_id !== resolvedAgentId) {
            missReason = "hint_agent_mismatch";
          } else if (
            parsed.host_pid !== resolvedHostPid ||
            (options.checkHostPidAlive === true && !isPidAlive(resolvedHostPid))
          ) {
            missReason = "hint_pid_not_alive";
          } else {
            const updatedAtRaw = typeof parsed.updated_at === "string" ? parsed.updated_at : "";
            const updatedAtMs = updatedAtRaw ? Date.parse(updatedAtRaw) : Number.NaN;
            if (!Number.isFinite(updatedAtMs)) {
              missReason = "hint_parse_error";
            } else if (updatedAtMs - nowMs > 60_000) {
              missReason = "hint_clock_skew";
            } else if (nowMs - updatedAtMs > ttlMs) {
              missReason = "hint_expired_ttl";
            } else {
              const activeWs =
                typeof parsed.workspace_id === "string" ? normalizeWorkspaceId(parsed.workspace_id) : "";
              if (activeWs && activeWs !== "workspace-unknown") {
                return {
                  workspace_id: activeWs,
                  source: "active_session_hint",
                  hint_miss_reason: null,
                };
              }
              missReason = "hint_workspace_invalid";
            }
          }
        }
      } else {
        missReason = "hint_file_missing";
        if (existsSync(runtimeDir)) {
          const prefix = `active-session-${sanitizeSessionKeyPart(resolvedAgentId)}-`;
          for (const entry of readdirSync(runtimeDir)) {
            if (entry === targetName || !entry.startsWith(prefix) || !entry.endsWith(".json")) {
              continue;
            }
            const pidPart = entry.slice(prefix.length, -".json".length);
            if (!/^\d+$/.test(pidPart)) continue;
            try {
              const parsed = JSON.parse(
                readFileSync(path.join(runtimeDir, entry), "utf8"),
              ) as Record<string, unknown>;
              if (
                parsed?.agent_id !== resolvedAgentId ||
                typeof parsed?.host_pid !== "number" ||
                parsed.host_pid === resolvedHostPid
              ) {
                continue;
              }
              const updatedAtRaw = typeof parsed?.updated_at === "string" ? parsed.updated_at : "";
              const updatedAtMs = updatedAtRaw ? Date.parse(updatedAtRaw) : Number.NaN;
              if (!Number.isFinite(updatedAtMs) || nowMs - updatedAtMs > ttlMs || updatedAtMs - nowMs > 60_000) {
                continue;
              }
              const otherWs =
                typeof parsed?.workspace_id === "string" ? normalizeWorkspaceId(parsed.workspace_id) : "";
              if (otherWs && otherWs !== "workspace-unknown") {
                otherFreshPids.push(parsed.host_pid);
                if (!warnedSessionPpidMismatch) {
                  warnedSessionPpidMismatch = true;
                  process.stderr.write(
                    `[minni] active-session hint missing for agent=${resolvedAgentId} host_pid=${resolvedHostPid}, but fresh hint exists for host_pid=${parsed.host_pid}; falling back to ${DEFAULT_WORKSPACE_ID}\n`,
                  );
                }
              }
            } catch {
              // Ignore unreadable candidate file.
            }
          }
        }
      }
    } catch {
      missReason = "hint_parse_error";
    }
  }

  const fallbackWs = DEFAULT_WORKSPACE_ID;
  const fallbackSource = fallbackWs === "workspace-unknown" ? "unknown_fallback" : "cwd_git_walk";
  const diag: WorkspaceResolutionDiag = {
    event: "workspace_resolution_diag",
    agent_id: resolvedAgentId,
    host_pid: resolvedHostPid,
    resolved_workspace_id: fallbackWs,
    source: fallbackSource,
    hint_miss_reason: missReason,
    ...(otherFreshPids.length > 0 ? { candidate_other_pids: otherFreshPids } : {}),
    timestamp: new Date(nowMs).toISOString(),
  };
  if (typeof options.onDiagnostic === "function") {
    try {
      options.onDiagnostic(diag);
    } catch {
      // Never throw from diagnostic hook.
    }
  }
  return {
    workspace_id: fallbackWs,
    source: fallbackSource,
    hint_miss_reason: missReason,
    diag,
  };
}

export function resolveSessionWorkspaceId(
  vaultPath: string = DEFAULT_VAULT_PATH,
  explicitWorkspaceId?: string,
  options: ResolveSessionWorkspaceOptions = {},
): string {
  return resolveSessionWorkspaceWithDiag(vaultPath, explicitWorkspaceId, options).workspace_id;
}

/**
 * Derive a workspace id from the working directory, adaptively.
 *
 * A bare `path.basename(process.cwd())` is not safe on its own. Hooks and MCP
 * servers are frequently launched with cwd set to the home directory or to a
 * plugin cache, which stamps meaningless ids like `workspace-operator` or
 * `workspace-0.3.0` onto every row written for that session. Once written they
 * are indistinguishable from real project scopes.
 *
 * Prefer the enclosing git repository root, which is the unit users actually
 * mean by "workspace" and which resolves git worktrees to their own name.
 * Fail closed to `workspace-unknown` when cwd cannot identify a project, so a
 * missing signal stays visibly missing instead of masquerading as a scope.
 */
export function deriveWorkspaceIdFromCwd(cwd: string = process.cwd()): string {
  const resolved = path.resolve(cwd);
  const filesystemRoot = path.parse(resolved).root;

  // A git worktree stores `.git` as a FILE, not a directory, so test existence
  // rather than directory-ness.
  let dir = resolved;
  while (dir !== filesystemRoot) {
    if (existsSync(path.join(dir, ".git"))) {
      return normalizeWorkspaceId(dir) || "workspace-unknown";
    }
    dir = path.dirname(dir);
  }

  // No enclosing repository, so cwd carries no evidence of which project this
  // is. Fail closed rather than stamping the directory name: hooks and MCP
  // servers launch from plugin caches, temp dirs, and $HOME, and a plausible
  // looking id minted from one of those is worse than an explicit unknown --
  // it is indistinguishable from a real scope once written.
  return "workspace-unknown";
}

/**
 * Minni home dir, honoring MINNI_HOME (mirror of engine/config.py, which reads
 * os.environ["MINNI_HOME"] for the same paths — without this the engine and
 * the plugin would silently load different provider configs).
 */
export function minniHome(): string {
  const fromEnv = process.env.MINNI_HOME;
  return fromEnv ? expandTilde(fromEnv) : path.join(os.homedir(), ".minni");
}

export const DEFAULT_VAULT_PATH = expandTilde(
  process.env.MINNI_VAULT_PATH ??
    process.env.MINNI_CLAUDECODE_VAULT_PATH ??
    process.env.MINNI_CODEX_VAULT_PATH ??
    path.join(minniHome(), "unknown-vault"),
);

export function resolveActiveVaultPath(preferredVaultPath?: string): string {
  if (typeof preferredVaultPath === "string" && preferredVaultPath.trim()) {
    const expanded = expandTilde(preferredVaultPath.trim());
    if (path.basename(expanded) !== "unknown-vault" || existsSync(expanded)) {
      return expanded;
    }
    const parent = path.dirname(expanded);
    for (const sub of ["claudecode-vault", "vault", "codex-vault"]) {
      const candidate = path.join(parent, sub);
      try {
        if (existsSync(candidate)) return candidate;
      } catch {
        // Continue checking candidates.
      }
    }
  }
  const envVault =
    process.env.MINNI_VAULT_PATH ??
    process.env.MINNI_CLAUDECODE_VAULT_PATH ??
    process.env.MINNI_CODEX_VAULT_PATH;
  if (envVault && envVault.trim()) {
    return expandTilde(envVault.trim());
  }
  const home = minniHome();
  for (const sub of ["claudecode-vault", "vault", "codex-vault"]) {
    const candidate = path.join(home, sub);
    try {
      if (existsSync(candidate)) return candidate;
    } catch {
      // Continue checking candidates.
    }
  }
  return path.join(home, "vault");
}

export const SOCKET_PATH = expandTilde(
  process.env.MINNI_SOCKET_PATH ??
    path.join(os.homedir(), ".minni", "run", "minnid.sock"),
);

export const AFM_HEALTH_URL =
  process.env.MINNI_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

export const AFM_PREPARE_TASK_URL =
  process.env.MINNI_AFM_PREPARE_TASK_URL ?? "http://127.0.0.1:11437/v1/chat/completions";

export const AFM_PREPARE_TASK_MODEL =
  process.env.MINNI_AFM_PREPARE_TASK_MODEL ?? "apple-foundation-models";

// G13 (SEC-004): explicit operator allowlist for non-loopback model targets.
// Comma-separated hosts (e.g. "192.168.1.10,afm.internal"). Loopback (127.0.0.1,localhost,::1) always allowed.
// MINNI_MODEL_ALLOWED_TARGETS is the provider-protocol alias for MINNI_AFM_ALLOWED_TARGETS;
// both are honored (union). Non-loopback targets additionally require HTTPS.
// If a non-local target is configured without being listed, callAfmJson will deny with structured error.
export function modelAllowedTargets(): string[] {
  return [process.env.MINNI_AFM_ALLOWED_TARGETS, process.env.MINNI_MODEL_ALLOWED_TARGETS]
    .flatMap((value) => (value ?? "").split(","))
    .map((s) => s.trim())
    .filter(Boolean);
}

export const AFM_ALLOWED_TARGETS: string[] = modelAllowedTargets();
export const MODEL_ALLOWED_TARGETS: string[] = AFM_ALLOWED_TARGETS;

// --- Model provider chain config (P3) ---------------------------------------
// ~/.minni/providers.json configures the provider chain and per-operation
// routing policy. MINNI_AFM_* env vars keep precedence over file values.
// Secrets are NEVER stored in providers.json: cloud credentials come only from
// apiKeyEnv (env var name) or apiKeyFile (0600 file under ~/.minni/secrets/).

export interface CloudProviderConfig {
  enabled?: boolean;
  vendor?: string;
  model?: string;
  apiKeyEnv?: string;
  apiKeyFile?: string;
  privacyMax?: boolean;
}

export interface ProvidersConfig {
  chain: string[];
  operations: Record<string, { localOnly?: boolean }>;
  providers: {
    mlx?: { baseUrl?: string; model?: string };
    ollama?: { baseUrl?: string; model?: string };
    cloud?: CloudProviderConfig;
  };
}

const DEFAULT_PROVIDERS_CONFIG: ProvidersConfig = {
  chain: ["afm"],
  operations: { retrieval: { localOnly: true } },
  providers: {},
};

/** Mirror of engine/config.py providers_config_path (env read at call time). */
export function providersConfigPath(): string {
  return expandTilde(process.env.MINNI_PROVIDERS_CONFIG ?? path.join(minniHome(), "providers.json"));
}

export const PROVIDERS_CONFIG_PATH = providersConfigPath();

export function loadProvidersConfig(filePath = providersConfigPath()): ProvidersConfig {
  let raw: string;
  try {
    raw = readFileSync(filePath, "utf8");
  } catch {
    return { ...DEFAULT_PROVIDERS_CONFIG };
  }
  try {
    const parsed = JSON.parse(raw) as Partial<ProvidersConfig> & Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_PROVIDERS_CONFIG };
    const chain = Array.isArray(parsed.chain)
      ? parsed.chain.filter((item): item is string => typeof item === "string" && item.length > 0)
      : DEFAULT_PROVIDERS_CONFIG.chain;
    const operations =
      parsed.operations && typeof parsed.operations === "object"
        ? (parsed.operations as ProvidersConfig["operations"])
        : DEFAULT_PROVIDERS_CONFIG.operations;
    const providers =
      parsed.providers && typeof parsed.providers === "object"
        ? (parsed.providers as ProvidersConfig["providers"])
        : {};
    // SEC: inline secrets are rejected outright — keys live in env or 0600 files only.
    const cloud = providers.cloud as (CloudProviderConfig & { apiKey?: unknown }) | undefined;
    if (cloud && "apiKey" in cloud) {
      console.warn(
        "[minni] providers.json: inline providers.cloud.apiKey is not allowed (use apiKeyEnv or apiKeyFile); cloud provider disabled",
      );
      providers.cloud = { ...cloud, apiKey: undefined, enabled: false } as CloudProviderConfig;
      delete (providers.cloud as Record<string, unknown>).apiKey;
    }
    return { chain: chain.length > 0 ? chain : ["afm"], operations, providers };
  } catch {
    console.warn("[minni] providers.json: invalid JSON; using default AFM-only chain");
    return { ...DEFAULT_PROVIDERS_CONFIG };
  }
}

export const PROVIDERS_CONFIG: ProvidersConfig = loadProvidersConfig();

/** Mirror of engine/config.py minni_secrets_dir (honors MINNI_HOME at call time). */
export function minniSecretsDir(): string {
  return path.join(minniHome(), "secrets");
}

export const MINNI_SECRETS_DIR = minniSecretsDir();

export interface CloudApiKeyResolution {
  key?: string;
  /** Structured, key-free reason when no usable secret was found. */
  error?: string;
}

/**
 * Resolve the cloud provider API key. Secrets come ONLY from:
 *   - apiKeyEnv: the named environment variable, or
 *   - apiKeyFile: a 0600 file under ~/.minni/secrets/
 * Never from providers.json itself. The resolved key must never be written to
 * status/audit/error output (safeError strips auth material as a backstop).
 */
export function resolveCloudApiKey(
  cloud: CloudProviderConfig | undefined,
  secretsDir = minniSecretsDir(),
): CloudApiKeyResolution {
  if (!cloud || cloud.enabled !== true) return {};
  if (cloud.apiKeyEnv) {
    const key = process.env[cloud.apiKeyEnv];
    return key ? { key } : { error: `cloud_key_unavailable: env ${cloud.apiKeyEnv} is not set` };
  }
  if (cloud.apiKeyFile) {
    // SEC: realpath BOTH sides before the containment check (mirror of
    // engine/config.py os.path.realpath). path.resolve alone is lexical, so a
    // symlink under ~/.minni/secrets/ pointing at any 0600 file on disk would
    // pass containment and turn apiKeyFile into an exfiltration primitive.
    let resolved: string;
    try {
      resolved = realpathSync(path.resolve(expandTilde(cloud.apiKeyFile)));
    } catch {
      return { error: "cloud_key_unavailable: apiKeyFile is not readable" };
    }
    let root: string;
    try {
      root = realpathSync(path.resolve(secretsDir)) + path.sep;
    } catch {
      return { error: "cloud_key_denied: apiKeyFile must live under ~/.minni/secrets/" };
    }
    if (!resolved.startsWith(root)) {
      return { error: "cloud_key_denied: apiKeyFile must live under ~/.minni/secrets/" };
    }
    try {
      const st = statSync(resolved);
      if (!st.isFile()) {
        return { error: "cloud_key_denied: apiKeyFile must be a regular file" };
      }
      if ((st.mode & 0o077) !== 0) {
        return { error: "cloud_key_denied: apiKeyFile must be mode 0600 (no group/other access)" };
      }
      const key = readFileSync(resolved, "utf8").trim();
      return key ? { key } : { error: "cloud_key_unavailable: apiKeyFile is empty" };
    } catch {
      return { error: "cloud_key_unavailable: apiKeyFile is not readable" };
    }
  }
  return { error: "cloud_key_unavailable: cloud provider enabled without apiKeyEnv or apiKeyFile" };
}

export type AFM_PROVIDER_MODE = "auto" | "bridge" | "native" | "off";

function parseAfmProviderMode(value: string | undefined): AFM_PROVIDER_MODE {
  return value === "auto" || value === "native" || value === "off" ? value : "bridge";
}

export const AFM_PROVIDER_MODE =
  parseAfmProviderMode(process.env.MINNI_AFM_PROVIDER_MODE);

/**
 * Whether the AFM provider mode of THIS process was actually chosen by the
 * operator, as opposed to falling through to the "bridge" default (#195).
 *
 * The daemon carries MINNI_AFM_PROVIDER_MODE in its launchd plist, and
 * native_afm_env() in
 * plugins/minni/skills/minni-install/scripts/propagate.py injects the same
 * variable into installer-written MCP env blocks; a hook, MCP, or shell
 * process with neither source falls through to "bridge" as a default, not a
 * decision. Callers that compare their own mode against the daemon's use
 * this to tell the two apart. Read at call time (like
 * generationProbeTimeoutMs) so it stays testable and follows a mid-process
 * env change; an unrecognized value is not a decision either.
 */
export function afmProviderModeConfigured(): boolean {
  const raw = process.env.MINNI_AFM_PROVIDER_MODE ?? process.env.MINNI_AFM_MODE;
  if (!raw) return false;
  const value = raw.trim().toLowerCase();
  return value === "auto" || value === "bridge" || value === "native" || value === "off";
}

export const DEFAULT_AGENT_ID =
  process.env.MINNI_AGENT_ID ??
  process.env.MINNI_CODEX_AGENT_ID ??
  "unknown-agent";

// Derives from cwd when no env override is set. A hardcoded MINNI_WORKSPACE_ID
// in a runtime's MCP config is a static value that cannot follow the user
// between projects: it silently stamps every session with whatever workspace
// was current when that config was written.
export const DEFAULT_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_PINNED_WORKSPACE_ID ??
      process.env.MINNI_WORKSPACE_ID ??
      process.env.MINNI_CODEX_WORKSPACE_ID ??
      deriveWorkspaceIdFromCwd()
  );

// --- Codex agent defaults (hook-native identity) ---
// The Codex hook must never inherit the generic MCP identity (DEFAULT_*):
// those fall back to unknown-deny and, worse, share env with other runtimes.
// Codex hooks also often launch with cwd = plugin cache, so the workspace
// stamp fails closed to "workspace-unknown" instead of deriving from cwd.

export const CODEX_AGENT_ID = process.env.MINNI_CODEX_AGENT_ID ?? "codex";

export const CODEX_WORKSPACE_ID = normalizeWorkspaceId(
  process.env.MINNI_CODEX_WORKSPACE_ID ?? "workspace-unknown",
);

export const CODEX_VAULT_PATH = expandTilde(
  process.env.MINNI_CODEX_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "codex-vault"),
);

export const CODEX_HOOKS_ENABLED =
  (process.env.MINNI_CODEX_HOOKS ?? "on").toLowerCase() !== "off";

export const CODEX_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.CODEX_CONTEXT_WINDOW ?? process.env.MINNI_CODEX_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

export const CLAUDECODE_AGENT_ID =
  process.env.MINNI_CLAUDECODE_AGENT_ID ?? "claude-code";

export const CLAUDECODE_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_CLAUDECODE_WORKSPACE_ID ??
      deriveWorkspaceIdFromCwd()
  );

export const CLAUDECODE_VAULT_PATH = expandTilde(
  process.env.MINNI_CLAUDECODE_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "claudecode-vault"),
);

export const CLAUDECODE_HOOKS_ENABLED =
  (process.env.MINNI_CLAUDECODE_HOOKS ?? "on").toLowerCase() !== "off";

export const CLAUDECODE_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.CLAUDE_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

// --- KiloCode agent defaults ---

export const KILOCODE_AGENT_ID =
  process.env.MINNI_KILOCODE_AGENT_ID ?? "kilocode";

export const KILOCODE_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_KILOCODE_WORKSPACE_ID ??
      deriveWorkspaceIdFromCwd()
  );

export const KILOCODE_VAULT_PATH = expandTilde(
  process.env.MINNI_KILOCODE_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "kilocode-vault"),
);

export const KILOCODE_HOOKS_ENABLED =
  (process.env.MINNI_KILOCODE_HOOKS ?? "on").toLowerCase() !== "off";

export const KILOCODE_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.KILO_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

// --- Grok-build agent defaults ---

export const GROK_AGENT_ID =
  process.env.MINNI_GROK_AGENT_ID ?? "grok-build";

export const GROK_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_GROK_WORKSPACE_ID ??
      deriveWorkspaceIdFromCwd()
  );

export const GROK_VAULT_PATH = expandTilde(
  process.env.MINNI_GROK_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "grok-build-vault"),
);

export const GROK_HOOKS_ENABLED =
  (process.env.MINNI_GROK_HOOKS ?? "on").toLowerCase() !== "off";

export const GROK_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.GROK_CONTEXT_WINDOW ?? process.env.MINNI_GROK_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 256_000;
})();

// --- Cursor agent defaults ---
// Cursor launches plugin hooks outside the MCP server environment, so this
// identity stays independent of both the generic MCP variables and Claude Code.
// Workspace is fail-closed ("workspace-unknown") rather than cwd-derived, to
// match the Codex adapter: an unstamped hook process must not silently claim a
// workspace it cannot prove.

export const CURSOR_AGENT_ID =
  process.env.MINNI_CURSOR_AGENT_ID ?? "cursor";

export const CURSOR_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_CURSOR_WORKSPACE_ID ?? "workspace-unknown"
  );

export const CURSOR_VAULT_PATH = expandTilde(
  process.env.MINNI_CURSOR_VAULT_PATH ??
    path.join(os.homedir(), ".minni", "cursor-vault"),
);

export const CURSOR_HOOKS_ENABLED =
  (process.env.MINNI_CURSOR_HOOKS ?? "on").toLowerCase() !== "off";

export const CURSOR_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.CURSOR_CONTEXT_WINDOW ?? process.env.MINNI_CURSOR_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 200_000;
})();

// --- Gemini / Antigravity agent defaults ---
// One agent identity ("gemini") across the Antigravity family: the agy CLI,
// Antigravity 2.0, and the Antigravity IDE all share the ~/.gemini tree and
// the same vault. The hook entrypoint (gemini-hook.ts) currently only fires
// on the agy CLI, whose plugin system dispatches PreToolUse/PostToolUse/Stop.

export const GEMINI_AGENT_ID =
  process.env.MINNI_GEMINI_AGENT_ID ?? "gemini";

export const GEMINI_WORKSPACE_ID =
  normalizeWorkspaceId(
    process.env.MINNI_GEMINI_WORKSPACE_ID ??
      deriveWorkspaceIdFromCwd()
  );

// Codex review (PR #134): mirror propagate.vault_for("gemini")'s legacy
// fallback. Older installs may hold memory only at ~/.gemini/minni-vault;
// propagation keeps using that vault so prior memory is not stranded, and the
// agy hook (launched from hooks.json OUTSIDE the MCP env, so no stamped
// MINNI_GEMINI_VAULT_PATH) must land on the same vault — not a fresh empty
// canonical one. Legacy wins only when the canonical vault is missing AND the
// legacy one exists with content, exactly like propagate.py.
function geminiVaultDefault(): string {
  const canonical = path.join(os.homedir(), ".minni", "gemini-vault");
  const legacy = path.join(os.homedir(), ".gemini", "minni-vault");
  try {
    if (!existsSync(canonical) && existsSync(legacy) && readdirSync(legacy).length > 0) {
      return legacy;
    }
  } catch {
    // unreadable legacy path -> canonical
  }
  return canonical;
}

export const GEMINI_VAULT_PATH = expandTilde(
  process.env.MINNI_GEMINI_VAULT_PATH ?? geminiVaultDefault(),
);

export const GEMINI_HOOKS_ENABLED =
  (process.env.MINNI_GEMINI_HOOKS ?? "on").toLowerCase() !== "off";

export const GEMINI_CONTEXT_WINDOW = (() => {
  const raw = Number(process.env.GEMINI_CONTEXT_WINDOW ?? process.env.MINNI_GEMINI_CONTEXT_WINDOW);
  return Number.isFinite(raw) && raw > 0 ? raw : 1_000_000;
})();
