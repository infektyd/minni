// Prompt classifier, record-only (recall-to-use rev5, 5.2.1): the template rule
// that tells a recurring scheduled check-in from a human prompt, the counts
// file it reads, and the envelope classes a real harness prompt produces.
//
// Nothing calls the classifier yet. These tests drive it the way UPS (write)
// and harvest (read-only) will.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  HARNESS_ENVELOPE_RULES,
  classificationRuleIds,
} from "../dist/harness-envelope-rules.js";
import {
  PROMPT_TEMPLATES_RELPATH,
  classifyPromptEnvelope,
  isScheduledTemplate,
  promptTemplatesPath,
  readPromptTemplates,
  recordOccurrence,
  stripHarnessBlocks,
  templateHash,
} from "../dist/harness-envelope.js";

const RULE = HARNESS_ENVELOPE_RULES.templateRule;
const T0 = Date.parse("2026-03-01T00:00:00.000Z");
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

// 100+ chars; the digit differs per occurrence and normalizes to N.
const checkIn = (n) =>
  `Scheduled check-in ${n}: review the queue depth and report anything that stalled since the previous check-in.`;

async function withVault(fn) {
  const vault = await mkdtemp(path.join(tmpdir(), "minni-prompt-templates-"));
  try {
    await fn(vault);
  } finally {
    await rm(vault, { recursive: true, force: true });
  }
}

// Classification takes no session key: the rule reads the counts file by hash.
function ctxFor(file, { at, pid }) {
  return {
    at,
    pid,
    templateEntry: (hash) => file?.templates[hash],
  };
}

/** What UPS will do: read, classify before counting, then record the occurrence. */
async function ups(vault, text, { at, pid, session }) {
  const file = await readPromptTemplates(vault);
  const result = classifyPromptEnvelope(text, ctxFor(file, { at, pid }));
  if (result.template) {
    await recordOccurrence(vault, {
      hash: result.template.hash,
      ts: at,
      pid,
      sessionKeyPrefix: session,
      now: at,
    });
  }
  return result;
}

/** What harvest will do: read-only, the same prompt by its own promptId. */
async function harvest(vault, text, { at, pid }) {
  const file = await readPromptTemplates(vault);
  return classifyPromptEnvelope(text, ctxFor(file, { at, pid }));
}

// ---------------------------------------------------------------------------
// The envelope classes a real harness prompt produces
// ---------------------------------------------------------------------------

test("a system-reminder block on a human-typed prompt is stripped and stays human", () => {
  const typed = "<system-reminder>\nAttribution note.\n</system-reminder>\nFix the flaky retry test";
  const got = classifyPromptEnvelope(typed);
  assert.equal(got.kind, "human");
  assert.equal(got.subkind, "typed");
  assert.equal(got.body, "Fix the flaky retry test");
  assert.equal(got.rule_id, "block.system_reminder");
  assert.equal(got.template_rule, "off");
});

test("a relay is wrapped and its body is kept", () => {
  const relay =
    "Another Claude session sent a message:\n" +
    '<cross-session-message from="peer-a">\nPlease review the plan.\n</cross-session-message>';
  const got = classifyPromptEnvelope(relay);
  assert.equal(got.kind, "wrapped");
  assert.equal(got.body, "Please review the plan.");
  assert.equal(got.rule_id, "wrap.another_session_lead");
});

test("a task notification is skip", () => {
  const got = classifyPromptEnvelope(
    "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n</task-notification>",
  );
  assert.equal(got.kind, "skip");
  assert.equal(got.subkind, "task_notification");
  assert.equal(got.rule_id, "skip.task_notification");
});

test("text rules win over the template rule: skip and wrapped prompts never reach it", () => {
  const file = { v: 1, templates: {} };
  const ctx = ctxFor(file, { at: T0, pid: "p1" });
  const skip = classifyPromptEnvelope(
    `<task-notification>${"x".repeat(200)}</task-notification>`,
    ctx,
  );
  assert.equal(skip.kind, "skip");
  assert.equal(skip.template_rule, "on");
  assert.equal(skip.template, undefined);
  const wrapped = classifyPromptEnvelope(
    `<cross-session-message>${"x".repeat(200)}</cross-session-message>`,
    ctx,
  );
  assert.equal(wrapped.kind, "wrapped");
  assert.equal(wrapped.template, undefined);
});

