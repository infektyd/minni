#!/usr/bin/env node
// Exploration: how AFM behaves on the kinds of extraction/summarization jobs
// sovereign memory actually does. Hits the AFM bridge over OpenAI-compat HTTP.
//
//   node scripts/afm-sovereign-scenarios.mjs
//   AFM_URL=http://127.0.0.1:11437/v1/chat/completions node scripts/afm-sovereign-scenarios.mjs

const URL = process.env.AFM_URL ?? "http://127.0.0.1:11437/v1/chat/completions";
const MODEL = process.env.AFM_MODEL ?? "apple-foundation-models";

async function ask({ system, user, max_tokens = 300, temperature = 0.2 }) {
  const messages = [];
  if (system) messages.push({ role: "system", content: system });
  messages.push({ role: "user", content: user });
  const t0 = performance.now();
  const res = await fetch(URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: MODEL, messages, max_tokens, temperature }),
  });
  const ms = performance.now() - t0;
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const json = await res.json();
  return { text: json.choices?.[0]?.message?.content ?? "", ms };
}

function section(n, title) {
  console.log("\n" + "─".repeat(72));
  console.log(`${n}. ${title}`);
  console.log("─".repeat(72));
}

function show(label, text) {
  console.log(`\n[${label}]\n${text.trim()}`);
}

// ─── Scenarios ─────────────────────────────────────────────────────────────

const HARVEST_SYSTEM =
  "You distill raw agent work into one durable learning for a personal memory vault. " +
  "Output ONE line in this shape: `LEARNING: <what was learned> | WHEN TO USE: <trigger>`. " +
  "If nothing is durable, output exactly: SKIP.";

const HARVEST_INPUT = `
Temporary agent (role: worker) ran the task: "Wire up TraceContext propagation in OpenAI provider."
Files inspected: daemon/Sources/PraxisDaemonCore/Providers/OpenAI/*.swift
Changed files: OpenAIProvider.swift, OpenAIRequestBuilder.swift
Verification: swift test --filter OpenAIProviderTests passed (12/12)
Findings: OpenAI's Responses API expects 'metadata' as a flat string-to-string map; nested objects 200 OK
but silently get dropped on retrieval. Spent ~30 min before noticing. Switched to flattening
trace_id and span_id as separate top-level metadata keys.
Blockers: none.
`;

const EVIDENCE_SUMMARY_INPUT = JSON.stringify(
  {
    runtimeId: "team-9a3f12c8e0",
    task: "Audit daemon error-handling spine for unhandled cancellation paths",
    reports: [
      {
        agentId: "team-explorer-1",
        status: "completed",
        summary: "Mapped 14 call sites that hop actors during turn execution.",
        evidence: ["SessionTurnRunner.swift:142-218", "TurnObservation.swift:33-71"],
        blockers: [],
      },
      {
        agentId: "team-worker-2",
        status: "completed",
        summary: "Patched 3 sites to forward CancellationError as TurnEvent.error.",
        changedFiles: ["SessionTurnRunner.swift", "AgentRunCoordinator.swift"],
        verification: ["swift test --filter SessionTurnRunnerTests"],
        blockers: [],
      },
      {
        agentId: "team-reviewer-3",
        status: "blocked",
        summary: "Cannot verify AgentRunStore cancellation without a fake clock.",
        blockers: ["No deterministic clock for AgentRunStore tests."],
      },
    ],
  },
  null,
  2
);

const PRIVACY_SYSTEM =
  "You are a strict pre-write privacy gate for a personal memory vault. " +
  "Decide if this candidate learning is SAFE to write or BLOCKED. " +
  "BLOCK if it contains: API keys, passwords, tokens, absolute /Users/ or /Volumes/ paths, " +
  ".fmadapter filenames, launchd plists, raw email addresses, or anything that looks like a secret. " +
  "Output exactly one line: `SAFE` or `BLOCKED: <reason>`.";

const PRIVACY_CASES = [
  "When using OpenAI Responses API, flatten nested metadata to top-level string keys.",
  "Set OPENAI_API_KEY=sk-proj-AbCdEf12345 in ~/.zshrc to test daemon startup.",
  "AFM bridge listens on 127.0.0.1:11437 by default; override via SOVEREIGN_AFM_PREPARE_TASK_URL.",
  "User /Users/operator keeps adapter at /Users/operator/.adapters/harvest_v3.fmadapter; load it manually.",
];

const RECALL_EXPAND_SYSTEM =
  "Given one user query against a personal dev memory vault, output exactly 3 alternate phrasings " +
  "as a JSON array of strings. No prose, no markdown, just the JSON array.";

const TAG_SYSTEM =
  "Extract 3-6 short topic tags (lowercase, dash-separated, no #) from the snippet below. " +
  "Output as a JSON array of strings, no prose.";

const TAG_INPUT = `
Spent the morning fighting Swift 6 strict concurrency on Praxis's GlacialTerminalSession.
The DispatchSource read handler runs on a utility-QoS queue and can't cross @MainActor.
Solution was nonisolated @Observable @unchecked Sendable + DispatchQueue.main.async hops
for state mutation. Project-wide -default-isolation=MainActor would have broken this if
GlacialTerminalSession had been MainActor-bound.
`;

