// Shared fixture for the use-telemetry hook tests (recall-to-use S1a-2): a fake
// daemon on a Unix socket, a spawned hook process, and a ledger reader. Not a
// test file itself (no `.test.mjs`), so the suite glob does not run it.
//
// Every hook runs as a real child process, because "deliver first, record
// second" is only observable through emitAndCommit: in-process handler calls
// never flush, so their deferred rows never land.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, readFile, rm, stat } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_DIST = path.join(PLUGIN_ROOT, "dist");

/** sha256(session_id)[:16], the ledger's session key (§4.2). */
export function sessionKeyOf(sessionId) {
  return createHash("sha256").update(sessionId, "utf8").digest("hex").slice(0, 16);
}

/** `a-<sha256(canonical id)[:12]>` for a subagent id (§4.2). */
export function agentKeyOf(agentId) {
  const id = agentId.replace(/\.jsonl$/, "").replace(/^agent-/, "") || agentId;
  return `a-${createHash("sha256").update(id, "utf8").digest("hex").slice(0, 12)}`;
}

export function sha1Prefix(text, length = 12) {
  return createHash("sha1").update(text, "utf8").digest("hex").slice(0, length);
}

/**
 * A fresh root with `home/` and `vault/`. With `at`, the root is that exact
 * path (recreated empty), so two runs can share byte-identical paths.
 */
export async function makeFixture(t, label, { at } = {}) {
  let root;
  if (at) {
    await rm(at, { recursive: true, force: true });
    await mkdir(at, { recursive: true });
    root = at;
  } else {
    root = await mkdtemp(path.join(tmpdir(), `minni-${label}-`));
  }
  const home = path.join(root, "home");
  const vault = path.join(root, "claudecode-vault");
  await mkdir(home, { recursive: true });
  await mkdir(vault, { recursive: true });
  const fixture = { root, home, vault, socket: path.join(home, "minnid.sock"), servers: [] };
  if (t) {
    t.after(async () => {
      for (const server of fixture.servers) server.close();
      await rm(root, { recursive: true, force: true });
    });
  }
  return fixture;
}

export async function closeFixture(fixture) {
  for (const server of fixture.servers) server.close();
  fixture.servers = [];
  await rm(fixture.root, { recursive: true, force: true });
}

/**
 * A JSON-RPC daemon on the fixture socket. `handlers[method](params)` returns
 * the result (or a promise of it); `delay[method]` holds the reply that long.
 */
export async function startFakeDaemon(fixture, handlers = {}, { delay = {} } = {}) {
  const defaults = {
    status: () => ({ status: "ok" }),
    search: (params) => ({ agent_id: params.agent_id, results: [] }),
    read: (params) => ({ agent_id: params.agent_id, context: `No context for '${params.agent_id}'.` }),
    minni_list_pending_handoffs: () => ({ handoffs: [] }),
    minni_subscribe_contradictions: () => ({ events: [], status: "checked_no_match" }),
  };
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("error", () => {});
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        const handler = handlers[request.method] ?? defaults[request.method] ?? (() => ({ ok: true }));
        const reply = async () => {
          const result = await handler(request.params ?? {});
          if (!socket.destroyed) {
            socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
          }
        };
        const wait = delay[request.method];
        if (wait) setTimeout(() => void reply(), wait);
        else void reply();
      }
    });
  });
  await new Promise((resolve) => server.listen(fixture.socket, resolve));
  fixture.servers.push(server);
  return server;
}

export async function stopDaemons(fixture) {
  for (const server of fixture.servers) await new Promise((resolve) => server.close(resolve));
  fixture.servers = [];
}

const SCRUBBED_ENV = [
  "MINNI_USE_LEDGER",
  "MINNI_BOOT_FIT",
  "MINNI_GUARD_V2",
  "MINNI_SUBAGENT_BRIEF",
  "MINNI_PROMPT_CLASSES",
  "MINNI_RECENT_LEARNINGS_RANK",
  "MINNI_UPS_RANK_V2",
  "MINNI_RECALL_NOISE_LAST",
  "MINNI_RECALL_GUARD_MODE",
  "MINNI_RECALL_GUARD_DELIVERY",
  "MINNI_RECALL_POINTER_THRESHOLD",
  "MINNI_LIFECYCLE_NUDGE_MODE",
  "MINNI_HOOK_BUDGET_MS",
  "MINNI_CLAUDECODE_HOOKS_ENABLED",
];

export function hookEnv(fixture, extra = {}) {
  const env = { ...process.env };
  for (const name of SCRUBBED_ENV) delete env[name];
  return {
    ...env,
    HOME: fixture.home,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: fixture.socket,
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_CLAUDECODE_AGENT_ID: "claude-code",
    ...extra,
  };
}