test("without a ctx the template rule is off and nothing is evaluated", () => {
  const got = classifyPromptEnvelope(checkIn(1));
  assert.equal(got.kind, "human");
  assert.equal(got.template_rule, "off");
  assert.equal(got.template, undefined);
});

test("a non-string prompt is an empty human prompt, not an exception", () => {
  for (const bad of [undefined, null, 42, {}, []]) {
    const got = classifyPromptEnvelope(bad);
    assert.equal(got.kind, "human");
    assert.equal(got.subkind, "empty");
    assert.equal(got.body, "");
  }
  assert.equal(stripHarnessBlocks(undefined), "");
});

test("hostile inputs stay linear: unclosed open tags, whitespace runs, huge prompts", () => {
  const started = performance.now();
  classifyPromptEnvelope("<system-reminder ".repeat(100_000));
  classifyPromptEnvelope(`${" ".repeat(2_000_000)}x`);
  classifyPromptEnvelope("<cross-session-message ".repeat(50_000));
  classifyPromptEnvelope("[Workflow harness ".repeat(50_000));
  classifyPromptEnvelope(`${"<system-reminder>".repeat(50_000)}tail`);
  templateHash("9".repeat(3_000_000));
  templateHash(`${"word ".repeat(1_000_000)}`);
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 10_000, `took ${Math.round(elapsed)} ms`);
});

// ---------------------------------------------------------------------------
// templateHash
// ---------------------------------------------------------------------------

test("templateHash normalizes the whole prompt, then takes sha1[:12]", () => {
  const raw =
    "<system-reminder>x</system-reminder>Run Check 12 NOW,\n\n  then  report 3 items";
  const expected = createHash("sha1")
    .update("run check N now, then report N items", "utf8")
    .digest("hex")
    .slice(0, 12);
  assert.match(templateHash(raw), /^[0-9a-f]{12}$/);
  assert.equal(templateHash(raw), expected);
  // Digit runs, case, whitespace and blocks do not change the template.
  assert.equal(
    templateHash("run check 9876 now, then report 40 items"),
    expected,
  );
  assert.notEqual(templateHash("run check now, then report items"), expected);
});

test("templateHash uses the first 2000 normalized chars of a long prompt", () => {
  const base = "word ".repeat(500); // 2,500 chars, 2,499 after the trim
  assert.equal(templateHash(`${base}alpha`), templateHash(`${base}beta`));
  assert.notEqual(templateHash(`x${base}alpha`), templateHash(`${base}alpha`));
  assert.equal(RULE.hashInputChars, 2000);
});

// ---------------------------------------------------------------------------
// isScheduledTemplate (pure)
// ---------------------------------------------------------------------------

test("isScheduledTemplate: prior means ts < at and a different pid", () => {
  const entry = {
    first_seen: T0,
    occ: [
      { ts: T0, pid: "a" },
      { ts: T0 + MIN, pid: "b" },
      { ts: T0 + 2 * MIN, pid: "c" },
    ],
    sessions: { s1: T0, s2: T0 + MIN },
  };
  // 3 priors, 2 sessions first seen before `at`.
  assert.equal(isScheduledTemplate(entry, T0 + 3 * MIN, "own"), true);
  // The third occurrence is not before `at` (ts == at), so only 2 priors.
  assert.equal(isScheduledTemplate(entry, T0 + 2 * MIN, "own"), false);
  // The prompt's own pid is never a prior.
  assert.equal(isScheduledTemplate(entry, T0 + 3 * MIN, "c"), false);
  // Absent, null and malformed entries are never scheduled.
  assert.equal(isScheduledTemplate(undefined, T0, "own"), false);
  assert.equal(isScheduledTemplate(null, T0, "own"), false);
  assert.equal(isScheduledTemplate({}, T0, "own"), false);
  assert.equal(isScheduledTemplate(entry, Number.NaN, "own"), false);
});

test("isScheduledTemplate: the span arm needs 2 h exactly or more", () => {
  const entry = {
    first_seen: T0,
    occ: [
      { ts: T0, pid: "a" },
      { ts: T0 + MIN, pid: "b" },
      { ts: T0 + 2 * MIN, pid: "c" },
    ],
    sessions: { s1: T0 },
  };
  assert.equal(isScheduledTemplate(entry, T0 + 2 * HOUR - 1, "own"), false);
  assert.equal(isScheduledTemplate(entry, T0 + 2 * HOUR, "own"), true);
});

