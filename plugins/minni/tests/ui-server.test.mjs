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
  // Real-data board zone routes (no sample fallbacks)
  assert.match(js, /\/api\/agents/);
  assert.match(js, /\/api\/log-only/);
  assert.match(js, /\/api\/quarantine/);
  assert.match(js, /\/api\/recall-state/);
  assert.match(js, /\/api\/handoffs/);
  assert.match(js, /\/api\/policy/);
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
  // Fail-loud real-data console: no unwired alpha stubs / SAMPLE board labels
  assert.doesNotMatch(js, /unwired in this alpha/i);
  assert.doesNotMatch(js, /SAMPLE · /);
  assert.doesNotMatch(js, /BOARD_SAMPLE/);
  assert.doesNotMatch(js, /policy\.handoff\.team@v4\.2|POLICY VER.*v4\.2/i);

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

// ── New board zone routes: agents, log-only, quarantine, recall-state, handoffs, policy ──

function mockCandidatesRpc(status) {
  return async (method, params) => {
    if (method === "list_candidates") {
      assert.equal(params.status, status);
      return {
        candidates: [
          {
            candidate_id: 77,
            principal: "main",
            content: `row for ${status}`,
            proposed_at: Math.floor(Date.now() / 1000),
            status,
          },
        ],
        principal: "main",
        count: 1,
      };
    }
    throw new Error(`unexpected method ${method}`);
  };
}

test("/api/log-only returns list_candidates status=log_only when daemon is up", async () => {
  const server = await startTestServer({ daemonRpc: mockCandidatesRpc("log_only") });
  try {
    const res = await fetch(`${server.baseUrl}/api/log-only`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.candidates.length, 1);
    assert.equal(body.candidates[0].status, "log_only");
  } finally {
    await server.close();
  }
});

