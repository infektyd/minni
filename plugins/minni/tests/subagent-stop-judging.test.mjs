import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  agentKeyOf,
  daemonResult,
  makeFixture,
  readLedger,
  runHook,
  sessionKeyOf,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const SESSION = "sess-subagent-stop-01";
const SUBAGENT_ID = "agent-sub-worker-42";

test(
  "SubagentStop runs use detector against agent_transcript_path with sessionEnd:true and writes final judged + exposure rows under subagent agent_key",
  { timeout: 60_000 },
  async (t) => {
    const fixture = await makeFixture(t, "subagent-stop-judging");
    const taskNote = path.join(fixture.vault, "wiki", "ops", "task-note.md");
    await mkdir(path.dirname(taskNote), { recursive: true });
    await writeFile(
      taskNote,
      "# Task Note\n\nSubagent operational protocol requires strict atomic claim files.\n",
      "utf8",
    );

    await startFakeDaemon(fixture, {
      search: () => ({
        agent_id: "claude-code",
        results: [
          daemonResult({
            source: taskNote,
            docId: 501,
            wikilink: "[[wiki/ops/task-note]]",
            title: "Task Note",
            confidence: 0.96,
          }),
        ],
      }),
    });

    // 1. Parent PreToolUse on Agent stashes the pending subagent task prompt.
    await runHook(fixture, "PreToolUse", {
      session_id: SESSION,
      tool_name: "Agent",
      tool_use_id: "toolu_stop_judge_01",
      tool_input: {
        subagent_type: "Explore",
        prompt: "verify subagent operational protocol",
      },
    });

    // 2. SubagentStart claims the prompt and surfaces the brief under SUBAGENT_ID.
    const startRes = await runHook(fixture, "SubagentStart", {
      session_id: SESSION,
      agent_id: SUBAGENT_ID,
      agent_type: "Explore",
    });
    assert.equal(startRes.code, 0);
    assert.match(
      startRes.output.hookSpecificOutput?.additionalContext ?? "",
      /\[\[wiki\/ops\/task-note\]\]/,
    );

    // 3. Write a subagent JSONL transcript at agent_transcript_path after the surfacing timestamp.
    const agentTranscriptPath = path.join(fixture.root, `${SUBAGENT_ID}.jsonl`);
    const t1 = new Date(Date.now() + 50).toISOString();
    const t2 = new Date(Date.now() + 100).toISOString();
    const transcriptLines = [
      JSON.stringify({
        type: "user",
        timestamp: t1,
        message: {
          role: "user",
          content: "verify subagent operational protocol",
        },
      }),
      JSON.stringify({
        type: "assistant",
        timestamp: t2,
        message: {
          id: "msg_sub_01",
          role: "assistant",
          content: [
            {
              type: "text",
              text: "Consulting [[wiki/ops/task-note]] to verify the operational protocol.",
            },
            {
              type: "tool_use",
              id: "toolu_read_note",
              name: "Read",
              input: { file_path: taskNote },
            },
          ],
        },
      }),
    ];
    await writeFile(agentTranscriptPath, `${transcriptLines.join("\n")}\n`, "utf8");

    // 4. Run SubagentStop with agent_transcript_path.
    const stopRes = await runHook(fixture, "SubagentStop", {
      session_id: SESSION,
      agent_id: SUBAGENT_ID,
      agent_type: "Explore",
      agent_transcript_path: agentTranscriptPath,
    });
    assert.equal(stopRes.code, 0);
    assert.deepEqual(stopRes.output, { continue: true });

    // 5. Verify final judged and exposure rows under the subagent's agent_key.
    const expectedAgentKey = agentKeyOf(SUBAGENT_ID);
    const expectedSessionKey = sessionKeyOf(SESSION);
    const rows = await readLedger(fixture.vault);

    const surfacedRows = rows.filter(
      (r) =>
        r.kind === "surfaced" &&
        r.agent_key === expectedAgentKey &&
        r.channel === "subagent_brief",
    );
    assert.equal(surfacedRows.length, 1);

    const judgedRows = rows.filter(
      (r) =>
        r.kind === "judged" &&
        r.agent_key === expectedAgentKey &&
        r.sid === surfacedRows[0].sid,
    );
    assert.ok(judgedRows.length >= 1, "SubagentStop must write a judged row for subagent_brief");
    const finalJudged = judgedRows.find((r) => r.final === true);
    assert.ok(finalJudged, "judged row must be final:true");
    assert.equal(finalJudged.session_key, expectedSessionKey);
    assert.ok(
      ["opened", "cited", "applied"].includes(finalJudged.outcome),
      `expected used outcome (opened/cited/applied), got ${finalJudged.outcome}`,
    );

    const exposureRows = rows.filter(
      (r) =>
        r.kind === "exposure" &&
        r.agent_key === expectedAgentKey &&
        r.channel === "subagent_brief",
    );
    assert.equal(
      exposureRows.length,
      1,
      "SubagentStop with sessionEnd:true must close and write the final exposure row",
    );
    assert.equal(exposureRows[0].final, true);
    assert.equal(exposureRows[0].session_key, expectedSessionKey);
    assert.equal(exposureRows[0].mkey, "vault:wiki/ops/task-note.md");
  },
);