// ---------------------------------------------------------------------------
// The rule, driven through the counts file the way UPS drives it
// ---------------------------------------------------------------------------

test("20 occurrences in one session within 1 h stay human", async () => {
  await withVault(async (vault) => {
    for (let i = 0; i < 20; i++) {
      const got = await ups(vault, checkIn(i), {
        at: T0 + i * 3 * MIN, // 57 minutes in total
        pid: `p${i}`,
        session: "s1",
      });
      assert.equal(got.kind, "human", `occurrence ${i}`);
      assert.equal(got.template_rule, "on");
      assert.equal(got.template.scheduled, false);
    }
  });
});

test("the 4th occurrence 2 h after first_seen is scheduled", async () => {
  await withVault(async (vault) => {
    const offsets = [0, 10 * MIN, 20 * MIN];
    for (const [i, off] of offsets.entries()) {
      const got = await ups(vault, checkIn(i), { at: T0 + off, pid: `p${i}`, session: "s1" });
      assert.equal(got.kind, "human");
    }
    // One millisecond short of 2 h: still human.
    const early = await harvest(vault, checkIn(3), {
      at: T0 + 2 * HOUR - 1,
      pid: "p3",
      session: "s1",
    });
    assert.equal(early.kind, "human");
    // Exactly 2 h after first_seen: scheduled.
    const fourth = await ups(vault, checkIn(3), { at: T0 + 2 * HOUR, pid: "p3", session: "s1" });
    assert.equal(fourth.kind, "scheduled");
    assert.equal(fourth.subkind, "template");
    assert.equal(fourth.rule_id, RULE.rule_id);
    assert.ok(classificationRuleIds().includes(fourth.rule_id));
    assert.equal(fourth.template.scheduled, true);
    assert.equal(fourth.body, checkIn(3));
  });
});

test("only 2 priors 2 h on is still human", async () => {
  await withVault(async (vault) => {
    await ups(vault, checkIn(0), { at: T0, pid: "p0", session: "s1" });
    await ups(vault, checkIn(1), { at: T0 + 10 * MIN, pid: "p1", session: "s1" });
    const third = await ups(vault, checkIn(2), { at: T0 + 2 * HOUR, pid: "p2", session: "s1" });
    assert.equal(third.kind, "human");
  });
});

test("one session at a 10-minute cadence for 72 h never flips back once scheduled", async () => {
  await withVault(async (vault) => {
    const first = [];
    let becameScheduledAt = -1;
    const total = (72 * HOUR) / (10 * MIN) + 1; // 433 occurrences
    for (let i = 0; i < total; i++) {
      const got = await ups(vault, checkIn(i), {
        at: T0 + i * 10 * MIN,
        pid: `p${i}`,
        session: "s1",
      });
      first.push(got.kind);
      if (got.kind === "scheduled" && becameScheduledAt < 0) becameScheduledAt = i;
    }
    // 12 priors and exactly 2 h after first_seen.
    assert.equal(becameScheduledAt, 12);
    assert.deepEqual(
      first.slice(0, 12),
      Array.from({ length: 12 }, () => "human"),
    );
    assert.deepEqual(
      first.slice(12),
      Array.from({ length: total - 12 }, () => "scheduled"),
      "a scheduled template must never flip back to human",
    );
    // The ring rolled over many times, yet first_seen survived.
    const file = await readPromptTemplates(vault);
    const entry = file.templates[templateHash(checkIn(0))];
    assert.equal(entry.occ.length, RULE.ringMax);
    assert.equal(entry.first_seen, T0);
    assert.equal(entry.occ.at(-1).ts, T0 + (total - 1) * 10 * MIN);
  });
});

test("a 1-minute cadence never flips back either, though the ring only spans 15 minutes", async () => {
  await withVault(async (vault) => {
    // At this cadence the 16-entry ring covers 15 minutes, far less than the 2 h
    // span arm needs, so first_seen has to live outside the ring.
    const kinds = [];
    for (let i = 0; i <= 180; i++) {
      const got = await ups(vault, checkIn(i), { at: T0 + i * MIN, pid: `p${i}`, session: "s1" });
      kinds.push(got.kind);
    }
    assert.deepEqual(kinds.slice(0, 120), Array.from({ length: 120 }, () => "human"));
    assert.deepEqual(kinds.slice(120), Array.from({ length: 61 }, () => "scheduled"));
  });
});

