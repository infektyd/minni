// P0 capability-freeze: golden contract tests for every AFM call shape.
//
// These tests freeze the exact wire payloads (bridge chat completions + native
// helper envelopes) and the tolerant parser behaviors that downstream code
// depends on, so the provider-protocol refactor (P2) can be verified
// byte-identical against them. They run entirely against fakes: a loopback
// HTTP server for the bridge and an executable stub for the native helper.
//
// Enumerated call sites frozen here:
//   - task.ts buildAfmChatPayload (task + outcome purposes)
//   - task.ts callAfmPrepareTask (bridge chat URL, bridge non-chat URL, native helper op)
//   - task.ts normalizeAfmResponse (via callAfmPrepareTask responses)
//   - team-harvest.ts defaultCallAfm wire shape + parseAfmResponse contract
//   - afm.ts callAfmJson native helper envelope {schema_version, operation, input}

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { callAfmJson } from "../dist/afm.js";
import { buildAfmChatPayload, callAfmPrepareTask } from "../dist/task.js";
import { harvestEvidence } from "../dist/team-harvest.js";

const AFM_ENV_KEYS = [
  "MINNI_AFM_NATIVE_HELPER",
  "MINNI_AFM_ADAPTER_PATH",
  "MINNI_AFM_ADAPTER_ID",
  "MINNI_AFM_ALLOWED_TARGETS",
  "MINNI_AFM_PROVIDER_MODE",
];