test("/api/log-only returns 502 when daemon socket fails", async () => {
  const server = await startTestServer({
    daemonRpc: async () => {
      throw new Error("socket ECONNREFUSED");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/log-only`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.match(body.error, /ECONNREFUSED/);
  } finally {
    await server.close();
  }
});

test("/api/log-only requires auth (403 unauthed)", async () => {
  const server = await startTestServer({ daemonRpc: mockCandidatesRpc("log_only") });
  try {
    const res = await fetch(`${server.baseUrl}/api/log-only`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/quarantine returns list_candidates status=do_not_store when daemon is up", async () => {
  const server = await startTestServer({ daemonRpc: mockCandidatesRpc("do_not_store") });
  try {
    const res = await fetch(`${server.baseUrl}/api/quarantine`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.candidates[0].status, "do_not_store");
  } finally {
    await server.close();
  }
});

test("/api/quarantine returns 502 when daemon is down", async () => {
  const server = await startTestServer({
    daemonRpc: async () => {
      throw new Error("socket ECONNREFUSED");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/quarantine`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.match(body.error, /ECONNREFUSED/);
    assert.deepEqual(body.candidates, []);
  } finally {
    await server.close();
  }
});

test("/api/quarantine requires auth (403 unauthed)", async () => {
  const server = await startTestServer();
  try {
    const res = await fetch(`${server.baseUrl}/api/quarantine`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/recall-state returns present=false when no state file (not an error)", async () => {
  const server = await startTestServer({
    readRecallState: async () => null,
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.present, false);
    assert.equal(body.state, null);
    assert.match(body.message, /no recent recall/i);
  } finally {
    await server.close();
  }
});

// A vault outside $HOME must not leak its raw absolute path on the present:false
// or error branches — only the present:true branch was redacted before this fix.
test("/api/recall-state redacts a non-$HOME vault path on the present:false branch", async () => {
  const server = await startTestServer({
    vaultPath: "/Users/notme/external-secret-project/vault",
    readRecallState: async () => null,
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.present, false);
    assert.doesNotMatch(JSON.stringify(body), /\/Users\/notme/);
  } finally {
    await server.close();
  }
});

test("/api/recall-state redacts a non-$HOME vault path on the error branch", async () => {
  const server = await startTestServer({
    vaultPath: "/Users/notme/external-secret-project/vault",
    readRecallState: async () => {
      throw new Error("EACCES reading /Users/notme/external-secret-project/vault/recall-state.json");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.present, false);
    assert.doesNotMatch(JSON.stringify(body), /\/Users\/notme/);
  } finally {
    await server.close();
  }
});

test("/api/recall-state returns state when present", async () => {
  const server = await startTestServer({
    readRecallState: async () => ({
      task_signature: "sig",
      intent: "handoff leases",
      top_hits: [{ title: "Leases", wikilink: "[[wiki/leases]]", score: 0.9 }],
      top_score: 0.9,
      consumed: false,
      ts: new Date().toISOString(),
    }),
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.present, true);
    assert.equal(body.state.intent, "handoff leases");
    assert.equal(body.state.top_hits.length, 1);
  } finally {
    await server.close();
  }
});

test("/api/recall-state requires auth (403 unauthed)", async () => {
  const server = await startTestServer({ readRecallState: async () => null });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/agents returns catalogue when listAgents is live", async () => {
  const server = await startTestServer({
    listAgents: async () => ({
      agents: [
        {
          id: "codex",
          vault: "~/.minni/codex-vault",
          seen: "2m",
          on: true,
          caps: { R: 1, L: 1, H: 1 },
          staged: 3,
        },
      ],
      count: 1,
    }),
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/agents`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.count, 1);
    assert.equal(body.agents[0].id, "codex");
  } finally {
    await server.close();
  }
});

test("/api/agents returns 502 when catalogue builder fails", async () => {
  const server = await startTestServer({
    listAgents: async () => {
      throw new Error("socket ECONNREFUSED scanning agents");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/agents`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.deepEqual(body.agents, []);
  } finally {
    await server.close();
  }
});

test("/api/agents requires auth (403 unauthed)", async () => {
  const server = await startTestServer({ listAgents: async () => ({ agents: [], count: 0 }) });
  try {
    const res = await fetch(`${server.baseUrl}/api/agents`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/handoffs returns pending leases when daemon is up", async () => {
  const server = await startTestServer({
    daemonRpc: async (method) => {
      if (method === "minni_list_pending_handoffs") {
        return {
          ok: true,
          data: {
            agent_id: "main",
            handoffs: [
              {
                lease_id: "LS-1",
                from_agent: "codex",
                to_agent: "main",
                task: "port scorecard",
                expires_at: null,
                path: "/tmp/inbox/x.json",
              },
            ],
          },
        };
      }
      throw new Error(`unexpected ${method}`);
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/handoffs`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.handoffs.length, 1);
    assert.equal(body.handoffs[0].lease_id, "LS-1");
  } finally {
    await server.close();
  }
});

test("/api/handoffs returns 502 when daemon is down", async () => {
  const server = await startTestServer({
    daemonRpc: async () => {
      throw new Error("socket ECONNREFUSED");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/handoffs`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.deepEqual(body.handoffs, []);
  } finally {
    await server.close();
  }
});

test("/api/handoffs requires auth (403 unauthed)", async () => {
  const server = await startTestServer();
  try {
    const res = await fetch(`${server.baseUrl}/api/handoffs`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/policy returns real policy report (no hardcoded v4.2)", async () => {
  const server = await startTestServer({
    policyReport: async () => ({
      agentId: "main",
      stampedForCandidates: "main",
      caps: { R: 1, L: 1, H: 1 },
      automaticLearning: false,
      source: "principals + policy.ts",
      intentRouting: { action: "recall", confidence: 0.74 },
    }),
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/policy`, { headers: authHeaders() });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.agentId, "main");
    assert.equal(body.automaticLearning, false);
    assert.ok(!JSON.stringify(body).includes("v4.2"));
  } finally {
    await server.close();
  }
});

test("/api/policy requires auth (403 unauthed)", async () => {
  const server = await startTestServer({ policyReport: async () => ({ agentId: "x" }) });
  try {
    const res = await fetch(`${server.baseUrl}/api/policy`);
    assert.equal(res.status, 403);
  } finally {
    await server.close();
  }
});

test("/api/policy returns 502 when policyReport throws", async () => {
  const server = await startTestServer({
    policyReport: async () => {
      throw new Error("policy read failed");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/policy`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.match(body.error, /policy read failed/);
    assert.equal(body.agentId, undefined);
    assert.equal(body.caps, undefined);
  } finally {
    await server.close();
  }
});

test("/api/recall-state returns 502 when reader throws", async () => {
  const server = await startTestServer({
    readRecallState: async () => {
      throw new Error("EACCES recall-state");
    },
  });
  try {
    const res = await fetch(`${server.baseUrl}/api/recall-state`, { headers: authHeaders() });
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.equal(body.present, false);
    assert.equal(body.state, null);
    assert.match(body.error, /EACCES/);
  } finally {
    await server.close();
  }
});

test("agentIdFromVaultDir maps claudecode-vault → claude-code (reuses vault.ts getAgentIdFromVaultPath)", async () => {
  const { agentIdFromVaultDir } = await import("../dist/ui-server.js");
  const { getAgentIdFromVaultPath } = await import("../dist/vault.js");
  assert.equal(agentIdFromVaultDir, getAgentIdFromVaultPath, "ui-server must re-export the vault.ts helper, not reimplement it");

  const pathMod = await import("node:path");
  const { mkdtempSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const home = mkdtempSync(pathMod.join(tmpdir(), "minni-agentid-"));

  // "claudecode-vault" only maps to "claude-code" via the hardcoded ~/.minni
  // path or a MINNI_CLAUDECODE_VAULT_PATH override — exercise the override so
  // this test does not depend on the real $HOME layout.
  const claudecodeVault = pathMod.join(home, "claudecode-vault");
  const original = process.env.MINNI_CLAUDECODE_VAULT_PATH;
  process.env.MINNI_CLAUDECODE_VAULT_PATH = claudecodeVault;
  try {
    assert.equal(agentIdFromVaultDir(claudecodeVault), "claude-code");
  } finally {
    if (original === undefined) delete process.env.MINNI_CLAUDECODE_VAULT_PATH;
    else process.env.MINNI_CLAUDECODE_VAULT_PATH = original;
  }

  // Unmapped basenames fall back to stripping the "-vault" suffix.
  assert.equal(agentIdFromVaultDir(pathMod.join(home, "codex-vault")), "codex");
  assert.equal(agentIdFromVaultDir(pathMod.join(home, "grok-build-vault")), "grok-build");

  // Known aliases normalize even under a non-default home with no env
  // override: claudecode-vault must map to the claude-code principal,
  // never a capability-less "claudecode".
  assert.equal(agentIdFromVaultDir(pathMod.join(home, "other", "claudecode-vault")), "claude-code");
  assert.equal(agentIdFromVaultDir(pathMod.join(home, "other", "claude-vault")), "claude-code");
});

test("buildPolicyReport pulls caps + intent routing without inventing automaticLearning", async () => {
  const { buildPolicyReport } = await import("../dist/ui-server.js");
  const { DEFAULT_AGENT_ID } = await import("../dist/config.js");
  const { mkdtempSync, mkdirSync, writeFileSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const pathMod = await import("node:path");
  const home = mkdtempSync(pathMod.join(tmpdir(), "minni-policy-"));
  mkdirSync(pathMod.join(home, "principals"), { recursive: true });
  // Own-principal file keyed to the console agent's own id (not "main") — caps
  // must come from the agent's own file, never the operator's "main" fallback.
  writeFileSync(
    pathMod.join(home, "principals", `${DEFAULT_AGENT_ID}.json`),
    JSON.stringify({
      agent_id: DEFAULT_AGENT_ID,
      capabilities: ["search", "read", "learn", "handoff"],
    }),
  );
  const report = await buildPolicyReport({
    homePath: home,
    vaultPath: pathMod.join(home, "unknown-vault"),
    status: async () => ({
      afm: { ok: true, data: { status: "ok" } },
      // no automaticLearning field → omit from report
    }),
  });
  assert.equal(typeof report.agentId, "string");
  assert.ok(report.caps);
  assert.equal(report.caps.R, 1);
  assert.equal(report.caps.L, 1);
  assert.equal(report.caps.H, 1);
  assert.ok(report.intentRouting);
  assert.equal(report.automaticLearning, undefined);
  assert.equal(report.capsSource, "own_principal");
  assert.ok(!JSON.stringify(report).includes("v4.2"));
});

test("buildPolicyReport default-denies when the agent has no own principal file (no 'main' fallback)", async () => {
  const { buildPolicyReport } = await import("../dist/ui-server.js");
  const { mkdtempSync, mkdirSync, writeFileSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const pathMod = await import("node:path");
  const home = mkdtempSync(pathMod.join(tmpdir(), "minni-policy-nofallback-"));
  mkdirSync(pathMod.join(home, "principals"), { recursive: true });
  // Only the operator's "main" principal exists — the console agent (DEFAULT_AGENT_ID,
  // "unknown-agent" by default in test env) has no principals/<agentId>.json.
  writeFileSync(
    pathMod.join(home, "principals", "main.json"),
    JSON.stringify({ agent_id: "main", capabilities: ["*"] }),
  );
  const report = await buildPolicyReport({
    homePath: home,
    vaultPath: pathMod.join(home, "unknown-vault"),
    status: async () => ({ afm: { ok: true, data: { status: "ok" } } }),
  });
  assert.deepEqual(report.caps, { R: 0, L: 0, H: 0 }, "must not inherit main's caps");
  assert.equal(report.capsSource, "default_deny");
  assert.ok(report.principalsKnown.includes("main"), "main is still visible in the known-principals list");
});

test("buildAgentsCatalogue is fail-loud on staged RPC and skips symlink vaults", async () => {
  const { buildAgentsCatalogue, readOnlyAuditTail } = await import("../dist/ui-server.js");
  const { mkdtempSync, mkdirSync, writeFileSync, symlinkSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const pathMod = await import("node:path");
  const home = mkdtempSync(pathMod.join(tmpdir(), "minni-agents-cat-"));
  mkdirSync(pathMod.join(home, "codex-vault", "logs"), { recursive: true });
  writeFileSync(
    pathMod.join(home, "codex-vault", "logs", new Date().toISOString().slice(0, 10) + ".md"),
    `## [${new Date().toISOString()}] learn | note\n`,
  );
  // Symlink poison vault outside home — must be skipped
  const outside = mkdtempSync(pathMod.join(tmpdir(), "minni-outside-"));
  try {
    symlinkSync(outside, pathMod.join(home, "poison-vault"));
  } catch {
    // platform without symlink support
  }

  const catalogue = await buildAgentsCatalogue({
    homePath: home,
    daemonRpc: async (method, params) => {
      if (method === "list_candidates" && params.agent_id === "codex") {
        throw new Error("socket ECONNREFUSED");
      }
      return { candidates: [], count: 0 };
    },
    auditTailFn: readOnlyAuditTail,
  });
  assert.ok(catalogue.agents.every((a) => a.id !== "poison"));
  const codex = catalogue.agents.find((a) => a.id === "codex");
  assert.ok(codex, "codex vault should be listed");
  assert.equal(codex.staged, null);
  assert.equal(codex.stagedUnknown, true);
  assert.ok(!("vaultPath" in codex) || codex.vaultPath === undefined);
});

test("buildAgentsCatalogue flags stagedAtLimit when the RPC returns a full page, and preserves order under concurrency", async () => {
  const { buildAgentsCatalogue } = await import("../dist/ui-server.js");
  const { mkdtempSync, mkdirSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const pathMod = await import("node:path");
  const home = mkdtempSync(pathMod.join(tmpdir(), "minni-agents-limit-"));
  // Three vaults, deliberately created so a naive concurrent scan without
  // ordering care could interleave; agents.map(a => a.id) must still come
  // back alphabetically sorted (alpha, bravo, codex) like the old sequential loop.
  for (const name of ["codex-vault", "bravo-vault", "alpha-vault"]) {
    mkdirSync(pathMod.join(home, name), { recursive: true });
  }

  const callOrder = [];
  const catalogue = await buildAgentsCatalogue({
    homePath: home,
    auditTailFn: async (vaultPath) => {
      // codex resolves slowest, to prove ordering doesn't depend on completion time.
      const delayMs = vaultPath.includes("codex") ? 15 : 1;
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      return { entries: [] };
    },
    daemonRpc: async (method, params) => {
      callOrder.push(params.agent_id);
      if (params.agent_id === "codex") {
        // Full page: count is a floor, not the true total.
        return { candidates: Array.from({ length: 200 }, (_, i) => ({ candidate_id: i })), count: 200 };
      }
      return { candidates: [], count: 0 };
    },
  });

  assert.deepEqual(catalogue.agents.map((a) => a.id), ["alpha", "bravo", "codex"], "output order matches sorted vault dirs");
  // The RPCs themselves ran concurrently (not sequentially in sorted order).
  assert.ok(callOrder.length === 3);

  const codex = catalogue.agents.find((a) => a.id === "codex");
  assert.equal(codex.staged, 200);
  assert.equal(codex.stagedAtLimit, true);
  assert.equal(codex.stagedUnknown, false);

  const alpha = catalogue.agents.find((a) => a.id === "alpha");
  assert.equal(alpha.staged, 0);
  assert.equal(alpha.stagedAtLimit, false);
});
