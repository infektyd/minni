import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_VAULT_PATH, PLUGIN_ROOT } from "./config.js";
import { buildStatusReport } from "./sovereign.js";
import { prepareOutcome, prepareTask, type PrepareOutcomeInput, type PrepareTaskInput } from "./task.js";
import { auditTail } from "./vault.js";

const MAX_JSON_BYTES = 256 * 1024;
const DEFAULT_UI_HOST = process.env.SOVEREIGN_UI_HOST ?? "127.0.0.1";
const DEFAULT_UI_PORT = Number(process.env.SOVEREIGN_UI_PORT ?? "8765");

export interface UiServerOptions {
  host?: string;
  port?: number;
  staticRoot?: string;
  vaultPath?: string;
  prepareTask?: (input: PrepareTaskInput) => Promise<unknown>;
  prepareOutcome?: (input: PrepareOutcomeInput) => Promise<unknown>;
  status?: () => Promise<unknown>;
  auditTail?: (limit: number) => Promise<unknown>;
}

export interface UiServerHandle {
  host: string;
  port: number;
  server: Server;
  start: () => Promise<void>;
  close: () => Promise<void>;
}

function sendJson(res: ServerResponse, statusCode: number, body: unknown): void {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
    "Cache-Control": "no-store",
  });
  res.end(payload);
}