/** Run `dist/<bin> <event>` with `payload` on stdin; resolves with the parsed last stdout line. */
export function runHook(fixture, event, payload, { env = {}, bin = "hook.js", dist = DEFAULT_DIST } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(dist, bin), event], {
      env: hookEnv(fixture, env),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`hook ${event} timed out; stderr=${stderr}`));
    }, 60_000);
    child.on("error", reject);
    child.on("close", (code) => {
      clearTimeout(timer);
      const raw = stdout.trim().split("\n").at(-1) ?? "";
      let output;
      try {
        output = JSON.parse(raw);
      } catch {
        reject(new Error(`unparseable hook output for ${event}: ${stdout} / ${stderr}`));
        return;
      }
      resolve({ output, raw, stdout, stderr, code });
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export async function exists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

/**
 * Every complete ledger row under the vault, by `ts` (a stable sort, so rows
 * of one session keep their file order on a tie).
 */
export async function readLedger(vault) {
  const root = path.join(vault, ".runtime", "use-ledger");
  const rows = [];
  let sessions = [];
  try {
    sessions = (await readdir(root)).sort();
  } catch {
    return rows;
  }
  for (const session of sessions) {
    let days = [];
    try {
      days = (await readdir(path.join(root, session))).filter((name) => name.endsWith(".jsonl")).sort();
    } catch {
      continue;
    }
    for (const day of days) {
      const text = await readFile(path.join(root, session, day), "utf8");
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        rows.push(JSON.parse(line));
      }
    }
  }
  return rows.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
}

/** The raw text of every ledger file, for "nothing leaked" checks. */
export async function ledgerText(vault) {
  const root = path.join(vault, ".runtime", "use-ledger");
  let out = "";
  let sessions = [];
  try {
    sessions = await readdir(root);
  } catch {
    return out;
  }
  for (const session of sessions) {
    for (const day of await readdir(path.join(root, session))) {
      out += await readFile(path.join(root, session, day), "utf8");
    }
  }
  return out;
}

export function additionalContext(output) {
  return output?.hookSpecificOutput?.additionalContext ?? "";
}

const SCENARIO_READ_CONTEXT = [
  "## Learnings (claude-code)",
  "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
  "",
  "## Recent Activity (claude-code)",
  "  - [note] boot",
].join("\n");

/**
 * The hook outputs the hard rule pins, one fresh fixture per scenario at the
 * SAME path (`root`), so two runs differ only in what is under test. Returns
 * `{ name: { raw, rows } }`: the exact last stdout line, and how many ledger
 * rows the scenario left (the positive control that capture actually ran).
 *
 * Exported so the same scenarios can be run through another build's dist.
 */
export async function runOutputScenarios({ root, dist = DEFAULT_DIST, env = {} }) {
  const strongResults = [
    daemonResult({ source: "/elsewhere/deploy.md", docId: 41, wikilink: "[[wiki/deploy]]", title: "Deploy procedure" }),
    daemonResult({ source: "/elsewhere/rollback.md", docId: 42, wikilink: "[[wiki/rollback]]", title: "Rollback", confidence: 0.7 }),
  ];
  const daemon = {
    search: (params) => ({
      agent_id: "claude-code",
      results: String(params.query ?? "").includes("STRONGQ") || String(params.query ?? "").startsWith("boot identity")
        ? strongResults
        : [],
    }),
    read: () => ({
      agent_id: "claude-code",
      context: SCENARIO_READ_CONTEXT,
      recent_learnings: [
        {
          learning_id: 77,
          category: "fix",
          line: "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
          content: "Correction: service X moved to port 9090",
        },
      ],
      daemon_version: "test",
    }),
  };
  const scenarios = {
    session_start: async (fixture) =>
      runHook(fixture, "SessionStart", { session_id: "scenario", source: "startup" }, { env, dist }),
    ups_strong: async (fixture) =>
      runHook(
        fixture,
        "UserPromptSubmit",
        { session_id: "scenario", prompt_id: "p1", prompt: "STRONGQ where does the deploy procedure live" },
        { env, dist },
      ),
    ups_weak: async (fixture) =>
      runHook(
        fixture,
        "UserPromptSubmit",
        { session_id: "scenario", prompt_id: "p1", prompt: "what is the zebra pancake status" },
        { env, dist },
      ),
    ups_envelope: async (fixture) =>
      runHook(
        fixture,
        "UserPromptSubmit",
        {
          session_id: "scenario",
          prompt_id: "p1",
          prompt: "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n</task-notification>",
        },
        { env, dist },
      ),
    pretooluse_deny: async (fixture) => {
      await runHook(
        fixture,
        "UserPromptSubmit",
        { session_id: "scenario", prompt_id: "p1", prompt: "STRONGQ where does the deploy procedure live" },
        { env, dist },
      );
      return runHook(
        fixture,
        "PreToolUse",
        { session_id: "scenario", prompt_id: "p1", tool_name: "Read", tool_input: { file_path: "/tmp/x.txt" } },
        { env, dist },
      );
    },
  };
  const out = {};
  for (const [name, run] of Object.entries(scenarios)) {
    const fixture = await makeFixture(null, "scenario", { at: root });
    await startFakeDaemon(fixture, daemon);
    try {
      const result = await run(fixture);
      out[name] = { raw: result.raw, code: result.code, rows: (await readLedger(fixture.vault)).length };
    } finally {
      await closeFixture(fixture);
    }
  }
  return out;
}

/** A daemon search result with the fields the plugin keys on. */
export function daemonResult({ source, docId, src = "personal", wikilink, confidence = 0.95, text = "", title, layer = "knowledge" }) {
  return {
    source,
    filename: path.basename(source),
    doc_id: docId,
    src,
    wikilink,
    confidence,
    score: confidence,
    layer,
    text,
    ...(title !== undefined ? { title } : {}),
  };
}
