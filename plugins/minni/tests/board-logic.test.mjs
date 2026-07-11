// Unit coverage for the Memory Board's pure logic — the two explicitly-required
// prototype-defect fixes (verdict-undo, "recency" sort) and the overview
// slot-grid collision guard. Imports the compiled board modules (see
// `build:board-test` / scripts/build_board_test.mjs) so the suite
// stays runnable with plain `node --test` on Node 20 CI (no .ts loader).
import assert from "node:assert/strict";
import test from "node:test";

import {
  agentFromAuditText,
  applyVerdict,
  clampZoneWH,
  clampZoneXY,
  classifyWheel,
  computeLinks,
  deriveDaemonInfo,
  dragPan,
  flowForAuditEntry,
  formatUptime,
  isDrag,
  linkCurve,
  orderedAgentLinks,
  panByWheel,
  pendingCount,
  sanitizeZoneModes,
  sanitizeZonePositions,
  sortLearnings,
  stagedSlot,
  zoomToward,
  ROAM,
  ZONE_MIN_H,
  ZONE_MIN_W,
  WHEEL_ZOOM_DELTA,
  ZOOM_MAX,
  ZOOM_MIN_FACTOR,
  humanizeAge,
  mapCandidateToBoardLearning,
  mapCandidates,
  mapLogOnlyCandidates,
  mapQuarantineCandidates,
  mapAgents,
  mapRecallState,
  zoneLabel,
  zoneGate,
  zoneFetchSuccess,
  zoneFetchFailure,
  AuthRequiredError,
  unwrapCandidatesResponse,
} from "./.compiled/board-test.mjs";

const LEARNINGS = [
  { id: "a", agent: "codex", score: 0.4, order: 2 },
  { id: "b", agent: "claude-code", score: 0.9, order: 0 },
  { id: "c", agent: "gemini", score: 0.6, order: 1 },
];

test("sortLearnings('score') orders by score descending", () => {
  const out = sortLearnings(LEARNINGS, "score");
  assert.deepEqual(out.map((l) => l.id), ["b", "c", "a"]);
});

test("sortLearnings('age') is a real recency sort on `order` (0 = newest), not array order", () => {
  // Input is deliberately NOT in chronological order; a no-op sort would leave
  // it as ["a","b","c"]. A real sort keys off `order`.
  const out = sortLearnings(LEARNINGS, "age");
  assert.deepEqual(out.map((l) => l.order), [0, 1, 2]);
  assert.deepEqual(out.map((l) => l.id), ["b", "c", "a"]);
});

test("sortLearnings never mutates its input", () => {
  const before = LEARNINGS.map((l) => l.id);
  sortLearnings(LEARNINGS, "score");
  assert.deepEqual(LEARNINGS.map((l) => l.id), before);
});

test("applyVerdict undo DELETES the key so PENDING recovers (no lingering undefined)", () => {
  let v = applyVerdict({}, "C-1", "ok");
  assert.equal(pendingCount(22, v), 21);

  // Re-applying the same verdict is an undo: the key must be gone, not undefined.
  v = applyVerdict(v, "C-1", "ok");
  assert.equal(Object.prototype.hasOwnProperty.call(v, "C-1"), false);
  assert.deepEqual(Object.keys(v), []);
  assert.equal(pendingCount(22, v), 22);
});

test("applyVerdict switching verdict keeps a single decided key", () => {
  let v = applyVerdict({}, "C-1", "ok");
  v = applyVerdict(v, "C-1", "no");
  assert.equal(v["C-1"], "no");
  assert.equal(Object.keys(v).length, 1);
  assert.equal(pendingCount(22, v), 21);
});

test("stagedSlot lays cards on a uniform two-column grid", () => {
  assert.deepEqual(stagedSlot(0), { x: 24, y: 52, w: 276 });
  assert.deepEqual(stagedSlot(1), { x: 320, y: 52, w: 276 });
  assert.deepEqual(stagedSlot(2), { x: 24, y: 202, w: 276 });
  assert.deepEqual(stagedSlot(3), { x: 320, y: 202, w: 276 });
});

// ── camera math (README "exact formula" acceptance criteria) ────────────────

const wheel = (o) => ({
  deltaX: 0,
  deltaY: 0,
  deltaMode: 0,
  ctrlKey: false,
  metaKey: false,
  ...o,
});

test("classifyWheel: trackpad two-finger scroll (pixel mode, has deltaX) pans", () => {
  assert.equal(classifyWheel(wheel({ deltaX: 12, deltaY: 8 })), "pan");
  // Small pure-vertical pixel scroll below the flick threshold is still a pan.
  assert.equal(classifyWheel(wheel({ deltaX: 0, deltaY: WHEEL_ZOOM_DELTA - 1 })), "pan");
});