const PROMOTION_SYSTEM =
  "You review a temporary agent's recent spawn history to decide if it should be promoted to a permanent profile. " +
  "Promote if: (a) spawned >= 3 times in last 14 days, (b) similar focus/role each time, (c) mostly successful. " +
  "Output: `PROMOTE: <suggested-permanent-id> | REASON: <one short line>` or `KEEP-EPHEMERAL: <reason>`.";

const PROMOTION_INPUT = JSON.stringify(
  {
    candidate: { role: "reviewer", focus: "Audit Swift concurrency boundaries (@MainActor / Sendable / actor)" },
    historyLast14d: [
      { date: "2026-04-26", role: "reviewer", focus: "Audit Swift concurrency in daemon spine", outcome: "completed" },
      { date: "2026-04-30", role: "reviewer", focus: "Review @MainActor boundaries in GlacialTerminalSession", outcome: "completed" },
      { date: "2026-05-02", role: "reviewer", focus: "Concurrency review of TurnObservation actor hops", outcome: "completed" },
      { date: "2026-05-06", role: "reviewer", focus: "Sendable conformance audit on AgentRunStore", outcome: "completed-with-blocker" },
    ],
  },
  null,
  2
);

const DEDUP_SYSTEM =
  "Two candidate learnings are below. Decide if they are DUPLICATE (write only one), MERGE " +
  "(combine into a richer single note), or DISTINCT (keep both). " +
  "Output: `DECISION: <DUPLICATE|MERGE|DISTINCT>` then on the next line `MERGED: <text>` if MERGE, else nothing.";

const DEDUP_INPUT = `
A: OpenAI Responses API drops nested metadata silently — flatten to string-only top-level keys.
B: Don't put structured objects in OpenAI 'metadata' field; only flat string-to-string survives the round trip.
`;

const SCAR_SYSTEM =
  "Given a narrative of a failed attempt, extract the warning to remember. " +
  "Output exactly one line: `WARNING: <what fails> | INSTEAD: <what to do>`.";

const SCAR_INPUT = `
Tried to make GlacialTerminalSession a @MainActor type so we could touch UI state directly
from the read handler. App crashed on first PTY read with a Swift concurrency violation
because DispatchSource.makeReadSource calls its handler on a utility-QoS queue, which can't
hop into @MainActor synchronously. Reverted to nonisolated + DispatchQueue.main.async hops.
`;

// ─── Runner ────────────────────────────────────────────────────────────────

async function main() {
  console.log(`AFM target: ${URL}\nModel:      ${MODEL}\n`);

  section(1, "Harvest: distill ephemeral agent's report → one durable learning");
  show("INPUT", HARVEST_INPUT);
  let r = await ask({ system: HARVEST_SYSTEM, user: HARVEST_INPUT, max_tokens: 120 });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  section(2, "Evidence summary: 3-agent team report → coordinator briefing");
  r = await ask({
    system: "Summarize this team-evidence packet into a 4-line coordinator briefing: status, what shipped, what's blocked, what to verify next.",
    user: EVIDENCE_SUMMARY_INPUT,
    max_tokens: 200,
  });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  section(3, "Privacy gate: classify 4 candidate learnings before vault write");
  for (const [i, candidate] of PRIVACY_CASES.entries()) {
    const sub = await ask({ system: PRIVACY_SYSTEM, user: candidate, max_tokens: 60 });
    console.log(`\n  case ${i + 1} (${sub.ms.toFixed(0)} ms): ${candidate}\n  → ${sub.text.trim()}`);
  }

  section(4, "Recall expansion: user query → 3 alternate phrasings");
  const queries = [
    "swift concurrency mainactor crash",
    "openai metadata weird behavior",
  ];
  for (const q of queries) {
    const sub = await ask({ system: RECALL_EXPAND_SYSTEM, user: q, max_tokens: 200 });
    console.log(`\n  query: "${q}" (${sub.ms.toFixed(0)} ms)\n  → ${sub.text.trim()}`);
  }

  section(5, "Auto-tag: snippet → topic tags for Obsidian frontmatter");
  show("INPUT", TAG_INPUT);
  r = await ask({ system: TAG_SYSTEM, user: TAG_INPUT, max_tokens: 100 });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  section(6, "Promotion classifier: recent spawns → promote or stay ephemeral?");
  show("INPUT", PROMOTION_INPUT);
  r = await ask({ system: PROMOTION_SYSTEM, user: PROMOTION_INPUT, max_tokens: 100 });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  section(7, "Dedup: two candidate learnings → keep both, merge, or duplicate?");
  show("INPUT", DEDUP_INPUT);
  r = await ask({ system: DEDUP_SYSTEM, user: DEDUP_INPUT, max_tokens: 200 });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  section(8, "Scar tissue: failed-attempt narrative → compact warning");
  show("INPUT", SCAR_INPUT);
  r = await ask({ system: SCAR_SYSTEM, user: SCAR_INPUT, max_tokens: 120 });
  show(`OUTPUT (${r.ms.toFixed(0)} ms)`, r.text);

  console.log("\n" + "─".repeat(72));
  console.log("done.");
}

main().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