function snapshotEnv() {
  const saved = {};
  for (const key of AFM_ENV_KEYS) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  return () => {
    for (const key of AFM_ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  };
}

async function withBridgeServer(respond, run) {
  const captured = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      captured.push({ url: req.url, body: JSON.parse(body) });
      const { status = 200, json = {} } = respond(captured.length);
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(json));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  try {
    return await run(`http://127.0.0.1:${port}`, captured);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function withNativeHelper(responseJson, run) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-golden-native-"));
  const captureFile = path.join(root, "capture.json");
  const helper = path.join(root, "helper.mjs");
  await writeFile(
    helper,
    [
      "#!/usr/bin/env node",
      'import { readFileSync, writeFileSync } from "node:fs";',
      'const input = readFileSync(0, "utf8");',
      "writeFileSync(process.env.MINNI_GOLDEN_CAPTURE_FILE, input);",
      `process.stdout.write(${JSON.stringify(JSON.stringify(responseJson))});`,
    ].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);
  const previousCapture = process.env.MINNI_GOLDEN_CAPTURE_FILE;
  process.env.MINNI_GOLDEN_CAPTURE_FILE = captureFile;
  try {
    return await run(helper, async () => JSON.parse(await readFile(captureFile, "utf8")));
  } finally {
    if (previousCapture === undefined) delete process.env.MINNI_GOLDEN_CAPTURE_FILE;
    else process.env.MINNI_GOLDEN_CAPTURE_FILE = previousCapture;
    await rm(root, { recursive: true, force: true });
  }
}

// --- buildAfmChatPayload goldens -------------------------------------------

const TASK_PAYLOAD = {
  task: "implement the AFM provider protocol",
  budgetTokens: 4000,
  profile: "standard",
  intent: "implement",
  constraints: ["Default automatic behavior is recall-only."],
  currentState: ["Daemon recall responded."],
  relevantSources: [
    {
      wikilink: "[[wiki/sessions/20260425-backend-handoff]]",
      snippet: "Plugin backend is stable.",
      score: 96,
      reasons: ["lexical match", "fresh handoff"],
      privacyLevel: "safe",
    },
    {
      wikilink: "[[wiki/private/secret-note]]",
      snippet: "Private content.",
      score: 90,
      reasons: ["lexical match"],
      privacyLevel: "private",
    },
  ],
  daemonLead: "### daemon.md lead line",
  provider: {
    provider: "bridge",
    mode: "bridge",
    backend: undefined,
    availability: undefined,
    adapterConfigured: false,
    fallbackUsed: false,
  },
  model: "apple-foundation-models",
};

test("golden: buildAfmChatPayload freezes the task-purpose chat body", () => {
  const restore = snapshotEnv();
  try {
    const body = buildAfmChatPayload(TASK_PAYLOAD);
    assert.deepEqual(body, {
      model: "apple-foundation-models",
      temperature: 0,
      max_tokens: 220,
      messages: [
        {
          role: "user",
          content: [
            "Return compact JSON only for Codex task prep.",
            "Keys: brief, recommendedNextActions, risks.",
            "Interpret wiring/config/providers as Minni software integration work, never physical or electrical wiring.",
            "No secrets, no raw private logs.",
            "AFM provider: bridge backend= availability= adapterConfigured=false fallbackUsed=false",
            "Task: implement the AFM provider protocol",
            "Intent: implement",
            "Profile: standard",
            "Budget: 4000 tokens",
            "Constraints: Default automatic behavior is recall-only.",
            "State: Daemon recall responded.",
            "Sources: [[wiki/sessions/20260425-backend-handoff]] score=96 reasons=lexical match, fresh handoff: Plugin backend is stable.",
            "Daemon: ### daemon.md lead line",
          ].join("\n"),
        },
      ],
    });
  } finally {
    restore();
  }
});

test("golden: buildAfmChatPayload freezes the outcome-purpose chat body", () => {
  const restore = snapshotEnv();
  try {
    const body = buildAfmChatPayload({
      purpose: "outcome",
      task: "ship the provider protocol",
      summary: "Implemented under /Users/hans/Projects/Minni with tests.",
      profile: "compact",
      budgetTokens: 1500,
      changedFiles: ["plugins/minni/src/providers.ts"],
      verification: ["npm test"],
      outcomeDraft: { learnCandidates: ["x"], logOnly: [], expires: [], doNotStore: [] },
      provider: { provider: "bridge", mode: "bridge", adapterConfigured: false, fallbackUsed: false },
      model: "apple-foundation-models",
    });
    assert.deepEqual(body, {
      model: "apple-foundation-models",
      temperature: 0,
      max_tokens: 140,
      messages: [
        {
          role: "user",
          content: [
            "Return compact JSON only for Codex outcome prep.",
            "Keys: outcomeDraft with learnCandidates, logOnly, expires, doNotStore.",
            "Buckets must be mutually exclusive; put uncertain or sensitive items in the most restrictive applicable bucket.",
            "No secrets, no raw private logs, no local absolute paths.",
            "AFM provider: bridge backend= availability= adapterConfigured=false fallbackUsed=false",
            "Task: ship the provider protocol",
            "Summary: Implemented under [local-path] with tests.",
            "Profile: compact",
            "Changed files: plugins/minni/src/providers.ts",
            "Verification: npm test",
            'Existing draft: {"learnCandidates":["x"],"logOnly":[],"expires":[],"doNotStore":[]}',
          ].join("\n"),
        },
      ],
    });
  } finally {
    restore();
  }
});

// --- callAfmPrepareTask wire goldens ----------------------------------------

test("golden: callAfmPrepareTask posts buildAfmChatPayload to chat/completions URLs", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ json: { choices: [{ message: { content: '{"brief":"frozen"}' } }] } }),
      async (base, captured) => {
        const result = await callAfmPrepareTask(`${base}/v1/chat/completions`, TASK_PAYLOAD);
        assert.equal(result.ok, true);
        assert.equal(captured.length, 1);
        assert.equal(captured[0].url, "/v1/chat/completions");
        assert.deepEqual(captured[0].body, buildAfmChatPayload(TASK_PAYLOAD));
        assert.deepEqual(result.data, { brief: "frozen" });
      },
    );
  } finally {
    restore();
  }
});

test("golden: callAfmPrepareTask posts the raw payload to non-chat URLs", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ json: { brief: "direct", constraints: ["keep it local"], ok: true } }),
      async (base, captured) => {
        const result = await callAfmPrepareTask(`${base}/prepare_task`, TASK_PAYLOAD);
        assert.equal(result.ok, true);
        assert.deepEqual(captured[0].body, JSON.parse(JSON.stringify(TASK_PAYLOAD)));
        assert.deepEqual(result.data, { brief: "direct", constraints: ["keep it local"] });
      },
    );
  } finally {
    restore();
  }
});