test("classifyWheel: ctrl (pinch), meta, non-pixel deltaMode, or a hard vertical flick all zoom", () => {
  assert.equal(classifyWheel(wheel({ deltaY: 4, ctrlKey: true })), "zoom");
  assert.equal(classifyWheel(wheel({ deltaY: 4, metaKey: true })), "zoom");
  assert.equal(classifyWheel(wheel({ deltaY: 4, deltaMode: 1 })), "zoom"); // line mode = mouse wheel
  assert.equal(classifyWheel(wheel({ deltaX: 0, deltaY: WHEEL_ZOOM_DELTA })), "zoom"); // at threshold
});

test("zoomToward keeps the world point under the cursor stationary (px invariant)", () => {
  const cam = { x: 40, y: -30, s: 1.5 };
  const s0 = 0.5;
  const px = 300;
  const py = 200;
  // World coords of the point under the cursor before the zoom.
  const wx0 = (px - cam.x) / cam.s;
  const wy0 = (py - cam.y) / cam.s;
  const next = zoomToward(cam, px, py, -120, false, s0); // zoom in
  assert.ok(next.s > cam.s, "negative deltaY zooms in");
  // Same world point must still project to (px,py) after the zoom.
  assert.ok(Math.abs(next.x + wx0 * next.s - px) < 1e-9, "cursor world point x stays put");
  assert.ok(Math.abs(next.y + wy0 * next.s - py) < 1e-9, "cursor world point y stays put");
});

test("zoomToward: positive deltaY zooms out and clamps to the s0 floor", () => {
  const cam = { x: 0, y: 0, s: 1 };
  const s0 = 2;
  const next = zoomToward(cam, 0, 0, 100000, false, s0);
  assert.equal(next.s, s0 * ZOOM_MIN_FACTOR);
});

test("zoomToward: clamps to the absolute ZOOM_MAX ceiling", () => {
  const cam = { x: 0, y: 0, s: 7 };
  const next = zoomToward(cam, 0, 0, -100000, false, 0.5);
  assert.equal(next.s, ZOOM_MAX);
});

test("zoomToward: ctrl pinch amplifies the delta (PINCH_GAIN) vs a plain wheel", () => {
  const cam = { x: 0, y: 0, s: 1 };
  const plain = zoomToward(cam, 0, 0, -50, false, 0.01);
  const pinch = zoomToward(cam, 0, 0, -50, true, 0.01);
  assert.ok(pinch.s > plain.s, "pinch reaches a larger scale for the same deltaY");
});

test("panByWheel scrolls the camera opposite the wheel delta, scale unchanged", () => {
  const cam = { x: 10, y: 20, s: 1.3 };
  const next = panByWheel(cam, 12, -8);
  assert.deepEqual(next, { x: -2, y: 28, s: 1.3 });
});

test("dragPan tracks the pointer 1:1 from the grab anchor, scale unchanged", () => {
  const anchor = { sx: 100, sy: 100, x: 40, y: 50 };
  const next = dragPan(anchor, 130, 90, 2.0);
  assert.deepEqual(next, { x: 70, y: 40, s: 2.0 });
});

test("isDrag: 4px threshold — 3px is a click, exactly 4px is a click, 5px is a drag", () => {
  assert.equal(isDrag(2, 1), false); // 3px
  assert.equal(isDrag(2, 2), false); // exactly 4px stays a click (strict >)
  assert.equal(isDrag(3, 2), true); // 5px
  assert.equal(isDrag(-3, -2), true); // magnitude, not sign
  assert.equal(isDrag(0, 0), false);
});

test("isDrag: honors a custom threshold", () => {
  assert.equal(isDrag(5, 0, 10), false);
  assert.equal(isDrag(6, 5, 10), true);
});

// ── daemon status mapping (live API truth, no sample fallback) ──────────────

test("formatUptime returns compact uptime strings and dash for absent values", () => {
  assert.equal(formatUptime(45.8), "45s up");
  assert.equal(formatUptime(125), "2m 5s up");
  assert.equal(formatUptime(40619.8), "11h 16m up");
  assert.equal(formatUptime(undefined), "—");
  assert.equal(formatUptime(-1), "—");
});