test("a 3-session recurrence stays scheduled after the ring rolls past the other sessions", async () => {
  await withVault(async (vault) => {
    for (const [i, s] of ["sa", "sb", "sc"].entries()) {
      await ups(vault, checkIn(i), { at: T0 + i * 5 * MIN, pid: `p${i}`, session: s });
    }
    // 30 more in one session within 40 minutes: the ring now holds only session sa.
    for (let i = 0; i < 30; i++) {
      const got = await ups(vault, checkIn(10 + i), {
        at: T0 + (11 + i) * MIN,
        pid: `q${i}`,
        session: "sa",
      });
      assert.equal(got.kind, "scheduled", `occurrence ${i} after the first three`);
    }
    const entry = (await readPromptTemplates(vault)).templates[templateHash(checkIn(0))];
    assert.ok(entry.occ.every((o) => o.pid.startsWith("q")));
    assert.deepEqual(Object.keys(entry.sessions).sort(), ["sa", "sb", "sc"]);
  });
});

test("a 3-session recurrence is scheduled", async () => {
  await withVault(async (vault) => {
    const sessions = ["sa", "sb", "sc"];
    for (const [i, s] of sessions.entries()) {
      const got = await ups(vault, checkIn(i), { at: T0 + i * 5 * MIN, pid: `p${i}`, session: s });
      assert.equal(got.kind, "human", `first sighting in ${s}`);
    }
    const again = await ups(vault, checkIn(3), { at: T0 + 15 * MIN, pid: "p3", session: "sa" });
    assert.equal(again.kind, "scheduled");
  });
});

test("3 priors across 2 sessions, then a 4th, is scheduled", async () => {
  await withVault(async (vault) => {
    const plan = [
      ["sa", 0],
      ["sa", 4],
      ["sb", 8],
    ];
    for (const [i, [s, m]] of plan.entries()) {
      await ups(vault, checkIn(i), { at: T0 + m * MIN, pid: `p${i}`, session: s });
    }
    const fourth = await ups(vault, checkIn(3), { at: T0 + 12 * MIN, pid: "p3", session: "sb" });
    assert.equal(fourth.kind, "scheduled");
  });
});

test("harvest evaluating a prompt by its own pid gets the class UPS recorded", async () => {
  await withVault(async (vault) => {
    // Three priors, all in one session, all within 30 minutes.
    for (const [i, m] of [0, 10, 20].entries()) {
      await ups(vault, checkIn(i), { at: T0 + m * MIN, pid: `a${i}`, session: "sa" });
    }
    // The prompt under test opens a NEW session. UPS counts 1 prior session, so human.
    const atUps = T0 + 30 * MIN;
    const seenByUps = await ups(vault, checkIn(3), { at: atUps, pid: "b1", session: "sb" });
    assert.equal(seenByUps.kind, "human");

    // UPS has now recorded the prompt, so session sb is in the file with first_ts == atUps.
    // Harvest sees the transcript timestamp a moment later and must still say human.
    const seenByHarvest = await harvest(vault, checkIn(3), {
      at: atUps + 37,
      pid: "b1",
      session: "sb",
    });
    assert.equal(seenByHarvest.kind, seenByUps.kind);

    // Control: the same state read as if the prompt were someone else's flips it,
    // so the own-pid exclusion is what keeps the two sides in agreement.
    const stranger = await harvest(vault, checkIn(3), {
      at: atUps + 37,
      pid: "not-b1",
      session: "sb",
    });
    assert.equal(stranger.kind, "scheduled");
  });
});

test("harvest agrees with UPS whatever session key it holds, or none", async () => {
  await withVault(async (vault) => {
    // Three priors in session sa; the prompt under test opens session sb.
    for (const [i, m] of [0, 10, 20].entries()) {
      await ups(vault, checkIn(i), { at: T0 + m * MIN, pid: `a${i}`, session: "sa" });
    }
    const atUps = T0 + 30 * MIN;
    const seenByUps = await ups(vault, checkIn(3), { at: atUps, pid: "b1", session: "sb" });
    assert.equal(seenByUps.kind, "human");

    // The file now holds sb with first_ts == atUps. The classifier ignores any key
    // a caller still hands it: none, the right one, a wrong one, another session's.
    const file = await readPromptTemplates(vault);
    for (const key of [undefined, "sb", "zzzzzzzz", "sa", ""]) {
      const ctx = {
        at: atUps + 37,
        pid: "b1",
        templateEntry: (hash) => file.templates[hash],
        ...(key === undefined ? {} : { sessionKeyPrefix: key }),
      };
      const seenByHarvest = classifyPromptEnvelope(checkIn(3), ctx);
      assert.equal(seenByHarvest.kind, seenByUps.kind, `session key ${JSON.stringify(key)}`);
    }
  });
});

