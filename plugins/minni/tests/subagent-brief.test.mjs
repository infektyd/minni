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

const SESSION = "sess-subagent-brief-01";

test(
  "SubagentStart claims pending prompt, runs bounded task recall, builds <=600 char brief, and records subagent_brief ledger rows with shingles and echo",
  { timeout: 60_000 },
  async (t) => {
    const fixture = await makeFixture(t, "subagent-brief");
    const parentNote = path.join(fixture.vault, "wiki", "arch", "parent-note.md");
    const taskNote = path.join(fixture.vault, "wiki", "ops", "task-note.md");
    await mkdir(path.dirname(parentNote), { recursive: true });
    await mkdir(path.dirname(taskNote), { recursive: true });
    await writeFile(
      parentNote,
      "# Parent Note\n\nParent architecture context requires atomic session files inside the runtime directory.\n",
      "utf8",
    );
    await writeFile(
      taskNote,
      "# Task Note\n\nSubagent task operational context requires exclusive claim markers during spawn.\n",
      "utf8",
    );

    await startFakeDaemon(fixture, {
      search: (params) => {
        const q = String(params.query ?? "");
        if (q.includes("parent architecture")) {
          return {
            agent_id: "claude-code",
            results: [
              daemonResult({
                source: parentNote,
                docId: 101,
                wikilink: "[[wiki/arch/parent-note]]",
                title: "Parent Note",
                confidence: 0.95,
              }),
              daemonResult({
                source: "/shared/parent-two.md",
                docId: 102,
                src: "shared",
                wikilink: "[[wiki/parent-two]]",
                title: "Parent Two",
                confidence: 0.89,
                text: "Shared parent two architecture document describes cross-session isolation invariants.",
              }),
            ],
          };
        }
        return {
          agent_id: "claude-code",
          results: [
            daemonResult({
              source: taskNote,
              docId: 201,
              wikilink: "[[wiki/ops/task-note]]",
              title: "Task Note",
              confidence: 0.93,
            }),
          ],
        };
      },
    });

    // 1. Parent UserPromptSubmit arms RecallState and writes hit-keys.json.
    await runHook(fixture, "UserPromptSubmit", {
      session_id: SESSION,
      prompt_id: "p-parent-1",
      prompt: "explain the parent architecture design",
    });

    // 2. Parent PreToolUse on Agent stashes the pending subagent prompt.
    const preOut = await runHook(fixture, "PreToolUse", {
      session_id: SESSION,
      tool_name: "Agent",
      tool_use_id: "toolu_sub_01",
      tool_input: {
        subagent_type: "Explore",
        prompt: "inspect subagent task operational context",
      },
    });
    assert.deepEqual(preOut.output, { continue: true });

    // 3. SubagentStart claims the pending prompt, runs bounded task recall, and emits brief.
    const startRes = await runHook(fixture, "SubagentStart", {
      session_id: SESSION,
      agent_id: "agent-sub-worker-1",
      agent_type: "Explore",
    });
    assert.equal(startRes.code, 0);
    assert.equal(startRes.output.continue, true);
    assert.equal(startRes.output.hookSpecificOutput?.hookEventName, "SubagentStart");
    const brief = startRes.output.hookSpecificOutput?.additionalContext ?? "";
    assert.ok(brief.length > 0 && brief.length <= 600, `brief length (${brief.length}) must be in 1..600`);
    assert.match(brief, /\[\[wiki\/arch\/parent-note\]\]/);
    assert.match(brief, /\[\[wiki\/ops\/task-note\]\]/);

    // 4. Ledger records surfaced rows under channel: "subagent_brief" and the subagent's agent_key.
    const rows = (await readLedger(fixture.vault)).filter(
      (row) => row.kind === "surfaced" && row.channel === "subagent_brief",
    );
    assert.equal(rows.length, 3, "2 parent hits + 1 task hit recorded under subagent_brief");
    const expectedAgentKey = agentKeyOf("agent-sub-worker-1");
    for (const row of rows) {
      assert.equal(row.session_key, sessionKeyOf(SESSION));
      assert.equal(row.agent_key, expectedAgentKey);
      assert.equal(row.subagent_type, "Explore");
      assert.equal(row.delivered, true);
      assert.equal(row.visible, true);
      assert.ok(Array.isArray(row.shingles) && row.shingles.length > 0, `expected shingles on ${row.mkey}`);
      assert.ok(Array.isArray(row.echo), `expected echo array on ${row.mkey}`);
      assert.ok(Array.isArray(row.handles) && row.handles.length > 0, `expected handles on ${row.mkey}`);
    }
    assert.deepEqual(
      rows.map((r) => r.mkey),
      ["vault:wiki/arch/parent-note.md", "doc:shared:102", "vault:wiki/ops/task-note.md"],
    );
  },
);

