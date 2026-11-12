import { execFileSync } from "node:child_process";
import { existsSync, lstatSync, readFileSync } from "node:fs";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

import {
  deriveWorkspaceIdFromCwd,
  minniHome as resolveDefaultMinniHome,
  normalizeWorkspaceId,
} from "./config.js";
import { sessionStateKey } from "./recall-state.js";

export const PROJECT_SHELF_MAX_CHARS = 2500;
const GIT_STALENESS_TIMEOUT_MS = 300;
const SCAR_PRIORITY_CATEGORIES: Record<string, number> = {
  scar: 1,
  gotcha: 1,
  correction: 2,
  invariant: 2,
  rule: 2,
  architecture: 3,
  gate: 3,
  security: 3,
  decision: 4,
};

interface RepoMapSubsystemEntry {
  slug?: string;
  title?: string;
  summary_line?: string;
  card_rel_path?: string;
  file_count?: number;
  owned_files?: string[];
  dir_prefixes?: string[];
}

interface RepoMapIndexFile {
  workspace_id?: string;
  workspace_slug?: string;
  repo_root?: string;
  git_sha?: string;
  gate_commands?: string[];
  subsystems?: RepoMapSubsystemEntry[];
}

export interface BuildProjectShelfOptions {
  minniHome?: string;
  vaultPath?: string;
  workspaceId?: string;
  cwd?: string;
  activePlan?: unknown;
  maxChars?: number;
}

function resolveCanonicalProjectWorkspace(workspaceId?: string, cwd?: string): string {
  const candidates = [workspaceId, cwd ? deriveWorkspaceIdFromCwd(cwd) : undefined];
  for (const raw of candidates) {
    if (!raw || typeof raw !== "string") continue;
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const lower = trimmed.toLowerCase();
    if (
      lower === "default" ||
      lower === "workspace-default" ||
      lower === "global" ||
      lower === "workspace-global" ||
      lower === "*" ||
      lower === "workspace-unknown"
    ) {
      continue;
    }
    const norm = normalizeWorkspaceId(trimmed);
    if (norm && norm !== "workspace-unknown" && norm !== "workspace-default" && norm !== "workspace-global") {
      return norm;
    }
  }
  return "";
}

