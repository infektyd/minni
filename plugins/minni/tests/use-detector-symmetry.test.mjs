// Recall-to-use S1b: symmetry of every detector rule across pointer formats.
//
// The before/after comparison (rev5 §4.7) is valid only if a citation passes
// or fails each rule identically in both windows. Each case feeds the SAME
// assistant transcript through two ledgers that differ only in pointer format:
//   - BEFORE, today's pointer (recall-state.ts buildRecallPointer): title only,
//     no handles, re-named on every strong turn;
//   - AFTER, rev5 §5.2 items 6-7: each named hit as `"<title ≤ 100>"
//     (m:<8hex>) <score>`, no wikilink, named once and then suppressed until a
//     reset or 10 turns have passed.
// and asserts equal used_ii (exposure credits), equal dismissed counts and
// equal exposure outcomes. Both ledgers record the memory's full title and
// body shingles; only the handles, the injected text (echo), the naming
// cadence and format-dependent actions (each window opens the memory its own
// natural way) differ. The one intended difference is the last case: a handle
// can be cited only where one was printed, and it lands in cited_handle, never
// in used_ii.
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { echoHashes, shingleHashes, vaultHandle } from "../dist/use-ledger.js";
import { runDetector } from "../dist/use-detector.js";
import {
  append,
  Clock,
  detectorArgs,
  ledgerRows,
  makeVault,
  resetRow,
  surfacedRow,
  Transcript,
  turnRow,
  writeDfSnapshot,
  writeNote,
} from "./fixtures/use-detector-fixtures.mjs";

delete process.env.MINNI_USE_LEDGER;

const REL = "wiki/sessions/beryl-cadence.md";
const MKEY = `vault:${REL}`;
const HANDLE = vaultHandle(REL);
const TITLE = "Beryl harbor checkpoint cadence";
const BODY = [
  "# Beryl harbor checkpoint cadence",
  "",
  "## Resolution",
  "Move the nightly harbor job ahead of the checkpoint flush so the cadence holds.",
].join("\n");
const RUN = "move the nightly harbor job ahead of the checkpoint flush so the cadence holds";
const CITE = "Per the beryl harbor checkpoint cadence, I moved the job earlier.";
const LEARNING = "Run the beryl migration scripts in numeric order, never in parallel batches.";

const BEFORE = {
  name: "before",
  renames: true,
  handles: [],
  text: (title) =>
    `📓 1 relevant memory (top: "${title}", score 0.91). Consult minni_recall (or the recall-state file) before deriving from scratch.`,
};
const AFTER = {
  name: "after",
  renames: false,
  handles: [HANDLE],
  text: (title) => `📓 1 relevant memory: "${title.slice(0, 100)}" (${HANDLE}) 0.91. Consult minni_recall before deriving from scratch.`,
};

/**
 * Run one window. `turns[i]` = { prompt?, strong? (default true), resetBefore?,
 * actions(format) => [text | [toolName, input]] }. Returns the exposures as
 * comparable facts: epoch order, mkey, credits, outcome, dismissed.
 */