test(
  "Two stashed prompts of one type are claimed once each in FIFO order (wx claim marker), and a third SubagentStart with no remaining prompt carries the parent named set alone",
  { timeout: 60_000 },
  async (t) => {
    const fixture = await makeFixture(t, "subagent-fifo-and-parent-only");
    await startFakeDaemon(fixture, {
      search: (params) => {
        const q = String(params.query ?? "");
        if (q.includes("parent-baseline")) {
          return {
            agent_id: "claude-code",
            results: [
              daemonResult({
                source: "/shared/parent.md",
                docId: 300,
                src: "shared",
                wikilink: "[[wiki/parent-baseline]]",
                title: "Parent Baseline",
                confidence: 0.96,
              }),
            ],
          };
        }
        if (q.includes("alpha-task")) {
          return {
            agent_id: "claude-code",
            results: [
              daemonResult({
                source: "/shared/alpha.md",
                docId: 301,
                src: "shared",
                wikilink: "[[wiki/alpha-hit]]",
                title: "Alpha Hit",
                confidence: 0.94,
              }),
            ],
          };
        }
        if (q.includes("beta-task")) {
          return {
            agent_id: "claude-code",
            results: [
              daemonResult({
                source: "/shared/beta.md",
                docId: 302,
                src: "shared",
                wikilink: "[[wiki/beta-hit]]",
                title: "Beta Hit",
                confidence: 0.92,
              }),
            ],
          };
        }
        return { agent_id: "claude-code", results: [] };
      },
    });

    await runHook(fixture, "UserPromptSubmit", {
      session_id: SESSION,
      prompt: "explain parent-baseline architecture",
    });

    await runHook(fixture, "PreToolUse", {
      session_id: SESSION,
      tool_name: "Agent",
      tool_use_id: "toolu_fifo_1",
      tool_input: {
        subagent_type: "Explore",
        prompt: "investigate alpha-task subsystem",
      },
    });
    await runHook(fixture, "PreToolUse", {
      session_id: SESSION,
      tool_name: "Agent",
      tool_use_id: "toolu_fifo_2",
      tool_input: {
        subagent_type: "Explore",
        prompt: "investigate beta-task subsystem",
      },
    });

    // Sequential claims verify strict FIFO ordering: first SubagentStart gets alpha-hit, second gets beta-hit.
    const first = await runHook(fixture, "SubagentStart", {
      session_id: SESSION,
      agent_id: "sub-fifo-1",
      agent_type: "Explore",
    });
    const brief1 = first.output.hookSpecificOutput?.additionalContext ?? "";
    assert.match(brief1, /\[\[wiki\/parent-baseline\]\]/);
    assert.match(brief1, /\[\[wiki\/alpha-hit\]\]/, "1st SubagentStart must claim 1st stashed prompt (alpha-task)");
    assert.doesNotMatch(brief1, /\[\[wiki\/beta-hit\]\]/);

    const second = await runHook(fixture, "SubagentStart", {
      session_id: SESSION,
      agent_id: "sub-fifo-2",
      agent_type: "Explore",
    });
    const brief2 = second.output.hookSpecificOutput?.additionalContext ?? "";
    assert.match(brief2, /\[\[wiki\/parent-baseline\]\]/);
    assert.match(brief2, /\[\[wiki\/beta-hit\]\]/, "2nd SubagentStart must claim 2nd stashed prompt (beta-task)");
    assert.doesNotMatch(brief2, /\[\[wiki\/alpha-hit\]\]/);

    // 3rd SubagentStart has no remaining unclaimed task prompt: carries the parent's named set alone.
    const third = await runHook(fixture, "SubagentStart", {
      session_id: SESSION,
      agent_id: "sub-fifo-3",
      agent_type: "Explore",
    });
    const brief3 = third.output.hookSpecificOutput?.additionalContext ?? "";
    assert.match(brief3, /\[\[wiki\/parent-baseline\]\]/, "with no task prompt found, brief carries parent named set alone");
    assert.doesNotMatch(brief3, /\[\[wiki\/alpha-hit\]\]/);
    assert.doesNotMatch(brief3, /\[\[wiki\/beta-hit\]\]/);
  },
);

test(
  "MINNI_SUBAGENT_BRIEF=off returns {continue:true} with no hookSpecificOutput and 0 subagent_brief ledger rows",
  { timeout: 60_000 },
  async (t) => {
    const fixture = await makeFixture(t, "subagent-brief-off");
    await startFakeDaemon(fixture, {
      search: () => ({
        agent_id: "claude-code",
        results: [
          daemonResult({
            source: "/shared/hit.md",
            docId: 401,
            src: "shared",
            wikilink: "[[wiki/hit]]",
            title: "Hit",
            confidence: 0.95,
          }),
        ],
      }),
    });

    await runHook(fixture, "UserPromptSubmit", {
      session_id: SESSION,
      prompt: "query with strong hit",
    });

    const res = await runHook(
      fixture,
      "SubagentStart",
      {
        session_id: SESSION,
        agent_id: "sub-off-1",
        agent_type: "Explore",
      },
      { env: { MINNI_SUBAGENT_BRIEF: "off" } },
    );

    assert.deepEqual(res.output, { continue: true });
    const rows = (await readLedger(fixture.vault)).filter((row) => row.channel === "subagent_brief");
    assert.equal(rows.length, 0);
  },
);