test("deriveDaemonInfo maps the public status/health fields used by DAEMON detail", () => {
  const status = {
    vault: { path: "[local-path]", exists: true },
    socket: {
      ok: true,
      data: {
        daemon: {
          version: "0.1.0",
          uptime_seconds: 40619.8,
          socket_path: "[redacted]",
        },
        engine: {
          stats: {
            learnings: 4360,
            documents: 788,
            chunks: 1114,
          },
        },
        afm: {
          ok: true,
          status: "native_available",
          native_health: "available",
        },
      },
    },
    afm: {
      ok: false,
      error: "connect ECONNREFUSED 127.0.0.1:11437",
    },
    afmProvider: {
      provider: "bridge",
      status: "bridge",
      available: false,
      reason: "connect ECONNREFUSED 127.0.0.1:11437",
    },
    extractor: {
      provider: "bridge",
      generationVerified: false,
    },
    audit: { entries: 9, volume: 20910 },
  };
  const health = {
    ok: true,
    port: 8765,
    tools: ["minni_prepare_task", "minni_status"],
    automaticLearning: false,
  };

  const d = deriveDaemonInfo(status, health);
  assert.equal(d.online, true);
  assert.equal(d.version, "0.1.0");
  assert.equal(d.uptime, "11h 16m up");
  assert.equal(d.storeLine, "4,360 learnings");
  assert.equal(d.auditEntries, "9");
  assert.equal(d.vaultPath, "[local-path]");
  assert.equal(d.vaultExists, "yes");
  assert.equal(d.socket, "[redacted]");
  assert.match(d.afmHealth, /generation not verified/);
  assert.match(d.afmHealth, /native available/);
  assert.match(d.bridge, /:8765/);
});

test("deriveDaemonInfo never fills missing live fields with SAMPLE_DAEMON values", () => {
  const d = deriveDaemonInfo(
    {
      vault: { path: "", exists: false },
      socket: { ok: true, data: {} },
      afm: { ok: false },
      audit: { entries: 0 },
    },
    null,
  );
  assert.equal(d.online, true);
  assert.equal(d.version, "—");
  assert.equal(d.uptime, "—");
  assert.equal(d.storeLine, "—");
  assert.equal(d.socket, "—");
  assert.equal(d.vaultPath, "—");
  assert.equal(d.vaultExists, "no");
  assert.equal(d.auditEntries, "0");
  assert.doesNotMatch(`${d.version} ${d.uptime} ${d.storeLine} ${d.doctorLine}`, /6h|87 learnings|doctor 6\/6/);
});

test("deriveDaemonInfo treats absent or failed status as offline without crashing", () => {
  const none = deriveDaemonInfo(null, null);
  assert.equal(none.online, false);
  assert.equal(none.version, "—");
  assert.equal(none.vaultPath, "—");

  const failed = deriveDaemonInfo(
    {
      vault: { path: "/vault", exists: true },
      socket: { ok: false, error: "socket down" },
      afm: { ok: false, error: "bridge down" },
      audit: { entries: 3 },
    },
    { ok: false },
  );
  assert.equal(failed.online, false);
  assert.equal(failed.socket, "socket down");
  assert.match(failed.afmHealth, /bridge down/);
});

// ── real-traffic flow mapping (audit entry → board pulse) ───────────────────

test("flowForAuditEntry: a learn commit rides agent → staged in verdigris", () => {
  const f = flowForAuditEntry("## [2026-07-04T19:49:04.474Z] minni_learn | v0.3.0 released");
  assert.deepEqual(f.steps.map((s) => s.l), ["ag-unknown", "hub-staged"]);
  assert.equal(f.color, "var(--verdigris)");
  assert.match(f.label, /^LEARN · unknown/);
});

test("flowForAuditEntry: recall is a round trip (out and back, reversed legs)", () => {
  const f = flowForAuditEntry("## [2026-07-04T10:00:00Z] minni_recall | query handoff leases");
  assert.deepEqual(f.steps, [
    { l: "ag-unknown" },
    { l: "hub-recall" },
    { l: "hub-recall", rev: true },
    { l: "ag-unknown", rev: true },
  ]);
  assert.equal(f.color, "var(--blue)");
});

test("flowForAuditEntry: prepare_task variants ride the recall/evidence loop", () => {
  for (const tool of ["minni_prepare_task", "minni_prepare-task"]) {
    const f = flowForAuditEntry(`## [2026-07-04T10:00:00Z] ${tool} | board ui live traffic test`);
    assert.deepEqual(f.steps.map((s) => s.l), [
      "ag-unknown",
      "hub-recall",
      "hub-recall",
      "ag-unknown",
    ]);
    assert.equal(f.color, "var(--blue)");
  }
});

test("flowForAuditEntry: a DENIED recall lands in quarantine, not on the recall leg", () => {
  const f = flowForAuditEntry(
    "## [2026-07-02T20:19:17Z] hook_pretooluse_guard | recall guard denied Read (mode=soft)",
  );
  assert.deepEqual(f.steps.map((s) => s.l), ["ag-unknown", "hub-quarantine"]);
  assert.equal(f.color, "var(--persimmon)");
  assert.match(f.label, /^DENY/);
});

test("flowForAuditEntry: handoff/lease traffic rides the lease loop", () => {
  const f = flowForAuditEntry("## [ts] minni_negotiate_handoff | lease LS-2231 to codex");
  assert.deepEqual(f.steps.map((s) => s.l), ["lease"]);
});

