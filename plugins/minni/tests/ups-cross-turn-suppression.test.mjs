import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { discardDeliveryCommits, runDeliveryCommits } from "../dist/hook-delivery.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { readRecallState } from "../dist/recall-state.js";
import { ensureVault } from "../dist/vault.js";

async function withTempVault(fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-s3c-suppression-"));
  const prevSocket = process.env.MINNI_SOCKET_PATH;
  const prevBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_SOCKET_PATH = path.join(root, "missing.sock");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    await ensureVault(root);
    return await fn(root);
  } finally {
    discardDeliveryCommits();
    if (prevSocket === undefined) delete process.env.MINNI_SOCKET_PATH;
    else process.env.MINNI_SOCKET_PATH = prevSocket;
    if (prevBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = prevBypass;
    await rm(root, { recursive: true, force: true });
  }
}

function makeHandlers(vaultPath) {
  return createHookHandlers({
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "test-ws",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
  });
}

async function runDeliveredUps(handlers, payload) {
  discardDeliveryCommits();
  const out = await handlers.handleUserPromptSubmit(payload);
  await runDeliveryCommits();
  return out;
}

test("Same memory on turn 1 and turn 2 with < 0.10 score change is suppressed on turn 2 and does not arm the guard", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    const phrase = "krypton_lease_renewal_policy_check_interval";
    await writeFile(
      path.join(dir, "krypton-lease-policy.md"),
      [
        "---",
        "title: Krypton lease renewal policy",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Krypton lease renewal policy",
        "",
        `Renew krypton leases every 15 minutes using ${phrase} before expiry.`,
        "",
      ].join("\n"),
      "utf8",
    );

    const handlers = makeHandlers(vault);
    const sessionId = "suppress-sess-1";
    const prompt = `Check ${phrase} details`;

    // Turn 1: delivered -> surfaces pointer and arms recall-state.
    const turn1 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.match(turn1.hookSpecificOutput?.additionalContext ?? "", /Krypton lease renewal policy/);
    const state1 = await readRecallState(vault, { sessionId, guardV2: true });
    assert.ok(state1, "turn 1 must arm recall-state");

    // Turn 2: same score (< 0.10 increase) within 5 turns -> suppressed, no pointer, guard cleared.
    const turn2 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    const ctx2 = turn2.hookSpecificOutput?.additionalContext ?? "";
    assert.doesNotMatch(ctx2, /Krypton lease renewal policy/, "turn 2 must suppress repeated memory");
    const state2 = await readRecallState(vault, { sessionId, guardV2: true });
    assert.equal(state2, null, "when all hits are suppressed, recall-state must be cleared");
  });
});

test("Score jump >= 0.10 on a subsequent turn overrides suppression and re-surfaces", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    const exactQuote = "xenon_hyperdrive_cooldown_interlock_sequence";
    await writeFile(
      path.join(dir, "xenon-interlock.md"),
      [
        "---",
        "title: Xenon cooldown interlock",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Xenon cooldown interlock",
        "",
        `The ${exactQuote} prevents thermal runaway during emergency shutdown.`,
        "",
      ].join("\n"),
      "utf8",
    );

    const handlers = makeHandlers(vault);
    const sessionId = "suppress-jump-sess";

    // Turn 1: Route (b) title match (~0.775)
    const turn1 = await runDeliveredUps(handlers, {
      session_id: sessionId,
      prompt: "xenon cooldown interlock details",
    });
    assert.match(turn1.hookSpecificOutput?.additionalContext ?? "", /Xenon cooldown interlock/);

    // Turn 2: Route (a) exact quote match (1.00, +0.225 >= 0.10 jump) -> NOT suppressed!
    const turn2 = await runDeliveredUps(handlers, {
      session_id: sessionId,
      prompt: `Verify ${exactQuote} behavior`,
    });
    assert.match(
      turn2.hookSpecificOutput?.additionalContext ?? "",
      /Xenon cooldown interlock/,
      "a >= 0.10 score jump must re-surface the memory",
    );
  });
});

test("Suppression expires after 5 turns (turn_diff > 5) and resets immediately on SessionStart/PreCompact/PostCompact", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    const phrase = "argon_backoff_jitter_rule_base_interval";
    await writeFile(
      path.join(dir, "argon-backoff-rule.md"),
      [
        "---",
        "title: Argon backoff jitter rule",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Argon backoff jitter rule",
        "",
        `Argon backoff jitter uses ${phrase} of 250ms.`,
        "",
      ].join("\n"),
      "utf8",
    );

    const handlers = makeHandlers(vault);
    const sessionId = "suppress-window-sess";
    const prompt = `Check ${phrase} configuration`;

    // Turn 1: surfaces
    const turn1 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.match(turn1.hookSpecificOutput?.additionalContext ?? "", /Argon backoff jitter rule/);

    // Turns 2..6 (diffs 1..5): suppressed
    for (let t = 2; t <= 6; t += 1) {
      const res = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
      assert.doesNotMatch(
        res.hookSpecificOutput?.additionalContext ?? "",
        /Argon backoff jitter rule/,
        `turn ${t} (diff ${t - 1} <= 5) must be suppressed`,
      );
    }

    // Turn 7 (diff 6 > 5): suppression window expired -> surfaces again!
    const turn7 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.match(
      turn7.hookSpecificOutput?.additionalContext ?? "",
      /Argon backoff jitter rule/,
      "turn 7 (diff 6 > 5) must re-surface",
    );

    // Turn 8 (diff 1 after turn 7): suppressed again
    const turn8 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.doesNotMatch(turn8.hookSpecificOutput?.additionalContext ?? "", /Argon backoff jitter rule/);

    // PreCompact resets the epoch -> next turn re-surfaces immediately!
    await handlers.handlePreCompact({ session_id: sessionId, trigger: "auto" });
    const afterCompact = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.match(
      afterCompact.hookSpecificOutput?.additionalContext ?? "",
      /Argon backoff jitter rule/,
      "reset epoch change must clear suppression immediately",
    );
  });
});

test("Undelivered turn 1 pointer (deferUntilDelivered not committed) does NOT suppress turn 2", async () => {
  await withTempVault(async (vault) => {
    const dir = path.join(vault, "wiki", "decisions");
    await mkdir(dir, { recursive: true });
    const phrase = "radon_circuit_breaker_threshold_policy";
    await writeFile(
      path.join(dir, "radon-circuit-breaker.md"),
      [
        "---",
        "title: Radon circuit breaker threshold",
        "type: decision",
        "status: accepted",
        "privacy: safe",
        "---",
        "",
        "# Radon circuit breaker threshold",
        "",
        `Trip the ${phrase} after 5 consecutive failures.`,
        "",
      ].join("\n"),
      "utf8",
    );

    const handlers = makeHandlers(vault);
    const sessionId = "suppress-undelivered-sess";
    const prompt = `Verify ${phrase} behavior`;

    // Turn 1: do NOT commit deferred callbacks (simulates aborted/dropped delivery)
    discardDeliveryCommits();
    const turn1 = await handlers.handleUserPromptSubmit({ session_id: sessionId, prompt });
    discardDeliveryCommits();
    assert.match(turn1.hookSpecificOutput?.additionalContext ?? "", /Radon circuit breaker threshold/);

    // Turn 2: because Turn 1 was never committed as delivered, Turn 2 still surfaces!
    const turn2 = await runDeliveredUps(handlers, { session_id: sessionId, prompt });
    assert.match(
      turn2.hookSpecificOutput?.additionalContext ?? "",
      /Radon circuit breaker threshold/,
      "undelivered turn 1 must not suppress turn 2",
    );
  });
});