test("golden: callAfmPrepareTask native sends the helper envelope untransformed", async () => {
  const restore = snapshotEnv();
  try {
    await withNativeHelper(
      { ok: true, data: { brief: "native brief" } },
      async (helper, readCapture) => {
        process.env.MINNI_AFM_NATIVE_HELPER = helper;
        const payload = {
          ...TASK_PAYLOAD,
          provider: { provider: "native", mode: "native" },
        };
        const result = await callAfmPrepareTask("http://127.0.0.1:1/v1/chat/completions", payload);
        assert.equal(result.ok, true);
        assert.deepEqual(result.data, { brief: "native brief" });
        const envelope = await readCapture();
        assert.equal(envelope.schema_version, 1);
        assert.equal(envelope.operation, "prepare_task");
        // Native transport sends the call-site payload verbatim (no chat re-shaping).
        assert.deepEqual(envelope.input, JSON.parse(JSON.stringify(payload)));
      },
    );
  } finally {
    restore();
  }
});

test("golden: callAfmPrepareTask native uses prepare_outcome for outcome purpose", async () => {
  const restore = snapshotEnv();
  try {
    await withNativeHelper(
      { ok: true, data: { outcomeDraft: { learnCandidates: ["a"], logOnly: [], expires: [], doNotStore: [] } } },
      async (helper, readCapture) => {
        process.env.MINNI_AFM_NATIVE_HELPER = helper;
        const payload = {
          purpose: "outcome",
          task: "t",
          summary: "s",
          provider: { provider: "native", mode: "native" },
        };
        const result = await callAfmPrepareTask("http://127.0.0.1:1/v1/chat/completions", payload);
        assert.equal(result.ok, true);
        const envelope = await readCapture();
        assert.equal(envelope.operation, "prepare_outcome");
        assert.deepEqual(envelope.input, payload);
        assert.deepEqual(result.data, {
          outcomeDraft: { learnCandidates: ["a"], logOnly: [], expires: [], doNotStore: [] },
        });
      },
    );
  } finally {
    restore();
  }
});

// --- normalizeAfmResponse goldens (via callAfmPrepareTask) -------------------

test("golden: normalizeAfmResponse parses fenced JSON inside chat content", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({
        json: {
          choices: [
            {
              message: {
                content:
                  '```json\n{"brief":"B","intent":"implement","risks":["r1",2],"recommendedNextActions":["a"],"outcomeDraft":{"learnCandidates":["l"],"logOnly":null}}\n```',
              },
            },
          ],
        },
      }),
      async (base) => {
        const result = await callAfmPrepareTask(`${base}/v1/chat/completions`, TASK_PAYLOAD);
        assert.equal(result.ok, true);
        assert.deepEqual(result.data, {
          brief: "B",
          intent: "implement",
          recommendedNextActions: ["a"],
          risks: ["r1"],
          outcomeDraft: { learnCandidates: ["l"], logOnly: [], expires: [], doNotStore: [] },
        });
      },
    );
  } finally {
    restore();
  }
});

test("golden: normalizeAfmResponse degrades plain chat text to brief", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ json: { choices: [{ message: { content: "  just prose, no JSON here  " } }] } }),
      async (base) => {
        const result = await callAfmPrepareTask(`${base}/v1/chat/completions`, TASK_PAYLOAD);
        assert.equal(result.ok, true);
        assert.deepEqual(result.data, { brief: "just prose, no JSON here" });
      },
    );
  } finally {
    restore();
  }
});

test("golden: normalizeAfmResponse returns empty packet for empty content", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ json: { choices: [{ message: { content: "" } }] } }),
      async (base) => {
        const result = await callAfmPrepareTask(`${base}/v1/chat/completions`, TASK_PAYLOAD);
        assert.equal(result.ok, true);
        assert.deepEqual(result.data, {});
      },
    );
  } finally {
    restore();
  }
});

test("golden: callAfmPrepareTask surfaces HTTP errors with empty normalized data", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ status: 500, json: { error: "boom" } }),
      async (base) => {
        const result = await callAfmPrepareTask(`${base}/v1/chat/completions`, TASK_PAYLOAD);
        assert.equal(result.ok, false);
        assert.equal(result.error, "HTTP 500");
        assert.deepEqual(result.data, {});
      },
    );
  } finally {
    restore();
  }
});