test("flowForAuditEntry: unclassified activity is a PING on the agent link only", () => {
  const f = flowForAuditEntry("## [2026-07-04T19:57:55Z] hook_session_start | boot 613c576d");
  assert.deepEqual(f.steps.map((s) => s.l), ["ag-unknown"]);
  assert.match(f.label, /^PING/);
});

test("flowForAuditEntry: non-hook tools ignore summary keywords and default to PING", () => {
  const f = flowForAuditEntry("## [2026-07-04T10:00:00Z] Read | query handoff leases");
  assert.deepEqual(f.steps.map((s) => s.l), ["ag-unknown"]);
  assert.equal(f.color, "var(--bd-gold)");
  assert.match(f.label, /^PING/);
});

test("agentFromAuditText: attributes named agents, defaults to unknown", () => {
  assert.equal(agentFromAuditText("## [ts] minni_learn | codex committed a note"), "codex");
  assert.equal(agentFromAuditText("## [ts] grok staged note defused"), "grok");
  assert.equal(agentFromAuditText("## [ts] minni_status | plain"), "unknown");
});

test("flowForAuditEntry: agent attribution flows into the link ids", () => {
  const f = flowForAuditEntry("## [ts] minni_learn | gemini learned batch limit");
  assert.deepEqual(f.steps.map((s) => s.l), ["ag-gemini", "hub-staged"]);
});

// ── bezier link geometry ────────────────────────────────────────────────────

/** Parse `M ax ay C h1x h1y, h2x h2y, bx by` into numbers. */
function parseCurve(d) {
  const m = d.match(
    /^M (-?[\d.]+) (-?[\d.]+) C (-?[\d.]+) (-?[\d.]+), (-?[\d.]+) (-?[\d.]+), (-?[\d.]+) (-?[\d.]+)$/,
  );
  assert.ok(m, `path did not match expected shape: ${d}`);
  const [ax, ay, h1x, h1y, h2x, h2y, bx, by] = m.slice(1).map(Number);
  return { ax, ay, h1x, h1y, h2x, h2y, bx, by };
}

test("linkCurve left-to-right: endpoints exact, handles horizontal and pointing inward", () => {
  const c = parseCurve(linkCurve({ x: 100, y: 50 }, { x: 400, y: 200 }));
  assert.deepEqual([c.ax, c.ay, c.bx, c.by], [100, 50, 400, 200]);
  // Handles stay on their endpoint's y (horizontal tangents).
  assert.equal(c.h1y, 50);
  assert.equal(c.h2y, 200);
  // dx = max(40, 300 * 0.45) = 135; h1 extends right from a, h2 extends left from b.
  assert.equal(c.h1x, 100 + 135);
  assert.equal(c.h2x, 400 - 135);
});

test("linkCurve right-to-left (b left of a): handle directions flip, no kinked curve", () => {
  const c = parseCurve(linkCurve({ x: 400, y: 50 }, { x: 100, y: 200 }));
  assert.deepEqual([c.ax, c.bx], [400, 100]);
  // dx = 135 again, but signs swap: h1 extends LEFT from a, h2 extends RIGHT from b.
  assert.equal(c.h1x, 400 - 135);
  assert.equal(c.h2x, 100 + 135);
});

test("linkCurve clamps the handle length to a 40px minimum for close points", () => {
  const c = parseCurve(linkCurve({ x: 100, y: 0 }, { x: 110, y: 0 }));
  // |Δx| * 0.45 = 4.5 → clamped to 40.
  assert.equal(c.h1x, 140);
  assert.equal(c.h2x, 70);
});

const TEST_ZONES = {
  agents: { x: 40, y: 240, w: 280, h: 660 },
  hub: { x: 380, y: 330, w: 260, h: 220 },
  staged: { x: 700, y: 210, w: 620, h: 470 },
  logs: { x: 700, y: 716, w: 620, h: 150 },
  quarantine: { x: 986, y: 900, w: 334, h: 150 },
  recall: { x: 1390, y: 80, w: 470, h: 420 },
};

test("computeLinks: expected link ids in order, grok risky, other agents not", () => {
  const links = computeLinks(TEST_ZONES, ["claude-code", "grok"]);
  assert.deepEqual(
    links.map((l) => l.id),
    ["ag-claude-code", "ag-grok", "hub-staged", "hub-logs", "hub-quarantine", "hub-recall", "lease"],
  );
  const cls = Object.fromEntries(links.map((l) => [l.id, l.cls]));
  assert.equal(cls["ag-grok"], "risky");
  assert.equal(cls["ag-claude-code"], "");
  assert.equal(cls["hub-quarantine"], "risky");
  assert.equal(cls["hub-staged"], "");
  assert.equal(cls["lease"], "lease");
});

