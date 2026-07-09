import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { request } from "node:http";
import { createServer as createNetServer } from "node:net";
import path from "node:path";
import test from "node:test";

import { consolePrincipalReport, createUiServer, daemonRpcHttpStatus } from "../dist/ui-server.js";

const CONSOLE_AUTH = {
  Authorization: `Bearer ${process.env.MINNI_CONSOLE_TOKEN}`,
};

function authHeaders(extra = {}) {
  return { ...CONSOLE_AUTH, ...extra };
}

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
        currentState: ["Bridge test state at /Users/alice/private/repo and ~/.minni/run/minnid.sock."],
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
        contextMarkdown: "# Packet\n/Users/alice/private/repo\n~/.minni/run/minnid.sock",
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

    const status = await fetch(`${server.baseUrl}/api/status`, { headers: authHeaders() }).then((response) => response.json());
    assert.equal(status.vault.exists, true);
    assert.equal(status.afm.data.adapter, "[local-path]");

    const prepare = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ task: "wire the complete frontend", profile: "compact", useAfm: false }),
    }).then((response) => response.json());
    assert.equal(prepare.task, "wire the complete frontend");
    assert.equal(prepare.profile, "compact");
    assert.equal(prepare.relevantSources[0].relativePath, "[local-path]");
    assert.equal(prepare.relevantSources[0].snippet, "adapter [local-path]");
    assert.doesNotMatch(JSON.stringify(prepare), /\/Users\/alice|\/tmp\/sovereign\.sock|\.fmadapter/);

    const outcome = await fetch(`${server.baseUrl}/api/prepare-outcome`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        task: "wire the complete frontend",
        summary: "Added a real local bridge.",
        changedFiles: ["frontend/app.js"],
        verification: ["node --test"],
      }),
    }).then((response) => response.json());
    assert.match(outcome.outcomeDraft.learnCandidates[0], /real local bridge/);

    const audit = await fetch(`${server.baseUrl}/api/audit-tail?limit=5`, { headers: authHeaders() }).then((response) => response.json());
    assert.equal(audit.entries.length, 1);

    const deepPaths = await fetch(`${server.baseUrl}/api/deep-research/paths`, { headers: authHeaders() }).then((response) => response.json());
    assert.equal(deepPaths.root, "[local-path]");

    const deepRuns = await fetch(`${server.baseUrl}/api/deep-research/runs`, { headers: authHeaders() }).then((response) => response.json());
    assert.equal(deepRuns[0].run_id, "20260429T000000Z-abc12345");

    const deepPlan = await fetch(`${server.baseUrl}/api/deep-research/plan`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ prompt: "research the console", mode: "web", enabledTools: ["google_search"] }),
    }).then((response) => response.json());
    assert.equal(deepPlan.id, "v1_plan");

    const indexHtml = await fetch(`${server.baseUrl}/`, { headers: authHeaders() }).then((response) => response.text());
    assert.match(indexHtml, /Minni Memory Console/);

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
      headers: authHeaders({ "Content-Type": "text/plain" }),
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
      headers: authHeaders({ "Content-Type": "application/json" }),
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
    const audit = await fetch(`${server.baseUrl}/api/audit-tail?limit=bad`, { headers: authHeaders() }).then((response) => response.json());
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
      const req = request(server.baseUrl + "/", { method: "HEAD", headers: authHeaders() }, (res) => {
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

test("frontend ships the Minni command center design and stays local-only", async () => {
  const [html, js, css] = await Promise.all([
    readFile(path.join(process.cwd(), "frontend", "index.html"), "utf8"),
    readFile(path.join(process.cwd(), "frontend", "app.js"), "utf8"),
    readFile(path.join(process.cwd(), "frontend", "styles.css"), "utf8"),
  ]);

  assert.match(html, /Minni Memory Console/);
  assert.match(html, /command-center-shell/);
  assert.doesNotMatch(html, /unpkg\.com|fonts\.googleapis\.com|text\/babel/);

  // Screen labels from the design bundle's Rail nav
  assert.match(js, /Recall/);
  assert.match(js, /Prepare Packet/);
  assert.match(js, /Dry-run Review/);
  assert.match(js, /Memory Board/);
  assert.match(js, /Audit Trail/);
  assert.match(js, /Settings/);
  // No write / learn endpoints exposed to the browser
  assert.doesNotMatch(js, /\/api\/learn|sovereign_learn/);

  // Design tokens from the paper + phosphor themes
  assert.match(css, /--graphite/);
  assert.match(css, /--verdigris/);
  assert.match(css, /--persimmon/);
  // Memory Board design tokens must survive the build:frontend embedding.
  assert.match(css, /--bd-gold/);
  assert.match(css, /--bd-ac-claude/);
  // Minifier drops quotes around static attribute values, so accept either form.
  assert.match(css, /data-theme=("?)phosphor\1/);
});

// ── S6: PR91-4 — console auth fails closed (403) on missing/invalid token ────
// A token IS configured (setup-env sets MINNI_CONSOLE_TOKEN), so /api/status and
// /api/prepare-* must reject a request with no or a wrong Authorization header.

test("PR91-4: /api/status returns 403 on missing or invalid bearer token", async () => {
  const server = await startTestServer();
  try {
    const missing = await fetch(`${server.baseUrl}/api/status`);
    assert.equal(missing.status, 403, "no token => 403");
    const body = await missing.json();
    assert.equal(body.error, "console_auth_required");

    const wrong = await fetch(`${server.baseUrl}/api/status`, {
      headers: { Authorization: "Bearer not-the-real-token" },
    });
    assert.equal(wrong.status, 403, "wrong token => 403");

    // Sanity: the correct token still authorizes.
    const ok = await fetch(`${server.baseUrl}/api/status`, { headers: authHeaders() });
    assert.equal(ok.status, 200, "correct token => 200");
  } finally {
    await server.close();
  }
});

test("PR91-4: /api/prepare-task returns 403 on missing or invalid bearer token", async () => {
  const server = await startTestServer();
  try {
    const body = JSON.stringify({ task: "x", profile: "compact", useAfm: false });
    const missing = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    assert.equal(missing.status, 403, "no token => 403");

    const wrong = await fetch(`${server.baseUrl}/api/prepare-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer nope" },
      body,
    });
    assert.equal(wrong.status, 403, "wrong token => 403");

    // A bearer of the wrong LENGTH must also 403 (constant-time compare path).
    const wrongLen = await fetch(`${server.baseUrl}/api/prepare-outcome`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer x" },
      body: JSON.stringify({ task: "x", summary: "y", changedFiles: [], verification: [] }),
    });
    assert.equal(wrongLen.status, 403, "wrong-length token => 403");
  } finally {
    await server.close();
  }
});

// ── Static shell is public; the data plane stays locked ─────────────────────
// The browser must be able to load index.html/app.js WITHOUT a token so it can
// reach the in-app token gate; every /api route except /api/health still 403s.
test("static shell serves without auth while /api stays locked", async () => {
  const server = await startTestServer();
  try {
    const index = await fetch(`${server.baseUrl}/`);
    assert.equal(index.status, 200, "index.html => 200 without token");
    const html = await index.text();
    assert.match(html, /<div id="root">/, "serves the app shell");

    const js = await fetch(`${server.baseUrl}/app.js`);
    assert.equal(js.status, 200, "app.js => 200 without token");

    const health = await fetch(`${server.baseUrl}/api/health`);
    assert.equal(health.status, 200, "/api/health stays open");

    const status = await fetch(`${server.baseUrl}/api/status`);
    assert.equal(status.status, 403, "/api/status still locked without token");

    // Traversal out of staticRoot is still refused even unauthenticated.
    const escape = await fetch(`${server.baseUrl}/..%2f..%2fpackage.json`);
    assert.notEqual(escape.status, 200, "path traversal refused");
  } finally {
    await server.close();
  }
});

// ── Fail-loud RPC status mapping + honest principal stamp ───────────────────
test("daemonRpcHttpStatus maps governance denials honestly", () => {
  assert.equal(daemonRpcHttpStatus("already_resolved: candidate #3 is 'accepted'"), 409);
  assert.equal(
    daemonRpcHttpStatus("operator_only: accepting candidate #1 into a durable learning requires an operator/govern principal"),
    403,
  );
  assert.equal(
    daemonRpcHttpStatus("accept_flagged_required: candidate is instruction_like; accepting it requires literal 'accept_flagged' capability"),
    403,
  );
  assert.equal(daemonRpcHttpStatus("principal_mismatch: agent cannot resolve"), 403);
  assert.equal(daemonRpcHttpStatus("candidate_not_found: 99999"), 404);
  assert.equal(daemonRpcHttpStatus("unknown candidate"), 404);
  // Must NOT treat JSON-RPC "Method not found" as a missing candidate.
  assert.equal(daemonRpcHttpStatus("Method not found: resolve_candidate"), 501);
  // Message-only paths (sovereign forwards error.message, not the numeric code).
  assert.equal(daemonRpcHttpStatus("decision must be one of ['accept','reject']"), 400);
  assert.equal(daemonRpcHttpStatus("candidate_id must be integer"), 400);
  assert.equal(daemonRpcHttpStatus("candidate_id is required"), 400);
  assert.equal(
    daemonRpcHttpStatus("invalid agent_id 'Bad': must match ^[a-z0-9][a-z0-9._-]{0,63}$"),
    400,
  );
  assert.equal(daemonRpcHttpStatus("agent_id must be a non-empty string, got ''"), 400);
  // Broad "must be" without a known validation shape stays 502 (not a fake client error).
  assert.equal(daemonRpcHttpStatus("internal: widget must be flipped"), 502);
  assert.equal(daemonRpcHttpStatus("socket ECONNREFUSED"), 502);
});

test("consolePrincipalReport does not invent capabilities", () => {
  const report = consolePrincipalReport();
  assert.equal(typeof report.agentId, "string");
  assert.equal(typeof report.unknownAgent, "boolean");
  assert.equal(typeof report.resolveOperatorsEnv, "boolean");
  assert.ok(!("capabilities" in report), "capabilities must not be fabricated on the bridge");
});

test("/api/status includes principal without capabilities", async () => {
  const server = await startTestServer();
  try {
    const res = await fetch(`${server.baseUrl}/api/status`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.ok(body.principal, "principal stamp present");
    assert.equal(typeof body.principal.agentId, "string");
    assert.ok(!("capabilities" in body.principal), "no fabricated capabilities on /api/status");
  } finally {
    await server.close();
  }
});

test("/api/candidates and /api/resolve-candidate use daemonRpcHttpStatus at the route seam", async () => {
  const server = await startTestServer({
    daemonRpc: async (method) => {
      if (method === "list_candidates") {
        return { ok: false, error: "operator_only: list denied for test" };
      }
      if (method === "resolve_candidate") {
        return { ok: false, error: "already_resolved: candidate #9 is 'rejected'" };
      }
      return { ok: false, error: `unexpected method ${method}` };
    },
  });
  try {
    const candidates = await fetch(`${server.baseUrl}/api/candidates?status=proposed`, { headers: authHeaders() });
    assert.equal(candidates.status, 403, "operator_only via JsonResult must be 403 at the route");
    const candBody = await candidates.json();
    assert.equal(candBody.ok, false);
    assert.match(candBody.error, /operator_only/);

    const resolve = await fetch(`${server.baseUrl}/api/resolve-candidate`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ candidate_id: 9, decision: "accept" }),
    });
    assert.equal(resolve.status, 409, "already_resolved via JsonResult must be 409 at the route");
    const resolveBody = await resolve.json();
    assert.equal(resolveBody.ok, false);
    assert.match(resolveBody.error, /already_resolved/);
  } finally {
    await server.close();
  }
});

test("/api/candidates catch branch maps thrown daemon errors via daemonRpcHttpStatus", async () => {
  const server = await startTestServer({
    daemonRpc: async () => {
      throw new Error("accept_flagged_required: candidate is instruction_like");
    },
  });
  try {
    const candidates = await fetch(`${server.baseUrl}/api/candidates`, { headers: authHeaders() });
    assert.equal(candidates.status, 403, "thrown accept_flagged_required must be 403 via catch");
    const body = await candidates.json();
    assert.equal(body.ok, false);
    assert.match(body.error, /accept_flagged_required/);
  } finally {
    await server.close();
  }
});