test("isScheduledTemplate takes exactly (entry, at, pid)", () => {
  assert.equal(isScheduledTemplate.length, 3);
});

test("UPS and harvest agree on every prompt of a mixed three-session history", async () => {
  await withVault(async (vault) => {
    const history = [
      ["s1", 0],
      ["s1", 3],
      ["s1", 7],
      ["s2", 45],
      ["s2", 50],
      ["s3", 90],
      ["s1", 130],
      ["s1", 131],
      ["s2", 400],
      ["s3", 405],
      ["s1", 1000],
      ["s1", 1001],
      ["s2", 2000],
      ["s2", 2001],
    ];
    const recorded = [];
    for (const [i, [session, m]] of history.entries()) {
      const at = T0 + m * MIN;
      const pid = `p${i}`;
      const seenByUps = await ups(vault, checkIn(i), { at, pid, session });
      // Harvest right after the Stop hook, with a slightly later transcript timestamp.
      const justAfter = await harvest(vault, checkIn(i), { at: at + 37, pid, session });
      assert.equal(justAfter.kind, seenByUps.kind, `just after occurrence ${i}`);
      recorded.push({ at, pid, session, kind: seenByUps.kind });
    }
    // The history exercises both classes.
    assert.ok(recorded.some((r) => r.kind === "human"));
    assert.ok(recorded.some((r) => r.kind === "scheduled"));
    // Harvest reading the final file (later prompts present) still agrees.
    for (const [i, r] of recorded.entries()) {
      const late = await harvest(vault, checkIn(i), {
        at: r.at + 37,
        pid: r.pid,
        session: r.session,
      });
      assert.equal(late.kind, r.kind, `final file, occurrence ${i}`);
    }
  });
});

test("a short prompt is never scheduled", async () => {
  await withVault(async (vault) => {
    const shortText = "a".repeat(RULE.minChars - 1);
    for (let i = 0; i < 30; i++) {
      const got = await ups(vault, shortText, {
        at: T0 + i * 3 * HOUR,
        pid: `p${i}`,
        session: `s${i % 5}`,
      });
      assert.equal(got.kind, "human");
      assert.equal(got.template, undefined, "an ineligible prompt carries no template");
    }
    assert.equal(await readPromptTemplates(vault), null, "and nothing is recorded for it");

    // One char more is eligible, and the same history then flips it.
    const eligible = "a".repeat(RULE.minChars);
    let last;
    for (let i = 0; i < 30; i++) {
      last = await ups(vault, eligible, {
        at: T0 + i * 3 * HOUR,
        pid: `q${i}`,
        session: `s${i % 5}`,
      });
    }
    assert.equal(last.kind, "scheduled");
  });
});

test("the minimum length counts the stripped text, not the raw prompt", () => {
  const padded = `<system-reminder>${"r".repeat(500)}</system-reminder>short typed line`;
  const got = classifyPromptEnvelope(padded, ctxFor(null, { at: T0, pid: "p" }));
  assert.equal(got.kind, "human");
  assert.equal(got.template, undefined);
});

// ---------------------------------------------------------------------------
// The counts file
// ---------------------------------------------------------------------------

const hashOf = (i) => i.toString(16).padStart(12, "0");

test("the file stores no text and keeps the documented shape", async () => {
  await withVault(async (vault) => {
    await ups(vault, checkIn(1), { at: T0, pid: "p1", session: "abcdef0123456789" });
    const raw = await readFile(promptTemplatesPath(vault), "utf8");
    assert.ok(!raw.includes("Scheduled check-in"));
    assert.ok(!raw.includes("queue depth"));
    const parsed = JSON.parse(raw);
    assert.deepEqual(Object.keys(parsed).sort(), ["templates", "v"]);
    assert.equal(parsed.v, 1);
    const entry = parsed.templates[templateHash(checkIn(1))];
    assert.deepEqual(Object.keys(entry).sort(), ["first_seen", "occ", "sessions"]);
    assert.deepEqual(entry.occ, [{ ts: T0, pid: "p1" }]);
    // The session key is cut to an 8-char prefix.
    assert.deepEqual(entry.sessions, { abcdef01: T0 });
    assert.equal(
      promptTemplatesPath(vault),
      path.join(vault, PROMPT_TEMPLATES_RELPATH),
    );
    assert.equal(PROMPT_TEMPLATES_RELPATH, path.join(".runtime", "prompt-templates.json"));
  });
});