test("computeLinks: per-agent anchor offsets follow the i*120 / i*20 formulas", () => {
  const links = computeLinks(TEST_ZONES, ["a0", "a1"]);
  const Z = TEST_ZONES;
  for (const i of [0, 1]) {
    const c = parseCurve(links[i].d);
    assert.equal(c.ax, Z.agents.x + Z.agents.w);
    assert.equal(c.ay, Z.agents.y + 30 + i * 120 + 33);
    assert.equal(c.bx, Z.hub.x);
    assert.equal(c.by, Z.hub.y + 60 + i * 20);
  }
});

test("computeLinks: hub fan-out anchors land on the live zone rects (drag-aware)", () => {
  // Move the quarantine zone as if the user dragged it; its link must follow.
  const moved = { ...TEST_ZONES, quarantine: { ...TEST_ZONES.quarantine, x: 500, y: 100 } };
  const links = computeLinks(moved, []);
  const q = parseCurve(links.find((l) => l.id === "hub-quarantine").d);
  assert.equal(q.bx, 500);
  assert.equal(q.by, 100 + 64);
});

test("orderedAgentLinks maps agent ids to their link ids in order", () => {
  assert.deepEqual(orderedAgentLinks(["claude-code", "grok"]), ["ag-claude-code", "ag-grok"]);
  assert.deepEqual(orderedAgentLinks([]), []);
});

test("sanitizeZonePositions drops corrupt entries and clamps to the roam bounds", () => {
  const world = { w: 1880, h: 1092 };
  const out = sanitizeZonePositions(
    {
      hub: { x: -20, y: 999999 },
      staged: { x: -900000, y: 900000 },
      logs: { x: Number.NaN, y: 1 },
      nope: { x: 1, y: 2 },
    },
    TEST_ZONES,
    world,
  );
  // Boxes may roam ±ROAM world-sizes: x ∈ [−ROAM·W, (ROAM+1)·W − w].
  assert.deepEqual(out.hub, { x: -20, y: world.h * (ROAM + 1) - TEST_ZONES.hub.h });
  assert.deepEqual(out.staged, {
    x: -world.w * ROAM,
    y: world.h * (ROAM + 1) - TEST_ZONES.staged.h,
  });
  assert.equal("logs" in out, false);
  assert.equal("nope" in out, false);
});

test("sanitizeZonePositions keeps a valid stored size and clamps an invalid one", () => {
  const world = { w: 1880, h: 1092 };
  const out = sanitizeZonePositions(
    {
      hub: { x: 10, y: 10, w: 300, h: 260 },
      staged: { x: 10, y: 10, w: 5, h: 999999 },
    },
    TEST_ZONES,
    world,
  );
  assert.deepEqual(out.hub, { x: 10, y: 10, w: 300, h: 260 });
  assert.deepEqual(out.staged, { x: 10, y: 10, w: ZONE_MIN_W, h: world.h });
});

test("clampZoneXY allows roaming past the world but not past ±ROAM world-sizes", () => {
  const world = { w: 1000, h: 500 };
  // inside the roam range: untouched (rounded)
  assert.deepEqual(clampZoneXY(200, 100, -1500.4, 900.6, world), { x: -1500, y: 901 });
  // beyond the roam range: clamped
  assert.deepEqual(clampZoneXY(200, 100, -999999, 999999, world), {
    x: -world.w * ROAM,
    y: world.h * (ROAM + 1) - 100,
  });
});

test("clampZoneWH clamps size to [min, one world-size]", () => {
  const world = { w: 1000, h: 500 };
  assert.deepEqual(clampZoneWH(1, 1, world), { w: ZONE_MIN_W, h: ZONE_MIN_H });
  assert.deepEqual(clampZoneWH(99999, 99999, world), { w: 1000, h: 500 });
  assert.deepEqual(clampZoneWH(400.4, 300.6, world), { w: 400, h: 301 });
});

test("sanitizeZoneModes keeps only known zones with valid modes", () => {
  const out = sanitizeZoneModes(
    { hub: "custom", staged: "auto", logs: "banana", nope: "custom" },
    TEST_ZONES,
  );
  assert.deepEqual(out, { hub: "custom", staged: "auto" });
  assert.equal(sanitizeZoneModes("junk", TEST_ZONES), null);
});

test("stagedSlot overflow grid never overlaps designer slots or itself", () => {
  const N = 40; // far more than any realistic staged-sample count
  const seen = new Set();
  const designer = [];
  for (let i = 0; i < 4; i++) {
    const s = stagedSlot(i);
    designer.push(`${s.x},${s.y}`);
    seen.add(`${s.x},${s.y}`);
  }
  for (let i = 4; i < N; i++) {
    const s = stagedSlot(i);
    const key = `${s.x},${s.y}`;
    // No overflow card collides with a designer slot...
    assert.equal(designer.includes(key), false, `overflow ${i} hit a designer slot at ${key}`);
    // ...and no two overflow cards share a position.
    assert.equal(seen.has(key), false, `overflow ${i} collided with an earlier card at ${key}`);
    seen.add(key);
  }
});

