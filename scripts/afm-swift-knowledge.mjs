#!/usr/bin/env node
// Probe: how well does AFM actually know modern Swift?
// Mix of: language features, modern frameworks, strict concurrency,
// Apple's own current APIs, and a couple of "would you catch this?" prompts.
//
//   node scripts/afm-swift-knowledge.mjs

const URL = process.env.AFM_URL ?? "http://127.0.0.1:11437/v1/chat/completions";
const MODEL = process.env.AFM_MODEL ?? "apple-foundation-models";

async function ask({ system, user, max_tokens = 400, temperature = 0.2 }) {
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

function section(n, title, difficulty) {
  console.log("\n" + "─".repeat(72));
  console.log(`${n}. [${difficulty}] ${title}`);
  console.log("─".repeat(72));
}

const SWIFT_SYSTEM =
  "You are a senior Swift engineer. Answer concisely and concretely. " +
  "If you are not sure about a current API, say so — do not invent.";

const PROBES = [
  {
    n: 1,
    diff: "easy",
    title: "Observation framework basics",
    prompt:
      "What does the @Observable macro do in Swift 5.9+, and how does it differ from ObservableObject? " +
      "Show a 5-line example of an @Observable view model and how a SwiftUI view consumes it.",
  },
  {
    n: 2,
    diff: "medium",
    title: "Strict concurrency (Swift 6)",
    prompt:
      "Under Swift 6 strict concurrency, this code fails to compile. Explain why and give the minimal fix.\n\n" +
      "```swift\n" +
      "@MainActor\n" +
      "final class TerminalSession {\n" +
      "    var lines: [String] = []\n" +
      "    func start() {\n" +
      "        let src = DispatchSource.makeReadSource(fileDescriptor: 0, queue: .global())\n" +
      "        src.setEventHandler {\n" +
      "            self.lines.append(\"new\")\n" +
      "        }\n" +
      "        src.resume()\n" +
      "    }\n" +
      "}\n" +
      "```",
  },
  {
    n: 3,
    diff: "medium",
    title: "Actor reentrancy gotcha",
    prompt:
      "Briefly: what is actor reentrancy in Swift, and what's the most common bug it causes? " +
      "Give a one-line example of state that becomes stale because of it.",
  },
  {
    n: 4,
    diff: "hard",
    title: "MainActor.assumeIsolated",
    prompt:
      "When would you reach for MainActor.assumeIsolated { ... } over plain MainActor.run { ... }, " +
      "and what's the precondition you must guarantee at the call site?",
  },
  {
    n: 5,
    diff: "hard",
    title: "Knows its own framework — FoundationModels",
    prompt:
      "Show a minimal Swift example that uses Apple's FoundationModels framework to: " +
      "(a) create a LanguageModelSession against the system model, " +
      "(b) use the @Generable macro to define a Recipe { name: String, ingredients: [String] } struct, " +
      "(c) ask the session to generate one Recipe. Use the actual current API names — don't guess.",
  },
  {
    n: 6,
    diff: "hard",
    title: "Trap: would you catch this?",
    prompt:
      "Is there anything wrong with this Swift 6 code? If yes, what?\n\n" +
      "```swift\n" +
      "actor Counter {\n" +
      "    private var n = 0\n" +
      "    func bump() async { n += 1 }\n" +
      "}\n" +
      "\n" +
      "func parallel(_ c: Counter) async {\n" +
      "    await withTaskGroup(of: Void.self) { group in\n" +
      "        for _ in 0..<1000 { group.addTask { await c.bump() } }\n" +
      "    }\n" +
      "}\n" +
      "```",
  },
  {
    n: 7,
    diff: "hard",
    title: "withDiscardingTaskGroup",
    prompt:
      "What is withDiscardingTaskGroup and when would you use it instead of withTaskGroup? " +
      "Give a one-sentence rule of thumb.",
  },
  {
    n: 8,
    diff: "trap",
    title: "Deprecation check — should NOT recommend ObservableObject for new code",
    prompt:
      "I'm starting a brand-new SwiftUI app targeting iOS 17+. Should my view models use ObservableObject + @Published, or @Observable? Briefly explain why.",
  },
];

async function main() {
  console.log(`AFM target: ${URL}\nModel:      ${MODEL}\n`);
  for (const p of PROBES) {
    section(p.n, p.title, p.diff);
    console.log(`\n[PROMPT]\n${p.prompt}`);
    const r = await ask({ system: SWIFT_SYSTEM, user: p.prompt, max_tokens: 500 });
    console.log(`\n[ANSWER  (${r.ms.toFixed(0)} ms)]\n${r.text.trim()}`);
  }
  console.log("\n" + "─".repeat(72) + "\ndone.");
}

main().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