// --- team-harvest goldens ----------------------------------------------------

const HARVEST_REPORT = {
  agentId: "builder-1",
  status: "done",
  summary: "Implemented retry with backoff in the daemon client.",
  evidence: ["unit test added"],
  changedFiles: ["engine/minnid_client.py"],
  verification: ["pytest -q"],
  blockers: [],
};

function harvestDeps(captureWrites) {
  return {
    writeInbox: async (_vaultPath, slugBase, payload) => {
      captureWrites.push({ slugBase, payload });
      return { filePath: `/tmp/inbox/${slugBase}.json`, slug: slugBase };
    },
    audit: async () => "/tmp/vault/logs/today.md",
  };
}

test("golden: team-harvest wire shape is a 120-token system+user chat call", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ json: { choices: [{ message: { content: "LEARNING: Retry with backoff fixed flaky daemon calls." } }] } }),
      async (base, captured) => {
        const writes = [];
        const learnings = await harvestEvidence(
          {
            task: "harden daemon client under /Users/hans/Projects/Minni",
            vaultPath: "/tmp/vault",
            reports: [HARVEST_REPORT],
            afmUrl: `${base}/v1/chat/completions`,
            afmModel: "apple-foundation-models",
          },
          harvestDeps(writes),
        );
        assert.equal(captured.length, 1);
        const body = captured[0].body;
        assert.deepEqual(Object.keys(body), ["model", "temperature", "max_tokens", "messages"]);
        assert.equal(body.model, "apple-foundation-models");
        assert.equal(body.temperature, 0);
        assert.equal(body.max_tokens, 120);
        assert.deepEqual(
          body.messages.map((m) => m.role),
          ["system", "user"],
        );
        assert.equal(
          body.messages[0].content,
          [
            "You distill one durable learning from a team agent's evidence report.",
            "Output exactly one line in one of these two forms, nothing else:",
            "  LEARNING: <single sentence the operator could re-use next session>",
            "  SKIP",
            "Use SKIP when the report holds nothing reusable beyond this task.",
            "No preamble, no chain-of-thought, no markdown, no quotes, no JSON.",
          ].join("\n"),
        );
        assert.equal(
          body.messages[1].content,
          [
            "Task: harden daemon client under [local-path]",
            "Agent: builder-1",
            "Status: done",
            "Summary: Implemented retry with backoff in the daemon client.",
            "Evidence: unit test added",
            "Changed files: engine/minnid_client.py",
            "Verification: pytest -q",
            "Blockers: ",
          ].join("\n"),
        );
        assert.equal(learnings.length, 1);
        assert.equal(learnings[0].source, "afm");
        assert.equal(learnings[0].candidateText, "Retry with backoff fixed flaky daemon calls.");
      },
    );
  } finally {
    restore();
  }
});

test("golden: team-harvest parseAfmResponse contract (LEARNING/SKIP/empty/off-contract)", async () => {
  const cases = [
    { raw: "LEARNING: Always pin the model name.", source: "afm", candidateText: "Always pin the model name." },
    { raw: "learning:   spaced and lowercased works too", source: "afm", candidateText: "spaced and lowercased works too" },
    { raw: "SKIP", source: "skipped", reason: "AFM returned SKIP" },
    { raw: "skip  ", source: "skipped", reason: "AFM returned SKIP" },
    { raw: "   ", source: "skipped", reason: "AFM returned empty response" },
    { raw: "Here is a learning: nope", source: "skipped", reason: "no LEARNING: prefix" },
  ];
  for (const expected of cases) {
    const writes = [];
    const learnings = await harvestEvidence(
      {
        task: "contract check",
        vaultPath: "/tmp/vault",
        reports: [HARVEST_REPORT],
      },
      { callAfm: async () => expected.raw, ...harvestDeps(writes) },
    );
    assert.equal(learnings.length, 1, expected.raw);
    assert.equal(learnings[0].source, expected.source, expected.raw);
    if (expected.candidateText) assert.equal(learnings[0].candidateText, expected.candidateText);
    if (expected.reason) assert.equal(learnings[0].reason, expected.reason);
  }
});