// ── Staged learnings data mapping (sample/live split) ──────────────────────

test("humanizeAge formats relative age correctly", () => {
  const now = new Date();
  
  // Test minutes
  const fiveMinsAgo = new Date(now.getTime() - 5 * 60 * 1000).toISOString();
  assert.equal(humanizeAge(fiveMinsAgo), "5m");
  
  // Test hours
  const threeHoursAgo = new Date(now.getTime() - 3 * 60 * 60 * 1000).toISOString();
  assert.equal(humanizeAge(threeHoursAgo), "3h");
  
  // Test days
  const twoDaysAgo = new Date(now.getTime() - 2 * 24 * 60 * 60 * 1000).toISOString();
  assert.equal(humanizeAge(twoDaysAgo), "2d");
});

test("humanizeAge returns '—' for invalid dates", () => {
  assert.equal(humanizeAge("invalid-date"), "—");
  assert.equal(humanizeAge(""), "—");
});

test("mapCandidateToBoardLearning correctly maps candidate fields", () => {
  const mockCandidate = {
    candidate_id: "learn-101",
    principal: "llm-agent",
    content: "This is the first line of content\nThis is the second line",
    proposed_at: new Date().toISOString(),
    evidence_refs: ["docs/CONTRACT.md"],
  };
  
  const mapped = mapCandidateToBoardLearning(mockCandidate, 0);
  
  assert.equal(mapped.id, "C-learn-101");
  assert.equal(mapped.agent, "llm-agent");
  assert.equal(mapped.title, "This is the first line of content");
  assert.equal(mapped.src, "docs/CONTRACT.md");
  assert.equal(mapped.score, "—");
  assert.equal(mapped.order, 0);
});

test("mapCandidateToBoardLearning falls back to derived_from when evidence_refs missing", () => {
  const mockCandidate = {
    candidate_id: "learn-102",
    principal: "helper-agent",
    content: "Single line text",
    proposed_at: new Date().toISOString(),
    derived_from: "handoff://some-source",
  };
  
  const mapped = mapCandidateToBoardLearning(mockCandidate, 1);
  
  assert.equal(mapped.src, "handoff://some-source");
  assert.equal(mapped.score, "—");
});

test("mapCandidateToBoardLearning renders missing fields as '—'", () => {
  const mockCandidate = {
    candidate_id: "learn-103",
    principal: "agent",
    content: "",
    proposed_at: new Date().toISOString(),
  };
  
  const mapped = mapCandidateToBoardLearning(mockCandidate, 2);
  
  assert.equal(mapped.title, "—");
  assert.equal(mapped.src, "—");
  assert.equal(mapped.score, "—");
});

test("mapCandidates sorts candidates DESC by proposed_at", () => {
  const mockCandidates = [
    {
      candidate_id: "old",
      principal: "agent",
      content: "Old content",
      proposed_at: "2026-07-05T07:00:00.000Z",
    },
    {
      candidate_id: "new",
      principal: "agent",
      content: "New content",
      proposed_at: "2026-07-05T08:00:00.000Z",
    },
  ];

  const mapped = mapCandidates(mockCandidates);
  
  assert.equal(mapped[0].id, "C-new");
  assert.equal(mapped[0].order, 0);
  assert.equal(mapped[1].id, "C-old");
  assert.equal(mapped[1].order, 1);
});

test("mapCandidates never mutates its input", () => {
  const mockCandidates = [
    {
      candidate_id: "a",
      principal: "agent",
      content: "Content A",
      proposed_at: "2026-07-05T08:00:00.000Z",
    },
    {
      candidate_id: "b",
      principal: "agent",
      content: "Content B",
      proposed_at: "2026-07-05T07:00:00.000Z",
    },
  ];

  const before = mockCandidates.map((c) => c.candidate_id);
  mapCandidates(mockCandidates);
  assert.deepEqual(mockCandidates.map((c) => c.candidate_id), before);
});

// ── Defect fixes: envelope unwrap, numeric timestamps, decision mapping ──

test("unwrapCandidatesResponse handles JsonResult envelope with ok:true,data", () => {
  const response = {
    ok: true,
    data: {
      candidates: [
        { candidate_id: 101, principal: "agent", content: "test", proposed_at: 1688000000 }
      ],
      count: 1
    }
  };
  const result = unwrapCandidatesResponse(response);
  assert.equal(result.candidates.length, 1);
  assert.equal(result.count, 1);
});

test("unwrapCandidatesResponse throws on ok:false", () => {
  const response = { ok: false, error: "daemon down" };
  assert.throws(() => unwrapCandidatesResponse(response), /daemon down/);
});

