// Recall-to-use S1b (§4.4): the outcome rules, one case each.
//
// Order: applied, then cited (handle or title), then opened, then dismissed
// (sticky), then ignored/unknown. Title citation needs at least 3 distinctive
// title tokens in one assistant sentence, after dropping the tokens of the
// current and previous 2 prompts. Writing a handle and then opening it is
// `opened`, not `cited`. Boot channels' injected text IS the memory, so its
// shingles count only inside a tool input.
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { echoHashes, shingleHashes, vaultHandle } from "../dist/use-ledger.js";
import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  finalJudged,
  makeVault,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
  writeNote,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

async function setup(t) {
  const env = await makeVault(t);
  const clock = new Clock();
  return { ...env, clock, tx: new Transcript(env.transcriptPath, clock) };
}

function stop(s, lastAssistantMessage, extra = {}) {
  const result = runDetector(detectorArgs(s.vault, s.transcriptPath, s.clock, { lastAssistantMessage, ...extra }));
  assert.equal(result.status, "ran", JSON.stringify(result));
  return result;
}

test("applied: a body shingle the injection did not carry, inside a tool input", async (t) => {
  const s = await setup(t);
  const body = [
    "# Quartz rotation",
    "",
    "## Resolution",
    "Always drain the quartz queue before rotating the lattice keys at midnight.",
  ].join("\n");
  s.tx.prompt("Please fix the nightly rotation job.", "p1");
  const row = surfacedRow({
    mkey: "vault:wiki/sessions/quartz-rotation.md",
    title: "Quartz rotation",
    promptId: "p1",
    ts: s.clock.next(10),
    shingles: shingleHashes(body),
    echo: echoHashes("Relevant memory: Quartz rotation (m:0a1b2c3d)."),
    turnIx: 1,
  });
  append(s.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: row.ts }), row]);
  s.tx.tool("Edit", {
    file_path: "/synthetic/rotate.ts",
    old_string: "rotate();",
    new_string: "// always drain the quartz queue before rotating the lattice keys at midnight\ndrain();\nrotate();",
  });
  s.tx.say("Done: the job drains first now.");
  await s.tx.flush();
  stop(s, "Done: the job drains first now.");

  const judged = finalJudged(s.vault, row.sid);
  assert.equal(judged.outcome, "applied");
  assert.ok(judged.evidence.includes("shingle"));
});

test("applied on a boot channel: reciting injected text is not applying it; a tool input is", async (t) => {
  const s = await setup(t);
  const recited = "Run the beryl migration scripts in numeric order, never in parallel batches.";
  const written = "Rotate the harbor signing keys only after the quartz drain has fully completed.";
  const boot = (mkey, text, rank) =>
    surfacedRow({
      channel: "boot_recent_learnings",
      mkey,
      title: text,
      rank,
      ts: s.clock.next(10),
      shingles: shingleHashes(text),
      echo: echoHashes(text),
    });
  const onlyRecited = boot("learning:41", recited, 0);
  const alsoWritten = boot("learning:42", written, 1);
  append(s.vault, [onlyRecited, alsoWritten]);

  s.tx.prompt("Set up the database for the release.", "p1");
  append(s.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: s.clock.next(5) })]);
  s.tx.say(`Noted: ${recited.toLowerCase()} Also: ${written.toLowerCase()}`);
  await s.tx.flush();
  stop(s, `Noted: ${recited.toLowerCase()} Also: ${written.toLowerCase()}`);

  s.tx.prompt("Go ahead with the plan.", "p2");
  append(s.vault, [turnRow({ turnIx: 2, promptId: "p2", ts: s.clock.next(5) })]);
  s.tx.tool("Write", { file_path: "/synthetic/PLAN.md", content: `Checklist: ${written}` });
  s.tx.say("Plan written.");
  await s.tx.flush();
  stop(s, "Plan written.");

  assert.equal(finalJudged(s.vault, onlyRecited.sid).outcome, "ignored", "recital in assistant text is not use");
  const applied = finalJudged(s.vault, alsoWritten.sid);
  assert.equal(applied.outcome, "applied", "the injected text inside a tool input is use");
  assert.ok(applied.evidence.includes("shingle"));
});