test("recording the same pid twice does not count twice", async () => {
  await withVault(async (vault) => {
    const input = { hash: hashOf(1), ts: T0, pid: "p1", sessionKeyPrefix: "s1", now: T0 };
    assert.equal(await recordOccurrence(vault, input), true);
    assert.equal(await recordOccurrence(vault, { ...input, ts: T0 + MIN, now: T0 + MIN }), false);
    const file = await readPromptTemplates(vault);
    assert.equal(file.templates[hashOf(1)].occ.length, 1);
  });
});

test("the occurrence ring keeps the newest 16 and the session map the earliest 8", async () => {
  await withVault(async (vault) => {
    for (let i = 0; i < 20; i++) {
      await recordOccurrence(vault, {
        hash: hashOf(1),
        ts: T0 + i * MIN,
        pid: `p${i}`,
        sessionKeyPrefix: `s${i % 12}`,
        now: T0 + i * MIN,
      });
    }
    const entry = (await readPromptTemplates(vault)).templates[hashOf(1)];
    assert.equal(entry.first_seen, T0);
    assert.deepEqual(
      entry.occ.map((o) => o.pid),
      Array.from({ length: 16 }, (_, k) => `p${k + 4}`),
    );
    assert.equal(Object.keys(entry.sessions).length, RULE.sessionsMax);
    // Earliest first sightings are kept: s0..s7 first appeared at i = 0..7.
    assert.deepEqual(Object.keys(entry.sessions).sort(), ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"]);
    assert.equal(entry.sessions.s0, T0);
  });
});

test("the file holds at most 500 entries and drops the least recently seen first", async () => {
  await withVault(async (vault) => {
    const rec = (i, pid, ts) =>
      recordOccurrence(vault, {
        hash: hashOf(i),
        ts,
        pid,
        sessionKeyPrefix: "s1",
        now: ts,
      });
    for (let i = 0; i < RULE.entriesMax; i++) await rec(i, `p${i}`, T0 + i * MIN);
    assert.equal(Object.keys((await readPromptTemplates(vault)).templates).length, 500);

    // Touch the oldest entry again: it becomes the most recently seen.
    await rec(0, "touch", T0 + 500 * MIN);
    for (let i = 500; i < 550; i++) await rec(i, `p${i}`, T0 + (i + 1) * MIN);

    const keys = Object.keys((await readPromptTemplates(vault)).templates);
    assert.equal(keys.length, 500);
    assert.ok(keys.includes(hashOf(0)), "a refreshed entry survives");
    for (let i = 1; i <= 50; i++) assert.ok(!keys.includes(hashOf(i)), `entry ${i} is evicted`);
    for (let i = 51; i < 550; i++) assert.ok(keys.includes(hashOf(i)), `entry ${i} is kept`);
  });
});

test("entries idle more than 14 days are pruned, and 14 days exactly is kept", async () => {
  await withVault(async (vault) => {
    const rec = (i, ts) =>
      recordOccurrence(vault, {
        hash: hashOf(i),
        ts,
        pid: `p${i}`,
        sessionKeyPrefix: "s1",
        now: ts,
      });
    await rec(1, T0);
    await rec(2, T0 + DAY);
    await rec(3, T0 + 14 * DAY); // entry 1 has been idle exactly 14 days
    let keys = Object.keys((await readPromptTemplates(vault)).templates);
    assert.deepEqual(keys.sort(), [hashOf(1), hashOf(2), hashOf(3)]);

    await rec(4, T0 + 14 * DAY + MIN); // now entry 1 is idle 14 days and a minute
    keys = Object.keys((await readPromptTemplates(vault)).templates);
    assert.deepEqual(keys.sort(), [hashOf(2), hashOf(3), hashOf(4)]);
    assert.equal(RULE.idleMaxMs, 14 * DAY);
  });
});

test("a symlinked .runtime that escapes the vault is refused", async () => {
  await withVault(async (vault) => {
    const outside = await mkdtemp(path.join(tmpdir(), "minni-prompt-templates-outside-"));
    try {
      await symlink(outside, path.join(vault, ".runtime"));
      await assert.rejects(
        recordOccurrence(vault, {
          hash: hashOf(1),
          ts: T0,
          pid: "p1",
          sessionKeyPrefix: "s1",
          now: T0,
        }),
        /escapes root|symlink|containment/i,
      );
      assert.deepEqual(await readdir(outside), [], "nothing was written outside the vault");
      assert.equal(await readPromptTemplates(vault), null);
    } finally {
      await rm(outside, { recursive: true, force: true });
    }
  });
});

test("a symlinked counts file is refused and its target is left untouched", async () => {
  await withVault(async (vault) => {
    const outside = await mkdtemp(path.join(tmpdir(), "minni-prompt-templates-outside-"));
    try {
      const victim = path.join(outside, "victim.json");
      await writeFile(victim, '{"keep":"me"}', "utf8");
      await mkdir(path.join(vault, ".runtime"), { recursive: true });
      await symlink(victim, promptTemplatesPath(vault));
      await assert.rejects(
        recordOccurrence(vault, {
          hash: hashOf(1),
          ts: T0,
          pid: "p1",
          sessionKeyPrefix: "s1",
          now: T0,
        }),
        /symlink/i,
      );
      assert.equal(await readFile(victim, "utf8"), '{"keep":"me"}');
      assert.equal(await readPromptTemplates(vault), null);
    } finally {
      await rm(outside, { recursive: true, force: true });
    }
  });
});

test("concurrent writers leave a whole file and no temp files behind", async () => {
  await withVault(async (vault) => {
    await Promise.all(
      Array.from({ length: 24 }, (_, i) =>
        recordOccurrence(vault, {
          hash: hashOf(i),
          ts: T0 + i,
          pid: `p${i}`,
          sessionKeyPrefix: `s${i}`,
          now: T0 + i,
        }),
      ),
    );
    // Read-modify-write is last-writer-wins, so some counts may be lost, but the
    // file is never torn.
    const file = await readPromptTemplates(vault);
    assert.ok(file !== null);
    assert.ok(Object.keys(file.templates).length >= 1);
    const leftovers = (await readdir(path.join(vault, ".runtime"))).filter((n) => n !== "prompt-templates.json");
    assert.deepEqual(leftovers, []);
  });
});

test("recordOccurrence refuses malformed input", async () => {
  await withVault(async (vault) => {
    const ok = { hash: hashOf(1), ts: T0, pid: "p1", sessionKeyPrefix: "s1", now: T0 };
    for (const bad of [
      { hash: "XYZ" },
      { hash: "0000000000AB" },
      { hash: "0000000000a" },
      { ts: Number.NaN },
      { ts: "1" },
      { pid: "" },
      { pid: "p".repeat(129) },
      { sessionKeyPrefix: "" },
      { sessionKeyPrefix: "../etc" },
    ]) {
      await assert.rejects(recordOccurrence(vault, { ...ok, ...bad }), TypeError, JSON.stringify(bad));
    }
    assert.equal(await readPromptTemplates(vault), null);
  });
});

test("the reader returns null for a missing, garbled or foreign-version file", async () => {
  await withVault(async (vault) => {
    assert.equal(await readPromptTemplates(vault), null);
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    await writeFile(promptTemplatesPath(vault), "not json {", "utf8");
    assert.equal(await readPromptTemplates(vault), null);
    await writeFile(promptTemplatesPath(vault), JSON.stringify({ v: 2, templates: {} }), "utf8");
    assert.equal(await readPromptTemplates(vault), null);
    await writeFile(promptTemplatesPath(vault), "[]", "utf8");
    assert.equal(await readPromptTemplates(vault), null);
  });
});

test("a plain file where .runtime should be reads as absent and cannot be recorded into", async () => {
  await withVault(async (vault) => {
    await writeFile(path.join(vault, ".runtime"), "not a directory", "utf8");
    assert.equal(await readPromptTemplates(vault), null);
    await assert.rejects(
      recordOccurrence(vault, {
        hash: hashOf(1),
        ts: T0,
        pid: "p1",
        sessionKeyPrefix: "s1",
        now: T0,
      }),
    );
    assert.equal(await readFile(path.join(vault, ".runtime"), "utf8"), "not a directory");
  });
});

test("a foreign-version or oversize file is replaced by a fresh one on the next record", async () => {
  await withVault(async (vault) => {
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    await writeFile(promptTemplatesPath(vault), JSON.stringify({ v: 9, templates: { keep: 1 } }), "utf8");
    assert.equal(await recordOccurrence(vault, {
      hash: hashOf(1),
      ts: T0,
      pid: "p1",
      sessionKeyPrefix: "s1",
      now: T0,
    }), true);
    assert.deepEqual(Object.keys((await readPromptTemplates(vault)).templates), [hashOf(1)]);

    // Anything past 2 MiB is not ours: the reader ignores it, the writer replaces it.
    await writeFile(promptTemplatesPath(vault), " ".repeat(2 * 1024 * 1024 + 1), "utf8");
    assert.equal(await readPromptTemplates(vault), null);
    await recordOccurrence(vault, {
      hash: hashOf(2),
      ts: T0,
      pid: "p2",
      sessionKeyPrefix: "s1",
      now: T0,
    });
    assert.deepEqual(Object.keys((await readPromptTemplates(vault)).templates), [hashOf(2)]);
  });
});

test("a directory where the counts file should be reads as no evidence", async () => {
  await withVault(async (vault) => {
    await mkdir(promptTemplatesPath(vault), { recursive: true });
    assert.equal(await readPromptTemplates(vault), null);
    // The writer may keep throwing: it must not pretend it recorded anything.
    await assert.rejects(
      recordOccurrence(vault, {
        hash: hashOf(1),
        ts: T0,
        pid: "p1",
        sessionKeyPrefix: "s1",
        now: T0,
      }),
      { code: "EISDIR" },
    );
  });
});

test("an unreadable counts file (mode 0o000) reads as no evidence", async (t) => {
  if (typeof process.getuid === "function" && process.getuid() === 0) {
    t.skip("root ignores file modes, so the file would be readable");
    return;
  }
  await withVault(async (vault) => {
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    const file = promptTemplatesPath(vault);
    // A valid file: were it readable, the reader would return it, so null can only
    // come from the permission error.
    await writeFile(file, JSON.stringify({ v: 1, templates: {} }), "utf8");
    await chmod(file, 0o000);
    try {
      assert.equal(await readPromptTemplates(vault), null);
      await assert.rejects(
        recordOccurrence(vault, {
          hash: hashOf(1),
          ts: T0,
          pid: "p1",
          sessionKeyPrefix: "s1",
          now: T0,
        }),
        { code: "EACCES" },
      );
    } finally {
      await chmod(file, 0o600);
    }
  });
});

test("the reader drops invalid entries and fields instead of trusting the file", async () => {
  await withVault(async (vault) => {
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    const good = hashOf(7);
    await writeFile(
      promptTemplatesPath(vault),
      JSON.stringify({
        v: 1,
        templates: {
          [good]: {
            first_seen: T0,
            occ: [
              { ts: T0 + 2, pid: "b" },
              { ts: "bad", pid: "x" },
              { ts: T0 + 1, pid: "" },
              { ts: T0, pid: "a" },
              null,
            ],
            sessions: { s1: T0, "bad key!": T0, s2: "nope" },
          },
          "not-a-hash": { first_seen: T0, occ: [], sessions: {} },
          [hashOf(8)]: { first_seen: "nope", occ: [], sessions: {} },
          [hashOf(9)]: "string",
        },
      }),
      "utf8",
    );
    const file = await readPromptTemplates(vault);
    assert.deepEqual(Object.keys(file.templates), [good]);
    assert.deepEqual(file.templates[good], {
      first_seen: T0,
      occ: [
        { ts: T0, pid: "a" },
        { ts: T0 + 2, pid: "b" },
      ],
      sessions: { s1: T0 },
    });
  });
});

test("a __proto__ key in the file cannot pollute anything", async () => {
  await withVault(async (vault) => {
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    await writeFile(
      promptTemplatesPath(vault),
      '{"v":1,"templates":{"__proto__":{"first_seen":1,"occ":[],"sessions":{}},' +
        `"${hashOf(1)}":{"first_seen":1,"occ":[],"sessions":{"__proto__":5}}}}`,
      "utf8",
    );
    const file = await readPromptTemplates(vault);
    assert.deepEqual(Object.keys(file.templates), [hashOf(1)]);
    assert.deepEqual(file.templates[hashOf(1)].sessions, {});
    assert.equal({}.first_seen, undefined);
    assert.equal({}.polluted, undefined);
  });
});