export function workspaceSlugFromWorkspaceId(workspaceId: string): string {
  const stripped = workspaceId.trim().toLowerCase().replace(/^workspace-/, "");
  const slug = stripped.replace(/[^a-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return slug || "repo";
}

function effectiveMinniHomeForShelf(explicitHome?: string, vaultPath?: string): string {
  if (explicitHome && explicitHome.trim()) {
    return path.resolve(explicitHome.trim());
  }
  if (process.env.MINNI_HOME && process.env.MINNI_HOME.trim()) {
    return path.resolve(resolveDefaultMinniHome());
  }
  if (vaultPath && vaultPath.trim()) {
    return path.dirname(path.resolve(vaultPath.trim()));
  }
  return path.resolve(resolveDefaultMinniHome());
}

function isSafeNonSymlinkPath(homeDir: string, targetPath: string): boolean {
  try {
    const vaultDir = path.join(homeDir, "vault");
    const mapsDir = path.join(vaultDir, "maps");
    if (existsSync(vaultDir) && lstatSync(vaultDir).isSymbolicLink()) return false;
    if (existsSync(mapsDir) && lstatSync(mapsDir).isSymbolicLink()) return false;
    const subDir = path.dirname(targetPath);
    if (existsSync(subDir) && lstatSync(subDir).isSymbolicLink()) return false;
    if (existsSync(targetPath) && lstatSync(targetPath).isSymbolicLink()) return false;
    return true;
  } catch {
    return false;
  }
}

function readRepoMapIndex(homeDir: string, workspaceSlug: string): RepoMapIndexFile | null {
  const indexPath = path.join(homeDir, "vault", "maps", workspaceSlug, "_index.json");
  if (!existsSync(indexPath) || !isSafeNonSymlinkPath(homeDir, indexPath)) {
    return null;
  }
  try {
    const raw = readFileSync(indexPath, "utf8");
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as RepoMapIndexFile;
  } catch {
    return null;
  }
}

const GIT_HEX_SHA_RE = /^[0-9a-fA-F]{7,64}$/;

function runFastGit(repoRoot: string, args: string[]): string | null {
  if (!repoRoot || repoRoot.startsWith("-") || !path.isAbsolute(repoRoot)) {
    return null;
  }
  try {
    const out = execFileSync("git", ["-C", repoRoot, ...args], {
      encoding: "utf8",
      timeout: GIT_STALENESS_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
      env: { ...process.env, GIT_OPTIONAL_LOCKS: "0" },
    });
    return typeof out === "string" ? out.trim() : null;
  } catch {
    return null;
  }
}

function computeChangedFilesBySubsystem(
  indexData: RepoMapIndexFile,
): { builtSha7: string; bySlug: Map<string, string[]> } {
  const bySlug = new Map<string, string[]>();
  const repoRoot = typeof indexData.repo_root === "string" ? indexData.repo_root.trim() : "";
  const builtSha = typeof indexData.git_sha === "string" ? indexData.git_sha.trim() : "";
  if (
    !repoRoot ||
    repoRoot.startsWith("-") ||
    !path.isAbsolute(repoRoot) ||
    !GIT_HEX_SHA_RE.test(builtSha) ||
    !existsSync(path.join(repoRoot, ".git"))
  ) {
    return { builtSha7: "", bySlug };
  }
  const headSha = runFastGit(repoRoot, ["rev-parse", "HEAD"]);
  if (!headSha || !GIT_HEX_SHA_RE.test(headSha) || headSha === builtSha) {
    return { builtSha7: builtSha.slice(0, 7), bySlug };
  }
  const diffOut = runFastGit(repoRoot, ["diff", "--name-only", `${builtSha}..${headSha}`, "--"]);
  const changedFiles =
    diffOut !== null
      ? diffOut
          .split(/\r?\n/)
          .map((line) => line.trim().replace(/\\/g, "/"))
          .filter((line) => line.length > 0 && !line.startsWith("_private/"))
      : [];
  if (changedFiles.length === 0) {
    return { builtSha7: builtSha.slice(0, 7), bySlug };
  }

  const subsystems = Array.isArray(indexData.subsystems) ? indexData.subsystems : [];
  for (const sub of subsystems) {
    const slug = typeof sub.slug === "string" ? sub.slug : "";
    if (!slug) continue;
    const owned = new Set(Array.isArray(sub.owned_files) ? sub.owned_files : []);
    const prefixes = Array.isArray(sub.dir_prefixes) ? sub.dir_prefixes : [];
    const matched: string[] = [];
    for (const file of changedFiles) {
      const stem = path.posix.basename(file, path.posix.extname(file)).toLowerCase();
      if (owned.has(file) || prefixes.some((p) => file.startsWith(p)) || stem === slug) {
        matched.push(file);
      }
    }
    if (matched.length > 0) {
      bySlug.set(slug, matched);
    }
  }
  return { builtSha7: builtSha.slice(0, 7), bySlug };
}

export function computeRepoMapCardStalenessNote(cardPath: string): string | null {
  if (!cardPath || !cardPath.includes(`${path.sep}maps${path.sep}`) && !cardPath.includes("/maps/")) {
    return null;
  }
  try {
    const resolvedCard = path.resolve(cardPath);
    const wsDir = path.dirname(resolvedCard);
    const indexPath = path.join(wsDir, "_index.json");
    if (!existsSync(indexPath) || lstatSync(indexPath).isSymbolicLink()) return null;
    const raw = readFileSync(indexPath, "utf8");
    const indexData = JSON.parse(raw) as RepoMapIndexFile;
    if (!indexData || typeof indexData !== "object") return null;
    const slug = path.basename(resolvedCard, path.extname(resolvedCard));
    const { builtSha7, bySlug } = computeChangedFilesBySubsystem(indexData);
    if (!builtSha7) return null;
    if (slug === "overview") {
      const allChanged = new Set<string>();
      for (const files of bySlug.values()) {
        for (const f of files) allChanged.add(f);
      }
      if (allChanged.size === 0) return null;
      const arr = [...allChanged];
      const shown = arr.slice(0, 8).join(", ") + (arr.length > 8 ? ` (+${arr.length - 8} more)` : "");
      return `[Note: Map built at ${builtSha7}; changed since then: ${shown}]`;
    }
    const matched = bySlug.get(slug);
    if (!matched || matched.length === 0) return null;
    const shown =
      matched.slice(0, 8).join(", ") + (matched.length > 8 ? ` (+${matched.length - 8} more)` : "");
    return `[Note: Map built at ${builtSha7}; changed since then: ${shown}]`;
  } catch {
    return null;
  }
}

async function queryTopWorkspaceScars(
  homeDir: string,
  workspaceId: string,
  limit = 5,
): Promise<Array<{ category: string; text: string }>> {
  const dbPath =
    process.env.MINNI_DB_PATH && process.env.MINNI_DB_PATH.trim()
      ? path.resolve(process.env.MINNI_DB_PATH.trim())
      : path.join(homeDir, "minni.db");
  if (!existsSync(dbPath)) return [];

  try {
    const sqliteMod = (await import("node:sqlite")) as {
      DatabaseSync?: new (p: string, opts?: { readOnly?: boolean }) => {
        prepare(sql: string): { all(...args: unknown[]): Array<Record<string, unknown>> };
        close(): void;
      };
    };
    if (!sqliteMod.DatabaseSync) return [];
    const db = new sqliteMod.DatabaseSync(dbPath, { readOnly: true });
    try {
      const tables = new Set(
        db
          .prepare("SELECT name FROM sqlite_master WHERE type='table'")
          .all()
          .map((r) => String(r.name ?? "")),
      );
      if (!tables.has("learnings")) return [];
      const cols = new Set(
        db
          .prepare("PRAGMA table_info(learnings)")
          .all()
          .map((r) => String(r.name ?? "")),
      );
      if (!cols.has("workspace_id") || !cols.has("content")) return [];

      const idCol = cols.has("learning_id") ? "learning_id" : cols.has("id") ? "id" : "rowid";
      const catCol = cols.has("category") ? "l.category" : "'general' AS category";
      const confCol = cols.has("confidence") ? "COALESCE(l.confidence, 0.5) AS confidence" : "0.5 AS confidence";
      const whereParts = ["l.workspace_id = ?"];
      if (cols.has("superseded_by")) {
        whereParts.push("l.superseded_by IS NULL");
      }
      if (cols.has("status")) {
        whereParts.push("LOWER(TRIM(COALESCE(l.status, 'active'))) IN ('active', 'accepted')");
      }
      if (cols.has("expires_at")) {
        whereParts.push(
          "(l.expires_at IS NULL OR (typeof(l.expires_at) IN ('integer', 'real') AND l.expires_at > CAST(strftime('%s', 'now') AS REAL)) OR (typeof(l.expires_at) = 'text' AND (TRIM(l.expires_at) = '' OR datetime(l.expires_at) > datetime('now'))))",
        );
      }
      if (tables.has("documents")) {
        const docCols = new Set(
          db
            .prepare("PRAGMA table_info(documents)")
            .all()
            .map((r) => String(r.name ?? "")),
        );
        const badDocDisjuncts: string[] = [];
        if (docCols.has("privacy_level")) {
          badDocDisjuncts.push(
            "LOWER(TRIM(COALESCE(d.privacy_level, 'safe'))) IN ('blocked', 'private', 'local-only', 'secret')",
          );
        }
        if (docCols.has("page_status")) {
          badDocDisjuncts.push(
            "LOWER(TRIM(COALESCE(d.page_status, 'accepted'))) IN ('rejected', 'superseded', 'expired', 'archived', 'retired', 'deleted')",
          );
        }
        if (docCols.has("superseded_by")) {
          badDocDisjuncts.push("d.superseded_by IS NOT NULL");
        }
        if (docCols.has("expires_at")) {
          badDocDisjuncts.push(
            "(d.expires_at IS NOT NULL AND ((typeof(d.expires_at) IN ('integer', 'real') AND d.expires_at <= CAST(strftime('%s', 'now') AS REAL)) OR (typeof(d.expires_at) = 'text' AND TRIM(d.expires_at) != '' AND datetime(d.expires_at) IS NOT NULL AND datetime(d.expires_at) <= datetime('now'))))",
          );
        }
        if (badDocDisjuncts.length > 0) {
          const badDocCond = badDocDisjuncts.join(" OR ");
          if (tables.has("learning_documents") && docCols.has("doc_id")) {
            whereParts.push(
              `NOT EXISTS (SELECT 1 FROM learning_documents ld JOIN documents d ON d.doc_id = ld.doc_id WHERE ld.learning_id = l.${idCol} AND (${badDocCond}))`,
            );
          }
          if (docCols.has("memory_uri")) {
            whereParts.push(
              `NOT EXISTS (SELECT 1 FROM documents d WHERE d.memory_uri = ('learning://' || l.${idCol}) AND (${badDocCond}))`,
            );
          }
        }
      } else if (cols.has("privacy_level")) {
        whereParts.push(
          "LOWER(TRIM(COALESCE(l.privacy_level, 'safe'))) NOT IN ('blocked', 'private', 'local-only', 'secret')",
        );
      }
      const sql = `SELECT l.${idCol} AS lid, ${catCol}, ${confCol}, l.content FROM learnings l WHERE ${whereParts.join(" AND ")} ORDER BY l.${idCol} DESC LIMIT 60`;
      const rows = db.prepare(sql).all(workspaceId);
      const scored = rows
        .map((r) => {
          const cat = String(r.category ?? "general").trim().toLowerCase() || "general";
          const rawText = String(r.content ?? "").replace(/\s+/g, " ").trim();
          const conf = typeof r.confidence === "number" ? r.confidence : Number(r.confidence ?? 0.5);
          const lid = typeof r.lid === "number" ? r.lid : Number(r.lid ?? 0);
          const rank = SCAR_PRIORITY_CATEGORIES[cat] ?? 9;
          return { cat, rawText, conf: Number.isFinite(conf) ? conf : 0.5, lid, rank };
        })
        .filter((item) => item.rawText.length > 0);

      scored.sort((a, b) => {
        if (a.rank !== b.rank) return a.rank - b.rank;
        if (b.conf !== a.conf) return b.conf - a.conf;
        return b.lid - a.lid;
      });

      return scored.slice(0, limit).map((item) => ({
        category: item.cat,
        text: item.rawText.length > 150 ? `${item.rawText.slice(0, 147)}...` : item.rawText,
      }));
    } finally {
      db.close();
    }
  } catch {
    return [];
  }
}

function formatActivePlanOneLiner(activePlan: unknown): string | null {
  if (!activePlan || typeof activePlan !== "object" || Array.isArray(activePlan)) {
    return null;
  }
  const obj = activePlan as Record<string, unknown>;
  const view =
    obj.view && typeof obj.view === "object" && !Array.isArray(obj.view)
      ? (obj.view as Record<string, unknown>)
      : obj;
  const planId = typeof obj.plan_id === "string" ? obj.plan_id : "";
  const headline =
    (typeof view.headline === "string" && view.headline.trim()) ||
    (typeof view.goal === "string" && view.goal.trim()) ||
    (typeof obj.headline === "string" && obj.headline.trim()) ||
    (typeof obj.plan_goal === "string" && obj.plan_goal.trim()) ||
    "";
  const nextAction =
    (typeof view.next_action === "string" && view.next_action.trim()) ||
    (typeof obj.next_action === "string" && obj.next_action.trim()) ||
    "";
  if (!headline && !nextAction) return null;
  const label = planId ? `\`${planId}\`: ` : "";
  const body = [headline, nextAction ? `next: ${nextAction}` : ""].filter(Boolean).join(" — ");
  const clipped = body.length > 180 ? `${body.slice(0, 177)}...` : body;
  return `- ${label}${clipped}`;
}

export async function buildProjectPermanentShelf(
  options: BuildProjectShelfOptions,
): Promise<string | undefined> {
  const maxChars = options.maxChars ?? PROJECT_SHELF_MAX_CHARS;
  const canonicalWs = resolveCanonicalProjectWorkspace(options.workspaceId, options.cwd);
  if (!canonicalWs) return undefined;

  const homeDir = effectiveMinniHomeForShelf(options.minniHome, options.vaultPath);
  const wsSlug = workspaceSlugFromWorkspaceId(canonicalWs);
  const indexData = readRepoMapIndex(homeDir, wsSlug);
  const subsystems = Array.isArray(indexData?.subsystems) ? indexData!.subsystems! : [];
  const scars = await queryTopWorkspaceScars(homeDir, canonicalWs, 5);
  const planLine =
    subsystems.length > 0 || scars.length > 0 ? formatActivePlanOneLiner(options.activePlan) : null;

  if (subsystems.length === 0 && scars.length === 0 && !planLine) {
    return undefined;
  }

  const lines: string[] = [`### Project Shelf (${canonicalWs})`];

  if (subsystems.length > 0 && indexData) {
    const { builtSha7, bySlug } = computeChangedFilesBySubsystem(indexData);
    lines.push(
      "**Subsystem Maps** (call `minni_recall` or `minni_drill` on a map card before broad repo scans):",
    );
    for (const sub of subsystems.slice(0, 18)) {
      const slug = typeof sub.slug === "string" ? sub.slug : "module";
      const relPath =
        typeof sub.card_rel_path === "string" && sub.card_rel_path
          ? sub.card_rel_path
          : `maps/${wsSlug}/${slug}.md`;
      const summary =
        typeof sub.summary_line === "string" && sub.summary_line.trim()
          ? sub.summary_line.trim()
          : typeof sub.title === "string"
            ? sub.title
            : slug;
      const shortSummary = summary.length > 88 ? `${summary.slice(0, 85)}...` : summary;
      const changed = bySlug.get(slug);
      const staleTag =
        changed && changed.length > 0 && builtSha7
          ? ` (changed since ${builtSha7}: ${changed.slice(0, 3).join(", ")}${changed.length > 3 ? `, +${changed.length - 3}` : ""})`
          : "";
      lines.push(`- \`${relPath}\` — ${shortSummary}${staleTag}`);
    }
    const gates = Array.isArray(indexData.gate_commands) ? indexData.gate_commands.slice(0, 4) : [];
    if (gates.length > 0) {
      lines.push(`**Gates**: \`${gates.join("` · `")}\``);
    }
  }

  if (scars.length > 0) {
    lines.push("**Repo Scars & Rules**:");
    for (const scar of scars) {
      lines.push(`- [${scar.category}] ${scar.text}`);
    }
  }

  if (planLine) {
    lines.push("**Active Plan**:");
    lines.push(planLine);
  }

  const bounded: string[] = [];
  let used = 0;
  for (const line of lines) {
    const addLen = (bounded.length > 0 ? 1 : 0) + line.length;
    if (used + addLen > maxChars) {
      const remaining = maxChars - used - 1;
      if (remaining > 32) {
        bounded.push(`${line.slice(0, remaining - 3)}...`);
      }
      break;
    }
    bounded.push(line);
    used += addLen;
  }

  return bounded.length > 1 ? bounded.join("\n") : undefined;
}

function postCompactMarkerPath(vaultPath: string, sessionId?: string): string {
  const key = sessionStateKey(sessionId && sessionId.trim() ? sessionId : "session") ?? "session";
  return path.join(vaultPath, ".runtime", "post-compact-reinject", `${key}.json`);
}

export async function writePostCompactReinjectMarker(
  vaultPath: string,
  sessionId: string,
  workspaceId?: string,
): Promise<void> {
  const target = postCompactMarkerPath(vaultPath, sessionId);
  const dir = path.dirname(target);
  await mkdir(dir, { recursive: true });
  const tmp = `${target}.${process.pid}.${Date.now()}.tmp`;
  const payload = JSON.stringify({
    session_id: sessionId || "session",
    workspace_id: workspaceId ?? "",
    created_at: Date.now(),
  });
  await writeFile(tmp, payload, "utf8");
  await rename(tmp, target);
}

export async function consumePostCompactReinjectMarker(
  vaultPath: string,
  sessionId?: string,
): Promise<{ workspaceId?: string; createdAt?: number } | null> {
  const candidates = [postCompactMarkerPath(vaultPath, sessionId)];
  if (!sessionId || !sessionId.trim()) {
    candidates.push(postCompactMarkerPath(vaultPath, "session"));
  }
  for (const target of candidates) {
    if (!existsSync(target)) continue;
    const claimPath = `${target}.claimed.${process.pid}.${Date.now()}`;
    try {
      await rename(target, claimPath);
    } catch {
      continue;
    }
    try {
      const raw = await readFile(claimPath, "utf8");
      await unlink(claimPath).catch(() => {});
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      return {
        workspaceId: typeof parsed.workspace_id === "string" ? parsed.workspace_id : undefined,
        createdAt: typeof parsed.created_at === "number" ? parsed.created_at : undefined,
      };
    } catch {
      await unlink(claimPath).catch(() => {});
      return {};
    }
  }
  return null;
}
