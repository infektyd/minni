import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { request } from "node:http";
import { createServer as createNetServer } from "node:net";
import path from "node:path";
import test from "node:test";

import { createUiServer } from "../dist/ui-server.js";

async function freePort() {
  const server = createNetServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  await new Promise((resolve) => server.close(resolve));
  assert.equal(typeof address, "object");
  return address.port;
}

async function startTestServer(overrides = {}) {
  const port = await freePort();
  const calls = [];
  const server = createUiServer({
    host: "127.0.0.1",
    port,
    staticRoot: path.join(process.cwd(), "frontend"),
    vaultPath: "/tmp/vault",
    status: async () => ({
      vault: { path: "/tmp/vault", exists: true },
      socket: { ok: true },
      afm: { ok: true, data: { adapter: "/Users/alice/private/model.fmadapter", status: "ok" } },
      audit: { entries: 0 },
    }),
    auditTail: async () => ({ entries: ["## audit one"], text: "## audit one" }),
    prepareTask: async (input) => {
      calls.push(["prepareTask", input]);
      return {
        task: input.task,
        budgetTokens: input.budgetTokens ?? 1500,
        profile: input.profile ?? "compact",
        budget: { profile: input.profile ?? "compact", tokens: input.budgetTokens ?? 1500, sourceLimit: 3 },
        mode: "deterministic",
        intent: "implement",
        brief: "Real task packet.",
        constraints: ["No automatic learning."],
        currentState: ["Bridge test state at /Users/alice/private/repo and /tmp/sovereign.sock."],
        relevantSources: [
          {
            title: "Local source",
            snippet: "adapter /Users/alice/private/model.fmadapter",
            relativePath: "/Users/alice/private/vault/wiki/local.md",
          },
        ],
        recommendedNextActions: ["Inspect sources."],
        risks: [],
        recall: { daemonOk: true },
        afm: { requested: false, used: false },
        contextMarkdown: "# Packet\n/Users/alice/private/repo\n/tmp/sovereign.sock",
      };
    },
    prepareOutcome: async (input) => {
      calls.push(["prepareOutcome", input]);
      return {
        task: input.task,
        summary: input.summary,
        profile: input.profile ?? "compact",
        budget: { profile: input.profile ?? "compact", tokens: 1500, sourceLimit: 3 },
        mode: "deterministic",
        changedFiles: input.changedFiles ?? [],
        verification: input.verification ?? [],
        outcomeDraft: {
          learnCandidates: [`${input.task}: ${input.summary}`],
          logOnly: input.verification ?? [],
          expires: [],
          doNotStore: ["No raw logs."],
        },
        afm: { requested: false, used: false },
        contextMarkdown: "# Outcome",
      };
    },
    deepResearch: {
      paths: async () => ({
        root: "/Users/alice/deep-research-agent",
        cli: "/Users/alice/deep-research-agent/.venv/bin/deep-research",
        local_docs: "/Users/alice/deep-research-agent/local-docs",
        runs: "/Users/alice/deep-research-agent/runs",
      }),
      listRuns: async () => [
        {
          run_id: "20260429T000000Z-abc12345",
          created_at: "2026-04-29T00:00:00Z",
          prompt: "Research local console UX.",
          mode: "web",
          interaction_id: "v1_test",
          status: "completed",
          has_result: true,
          has_report: true,
        },
      ],
      getRun: async (runId) => ({
        metadata: { run_id: runId, status: "completed", interaction_id: "v1_test" },
        result: { id: "v1_test", status: "completed", outputs: [{ type: "text", text: "report" }] },
        report: "Deep Research report from /Users/alice/private/run.",
        events: [],
      }),
      localDocsManifest: async () => ({ root: "/Users/alice/deep-research-agent/local-docs", files: [] }),
      listFileStores: async () => ({ fileStores: [{ name: "fileSearchStores/test" }] }),
      createFileStore: async (displayName) => ({ name: "fileSearchStores/test", displayName }),
      deleteFileStore: async (name) => ({ deleted: name }),
      plan: async (input) => {
        calls.push(["deepPlan", input]);
        return { id: "v1_plan", status: "completed", outputs: [{ type: "text", text: "plan" }] };
      },
      refinePlan: async (input) => ({ id: input.previousInteractionId, status: "completed" }),
      approvePlan: async (input) => ({ id: input.previousInteractionId, status: "created" }),
      run: async (input) => {
        calls.push(["deepRun", input]);
        return { run_id: "20260429T000000Z-abc12345", interaction_id: "v1_run" };
      },
      status: async (input) => ({ id: input.interactionId, status: "completed" }),
    },
    ...overrides,
  });
  await server.start();
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    calls,
    close: () => server.close(),
  };
}