function sendText(res: ServerResponse, statusCode: number, body: string): void {
  res.writeHead(statusCode, {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function localHostAllowed(hostHeader: string | undefined): boolean {
  if (!hostHeader) return true;
  const host = hostHeader.startsWith("[")
    ? hostHeader.slice(1, hostHeader.indexOf("]"))
    : hostHeader.split(":")[0].toLowerCase();
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

function localBindHostAllowed(host: string): boolean {
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

function localOriginAllowed(origin: string | undefined): boolean {
  if (!origin) return true;
  try {
    return localBindHostAllowed(new URL(origin).hostname);
  } catch {
    return false;
  }
}

function fetchSiteAllowed(value: string | undefined): boolean {
  return !value || value === "same-origin" || value === "same-site" || value === "none";
}

function jsonContentTypeAllowed(value: string | undefined): boolean {
  return value?.toLowerCase().split(";")[0].trim() === "application/json";
}

function parseAuditLimit(value: string | null): number {
  const parsed = Number(value ?? 20);
  if (!Number.isFinite(parsed)) return 20;
  return Math.max(1, Math.min(Math.trunc(parsed), 100));
}

function contentTypeFor(filePath: string): string {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (filePath.endsWith(".svg")) return "image/svg+xml";
  if (filePath.endsWith(".png")) return "image/png";
  return "application/octet-stream";
}

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buffer.length;
    if (total > MAX_JSON_BYTES) throw new Error("Request body too large.");
    chunks.push(buffer);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) return {};
  const parsed = JSON.parse(raw) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("JSON body must be an object.");
  return parsed as Record<string, unknown>;
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is string => typeof item === "string");
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function boolValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function redactLocalValue(value: unknown): unknown {
  if (typeof value === "string") {
    return value
      .replace(/\/Users\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/Volumes\/[^\s"',)]+/g, "[local-path]")
      .replace(/[^\s"',)]+\.fmadapter\b/g, "[local-path]");
  }
  if (Array.isArray(value)) return value.map(redactLocalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactLocalValue(item)]));
  }
  return value;
}

function prepareTaskInput(body: Record<string, unknown>, vaultPath: string): PrepareTaskInput {
  const task = stringValue(body.task);
  if (!task?.trim()) throw new Error("prepare-task requires task.");
  return {
    task,
    profile: body.profile === "compact" || body.profile === "standard" || body.profile === "deep" ? body.profile : undefined,
    budgetTokens: numberValue(body.budgetTokens),
    useAfm: boolValue(body.useAfm),
    layer:
      body.layer === "identity" || body.layer === "episodic" || body.layer === "knowledge" || body.layer === "artifact"
        ? body.layer
        : undefined,
    limit: numberValue(body.limit),
    workspaceId: stringValue(body.workspaceId),
    agentId: stringValue(body.agentId),
    vaultPath,
    includeVault: boolValue(body.includeVault),
  };
}

function prepareOutcomeInput(body: Record<string, unknown>, vaultPath: string): PrepareOutcomeInput {
  const task = stringValue(body.task);
  const summary = stringValue(body.summary);
  if (!task?.trim()) throw new Error("prepare-outcome requires task.");
  if (!summary?.trim()) throw new Error("prepare-outcome requires summary.");
  return {
    task,
    summary,
    changedFiles: stringArray(body.changedFiles),
    verification: stringArray(body.verification),
    profile: body.profile === "compact" || body.profile === "standard" || body.profile === "deep" ? body.profile : undefined,
    useAfm: boolValue(body.useAfm),
    vaultPath,
  };
}

async function serveStatic(
  staticRoot: string,
  reqPath: string,
  method: string | undefined,
  res: ServerResponse,
): Promise<void> {
  const relative = reqPath === "/" ? "index.html" : decodeURIComponent(reqPath.replace(/^\/+/, ""));
  const fullPath = path.resolve(staticRoot, relative);
  const root = path.resolve(staticRoot);
  if (fullPath !== root && !fullPath.startsWith(`${root}${path.sep}`)) {
    sendText(res, 403, "Forbidden");
    return;
  }
  try {
    const info = await stat(fullPath);
    const filePath = info.isDirectory() ? path.join(fullPath, "index.html") : fullPath;
    const fileInfo = info.isDirectory() ? await stat(filePath) : info;
    if (method === "HEAD") {
      res.writeHead(200, {
        "Content-Type": contentTypeFor(filePath),
        "Content-Length": fileInfo.size,
        "Cache-Control": "no-store",
      });
      res.end();
      return;
    }
    const body = await readFile(filePath);
    res.writeHead(200, {
      "Content-Type": contentTypeFor(filePath),
      "Content-Length": body.length,
      "Cache-Control": "no-store",
    });
    res.end(body);
  } catch {
    sendText(res, 404, "Not found");
  }
}

export function createUiServer(options: UiServerOptions = {}): UiServerHandle {
  const host = options.host ?? DEFAULT_UI_HOST;
  const port = options.port ?? DEFAULT_UI_PORT;
  if (!localBindHostAllowed(host)) {
    throw new Error(`Sovereign Memory console requires a local bind host, got: ${host}`);
  }
  const staticRoot = options.staticRoot ?? path.join(PLUGIN_ROOT, "frontend");
  const vaultPath = options.vaultPath ?? DEFAULT_VAULT_PATH;
  const status = options.status ?? (() => buildStatusReport({ vaultPath }));
  const tail = options.auditTail ?? ((limit: number) => auditTail(vaultPath, limit));
  const taskPrep = options.prepareTask ?? ((input: PrepareTaskInput) => prepareTask(input));
  const outcomePrep = options.prepareOutcome ?? ((input: PrepareOutcomeInput) => prepareOutcome(input));

  const server = createServer(async (req, res) => {
    try {
      if (!localHostAllowed(req.headers.host)) {
        sendText(res, 403, "Sovereign Memory console only accepts local host requests.");
        return;
      }
      if (!localOriginAllowed(req.headers.origin) || !fetchSiteAllowed(req.headers["sec-fetch-site"])) {
        sendText(res, 403, "Sovereign Memory console only accepts same-origin local requests.");
        return;
      }

      const url = new URL(req.url ?? "/", `http://${req.headers.host ?? `${host}:${port}`}`);
      if (req.method === "GET" && url.pathname === "/api/health") {
        sendJson(res, 200, {
          ok: true,
          host,
          port,
          tools: ["sovereign_prepare_task", "sovereign_prepare_outcome", "sovereign_status", "sovereign_audit_tail"],
          automaticLearning: false,
        });
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/status") {
        sendJson(res, 200, redactLocalValue(await status()));
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/audit-tail") {
        sendJson(res, 200, redactLocalValue(await tail(parseAuditLimit(url.searchParams.get("limit")))));
        return;
      }
      if (req.method === "POST" && url.pathname === "/api/prepare-task") {
        if (!jsonContentTypeAllowed(req.headers["content-type"])) {
          sendText(res, 415, "POST requests must use application/json.");
          return;
        }
        const body = await readJsonBody(req);
        sendJson(res, 200, await taskPrep(prepareTaskInput(body, vaultPath)));
        return;
      }
      if (req.method === "POST" && url.pathname === "/api/prepare-outcome") {
        if (!jsonContentTypeAllowed(req.headers["content-type"])) {
          sendText(res, 415, "POST requests must use application/json.");
          return;
        }
        const body = await readJsonBody(req);
        sendJson(res, 200, await outcomePrep(prepareOutcomeInput(body, vaultPath)));
        return;
      }
      // G17: Candidates console API (minimal wiring for P1 approval pipeline)
      if (req.method === "GET" && url.pathname === "/api/candidates") {
        try {
          const { jsonRpcSocketRequestWithFallback } = await import("./sovereign.js");
          const limit = parseInt(url.searchParams.get("limit") || "50", 10);
          const status = url.searchParams.get("status") || undefined;
          const rpc = await jsonRpcSocketRequestWithFallback("list_candidates", {
            limit: Math.min(limit, 200),
            status,
          });
          sendJson(res, 200, rpc);
        } catch (e) {
          sendJson(res, 200, { candidates: [], error: String(e) });
        }
        return;
      }
      if (req.method === "POST" && url.pathname === "/api/resolve-candidate") {
        if (!jsonContentTypeAllowed(req.headers["content-type"])) {
          sendText(res, 415, "POST requests must use application/json.");
          return;
        }
        const body = await readJsonBody(req);
        try {
          const { jsonRpcSocketRequestWithFallback } = await import("./sovereign.js");
          const rpc = await jsonRpcSocketRequestWithFallback("resolve_candidate", {
            candidate_id: body.candidate_id,
            decision: body.decision,
            reason: body.reason || body.resolution_reason || "",
          });
          sendJson(res, 200, rpc);
        } catch (e) {
          sendJson(res, 200, { status: "error", error: String(e) });
        }
        return;
      }
      if (url.pathname.startsWith("/api/")) {
        sendText(res, 404, "Unknown API route.");
        return;
      }
      if (req.method !== "GET" && req.method !== "HEAD") {
        sendText(res, 405, "Method not allowed.");
        return;
      }
      await serveStatic(staticRoot, url.pathname, req.method, res);
    } catch (error) {
      sendJson(res, 400, { error: error instanceof Error ? error.message : String(error) });
    }
  });

  return {
    host,
    port,
    server,
    start: () =>
      new Promise<void>((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, () => {
          server.off("error", reject);
          resolve();
        });
      }),
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}

async function main(): Promise<void> {
  const app = createUiServer();
  await app.start();
  console.log(`Sovereign Memory console: http://${app.host}:${app.port}/`);
}

const entry = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (entry === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