async function runWindow(t, format, { turns, boot = false }) {
  const env = await makeVault(t);
  await writeDfSnapshot(env.vault, { the: 90 });
  await writeNote(env.vault, REL, BODY);
  const clock = new Clock();
  const tx = new Transcript(env.transcriptPath, clock);
  if (boot) {
    append(env.vault, [
      surfacedRow({
        channel: "boot_recent_learnings",
        mkey: "learning:41",
        title: LEARNING,
        ts: clock.next(10),
        shingles: shingleHashes(LEARNING),
        echo: echoHashes(LEARNING),
      }),
    ]);
  }
  let lastNamed = null;
  for (let i = 0; i < turns.length; i += 1) {
    const ix = i + 1;
    const spec = turns[i];
    if (spec.resetBefore) {
      append(env.vault, [resetRow({ ts: clock.next(50), event: "PreCompact" })]);
      lastNamed = null; // suppression is per reset id
    }
    const promptId = `p${ix}`;
    tx.prompt(spec.prompt ?? `Synthetic follow-up number ${ix}.`, promptId);
    const rows = [turnRow({ turnIx: ix, promptId, ts: clock.next(5) })];
    const strong = spec.strong !== false;
    const named = strong && (format.renames || lastNamed === null || ix - lastNamed >= 10);
    if (named) {
      rows.push(
        surfacedRow({
          mkey: MKEY,
          title: TITLE,
          handles: format.handles,
          promptId,
          ts: clock.next(10),
          shingles: shingleHashes(BODY),
          echo: echoHashes(format.text(TITLE)),
        }),
      );
      lastNamed = ix;
    }
    append(env.vault, rows);
    let last = null;
    for (const action of spec.actions(format)) {
      if (typeof action === "string") {
        tx.say(action);
        last = action;
      } else {
        tx.tool(action[0], action[1]);
      }
    }
    await tx.flush();
    const result = runDetector(detectorArgs(env.vault, env.transcriptPath, clock, { lastAssistantMessage: last }));
    assert.equal(result.status, "ran", JSON.stringify(result));
  }
  const closed = runDetector(detectorArgs(env.vault, env.transcriptPath, clock, { endsTurn: false, sessionEnd: true }));
  assert.equal(closed.status, "ran");
  const epochs = [];
  const facts = ledgerRows(env.vault, "exposure").map((exposure) => {
    if (!epochs.includes(exposure.reset_id)) epochs.push(exposure.reset_id);
    return {
      epoch: epochs.indexOf(exposure.reset_id),
      mkey: exposure.mkey,
      credits: exposure.credits,
      outcome: exposure.outcome,
      dismissed: (exposure.dismissed_turns ?? 0) > 0,
    };
  });
  facts.sort((a, b) => a.epoch - b.epoch || a.mkey.localeCompare(b.mkey));
  return {
    facts,
    used_ii: facts.filter((fact) => fact.credits.length > 0).length,
    dismissed: facts.filter((fact) => fact.dismissed).length,
  };
}

async function symmetric(t, script) {
  const before = await runWindow(t, BEFORE, script);
  const after = await runWindow(t, AFTER, script);
  assert.deepEqual(after, before, "the same transcript is judged identically in both windows");
  return before;
}

const plain = (text) => () => [text];

test("applied in a tool input", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: plain("Still looking.") },
      { actions: () => [["Edit", { file_path: "/synthetic/jobs.ts", old_string: "x", new_string: `// ${RUN}` }], "Done."] },
    ],
  });
  assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[3], "applied"]]);
});

test("applied in assistant text", async (t) => {
  const result = await symmetric(t, {
    turns: [{ prompt: "Look at the nightly stall.", actions: plain("Looking.") }, { actions: plain(`The fix: ${RUN}.`) }],
  });
  assert.deepEqual(result.facts[0].credits, [2]);
});

test("title or content cited", async (t) => {
  const result = await symmetric(t, {
    turns: [{ prompt: "Look at the nightly stall.", actions: plain("Looking.") }, { actions: plain("Still.") }, { actions: plain(CITE) }],
  });
  assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[3], "cited"]]);
});

test("handle cited (the same text in both windows): cited_handle, never used_ii", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain(`See ${HANDLE} for the fix.`) },
      { actions: plain("See [[wiki/sessions/beryl-cadence]] and beryl-cadence again.") },
    ],
  });
  assert.equal(result.used_ii, 0);
  assert.equal(result.facts[0].outcome, "cited_handle");
});

const RECALL_BY_TITLE = ["mcp__minni__minni_recall", { query: TITLE }];
const DRILL_HANDLE = ["mcp__minni__minni_drill", { references: [HANDLE] }];

test("recall by title is an open in both windows", async (t) => {
  const result = await symmetric(t, {
    turns: [{ prompt: "Look at the nightly stall.", actions: () => ["Checking the archive.", RECALL_BY_TITLE, "Done."] }],
  });
  assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[], "opened"]]);
});

for (const afterOpen of ["drill", "recall"]) {
  test(`an open alone: before recalls by title, after opens by ${afterOpen}`, async (t) => {
    const result = await symmetric(t, {
      turns: [
        {
          prompt: "Look at the nightly stall.",
          actions: (format) => [
            "Checking the archive.",
            format.name === "after" && afterOpen === "drill" ? DRILL_HANDLE : RECALL_BY_TITLE,
            "Done.",
          ],
        },
      ],
    });
    assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[], "opened"]]);
  });

  test(`a title citation, then each window's natural open (before: recall by title; after: ${afterOpen})`, async (t) => {
    const result = await symmetric(t, {
      turns: [
        {
          prompt: "Look at the nightly stall.",
          actions: (format) => [
            "Per the beryl harbor checkpoint cadence note, let me check it.",
            format.name === "after" && afterOpen === "drill" ? DRILL_HANDLE : RECALL_BY_TITLE,
            "Checked; nothing to change.",
          ],
        },
      ],
    });
    assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[1], "cited"]]);
  });
}

