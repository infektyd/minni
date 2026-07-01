import assert from "node:assert/strict";
import { createServer } from "node:http";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { buildAfmChatPayload, prepareOutcome, callAfmPrepareTask, prepareTask } from "../dist/task.js";
import { ProviderChain } from "../dist/providers.js";

const vaultMatch = {
  notePath: "/tmp/vault/wiki/sessions/backend-handoff.md",
  relativePath: "wiki/sessions/backend-handoff.md",
  wikilink: "[[wiki/sessions/backend-handoff]]",
  title: "Backend handoff",
  snippet: "Plugin backend is stable; frontend should wait until retrieval ranking is deeper.",
  score: 77,
};

function vaultSource(overrides = {}) {
  return {
    notePath: "/tmp/vault/wiki/sessions/20260425-backend-handoff.md",
    relativePath: "wiki/sessions/20260425-backend-handoff.md",
    wikilink: "[[wiki/sessions/20260425-backend-handoff]]",
    title: "Backend handoff",
    snippet: "Plugin backend is stable; frontend should wait until retrieval ranking is deeper.",
    score: 20,
    ...overrides,
  };
}

test("prepareTask builds a compact deterministic Codex task packet", async () => {
  const audits = [];
  const packet = await prepareTask(
    {
      task: "implement AFM context-light prepare task without frontend",
      budgetTokens: 30000,
      vaultPath: "/tmp/vault",
      useAfm: false,
    },
    {
      searchVault: async () => [vaultMatch],
      recall: async () => ({
        ok: true,
        data: {
          results: "### daemon.md\nUse vault-first context packs before daemon recall.",
          agent_id: "codex",
        },
      }),
      audit: async (_vaultPath, entry) => {
        audits.push(entry);
        return "/tmp/vault/logs/today.md";
      },
    },
  );

  assert.equal(packet.mode, "deterministic");
  assert.equal(packet.profile, "standard");
  assert.equal(packet.budgetTokens, 30000);
  assert.equal(packet.budget.tokens, 30000);
  assert.equal(packet.intent, "implement");
  assert.equal(packet.relevantSources[0].wikilink, "[[wiki/sessions/backend-handoff]]");
  assert.ok(packet.relevantSources[0].reasons.includes("lexical match"));
  assert.match(packet.constraints.join("\n"), /Do not run AFM extraction/);
  assert.match(packet.contextMarkdown, /Minni Task Packet/);
  assert.equal(packet.afm.used, false);
  assert.equal(audits[0].tool, "minni_prepare_task");
});