test("UI server exposes local status, prepare, outcome, audit, and static assets", async () => {
  const server = await startTestServer();
  try {
    const health = await fetch(`${server.baseUrl}/api/health`).then((response) => response.json());
    assert.equal(health.ok, true);
    assert.equal(health.host, "127.0.0.1");

    const status = await fetch(`${server.baseUrl}/api/status`).then((response) => response.json());
    assert.equal(status.vault.exists, true);
    assert.equal(status.afm.data.adapter, "[local-path]");

    const prepare = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "wire the complete frontend", profile: "compact", useAfm: false }),
    }).then((response) => response.json());
    assert.equal(prepare.task, "wire the complete frontend");
    assert.equal(prepare.profile, "compact");
    assert.equal(prepare.relevantSources[0].relativePath, "[local-path]");
    assert.equal(prepare.relevantSources[0].snippet, "adapter [local-path]");
    assert.doesNotMatch(JSON.stringify(prepare), /\/Users\/alice|\/tmp\/sovereign\.sock|\.fmadapter/);

    const outcome = await fetch(`${server.baseUrl}/api/prepare-outcome`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: "wire the complete frontend",
        summary: "Added a real local bridge.",
        changedFiles: ["frontend/app.js"],
        verification: ["node --test"],
      }),
    }).then((response) => response.json());
    assert.match(outcome.outcomeDraft.learnCandidates[0], /real local bridge/);

    const audit = await fetch(`${server.baseUrl}/api/audit-tail?limit=5`).then((response) => response.json());
    assert.equal(audit.entries.length, 1);

    const deepPaths = await fetch(`${server.baseUrl}/api/deep-research/paths`).then((response) => response.json());
    assert.equal(deepPaths.root, "[local-path]");

    const deepRuns = await fetch(`${server.baseUrl}/api/deep-research/runs`).then((response) => response.json());
    assert.equal(deepRuns[0].run_id, "20260429T000000Z-abc12345");

    const deepPlan = await fetch(`${server.baseUrl}/api/deep-research/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "research the console", mode: "web", enabledTools: ["google_search"] }),
    }).then((response) => response.json());
    assert.equal(deepPlan.id, "v1_plan");

    const indexHtml = await fetch(`${server.baseUrl}/`).then((response) => response.text());
    assert.match(indexHtml, /Sovereign Memory Console/);

    assert.deepEqual(server.calls.map(([name]) => name), ["prepareTask", "prepareOutcome", "deepPlan"]);
  } finally {
    await server.close();
  }
});

test("UI server rejects non-local host headers", async () => {
  const server = await startTestServer();
  try {
    const response = await new Promise((resolve, reject) => {
      const req = request(
        server.baseUrl + "/api/health",
        {
          headers: { Host: "example.com" },
        },
        (res) => {
          let body = "";
          res.setEncoding("utf8");
          res.on("data", (chunk) => {
            body += chunk;
          });
          res.on("end", () => resolve({ status: res.statusCode, body }));
        },
      );
      req.on("error", reject);
      req.end();
    });
    assert.equal(response.status, 403);
    assert.match(response.body, /local host/);
  } finally {
    await server.close();
  }
});

test("UI server refuses non-local bind hosts", () => {
  assert.throws(() => createUiServer({ host: "0.0.0.0" }), /local bind host/);
});

test("UI server rejects cross-origin and non-JSON POST requests before side effects", async () => {
  const server = await startTestServer();
  try {
    const crossOrigin = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Origin: "https://example.com" },
      body: JSON.stringify({ task: "cross-origin drive-by" }),
    });
    assert.equal(crossOrigin.status, 403);

    const formPost = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "text/plain" },
      body: JSON.stringify({ task: "simple post drive-by" }),
    });
    assert.equal(formPost.status, 415);
    assert.equal(server.calls.length, 0);
  } finally {
    await server.close();
  }
});

test("UI server ignores client-controlled vault and AFM targets", async () => {
  const server = await startTestServer();
  try {
    await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: "do not trust client paths",
        vaultPath: "/tmp/evil-vault",
        afmPrepareUrl: "http://127.0.0.1:9999/evil",
        afmModel: "evil-model",
      }),
    });
    assert.equal(server.calls[0][1].vaultPath, "/tmp/vault");
    assert.equal(server.calls[0][1].afmPrepareUrl, undefined);
    assert.equal(server.calls[0][1].afmModel, undefined);
  } finally {
    await server.close();
  }
});

test("UI server validates audit limit and redacts audit-tail local paths", async () => {
  const seenLimits = [];
  const server = await startTestServer({
    auditTail: async (limit) => {
      seenLimits.push(limit);
      return {
        entries: ["adapter /Users/alice/private/model.fmadapter"],
        text: "adapter /Users/alice/private/model.fmadapter",
      };
    },
  });
  try {
    const audit = await fetch(`${server.baseUrl}/api/audit-tail?limit=bad`).then((response) => response.json());
    assert.deepEqual(seenLimits, [20]);
    assert.deepEqual(audit.entries, ["adapter [local-path]"]);
    assert.equal(audit.text, "adapter [local-path]");
  } finally {
    await server.close();
  }
});

test("UI server serves HEAD static requests without a body", async () => {
  const server = await startTestServer();
  try {
    const response = await new Promise((resolve, reject) => {
      const req = request(server.baseUrl + "/", { method: "HEAD" }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode, body, headers: res.headers }));
      });
      req.on("error", reject);
      req.end();
    });
    assert.equal(response.status, 200);
    assert.equal(response.body, "");
    assert.ok(Number(response.headers["content-length"]) > 0);
  } finally {
    await server.close();
  }
});

test("frontend bundle calls the local bridge endpoints", async () => {
  const js = await readFile(path.join(process.cwd(), "frontend", "app.js"), "utf8");
  assert.match(js, /\/api\/prepare-task/);
  assert.match(js, /\/api\/prepare-outcome/);
  assert.match(js, /\/api\/audit-tail/);
  assert.match(js, /\/api\/status/);
  assert.match(js, /\/api\/health/);
});

test("frontend ships the sovereign command center design and stays local-only", async () => {
  const [html, js, css] = await Promise.all([
    readFile(path.join(process.cwd(), "frontend", "index.html"), "utf8"),
    readFile(path.join(process.cwd(), "frontend", "app.js"), "utf8"),
    readFile(path.join(process.cwd(), "frontend", "styles.css"), "utf8"),
  ]);

  assert.match(html, /Sovereign Memory Console/);
  assert.match(html, /command-center-shell/);
  assert.doesNotMatch(html, /unpkg\.com|fonts\.googleapis\.com|text\/babel/);

  // Screen labels from the design bundle's Rail nav
  assert.match(js, /Recall/);
  assert.match(js, /Prepare Packet/);
  assert.match(js, /Dry-run Review/);
  assert.match(js, /Audit Trail/);
  assert.match(js, /Settings/);
  // No write / learn endpoints exposed to the browser
  assert.doesNotMatch(js, /\/api\/learn|sovereign_learn/);

  // Design tokens from the paper + phosphor themes
  assert.match(css, /--graphite/);
  assert.match(css, /--verdigris/);
  assert.match(css, /--persimmon/);
  // Minifier drops quotes around static attribute values, so accept either form.
  assert.match(css, /data-theme=("?)phosphor\1/);
});