test("title-cited: 3 distinctive title tokens in one sentence, prompt tokens excluded", async (t) => {
  const s = await setup(t);
  await writeDfSnapshot(s.vault, { the: 90, note: 80 });

  // Turn 1: "harbor" is in the prompt, so only beryl/checkpoint/cadence count.
  s.tx.prompt("Why does the harbor job stall overnight?", "p1");
  const echoed = surfacedRow({
    mkey: "vault:wiki/sessions/beryl-cadence.md",
    title: "Beryl harbor checkpoint cadence",
    promptId: "p1",
    ts: s.clock.next(10),
  });
  append(s.vault, [turnRow({ turnIx: 1, promptId: "p1", ts: echoed.ts }), echoed]);
  s.tx.say("The harbor checkpoint cadence looks stale.");
  await s.tx.flush();
  stop(s, "The harbor checkpoint cadence looks stale.");

  // Turn 2: "quartz" is in the prompt; lattice/drain/ordering remain.
  s.tx.prompt("And the quartz one?", "p2");
  const cited = surfacedRow({
    mkey: "vault:wiki/sessions/quartz-drain.md",
    title: "Quartz lattice drain ordering",
    promptId: "p2",
    ts: s.clock.next(10),
  });
  const tooShort = surfacedRow({
    mkey: "vault:wiki/sessions/quartz-lattice.md",
    title: "Quartz lattice",
    promptId: "p2",
    rank: 1,
    ts: cited.ts,
  });
  append(s.vault, [turnRow({ turnIx: 2, promptId: "p2", ts: cited.ts }), cited, tooShort]);
  s.tx.say("Per the quartz lattice drain ordering, I drained the queue first.");
  await s.tx.flush();
  stop(s, "Per the quartz lattice drain ordering, I drained the queue first.");

  const notCited = finalJudged(s.vault, echoed.sid);
  assert.equal(notCited.outcome, "ignored", "a prompt token does not count toward the 3");
  assert.equal(notCited.title_assessable, true);

  const yes = finalJudged(s.vault, cited.sid);
  assert.equal(yes.outcome, "cited");
  assert.ok(yes.evidence.includes("title"));
  assert.equal(yes.title_assessable, true);

  const short = finalJudged(s.vault, tooShort.sid);
  assert.equal(short.title_assessable, false, "fewer than 3 distinctive tokens left");
  assert.notEqual(short.outcome, "cited");
});

test("title rule off without a DF snapshot", async (t) => {
  const s = await setup(t);
  s.tx.prompt("Look at the overnight stall.", "p1");
  const row = surfacedRow({
    mkey: "vault:wiki/sessions/beryl-cadence.md",
    title: "Beryl harbor checkpoint cadence",
    promptId: "p1",
    ts: s.clock.next(10),
  });
  append(s.vault, [row]);
  s.tx.say("Per the beryl harbor checkpoint cadence, I moved the job.");
  await s.tx.flush();
  const result = stop(s, "Per the beryl harbor checkpoint cadence, I moved the job.");
  assert.equal(result.title_rule, "off");
  const judged = finalJudged(s.vault, row.sid);
  assert.equal(judged.outcome, "ignored");
  assert.equal(judged.title_assessable, false);
});

test("opened: a Read of the note's path, or a read-verb Bash over it", async (t) => {
  const s = await setup(t);
  await writeNote(s.vault, "wiki/sessions/gamma-note.md", "# Gamma note\n");
  await writeNote(s.vault, "wiki/sessions/delta-runbook.md", "# Delta runbook\n");
  s.tx.prompt("What did we decide about gamma?", "p1");
  const read = surfacedRow({ mkey: "vault:wiki/sessions/gamma-note.md", title: "Gamma note", promptId: "p1", ts: s.clock.next(10) });
  const bash = surfacedRow({
    mkey: "vault:wiki/sessions/delta-runbook.md",
    title: "Delta runbook",
    promptId: "p1",
    rank: 1,
    ts: read.ts,
  });
  append(s.vault, [read, bash]);
  s.tx.tool("Read", { file_path: path.join(s.vault, "wiki", "sessions", "gamma-note.md") });
  s.tx.tool("Bash", { command: "sed -n '1,40p' wiki/sessions/delta-runbook.md" });
  s.tx.say("I read both.");
  await s.tx.flush();
  stop(s, "I read both.");

  for (const row of [read, bash]) {
    const judged = finalJudged(s.vault, row.sid);
    assert.equal(judged.outcome, "opened", row.mkey);
    assert.ok(judged.evidence.includes("open"));
  }
});

