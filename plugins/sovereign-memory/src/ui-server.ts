import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { execFile } from "node:child_process";
import { access, readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { DEFAULT_VAULT_PATH, PLUGIN_ROOT } from "./config.js";
import { buildStatusReport } from "./sovereign.js";
import { prepareOutcome, prepareTask, type PrepareOutcomeInput, type PrepareTaskInput } from "./task.js";
import { auditTail } from "./vault.js";

const MAX_JSON_BYTES = 256 * 1024;
const DEFAULT_UI_HOST = process.env.SOVEREIGN_UI_HOST ?? "127.0.0.1";
const DEFAULT_UI_PORT = Number(process.env.SOVEREIGN_UI_PORT ?? "8765");
const DEFAULT_DEEP_RESEARCH_ROOT = process.env.DEEP_RESEARCH_AGENT_ROOT ?? "/Users/hansaxelsson/deep-research-agent";
const DEFAULT_DEEP_RESEARCH_CLI =
  process.env.DEEP_RESEARCH_CLI ?? path.join(DEFAULT_DEEP_RESEARCH_ROOT, ".venv", "bin", "deep-research");
const DEFAULT_DEEP_RESEARCH_PYTHON =
  process.env.DEEP_RESEARCH_PYTHON ?? path.join(DEFAULT_DEEP_RESEARCH_ROOT, ".venv", "bin", "python");
const execFileAsync = promisify(execFile);

type ResearchMode = "web" | "local-docs" | "hybrid";
type ResearchTool = "google_search" | "url_context" | "code_execution";

interface DeepResearchRunSummary {
  run_id: string;
  created_at?: string;
  updated_at?: string;
  prompt?: string;
  mode?: string;
  interaction_id?: string | null;
  status?: string;
  has_result?: boolean;
  has_report?: boolean;
  has_events?: boolean;
}

interface DeepResearchBridge {
  paths: () => Promise<unknown>;
  listRuns: () => Promise<unknown>;
  getRun: (runId: string) => Promise<unknown>;
  localDocsManifest: () => Promise<unknown>;
  listFileStores: () => Promise<unknown>;
  createFileStore: (displayName?: string) => Promise<unknown>;
  deleteFileStore: (name: string) => Promise<unknown>;
  plan: (body: Record<string, unknown>) => Promise<unknown>;
  refinePlan: (body: Record<string, unknown>) => Promise<unknown>;
  approvePlan: (body: Record<string, unknown>) => Promise<unknown>;
  run: (body: Record<string, unknown>) => Promise<unknown>;
  status: (body: Record<string, unknown>) => Promise<unknown>;
}

export interface UiServerOptions {
  host?: string;
  port?: number;
  staticRoot?: string;
  vaultPath?: string;
  prepareTask?: (input: PrepareTaskInput) => Promise<unknown>;
  prepareOutcome?: (input: PrepareOutcomeInput) => Promise<unknown>;
  status?: () => Promise<unknown>;
  auditTail?: (limit: number) => Promise<unknown>;
  deepResearch?: DeepResearchBridge;
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

function researchModeValue(value: unknown): ResearchMode {
  return value === "local-docs" || value === "hybrid" || value === "web" ? value : "web";
}

function researchToolArray(value: unknown): ResearchTool[] | undefined {
  const values = stringArray(value);
  if (!values) return undefined;
  return values.filter((item): item is ResearchTool =>
    item === "google_search" || item === "url_context" || item === "code_execution",
  );
}

function appendListArgs(args: string[], flag: string, values: string[] | undefined): void {
  for (const value of values ?? []) {
    if (value.trim()) args.push(flag, value);
  }
}

function redactLocalValue(value: unknown): unknown {
  if (typeof value === "string") {
    return value
      .replace(/\/Users\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/Volumes\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/tmp\/sov(?:ereign|rd)\.sock\b/g, "[local-path]")
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

function stripAnsi(value: string): string {
  return value.replace(/\u001b\[[0-9;]*m/g, "");
}

function parseJsonOutput(stdout: string): unknown {
  const text = stripAnsi(stdout).trim();
  try {
    return JSON.parse(text);
  } catch {
    const firstObject = text.indexOf("{");
    const firstArray = text.indexOf("[");
    const starts = [firstObject, firstArray].filter((item) => item >= 0);
    const start = starts.length ? Math.min(...starts) : -1;
    const end = Math.max(text.lastIndexOf("}"), text.lastIndexOf("]"));
    if (start >= 0 && end > start) return JSON.parse(text.slice(start, end + 1));
    throw new Error("Deep Research command did not return JSON.");
  }
}

async function runDeepResearchCli(args: string[]): Promise<unknown> {
  const { stdout } = await execFileAsync(DEFAULT_DEEP_RESEARCH_CLI, args, {
    cwd: DEFAULT_DEEP_RESEARCH_ROOT,
    maxBuffer: 20 * 1024 * 1024,
    timeout: 10 * 60 * 1000,
  });
  return parseJsonOutput(stdout);
}

const PLAN_FOLLOWUP_SCRIPT = `
import json
import sys
from deep_research_agent.research import ResearchService

payload = json.loads(sys.argv[1])
service = ResearchService()
action = payload["action"]
if action == "refine":
    result = service.refine_plan(payload["prompt"], previous_interaction_id=payload["previous_interaction_id"])
elif action == "approve":
    result = service.approve_plan(payload["prompt"], previous_interaction_id=payload["previous_interaction_id"])
else:
    raise ValueError("Unknown action")

if hasattr(result, "model_dump"):
    result = result.model_dump()
elif hasattr(result, "__dict__"):
    result = {k: v for k, v in vars(result).items() if not k.startswith("_")}
print(json.dumps(result, default=str))
`;

async function runDeepResearchPlanFollowup(
  action: "refine" | "approve",
  previousInteractionId: string,
  prompt: string,
): Promise<unknown> {
  const { stdout } = await execFileAsync(
    DEFAULT_DEEP_RESEARCH_PYTHON,
    ["-c", PLAN_FOLLOWUP_SCRIPT, JSON.stringify({ action, previous_interaction_id: previousInteractionId, prompt })],
    {
      cwd: DEFAULT_DEEP_RESEARCH_ROOT,
      maxBuffer: 20 * 1024 * 1024,
      timeout: 10 * 60 * 1000,
    },
  );
  return parseJsonOutput(stdout);
}

function deepResearchArgs(command: "plan" | "run", body: Record<string, unknown>): string[] {
  const prompt = stringValue(body.prompt);
  if (!prompt?.trim()) throw new Error(`deep-research ${command} requires prompt.`);
  const args = [command, prompt, "--mode", researchModeValue(body.mode)];
  appendListArgs(args, "--file-store", stringArray(body.fileSearchStores));
  appendListArgs(args, "--tool", researchToolArray(body.enabledTools));
  appendListArgs(args, "--document-uri", stringArray(body.documentUris));
  appendListArgs(args, "--image-uri", stringArray(body.imageUris));
  appendListArgs(args, "--mcp-server-json", stringArray(body.mcpServers));
  if (boolValue(body.maxMode)) args.push("--max-mode");
  if (command === "run" && boolValue(body.visualization)) args.push("--visualization");
  return args;
}

function safeRunId(value: string): string {
  if (!/^[0-9TZa-f-]+$/i.test(value)) throw new Error("Invalid Deep Research run id.");
  return value;
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function listDeepResearchRuns(): Promise<DeepResearchRunSummary[]> {
  const runsRoot = path.join(DEFAULT_DEEP_RESEARCH_ROOT, "runs");
  const names = await readdir(runsRoot);
  const runs: DeepResearchRunSummary[] = [];
  for (const name of names) {
    if (name.startsWith(".")) continue;
    const runDir = path.join(runsRoot, name);
    const metadataPath = path.join(runDir, "metadata.json");
    try {
      const metadata = JSON.parse(await readFile(metadataPath, "utf8")) as DeepResearchRunSummary;
      runs.push({
        ...metadata,
        has_result: await fileExists(path.join(runDir, "result.json")),
        has_report: await fileExists(path.join(runDir, "report.md")),
        has_events: await fileExists(path.join(runDir, "events.jsonl")),
      });
    } catch {
      /* Ignore incomplete run folders. */
    }
  }
  return runs.sort((a, b) => String(b.created_at ?? b.run_id).localeCompare(String(a.created_at ?? a.run_id)));
}

async function getDeepResearchRun(runId: string): Promise<unknown> {
  const id = safeRunId(runId);
  const runDir = path.join(DEFAULT_DEEP_RESEARCH_ROOT, "runs", id);
  const metadata = JSON.parse(await readFile(path.join(runDir, "metadata.json"), "utf8")) as DeepResearchRunSummary;
  const resultPath = path.join(runDir, "result.json");
  const reportPath = path.join(runDir, "report.md");
  const eventsPath = path.join(runDir, "events.jsonl");
  return {
    metadata,
    result: (await fileExists(resultPath)) ? JSON.parse(await readFile(resultPath, "utf8")) : null,
    report: (await fileExists(reportPath)) ? await readFile(reportPath, "utf8") : "",
    events: (await fileExists(eventsPath)) ? (await readFile(eventsPath, "utf8")).trim().split("\n").filter(Boolean).slice(-100) : [],
  };
}

function createDeepResearchBridge(): DeepResearchBridge {
  return {
    paths: async () => ({
      root: DEFAULT_DEEP_RESEARCH_ROOT,
      cli: DEFAULT_DEEP_RESEARCH_CLI,
      local_docs: path.join(DEFAULT_DEEP_RESEARCH_ROOT, "local-docs"),
      runs: path.join(DEFAULT_DEEP_RESEARCH_ROOT, "runs"),
    }),
    listRuns: listDeepResearchRuns,
    getRun: getDeepResearchRun,
    localDocsManifest: () => runDeepResearchCli(["local-docs-manifest"]),
    listFileStores: () => runDeepResearchCli(["list-file-stores"]),
    createFileStore: (displayName = "codex-local-docs") =>
      runDeepResearchCli(["create-file-store", "--display-name", displayName]),
    deleteFileStore: (name: string) => runDeepResearchCli(["delete-file-store", name]),
    plan: (body) => runDeepResearchCli(deepResearchArgs("plan", body)),
    refinePlan: (body) => {
      const previousInteractionId = stringValue(body.previousInteractionId);
      const prompt = stringValue(body.prompt);
      if (!previousInteractionId?.trim()) throw new Error("refine-plan requires previousInteractionId.");
      if (!prompt?.trim()) throw new Error("refine-plan requires prompt.");
      return runDeepResearchPlanFollowup("refine", previousInteractionId, prompt);
    },
    approvePlan: (body) => {
      const previousInteractionId = stringValue(body.previousInteractionId);
      const prompt = stringValue(body.prompt) ?? "Approve this collaborative plan and start execution.";
      if (!previousInteractionId?.trim()) throw new Error("approve-plan requires previousInteractionId.");
      return runDeepResearchPlanFollowup("approve", previousInteractionId, prompt);
    },
    run: (body) => runDeepResearchCli(deepResearchArgs("run", body)),
    status: (body) => {
      const interactionId = stringValue(body.interactionId);
      if (!interactionId?.trim()) throw new Error("status requires interactionId.");
      const args = ["status", interactionId];
      const runId = stringValue(body.runId);
      if (runId?.trim()) args.push("--run-id", safeRunId(runId));
      return runDeepResearchCli(args);
    },
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
  const deepResearch = options.deepResearch ?? createDeepResearchBridge();

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
          tools: [
            "sovereign_prepare_task",
            "sovereign_prepare_outcome",
            "sovereign_status",
            "sovereign_audit_tail",
            "deep_research_plan",
            "deep_research_run",
            "deep_research_status",
            "deep_research_local_docs",
          ],
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
        sendJson(res, 200, redactLocalValue(await taskPrep(prepareTaskInput(body, vaultPath))));
        return;
      }
      if (req.method === "POST" && url.pathname === "/api/prepare-outcome") {
        if (!jsonContentTypeAllowed(req.headers["content-type"])) {
          sendText(res, 415, "POST requests must use application/json.");
          return;
        }
        const body = await readJsonBody(req);
        sendJson(res, 200, redactLocalValue(await outcomePrep(prepareOutcomeInput(body, vaultPath))));
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/deep-research/paths") {
        sendJson(res, 200, redactLocalValue(await deepResearch.paths()));
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/deep-research/runs") {
        sendJson(res, 200, redactLocalValue(await deepResearch.listRuns()));
        return;
      }
      const deepRunMatch = url.pathname.match(/^\/api\/deep-research\/runs\/([^/]+)$/);
      if (req.method === "GET" && deepRunMatch) {
        sendJson(res, 200, redactLocalValue(await deepResearch.getRun(deepRunMatch[1])));
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/deep-research/local-docs-manifest") {
        sendJson(res, 200, redactLocalValue(await deepResearch.localDocsManifest()));
        return;
      }
      if (req.method === "GET" && url.pathname === "/api/deep-research/file-stores") {
        sendJson(res, 200, redactLocalValue(await deepResearch.listFileStores()));
        return;
      }
      if (req.method === "POST" && url.pathname.startsWith("/api/deep-research/")) {
        if (!jsonContentTypeAllowed(req.headers["content-type"])) {
          sendText(res, 415, "POST requests must use application/json.");
          return;
        }
        const body = await readJsonBody(req);
        if (url.pathname === "/api/deep-research/create-file-store") {
          sendJson(res, 200, redactLocalValue(await deepResearch.createFileStore(stringValue(body.displayName))));
          return;
        }
        if (url.pathname === "/api/deep-research/delete-file-store") {
          const name = stringValue(body.name);
          if (!name?.trim()) throw new Error("delete-file-store requires name.");
          sendJson(res, 200, redactLocalValue(await deepResearch.deleteFileStore(name)));
          return;
        }
        if (url.pathname === "/api/deep-research/plan") {
          sendJson(res, 200, redactLocalValue(await deepResearch.plan(body)));
          return;
        }
        if (url.pathname === "/api/deep-research/refine-plan") {
          sendJson(res, 200, redactLocalValue(await deepResearch.refinePlan(body)));
          return;
        }
        if (url.pathname === "/api/deep-research/approve-plan") {
          sendJson(res, 200, redactLocalValue(await deepResearch.approvePlan(body)));
          return;
        }
        if (url.pathname === "/api/deep-research/run") {
          sendJson(res, 200, redactLocalValue(await deepResearch.run(body)));
          return;
        }
        if (url.pathname === "/api/deep-research/status") {
          sendJson(res, 200, redactLocalValue(await deepResearch.status(body)));
          return;
        }
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