test("golden: team-harvest maps HTTP and timeout errors to skip reasons", async () => {
  const restore = snapshotEnv();
  try {
    await withBridgeServer(
      () => ({ status: 503, json: {} }),
      async (base) => {
        const writes = [];
        const learnings = await harvestEvidence(
          {
            task: "contract check",
            vaultPath: "/tmp/vault",
            reports: [HARVEST_REPORT],
            afmUrl: `${base}/v1/chat/completions`,
          },
          harvestDeps(writes),
        );
        assert.equal(learnings[0].source, "skipped");
        assert.equal(learnings[0].reason, "AFM harvest HTTP 503");
      },
    );
  } finally {
    restore();
  }
});

test("golden: team-harvest maps chain timeouts to the timed-out skip reason", async () => {
  const restore = snapshotEnv();
  try {
    const writes = [];
    const learnings = await harvestEvidence(
      {
        task: "contract check",
        vaultPath: "/tmp/vault",
        reports: [HARVEST_REPORT],
      },
      {
        transport: async () => ({ ok: false, error: "AFM request timed out" }),
        ...harvestDeps(writes),
      },
    );
    assert.equal(learnings[0].source, "skipped");
    assert.equal(learnings[0].reason, "AFM harvest request timed out");
    assert.equal(writes.length, 0);
  } finally {
    restore();
  }
});

test("golden: team-harvest surfaces G13 denials as the structured skip reason", async () => {
  const restore = snapshotEnv();
  try {
    const writes = [];
    const learnings = await harvestEvidence(
      {
        task: "contract check",
        vaultPath: "/tmp/vault",
        reports: [HARVEST_REPORT],
        afmUrl: "https://evil.example.com/v1/chat/completions",
      },
      harvestDeps(writes),
    );
    assert.equal(learnings[0].source, "skipped");
    assert.match(
      learnings[0].reason ?? "",
      /^afm_target_denied: target is not loopback-only/,
      "operator must see the structured denial, not a generic failure",
    );
    assert.doesNotMatch(learnings[0].reason ?? "", /evil\.example\.com/);
    assert.equal(writes.length, 0);
  } finally {
    restore();
  }
});

test("golden: team-harvest surfaces chain errors verbatim (generic branch)", async () => {
  const restore = snapshotEnv();
  try {
    const writes = [];
    const learnings = await harvestEvidence(
      {
        task: "contract check",
        vaultPath: "/tmp/vault",
        reports: [HARVEST_REPORT],
      },
      {
        transport: async () => ({ ok: false, error: "no provider eligible for operation extraction" }),
        ...harvestDeps(writes),
      },
    );
    assert.equal(learnings[0].source, "skipped");
    assert.equal(learnings[0].reason, "no provider eligible for operation extraction");
  } finally {
    restore();
  }
});

// --- callAfmJson native envelope golden --------------------------------------

test("golden: callAfmJson native helper envelope is {schema_version, operation, input}", async () => {
  const restore = snapshotEnv();
  try {
    await withNativeHelper(
      { ok: true, data: { answer: "ok" } },
      async (helper, readCapture) => {
        const result = await callAfmJson(
          "http://127.0.0.1:1/v1/chat/completions",
          { query: "what changed" },
          { mode: "native", nativeHelperPath: helper, operation: "hyde_generation" },
        );
        assert.equal(result.ok, true);
        assert.deepEqual(result.data, { answer: "ok" });
        const envelope = await readCapture();
        assert.deepEqual(envelope, {
          schema_version: 1,
          operation: "hyde_generation",
          input: { query: "what changed" },
        });
      },
    );
  } finally {
    restore();
  }
});

test("golden: callAfmJson defaults the native operation to json", async () => {
  const restore = snapshotEnv();
  try {
    await withNativeHelper(
      { ok: true, data: {} },
      async (helper, readCapture) => {
        const result = await callAfmJson(
          "http://127.0.0.1:1/ignored",
          { a: 1 },
          { mode: "native", nativeHelperPath: helper },
        );
        assert.equal(result.ok, true);
        const envelope = await readCapture();
        assert.equal(envelope.operation, "json");
        assert.deepEqual(envelope.input, { a: 1 });
      },
    );
  } finally {
    restore();
  }
});