test("opened", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: () => [["Read", { file_path: path.join("/synthetic-vault", REL) }], "Read it."] },
    ],
  });
  assert.deepEqual(result.facts.map((fact) => [fact.credits, fact.outcome]), [[[], "opened"]]);
});

test("dismissed on an un-named turn, then cited", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: () => ["Not relevant to this task.", "Moving on."] },
      { actions: plain(CITE) },
    ],
  });
  assert.equal(result.dismissed, 1);
  assert.deepEqual(result.facts[0].credits, [3], "dismissal is a diagnostic; it never gates a credit");
});

test("echo filter: restating a prompt that quotes the body is not applied", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { prompt: `Should we ${RUN}?`, actions: plain(`Yes, ${RUN}.`) },
    ],
  });
  assert.equal(result.used_ii, 0);
});

test("boot recital vs paraphrase", async (t) => {
  const result = await symmetric(t, {
    boot: true,
    turns: [
      { prompt: "Set up the database.", actions: plain(`Noted: ${LEARNING.toLowerCase()}`) },
      { prompt: "Go ahead.", actions: plain("I will keep the beryl migrations strictly numeric and avoid parallel runs.") },
    ],
  });
  const learning = result.facts.find((fact) => fact.mkey === "learning:41");
  assert.deepEqual(learning.credits, [2], "the recital on turn 1 is not use; the paraphrase on turn 2 is");
});

test("exposure credits across turns", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: plain(CITE) },
      { actions: plain("Still.") },
      { actions: plain(CITE) },
    ],
  });
  assert.deepEqual(result.facts[0].credits, [2, 4]);
});

test("reset handling: the reset closes the epoch in both windows", async (t) => {
  const result = await symmetric(t, {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: plain(CITE) },
      { resetBefore: true, actions: plain(CITE) },
    ],
  });
  assert.deepEqual(result.facts.map((fact) => [fact.epoch, fact.credits]), [[0, [2]], [1, [3]]]);
});

test("the intended difference: only the after window can cite a handle, and it never reaches used_ii", async (t) => {
  const script = {
    turns: [
      { prompt: "Look at the nightly stall.", actions: plain("Looking.") },
      { actions: (format) => [format.name === "after" ? `Per ${HANDLE}, moved it.` : "Per the note, moved it."] },
    ],
  };
  const before = await runWindow(t, BEFORE, script);
  const after = await runWindow(t, AFTER, script);
  assert.equal(after.used_ii, before.used_ii, "used_ii is equal");
  assert.equal(before.facts[0].outcome, "ignored");
  assert.equal(after.facts[0].outcome, "cited_handle", "the handle lands in its own bucket");
});

test("a known difference (F1, pinned): announcing an open names the title in before but a handle in after", async (t) => {
  // Each window announces with the identifier its own pointer gave it. BEFORE
  // only has the title, and a title citation is used_ii; AFTER also has a
  // handle, and a handle citation never is. This inflates BEFORE, which is
  // conservative for the before/after comparison (rev5 §4.4, §4.7).
  const script = {
    turns: [
      {
        prompt: "Look at the nightly stall.",
        actions: (format) =>
          format.name === "after"
            ? [`Let me drill ${HANDLE} first.`, DRILL_HANDLE, "Checked; nothing to change."]
            : [`Let me recall the ${TITLE} note first.`, RECALL_BY_TITLE, "Checked; nothing to change."],
      },
    ],
  };
  const before = await runWindow(t, BEFORE, script);
  const after = await runWindow(t, AFTER, script);
  assert.deepEqual(before.facts.map((fact) => [fact.credits, fact.outcome]), [[[1], "cited"]]);
  assert.deepEqual(after.facts.map((fact) => [fact.credits, fact.outcome]), [[[], "opened"]]);
});