test("dismissal is sticky: it blocks a later citation but not an application", async (t) => {
  const s = await setup(t);
  await writeDfSnapshot(s.vault);
  const body = "## Fix\nPin the harbor resolver to the beryl mirror before the nightly fetch runs.";
  s.tx.prompt("Fix the flaky build please.", "p1");
  const narrated = surfacedRow({
    mkey: "vault:wiki/sessions/beryl-cadence.md",
    title: "Beryl harbor checkpoint cadence",
    promptId: "p1",
    ts: s.clock.next(10),
  });
  const applied = surfacedRow({
    mkey: "vault:wiki/sessions/resolver-pin.md",
    title: "Resolver pin",
    promptId: "p1",
    rank: 1,
    ts: narrated.ts,
    shingles: shingleHashes(body),
    echo: echoHashes("Relevant memory: Resolver pin."),
  });
  append(s.vault, [narrated, applied]);
  s.tx.say("Not relevant to this task.");
  s.tx.say("For the record, the beryl harbor checkpoint cadence note covers something else.");
  s.tx.tool("Edit", {
    file_path: "/synthetic/build.sh",
    old_string: "fetch",
    new_string: "# pin the harbor resolver to the beryl mirror before the nightly fetch runs\nfetch",
  });
  s.tx.say("Build fixed.");
  await s.tx.flush();
  stop(s, "Build fixed.");

  const dismissed = finalJudged(s.vault, narrated.sid);
  assert.equal(dismissed.outcome, "dismissed", "a dismissed window never ends cited");
  assert.ok(dismissed.evidence.includes("lexicon"));
  assert.equal(finalJudged(s.vault, applied.sid).outcome, "applied", "a dismissed window can still end applied");
});

test("naming a handle and then opening it is opened; opening and then citing is cited", async (t) => {
  const s = await setup(t);
  const deltaRel = "wiki/sessions/delta-note.md";
  const epsilonRel = "wiki/sessions/epsilon-note.md";
  await writeNote(s.vault, deltaRel, "# Delta note\n");
  await writeNote(s.vault, epsilonRel, "# Epsilon note\n");
  const deltaHandle = vaultHandle(deltaRel);
  const epsilonHandle = vaultHandle(epsilonRel);
  s.tx.prompt("Check the delta and epsilon notes.", "p1");
  const namedThenOpened = surfacedRow({
    mkey: `vault:${deltaRel}`,
    title: "Delta note",
    handles: [deltaHandle],
    promptId: "p1",
    ts: s.clock.next(10),
  });
  const openedThenCited = surfacedRow({
    mkey: `vault:${epsilonRel}`,
    title: "Epsilon note",
    handles: [epsilonHandle],
    promptId: "p1",
    rank: 1,
    ts: namedThenOpened.ts,
  });
  append(s.vault, [namedThenOpened, openedThenCited]);
  s.tx.say(`I will check ${deltaHandle} first.`);
  s.tx.tool("mcp__minni__minni_drill", { references: [deltaHandle] });
  s.tx.tool("Read", { file_path: path.join(s.vault, epsilonRel) });
  s.tx.say(`Per ${epsilonHandle}, the retry budget is already raised.`);
  await s.tx.flush();
  stop(s, `Per ${epsilonHandle}, the retry budget is already raised.`);

  const opened = finalJudged(s.vault, namedThenOpened.sid);
  assert.equal(opened.outcome, "opened", "a handle written before opening is not a citation");
  const cited = finalJudged(s.vault, openedThenCited.sid);
  assert.equal(cited.outcome, "cited");
  assert.ok(cited.evidence.includes("handle"));
});

test("an invisible surfacing with no evidence is unknown(not_visible); an undelivered one unknown(not_delivered)", async (t) => {
  const s = await setup(t);
  s.tx.prompt("Anything on the harbor stall?", "p1");
  const hidden = surfacedRow({ mkey: "vault:wiki/sessions/hidden.md", title: "Hidden note", promptId: "p1", visible: false, ts: s.clock.next(10) });
  const dropped = surfacedRow({
    mkey: "vault:wiki/sessions/dropped.md",
    title: "Dropped note",
    promptId: "p1",
    rank: 1,
    visible: false,
    delivered: false,
    ts: hidden.ts,
  });
  append(s.vault, [hidden, dropped]);
  s.tx.say("Nothing obvious yet.");
  await s.tx.flush();
  stop(s, "Nothing obvious yet.");
  assert.equal(finalJudged(s.vault, hidden.sid).outcome, "unknown(not_visible)");
  assert.equal(finalJudged(s.vault, dropped.sid).outcome, "unknown(not_delivered)");
});