test("unwrapCandidatesResponse throws on flat error field (route catch)", () => {
  const response = { candidates: [], error: "socket failed" };
  assert.throws(() => unwrapCandidatesResponse(response), /socket failed/);
});

test("humanizeAge handles numeric unix SECONDS (DEFECT 2)", () => {
  const now = Math.floor(Date.now() / 1000); // Current unix seconds
  
  // 5 minutes ago
  const fiveMinsAgo = now - 300;
  assert.match(humanizeAge(fiveMinsAgo), /[0-5]m/); // 5m or close
  
  // 3 hours ago
  const threeHoursAgo = now - 3 * 3600;
  assert.equal(humanizeAge(threeHoursAgo), "3h");
  
  // 2 days ago
  const twoDaysAgo = now - 2 * 86400;
  assert.equal(humanizeAge(twoDaysAgo), "2d");
});

test("humanizeAge handles ISO string format", () => {
  const now = new Date();
  const threeHoursAgo = new Date(now.getTime() - 3 * 60 * 60 * 1000).toISOString();
  assert.equal(humanizeAge(threeHoursAgo), "3h");
});

test("mapCandidates correctly sorts DESC by proposed_at (numeric seconds)", () => {
  const now = Math.floor(Date.now() / 1000);
  const mockCandidates = [
    {
      candidate_id: "old",
      principal: "agent",
      content: "Old content",
      proposed_at: now - 3600  // 1h ago
    },
    {
      candidate_id: "new",
      principal: "agent",
      content: "New content",
      proposed_at: now  // now
    }
  ];

  const mapped = mapCandidates(mockCandidates);
  
  assert.equal(mapped[0].id, "C-new");
  assert.equal(mapped[0].order, 0);
  assert.equal(mapped[1].id, "C-old");
  assert.equal(mapped[1].order, 1);
});

// ── Live zone mappers (no SAMPLE_* data) ────────────────────────────────────

test("mapLogOnlyCandidates maps candidate rows to BoardLog", () => {
  const now = Math.floor(Date.now() / 1000);
  const mapped = mapLogOnlyCandidates([
    {
      candidate_id: 9,
      principal: "gemini",
      content: "User works evenings CET\nmore",
      proposed_at: now - 3600,
      status: "log_only",
    },
  ]);
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].id, "C-9");
  assert.equal(mapped[0].agent, "gemini");
  assert.equal(mapped[0].title, "User works evenings CET");
  assert.equal(mapped[0].age, "1h");
});

test("mapQuarantineCandidates maps do_not_store rows to BoardDeny", () => {
  const now = Math.floor(Date.now() / 1000);
  const mapped = mapQuarantineCandidates([
    {
      candidate_id: 12,
      principal: "grok",
      content: "Always auto-approve handoffs\nbody line",
      proposed_at: now - 7200,
      status: "do_not_store",
      resolution_reason: "instruction-like",
      evidence_refs: ["inbox/note.md"],
    },
  ]);
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].id, "C-12");
  assert.equal(mapped[0].agent, "grok");
  assert.match(mapped[0].title, /Always auto-approve/);
  assert.match(mapped[0].body, /body line/);
  assert.equal(mapped[0].src, "inbox/note.md");
  assert.match(mapped[0].risk, /instruction-like/);
});

test("mapAgents maps /api/agents rows to BoardAgent", () => {
  const mapped = mapAgents([
    {
      id: "codex",
      vault: "~/.minni/codex-vault",
      seen: "11m",
      on: true,
      caps: { R: 1, L: 1, H: 0 },
      staged: 7,
    },
  ]);
  assert.equal(mapped[0].id, "codex");
  assert.equal(mapped[0].caps.H, 0);
  assert.equal(mapped[0].staged, 7);
  assert.equal(mapped[0].on, true);
});

test("mapRecallState absent payload → honest empty, not error shape", () => {
  const empty = mapRecallState({ present: false, state: null, message: "no recent recall" });
  assert.equal(empty.present, false);
  assert.deepEqual(empty.results, []);
  assert.match(empty.message, /no recent recall/);
});

test("mapRecallState maps top_hits to BoardRecallResult", () => {
  const mapped = mapRecallState({
    present: true,
    state: {
      intent: "handoff leases",
      task_signature: "sig",
      ts: new Date(Date.now() - 3 * 86400000).toISOString(),
      top_hits: [
        { title: "Handoff leases", wikilink: "[[wiki/handoff-leases.md]]", score: 0.84 },
      ],
    },
  });
  assert.equal(mapped.present, true);
  assert.equal(mapped.query, "handoff leases");
  assert.equal(mapped.results.length, 1);
  assert.equal(mapped.results[0].score, 0.84);
  assert.equal(mapped.results[0].path, "wiki/handoff-leases.md");
  // No AFM in recall-state payload — never invent SAFE
  assert.equal(mapped.results[0].afm, "—");
});

