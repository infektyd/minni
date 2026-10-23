// Recall-to-use S1a (§4.2): agent keys.
//
// A subagent's events arrive in the parent's session. If they were keyed `main`,
// a subagent's reset (or its own surfacings) would close and pollute the main
// thread's windows. The host marks a subagent two ways: an `agent_id` on the
// payload (probe P5), or a transcript that lives under a `subagents/` directory.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  agentKeyFrom,
  buildResetRow,
  canonicalAgentId,
  ledgerContextFrom,
  sessionKeyFrom,
} from "../dist/use-ledger.js";

const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

const PARENT = { session_id: "synthetic-session-0001", transcript_path: "/tmp/minni-fixture/proj/synthetic.jsonl" };

test("the main thread is `main`", () => {
  assert.equal(agentKeyFrom(PARENT), "main");
  assert.equal(agentKeyFrom({}), "main");
  assert.equal(agentKeyFrom({ agent_id: "" }), "main", "an empty agent_id is not a subagent");
  assert.equal(agentKeyFrom({ agent_id: 17 }), "main", "a non-string agent_id is not a subagent");
});

test("a payload with agent_id gets an a- key from its canonical id's hash", () => {
  const key = agentKeyFrom({ ...PARENT, agent_id: "agent-fixture-7", agent_type: "Explore" });
  assert.equal(key, `a-${sha256("fixture-7").slice(0, 12)}`);
  assert.match(key, /^a-[0-9a-f]{12}$/);
  assert.ok(!key.includes("fixture"), "the raw agent id never reaches the key");
});

test("a transcript under subagents/ gets an a- key from the file basename", () => {
  const transcript = "/tmp/minni-fixture/proj/synthetic/subagents/agent-fixture-9.jsonl";
  assert.equal(
    agentKeyFrom({ session_id: PARENT.session_id, transcript_path: transcript }),
    `a-${sha256("fixture-9").slice(0, 12)}`,
  );
  // Windows separators sit under subagents\ just the same.
  assert.equal(
    agentKeyFrom({ transcript_path: "C:\\fixture\\proj\\subagents\\agent-fixture-9.jsonl" }),
    `a-${sha256("fixture-9").slice(0, 12)}`,
  );
});

test("a payload agent_id and that subagent's own transcript basename give the SAME key", () => {
  // The host spells one subagent two ways: `agent_id: "agent-<id>"` on its
  // events, and `subagents/agent-<id>.jsonl` for its transcript. Keyed apart,
  // one subagent would look like two, and a reset seen through one spelling
  // would never close windows opened through the other.
  const viaId = agentKeyFrom({ session_id: PARENT.session_id, agent_id: "agent-fixture-5" });
  const viaTranscript = agentKeyFrom({
    session_id: PARENT.session_id,
    transcript_path: "/tmp/minni-fixture/proj/synthetic/subagents/agent-fixture-5.jsonl",
  });
  assert.equal(viaId, viaTranscript);
  assert.equal(viaId, `a-${sha256("fixture-5").slice(0, 12)}`);
  // An agent_id without the prefix canonicalizes to the same id.
  assert.equal(agentKeyFrom({ agent_id: "fixture-5" }), viaId);
  // A different subagent stays distinct.
  assert.notEqual(agentKeyFrom({ agent_id: "agent-fixture-6" }), viaId);

  assert.equal(canonicalAgentId("agent-abc123"), "abc123");
  assert.equal(canonicalAgentId("agent-abc123.jsonl"), "abc123");
  assert.equal(canonicalAgentId("abc123"), "abc123");
  // Only one leading prefix and one trailing extension are stripped.
  assert.equal(canonicalAgentId("agent-agent-x.jsonl.jsonl"), "agent-x.jsonl");
});

test("a degenerate id that canonicalizes to nothing is still a subagent", () => {
  const bare = agentKeyFrom({ agent_id: "agent-" });
  assert.match(bare, /^a-[0-9a-f]{12}$/);
  assert.equal(bare, `a-${sha256("agent-").slice(0, 12)}`);
  assert.match(agentKeyFrom({ transcript_path: "/tmp/minni-fixture/subagents/.jsonl" }), /^a-[0-9a-f]{12}$/);
});

test("agent_id wins over the transcript path", () => {
  const key = agentKeyFrom({ agent_id: "agent-fixture-7", transcript_path: "/x/subagents/other.jsonl" });
  assert.equal(key, `a-${sha256("fixture-7").slice(0, 12)}`);
});

test("`subagents` must be a directory component, not a file name or substring", () => {
  assert.equal(agentKeyFrom({ transcript_path: "/tmp/minni-fixture/subagents" }), "main");
  assert.equal(agentKeyFrom({ transcript_path: "/tmp/minni-fixture/my-subagents/x.jsonl" }), "main");
  assert.equal(agentKeyFrom({ transcript_path: "/tmp/minni-fixture/subagents.jsonl" }), "main");
});

test("a subagent's reset row carries its own key and never closes the parent's", () => {
  const child = { ...PARENT, agent_id: "agent-fixture-7", agent_type: "Explore", prompt_id: "prompt-fixture" };
  const parentCtx = ledgerContextFrom(PARENT, { wire: "claude-code", principal: "claude-code", features: [] });
  const childCtx = ledgerContextFrom(child, { wire: "claude-code", principal: "claude-code", features: [] });

  assert.equal(childCtx.session_key, parentCtx.session_key, "same host session");
  assert.equal(childCtx.session_key, sessionKeyFrom(PARENT));
  assert.equal(parentCtx.agent_key, "main");
  assert.notEqual(childCtx.agent_key, "main");
  assert.equal(childCtx.subagent_type, "Explore");
  assert.equal(childCtx.prompt_id, "prompt-fixture");

  const reset = buildResetRow(childCtx, { event: "PreCompact" }, "2026-10-23T10:00:00.000Z");
  assert.equal(reset.kind, "reset");
  assert.equal(reset.agent_key, childCtx.agent_key);
  assert.notEqual(reset.agent_key, parentCtx.agent_key);
});