test("prepareTask ranks fresh handoff notes above older sessions and explains why", async () => {
  // Use a dynamically-recent date so the "fresh" (<=30d) assertion does not rot over time.
  const fresh = new Date(Date.now() - 5 * 86_400_000);
  const freshYmd = `${fresh.getUTCFullYear()}${String(fresh.getUTCMonth() + 1).padStart(2, "0")}${String(fresh.getUTCDate()).padStart(2, "0")}`;
  const freshSlug = `${freshYmd}-codex-minni-plugin-backend-handoff-clean`;
  const packet = await prepareTask(
    {
      task: "what is the latest backend handoff before frontend dashboard work",
      vaultPath: "/tmp/vault",
    },
    {
      searchVault: async () => [
        vaultSource({
          relativePath: "wiki/sessions/20240101-old-frontend-note.md",
          wikilink: "[[wiki/sessions/20240101-old-frontend-note]]",
          title: "Old frontend note",
          snippet: "Frontend dashboard can be explored later.",
          score: 40,
        }),
        vaultSource({
          relativePath: `wiki/sessions/${freshSlug}.md`,
          wikilink: `[[wiki/sessions/${freshSlug}]]`,
          title: "Codex Minni plugin backend handoff clean",
          snippet: "Frontend/dashboard work should wait until the plugin backend stabilizes further.",
          score: 18,
        }),
      ],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.match(packet.relevantSources[0].relativePath, new RegExp(freshSlug));
  assert.equal(packet.relevantSources[0].freshness, "fresh");
  assert.equal(packet.relevantSources[0].authority, "handoff");
  assert.ok(packet.relevantSources[0].reasons.includes("fresh handoff"));
  assert.match(packet.constraints.join("\n"), /Frontend\/dashboard work should wait/);
});

test("prepareTask profile budgets shape source counts and snippets", async () => {
  const manySources = Array.from({ length: 8 }, (_, index) =>
    vaultSource({
      relativePath: `wiki/sessions/2026042${index}-source-${index}.md`,
      wikilink: `[[wiki/sessions/2026042${index}-source-${index}]]`,
      title: `Source ${index}`,
      snippet: `Source ${index} `.repeat(80),
      score: 15 + index,
    }),
  );
  const deps = {
    searchVault: async () => manySources,
    recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
    audit: async () => "/tmp/vault/logs/today.md",
  };

  const compact = await prepareTask({ task: "rank context", profile: "compact", vaultPath: "/tmp/vault" }, deps);
  const deep = await prepareTask({ task: "rank context", profile: "deep", vaultPath: "/tmp/vault" }, deps);

  assert.equal(compact.budgetTokens, 1500);
  assert.equal(deep.budgetTokens, 12000);
  assert.ok(compact.relevantSources.length < deep.relevantSources.length);
  assert.ok(compact.relevantSources[0].snippet.length < deep.relevantSources[0].snippet.length);
});

test("AFM payload omits blocked and private sources while keeping safe source reasons", () => {
  const payload = buildAfmChatPayload({
    task: "prepare public-safe context",
    budgetTokens: 1500,
    model: "apple-foundation-models",
    profile: "compact",
    relevantSources: [
      {
        wikilink: "[[safe]]",
        snippet: "Safe public context.",
        score: 10,
        privacyLevel: "safe",
        reasons: ["lexical match"],
      },
      {
        wikilink: "[[private]]",
        snippet: "Private session token should not cross the AFM boundary.",
        score: 99,
        privacyLevel: "private",
        reasons: ["private signal"],
      },
      {
        wikilink: "[[blocked]]",
        snippet: "api key secret raw log",
        score: 100,
        privacyLevel: "blocked",
        reasons: ["blocked sensitive content"],
      },
    ],
  });
  const body = JSON.stringify(payload);

  assert.match(body, /Safe public context/);
  assert.match(body, /lexical match/);
  assert.doesNotMatch(body, /Private session token/);
  assert.doesNotMatch(body, /api key secret/);
});

test("AFM payload includes native provider metadata without adapter paths", () => {
  const payload = buildAfmChatPayload({
    task: "prepare native provider context",
    profile: "compact",
    provider: {
      mode: "native",
      backend: "apple-foundation-models",
      availability: "available",
      adapterConfigured: true,
      adapterPath: "/Users/alice/private/extractor.fmadapter",
    },
  });
  const body = JSON.stringify(payload);

  assert.match(body, /AFM provider: native/);
  assert.match(body, /backend=apple-foundation-models/);
  assert.match(body, /adapterConfigured=true/);
  assert.doesNotMatch(body, /\/Users\/alice/);
  assert.doesNotMatch(body, /extractor\.fmadapter/);
});

test("prepareOutcome returns a dry-run outcome packet without audit or learning writes", async () => {
  const packet = await prepareOutcome(
    {
      task: "ship AFM prepare task hardening",
      summary: "Added deterministic tests, privacy metadata, and live AFM opt-in checks.",
      changedFiles: ["plugins/minni/src/task.ts", "plugins/minni/tests/task.test.mjs"],
      verification: ["npm test passed"],
      profile: "compact",
      useAfm: false,
      vaultPath: "/tmp/vault",
    },
    {
      afmPrepare: async () => {
        throw new Error("AFM should not be called when useAfm is false");
      },
    },
  );

  assert.equal(packet.profile, "compact");
  assert.equal(packet.afm.used, false);
  assert.equal(packet.outcomeDraft.learnCandidates.length, 1);
  assert.match(packet.outcomeDraft.logOnly.join("\n"), /npm test passed/);
  assert.match(packet.outcomeDraft.doNotStore.join("\n"), /raw logs/);
});

test("prepareOutcome applies AFM outcome draft suggestions when requested", async () => {
  const packet = await prepareOutcome(
    {
      task: "summarize backend hardening",
      summary: "Prepared source ranking and budget profile changes.",
      profile: "compact",
      useAfm: true,
      vaultPath: "/tmp/vault",
    },
    {
      afmPrepare: async (_url, payload) => {
        assert.equal(payload.purpose, "outcome");
        assert.equal(payload.profile, "compact");
        assert.match(String(payload.summary), /budget profile/);
        return {
          ok: true,
          data: {
            outcomeDraft: {
              learnCandidates: ["Remember that prepare task now has profile-aware ranking."],
              logOnly: ["npm test passed"],
              expires: ["Refresh after next retrieval pass."],
              doNotStore: ["Do not store raw AFM responses."],
            },
          },
        };
      },
    },
  );

  assert.equal(packet.mode, "afm");
  assert.equal(packet.afm.used, true);
  assert.deepEqual(packet.outcomeDraft.learnCandidates, ["Remember that prepare task now has profile-aware ranking."]);
  assert.deepEqual(packet.outcomeDraft.doNotStore, ["Do not store raw AFM responses."]);
});

test("prepareOutcome keeps AFM governance buckets mutually exclusive with restrictive bucket priority", async () => {
  const packet = await prepareOutcome(
    {
      task: "summarize native AFM wiring repair",
      summary: "Removed stale bridge status and verified native helper behavior.",
      profile: "compact",
      useAfm: true,
      vaultPath: "/tmp/vault",
    },
    {
      afmPrepare: async () => ({
        ok: true,
        data: {
          outcomeDraft: {
            learnCandidates: ["Native helper is configured", "Do not store raw logs"],
            logOnly: ["Native helper is configured", "Changed /Users/alice/private/file.ts"],
            expires: ["Native helper is configured"],
            doNotStore: ["Do not store raw logs"],
          },
        },
      }),
    },
  );

  assert.deepEqual(packet.outcomeDraft.doNotStore, ["Do not store raw logs"]);
  assert.deepEqual(packet.outcomeDraft.expires, ["Native helper is configured"]);
  assert.deepEqual(packet.outcomeDraft.logOnly, ["Changed [local-path]"]);
  assert.deepEqual(packet.outcomeDraft.learnCandidates, []);
});

test("AFM outcome payload uses compact outcome instructions and redacts local paths", () => {
  const payload = buildAfmChatPayload({
    purpose: "outcome",
    task: "summarize backend work",
    summary: "Changed /Users/example/private/repo/file.ts and verified behavior.",
    changedFiles: ["/Users/example/private/repo/file.ts", "plugins/minni/src/task.ts"],
    verification: ["npm test passed", "raw log at /Volumes/private/log.txt"],
    profile: "compact",
    budgetTokens: 1500,
    model: "apple-foundation-models",
  });
  const body = JSON.stringify(payload);

  assert.match(body, /Return compact JSON only for Codex outcome prep/);
  assert.match(body, /plugins\/minni\/src\/task.ts/);
  assert.doesNotMatch(body, /\/Users\/example/);
  assert.doesNotMatch(body, /\/Volumes\/private/);
});

test("prepareTask uses AFM distillation when requested and available", async () => {
  const packet = await prepareTask(
    {
      task: "plan context-light memory ranking",
      vaultPath: "/tmp/vault",
      useAfm: true,
    },
    {
      searchVault: async () => [vaultMatch],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      afmPrepare: async () => ({
        ok: true,
        data: {
          brief: "AFM distilled brief.",
          recommendedNextActions: ["Ship prepare_task evals."],
          risks: ["Ranking regressions need tests."],
        },
      }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.equal(packet.mode, "afm");
  assert.equal(packet.afm.used, true);
  assert.equal(packet.brief, "AFM distilled brief.");
  assert.deepEqual(packet.recommendedNextActions, ["Ship prepare_task evals."]);
  assert.equal(packet.relevantSources[0].relativePath, "wiki/sessions/backend-handoff.md");
});

test("prepareTask uses native AFM provider metadata when native mode is requested", async () => {
  const packet = await prepareTask(
    {
      task: "plan native AFM adapter wiring",
      vaultPath: "/tmp/vault",
      useAfm: true,
      afmProviderMode: "native",
    },
    {
      searchVault: async () => [vaultMatch],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      afmHealth: async () => ({
        ok: true,
        data: {
          backend: "apple-foundation-models",
          availability: "available",
          adapter: "/Users/alice/private/extractor.fmadapter",
          status: "ok",
        },
      }),
      afmPrepare: async (_url, payload) => {
        assert.equal(payload.provider.mode, "native");
        assert.equal(payload.provider.backend, "apple-foundation-models");
        assert.equal(payload.provider.availability, "available");
        assert.equal(payload.provider.adapterConfigured, true);
        assert.equal(payload.provider.adapterPath, undefined);
        return {
          ok: true,
          data: {
            brief: "Native AFM adapter distilled brief.",
          },
        };
      },
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.equal(packet.mode, "afm");
  assert.equal(packet.afm.used, true);
  assert.equal(packet.afm.provider, "native");
  assert.equal(packet.afm.backend, "apple-foundation-models");
  assert.equal(packet.afm.adapterConfigured, true);
  assert.equal(packet.afm.adapterPath, undefined);
  assert.equal(packet.brief, "Native AFM adapter distilled brief.");
});

test("prepareTask native mode ignores dead bridge health when helper is configured", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-native-dead-bridge-"));
  const helper = path.join(root, "helper.mjs");
  const previousHelper = process.env.MINNI_AFM_NATIVE_HELPER;
  await writeFile(helper, "#!/usr/bin/env node\n", "utf8");
  await chmod(helper, 0o755);
  process.env.MINNI_AFM_NATIVE_HELPER = helper;
  try {
    const packet = await prepareTask(
      {
        task: "audit AFM wiring",
        vaultPath: "/tmp/vault",
        useAfm: true,
        afmProviderMode: "native",
      },
      {
        searchVault: async () => [],
        recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
        afmHealth: async () => ({ ok: false, error: "connect ECONNREFUSED 127.0.0.1:11437" }),
        afmPrepare: async (_url, payload) => {
          assert.equal(payload.provider.provider, "native");
          assert.equal(payload.provider.status, "native_available");
          assert.doesNotMatch(JSON.stringify(payload.provider), /11437/);
          return { ok: true, data: { brief: "Audit Minni AFM provider configuration." } };
        },
        audit: async () => "/tmp/vault/logs/today.md",
      },
    );

    assert.equal(packet.mode, "afm");
    assert.equal(packet.afm.provider, "native");
    assert.equal(packet.afm.error, undefined);
    assert.equal(packet.brief, "Audit Minni AFM provider configuration.");
  } finally {
    if (previousHelper === undefined) delete process.env.MINNI_AFM_NATIVE_HELPER;
    else process.env.MINNI_AFM_NATIVE_HELPER = previousHelper;
    await rm(root, { recursive: true, force: true });
  }
});

test("prepareTask does not call native AFM when shared provider health is unavailable", async () => {
  const packet = await prepareTask(
    {
      task: "plan native AFM adapter wiring",
      vaultPath: "/tmp/vault",
      useAfm: true,
      afmProviderMode: "native",
    },
    {
      searchVault: async () => [],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      afmHealth: async () => ({
        ok: false,
        data: {
          backend: "apple-foundation-models",
          availability: "unavailable",
          adapter: "/Users/alice/private/extractor.fmadapter",
          status: "error",
        },
        error: "FoundationModels unavailable at /Users/alice/private/extractor.fmadapter",
      }),
      afmPrepare: async () => {
        throw new Error("native provider must not be called when resolver marks it unavailable");
      },
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  const body = JSON.stringify(packet);
  assert.equal(packet.mode, "deterministic");
  assert.equal(packet.afm.used, false);
  assert.equal(packet.afm.provider, "native");
  assert.equal(packet.afm.requestedProvider, "native");
  assert.equal(packet.afm.adapterConfigured, true);
  assert.match(packet.afm.error ?? "", /FoundationModels unavailable/);
  assert.doesNotMatch(body, /\/Users\/alice/);
  assert.doesNotMatch(body, /extractor\.fmadapter/);
});

test("prepareTask falls back when AFM distillation fails", async () => {
  const packet = await prepareTask(
    {
      task: "debug prepare task",
      vaultPath: "/tmp/vault",
      useAfm: true,
    },
    {
      searchVault: async () => [],
      recall: async () => ({ ok: false, error: "socket offline" }),
      afmPrepare: async () => ({ ok: false, error: "AFM offline" }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.equal(packet.mode, "deterministic");
  assert.equal(packet.afm.requested, true);
  assert.equal(packet.afm.used, false);
  assert.equal(packet.afm.error, "AFM offline");
  assert.equal(packet.recall.daemonOk, false);
  assert.match(packet.currentState.join("\n"), /socket offline/);
});

test("callAfmPrepareTask parses v0 chat-completions JSON content", async () => {
  const server = await new Promise((resolve) => {
    const srv = createServer((req, res) => {
      assert.equal(req.method, "POST");
      assert.equal(req.url, "/v1/chat/completions");
      let body = "";
      req.on("data", (chunk) => {
        body += chunk;
      });
      req.on("end", () => {
        const payload = JSON.parse(body);
        assert.equal(payload.model, "apple-foundation-models");
        assert.ok(Array.isArray(payload.messages));
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            choices: [
              {
                message: {
                  role: "assistant",
                  content: "{\"brief\":\"chat distilled\",\"recommendedNextActions\":[\"Run live test\"]}",
                },
              },
            ],
          }),
        );
      });
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });

  try {
    const address = server.address();
    const result = await callAfmPrepareTask(`http://127.0.0.1:${address.port}/v1/chat/completions`, {
      task: "test v0 adapter",
      model: "apple-foundation-models",
    });

    assert.equal(result.ok, true);
    assert.equal(result.data.brief, "chat distilled");
    assert.deepEqual(result.data.recommendedNextActions, ["Run live test"]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("callAfmPrepareTask chunks oversized relevantSources instead of sending them all in one native call", async () => {
  const nativeCalls = [];
  // Fake native helper: records every payload's relevantSources length: the
  // first "map" calls see a slice, the final "reduce" call sees none
  // (partialBriefs instead).
  const fakeChat = async (request) => {
    nativeCalls.push(request.nativePayload);
    const sources = Array.isArray(request.nativePayload?.relevantSources)
      ? request.nativePayload.relevantSources
      : [];
    if (sources.length > 0) {
      return { ok: true, data: { brief: `partial brief ${nativeCalls.length}`, recommendedNextActions: [], risks: [] } };
    }
    return { ok: true, data: { brief: "synthesized final brief", recommendedNextActions: [], risks: [] } };
  };

  // A plain object literal implementing ModelProvider directly — NOT a
  // spread of a class instance, which would drop AfmProvider's prototype
  // methods (supports/chat/health) and make ProviderChain.chat() throw when
  // it calls provider.supports(operation).
  const fakeProvider = {
    name: "afm",
    tier: "local",
    supports: (_operation) => true,
    chat: fakeChat,
  };
  const chain = new ProviderChain([fakeProvider]);
  const bigSources = Array.from({ length: 60 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    wikilink: `[[note-${i}]]`,
    evidenceEnvelope: "context text ".repeat(50),
  }));

  const result = await callAfmPrepareTask("http://127.0.0.1:11437/v1/chat/completions", {
    task: "a task",
    budgetTokens: 4096,
    profile: "default",
    provider: { provider: "native", mode: "native" },
    intent: "intent",
    constraints: [],
    currentState: [],
    relevantSources: bigSources,
    daemonLead: "",
    model: "afm-local",
  }, chain);

  assert.equal(result.ok, true);
  assert.ok(nativeCalls.length > 1, `expected multiple native calls, got ${nativeCalls.length}`);
  assert.equal(result.data.brief, "synthesized final brief");
});

test("callAfmPrepareTask tree-reduces when the reduce payload itself is oversized", async () => {
  const nativeCalls = [];
  let mapCallCount = 0;
  // Fake native helper: map-phase calls (payload still carries
  // relevantSources) return a partial brief with a large `brief` string, so
  // that once all partials are gathered into a single partialBriefs reduce
  // payload, that payload itself busts the ~3200 token budget. Reduce-phase
  // calls (payload carries partialBriefs instead of relevantSources) return
  // a much smaller synthesized brief so the recursive reduce converges.
  const fakeChat = async (request) => {
    nativeCalls.push(request.nativePayload);
    const sources = Array.isArray(request.nativePayload?.relevantSources)
      ? request.nativePayload.relevantSources
      : [];
    if (sources.length > 0) {
      mapCallCount += 1;
      return {
        ok: true,
        data: {
          // Long enough that a handful of these together exceed the
          // AFM_INPUT_BUDGET_TOKENS (3200) budget used for the reduce call.
          brief: `partial brief ${mapCallCount} `.padEnd(3500, "x"),
          recommendedNextActions: [],
          risks: [],
        },
      };
    }
    return {
      ok: true,
      data: { brief: "synthesized final brief", recommendedNextActions: [], risks: [] },
    };
  };

  // A plain object literal implementing ModelProvider directly — NOT a
  // spread of a class instance, which would drop AfmProvider's prototype
  // methods (supports/chat/health) and make ProviderChain.chat() throw when
  // it calls provider.supports(operation).
  const fakeProvider = {
    name: "afm",
    tier: "local",
    supports: (_operation) => true,
    chat: fakeChat,
  };
  const chain = new ProviderChain([fakeProvider]);
  const bigSources = Array.from({ length: 60 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    wikilink: `[[note-${i}]]`,
    evidenceEnvelope: "context text ".repeat(50),
  }));

  const result = await callAfmPrepareTask("http://127.0.0.1:11437/v1/chat/completions", {
    task: "a task",
    budgetTokens: 4096,
    profile: "default",
    provider: { provider: "native", mode: "native" },
    intent: "intent",
    constraints: [],
    currentState: [],
    relevantSources: bigSources,
    daemonLead: "",
    model: "afm-local",
  }, chain);

  assert.equal(result.ok, true);
  assert.equal(result.data.brief, "synthesized final brief");

  // Map phase must have chunked relevantSources into 3+ groups so there are
  // 3+ large partial briefs to reduce.
  assert.ok(mapCallCount >= 3, `expected 3+ map calls, got ${mapCallCount}`);

  const reduceCalls = nativeCalls.filter(
    (payload) => !Array.isArray(payload?.relevantSources) || payload.relevantSources.length === 0,
  );
  // With the listField bug (passing "relevantSources" instead of
  // "partialBriefs" to reduceViaSameOp), callNativeOpChunked can never find
  // an array to split inside the reduce payload, so it always collapses to
  // exactly one flat reduce call regardless of size. The fix must make the
  // oversized partialBriefs reduce payload actually split into multiple
  // native calls (one per group), which then get reduced again.
  assert.ok(
    reduceCalls.length > 1,
    `expected the reduce phase to make more than one native call (true tree reduction), got ${reduceCalls.length}`,
  );
});

test("prepareTask native provider uses the configured native helper", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-native-prepare-"));
  const helper = path.join(root, "helper.mjs");
  const previousHelper = process.env.MINNI_AFM_NATIVE_HELPER;
  await writeFile(
    helper,
    [
      "#!/usr/bin/env node",
      "let raw = '';",
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data', chunk => raw += chunk);",
      "process.stdin.on('end', () => {",
      "  const request = JSON.parse(raw);",
      "  if (request.operation !== 'prepare_task') process.exit(2);",
      "  console.log(JSON.stringify({ ok: true, data: { brief: `native helper: ${request.input.task}` } }));",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);
  process.env.MINNI_AFM_NATIVE_HELPER = helper;
  try {
    const packet = await prepareTask(
      {
        task: "test native provider over FoundationModels helper",
        useAfm: true,
        afmProviderMode: "native",
        afmPrepareUrl: "http://127.0.0.1:1/v1/chat/completions",
        vaultPath: "/tmp/vault",
      },
      {
        searchVault: async () => [],
        recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
        afmHealth: async () => ({
          ok: true,
          data: {
            backend: "apple-foundation-models",
            availability: "available",
            status: "ok",
          },
        }),
        audit: async () => "/tmp/vault/logs/today.md",
      },
    );

    assert.equal(packet.mode, "afm");
    assert.equal(packet.afm.provider, "native");
    assert.equal(packet.brief, "native helper: test native provider over FoundationModels helper");
  } finally {
    if (previousHelper === undefined) delete process.env.MINNI_AFM_NATIVE_HELPER;
    else process.env.MINNI_AFM_NATIVE_HELPER = previousHelper;
    await rm(root, { recursive: true, force: true });
  }
});

test("callAfmPrepareTask reduce payload carries task context under the purpose's list key", async () => {
  const nativeCalls = [];
  const fakeChat = async (request) => {
    nativeCalls.push(request.nativePayload);
    const sources = Array.isArray(request.nativePayload?.relevantSources)
      ? request.nativePayload.relevantSources
      : [];
    if (sources.length > 0) {
      return { ok: true, data: { brief: `partial brief ${nativeCalls.length}`, recommendedNextActions: [], risks: [] } };
    }
    return { ok: true, data: { brief: "synthesized final brief", recommendedNextActions: [], risks: [] } };
  };
  const fakeProvider = { name: "afm", tier: "local", supports: (_operation) => true, chat: fakeChat };
  const chain = new ProviderChain([fakeProvider]);
  const bigSources = Array.from({ length: 60 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    wikilink: `[[note-${i}]]`,
    evidenceEnvelope: "context text ".repeat(50),
  }));

  const result = await callAfmPrepareTask("http://127.0.0.1:11437/v1/chat/completions", {
    task: "upgrade the retrieval pipeline",
    budgetTokens: 4096,
    profile: "default",
    provider: { provider: "native", mode: "native" },
    intent: "refactor",
    constraints: ["no schema changes"],
    currentState: ["tests green"],
    relevantSources: bigSources,
    daemonLead: "",
    model: "afm-local",
  }, chain);

  assert.equal(result.ok, true);
  assert.equal(result.data.brief, "synthesized final brief");

  // The reduce payload's list key comes from the single purpose mapping...
  const reduceCalls = nativeCalls.filter((p) => Array.isArray(p?.partialBriefs));
  assert.ok(reduceCalls.length >= 1, "expected at least one reduce call keyed by partialBriefs");
  // ...and the synthesis call sees the compact task context, not just partials.
  const reduce = reduceCalls[0];
  assert.equal(reduce.task, "upgrade the retrieval pipeline");
  assert.equal(reduce.intent, "refactor");
  assert.deepEqual(reduce.constraints, ["no schema changes"]);
  assert.deepEqual(reduce.currentState, ["tests green"]);
  assert.equal(reduce.budgetTokens, 4096);
  assert.ok(reduce.partialBriefs.length > 1);
});