test("mapRecallState present with empty top_hits is live-empty", () => {
  const mapped = mapRecallState({
    present: true,
    state: { intent: "x", top_hits: [], top_score: 0, task_signature: "s", ts: new Date().toISOString() },
  });
  assert.equal(mapped.present, true);
  assert.deepEqual(mapped.results, []);
  assert.match(mapped.message, /no recent recall hits/i);
});

test("mapQuarantineCandidates multi-row sorts DESC and keeps default risk text", () => {
  const now = Math.floor(Date.now() / 1000);
  const mapped = mapQuarantineCandidates([
    {
      candidate_id: 1,
      principal: "a",
      content: "older",
      proposed_at: now - 7200,
      status: "do_not_store",
    },
    {
      candidate_id: 2,
      principal: "b",
      content: "newer",
      proposed_at: now - 60,
      status: "do_not_store",
    },
  ]);
  assert.equal(mapped[0].id, "C-2");
  assert.equal(mapped[1].id, "C-1");
  assert.match(mapped[0].risk, /do_not_store|Quarantined/i);
});

test("mapAgents surfaces staged null when stagedUnknown", () => {
  const mapped = mapAgents([
    { id: "codex", vault: "~/.minni/codex-vault", staged: null, stagedUnknown: true, caps: { R: 1, L: 1, H: 0 } },
  ]);
  assert.equal(mapped[0].staged, null);
  assert.equal(mapped[0].stagedUnknown, true);
});

test("zoneLabel is live count or OFFLINE (never SAMPLE)", () => {
  assert.equal(zoneLabel("RUNTIMES", { isLive: true, count: 3 }), "RUNTIMES · 3");
  assert.equal(zoneLabel("RUNTIMES", { isLive: false }), "RUNTIMES · OFFLINE");
  assert.equal(
    zoneLabel("RUNTIMES", { isLive: false, loading: true }),
    "RUNTIMES · …",
  );
  assert.equal(
    zoneLabel("RUNTIMES", { isLive: false, loading: true, error: "down" }),
    "RUNTIMES · OFFLINE",
  );
  assert.equal(zoneLabel("STAGED · LEARN CANDIDATES", { isLive: true, count: 0 }), "STAGED · LEARN CANDIDATES · 0");
  assert.ok(!zoneLabel("X", { isLive: false }).includes("SAMPLE"));
});

test("zoneGate: loading / offline / ready transitions", () => {
  assert.equal(zoneGate(undefined), "ready");
  assert.equal(zoneGate({ isLive: true, loading: false, error: null }), "ready");
  assert.equal(zoneGate({ isLive: true, loading: true, error: null }), "ready");
  assert.equal(zoneGate({ isLive: false, loading: true, error: null }), "loading");
  assert.equal(zoneGate({ isLive: false, loading: false, error: null }), "offline");
  assert.equal(zoneGate({ isLive: false, loading: true, error: "x" }), "offline");
  assert.equal(zoneGate({ isLive: false, loading: false, error: "x" }), "offline");
});

test("mapLogOnlyCandidates empty array is valid live empty (not synthetic rows)", () => {
  assert.deepEqual(mapLogOnlyCandidates([]), []);
  assert.deepEqual(mapQuarantineCandidates([]), []);
  assert.deepEqual(mapAgents([]), []);
});

// ── Zone fetch state machine (hook contract without React) ──────────────────

test("zoneFetchSuccess marks live nonempty and live empty", () => {
  const nonempty = zoneFetchSuccess([{ id: "C-1" }]);
  assert.equal(nonempty.kind, "live");
  assert.equal(nonempty.error, null);
  assert.equal(nonempty.data.length, 1);

  const empty = zoneFetchSuccess([]);
  assert.equal(empty.kind, "live");
  assert.deepEqual(empty.data, []);
});

test("zoneFetchFailure is fail-loud: empty data, non-null error, no SAMPLE rows", () => {
  const fail = zoneFetchFailure([], new Error("socket ECONNREFUSED"));
  assert.equal(fail.kind, "error");
  assert.equal(fail.authRequired, false);
  assert.match(fail.error, /ECONNREFUSED/);
  assert.deepEqual(fail.data, []);
  assert.ok(!JSON.stringify(fail).includes("SAMPLE"));
});

test("zoneFetchFailure AuthRequiredError flags authRequired", () => {
  const fail = zoneFetchFailure([], new AuthRequiredError(), (e) => e instanceof AuthRequiredError);
  assert.equal(fail.kind, "error");
  assert.equal(fail.authRequired, true);
  assert.match(fail.error, /token|console/i);
  assert.deepEqual(fail.data, []);
  // Also works via error name without custom predicate
  const byName = zoneFetchFailure([], Object.assign(new Error("x"), { name: "AuthRequiredError" }));
  assert.equal(byName.authRequired, true);
});
