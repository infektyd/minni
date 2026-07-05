// ============================================================================
// Minni Memory Board — pure logic (no DOM, no React)
//
// The staged-wall verdict reducer, the learnings sort, and the overview slot
// grid live here as framework-free functions so they can be unit-tested
// directly (node:test imports this .ts module — type-only imports keep it
// dependency-free at runtime). Two of these back the explicitly-required
// prototype-defect fixes:
//   • applyVerdict  — undo DELETES the key (never sets `undefined`) so a
//     key-count-derived PENDING tally recovers.
//   • sortLearnings — "recency" is a real sort on the chronological `order`,
//     not a no-op relying on incidental array order.
// ============================================================================

import type { HealthReport, StatusReport } from "../api";
import type { BoardFlow, BoardLearning, DaemonInfo, ZoneDef, ZoneId } from "./boardData";

export type Verdict = "ok" | "no";

// ── infinite-canvas camera math ─────────────────────────────────────────────
//
// The wheel/trackpad heuristic and the zoom-toward-cursor / pan formulas are
// the README's explicit "exact formula" acceptance criteria. They live here as
// framework-free functions (no DOM event, no React state) so a sign flip in the
// zoom ratio, a wrong exponent constant, or a broken gesture classification is
// caught by node:test rather than shipping silently.

/** Camera: world-space origin offset (x,y) in screen px and scale s. */
export interface Cam {
  x: number;
  y: number;
  s: number;
}

export type WheelGesture = "zoom" | "pan";

/** Minimal shape of the wheel signals the classifier needs. */
export interface WheelSignals {
  deltaX: number;
  deltaY: number;
  deltaMode: number;
  ctrlKey: boolean;
  metaKey: boolean;
}

// Zoom tuning — kept as named constants so a test pins the exact values.
export const ZOOM_MIN_FACTOR = 0.12; // floor = s0 * this
export const ZOOM_MAX = 8;
export const ZOOM_RATE = 0.0016; // exp(-dy * rate)
export const PINCH_GAIN = 3; // ctrlKey pinch amplifies deltaY
export const WHEEL_ZOOM_DELTA = 24; // |deltaY| at/above this (with deltaX 0) = zoom

/**
 * Classify a wheel event: trackpad two-finger scroll → "pan"; mouse wheel,
 * pinch (ctrl), cmd, line/page delta modes, or a pure vertical flick of
 * magnitude ≥ WHEEL_ZOOM_DELTA → "zoom".
 */
export function classifyWheel(e: WheelSignals): WheelGesture {
  const isZoom =
    e.ctrlKey ||
    e.metaKey ||
    e.deltaMode !== 0 ||
    (e.deltaX === 0 && Math.abs(e.deltaY) >= WHEEL_ZOOM_DELTA);
  return isZoom ? "zoom" : "pan";
}

/**
 * Zoom toward the cursor at screen point (px,py). The new scale is clamped to
 * [s0 * ZOOM_MIN_FACTOR, ZOOM_MAX]; the origin is rescaled by r = s/s.old about
 * the cursor so the world point under the cursor stays put. `ctrlKey` marks a
 * pinch and amplifies the delta by PINCH_GAIN.
 */
export function zoomToward(
  cam: Cam,
  px: number,
  py: number,
  deltaY: number,
  ctrlKey: boolean,
  s0: number,
): Cam {
  const dy = ctrlKey ? deltaY * PINCH_GAIN : deltaY;
  const s = Math.max(s0 * ZOOM_MIN_FACTOR, Math.min(ZOOM_MAX, cam.s * Math.exp(-dy * ZOOM_RATE)));
  const r = s / cam.s;
  return { x: px - (px - cam.x) * r, y: py - (py - cam.y) * r, s };
}

/** Pan by a trackpad wheel delta (scroll content the natural direction). */
export function panByWheel(cam: Cam, deltaX: number, deltaY: number): Cam {
  return { x: cam.x - deltaX, y: cam.y - deltaY, s: cam.s };
}

/** Anchor captured on pointer-down for a drag-to-pan gesture. */
export interface PanAnchor {
  sx: number; // pointer client x at grab
  sy: number; // pointer client y at grab
  x: number; // camera x at grab
  y: number; // camera y at grab
}

/** Camera while dragging: origin follows the pointer 1:1, scale unchanged. */
export function dragPan(a: PanAnchor, clientX: number, clientY: number, s: number): Cam {
  return { x: a.x + clientX - a.sx, y: a.y + clientY - a.sy, s };
}

/**
 * Click-vs-drag boundary for draggable zones. Uses the Manhattan distance in
 * screen pixels; returns true once movement strictly exceeds `threshold` (so
 * an exactly-4px nudge is still a click, matching requirement 2).
 */
export function isDrag(dx: number, dy: number, threshold = 4): boolean {
  return Math.abs(dx) + Math.abs(dy) > threshold;
}

function dash(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "—";
}

function count(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return Math.max(0, Math.floor(value)).toLocaleString("en-US");
}

export function formatUptime(seconds: unknown): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  const secs = total % 60;
  if (days > 0) return `${days}d ${hours}h up`;
  if (hours > 0) return `${hours}h ${minutes}m up`;
  if (minutes > 0) return `${minutes}m ${secs}s up`;
  return `${secs}s up`;
}

function afmHealth(status: StatusReport | null): string {
  if (!status) return "—";
  const pieces: string[] = [];
  if (typeof status.afm?.ok === "boolean") {
    pieces.push(status.afm.ok ? "generation verified" : "generation not verified");
  }
  const provider = [...new Set([status.afmProvider?.provider, status.afmProvider?.status].filter(Boolean))]
    .join(" ");
  if (provider) pieces.push(provider);
  const native = status.socket?.data?.afm;
  const nativeStatus = native?.native_health || native?.status;
  if (nativeStatus) pieces.push(`native ${nativeStatus}`);
  const reason = status.afmProvider?.reason || status.afm?.error;
  if (!status.afm?.ok && reason) pieces.push(reason);
  return pieces.length ? pieces.join(" · ") : "—";
}

export function deriveDaemonInfo(
  status: StatusReport | null,
  health: HealthReport | null,
): DaemonInfo {
  const socketData = status?.socket?.data;
  const daemon = socketData?.daemon;
  const stats = socketData?.engine?.stats;
  const online = !!status?.socket?.ok;
  const toolCount = Array.isArray(health?.tools) ? health.tools.length : 0;
  const bridge =
    health?.ok
      ? `console API :${health.port ?? "—"}${toolCount ? ` · ${toolCount} tools` : ""}`
      : health
        ? "console API offline"
        : "—";
  const extractor =
    status?.extractor?.provider || status?.afmProvider?.provider
      ? [
          status?.extractor?.provider || status?.afmProvider?.provider,
          status?.extractor?.generationVerified === true
            ? "generation verified"
            : status?.extractor?.generationVerified === false
              ? "generation not verified"
              : undefined,
        ]
          .filter(Boolean)
          .join(" · ")
      : "—";

  return {
    online,
    version: dash(daemon?.version),
    uptime: formatUptime(daemon?.uptime_seconds),
    storeLine: stats?.learnings == null ? "—" : `${count(stats.learnings)} learnings`,
    doctorLine: afmHealth(status),
    socket: online ? dash(daemon?.socket_path) : status?.socket?.error || "offline",
    mode: extractor,
    vaultPath: dash(status?.vault?.path),
    vaultExists:
      typeof status?.vault?.exists === "boolean" ? (status.vault.exists ? "yes" : "no") : "—",
    auditEntries: count(status?.audit?.entries),
    afmHealth: afmHealth(status),
    bridge,
    tools: toolCount,
    automaticLearning: !!health?.automaticLearning,
  };
}

export type ZonePositions = Record<string, { x: number; y: number }>;

export function clampZonePosition(
  id: ZoneId,
  x: number,
  y: number,
  zones: Record<ZoneId, ZoneDef>,
  world: { w: number; h: number },
): { x: number; y: number } {
  const z = zones[id];
  return {
    x: Math.round(Math.max(0, Math.min(world.w - z.w, x))),
    y: Math.round(Math.max(0, Math.min(world.h - z.h, y))),
  };
}

export function sanitizeZonePositions(
  value: unknown,
  zones: Record<ZoneId, ZoneDef>,
  world: { w: number; h: number },
): ZonePositions | null {
  if (!value || typeof value !== "object") return null;
  const out: ZonePositions = {};
  for (const [id, pos] of Object.entries(value as Record<string, unknown>)) {
    if (!(id in zones) || !pos || typeof pos !== "object") continue;
    const raw = pos as { x?: unknown; y?: unknown };
    if (typeof raw.x !== "number" || typeof raw.y !== "number") continue;
    if (!Number.isFinite(raw.x) || !Number.isFinite(raw.y)) continue;
    out[id] = clampZonePosition(id as ZoneId, raw.x, raw.y, zones, world);
  }
  return out;
}

/**
 * Sort learnings by score (desc) or recency (chronological `order` asc, where
 * 0 = newest). Returns a new array — never mutates the input.
 */
export function sortLearnings(
  list: BoardLearning[],
  sort: "score" | "age",
): BoardLearning[] {
  const out = [...list];
  if (sort === "score") out.sort((a, b) => b.score - a.score);
  else out.sort((a, b) => a.order - b.order);
  return out;
}

/**
 * Toggle a verdict for `id`. Selecting the current verdict again clears it by
 * DELETING the key (not assigning `undefined`), so a PENDING count derived
 * from `Object.keys(...).length` recovers correctly. Returns a new object.
 */
export function applyVerdict(
  prev: Record<string, Verdict>,
  id: string,
  v: Verdict,
): Record<string, Verdict> {
  const next = { ...prev };
  if (next[id] === v) delete next[id];
  else next[id] = v;
  return next;
}

/** Undecided candidates = total minus the ones with a recorded verdict. */
export function pendingCount(
  total: number,
  verdicts: Record<string, Verdict>,
): number {
  return total - Object.keys(verdicts).length;
}

// ── overview staged-wall slot grid ──────────────────────────────────────────

export interface CardSlot {
  x: number;
  y: number;
  w?: number;
}

/** Hand-tuned designer slots for the first four staged overview cards. */
const STAGED_SLOTS: CardSlot[] = [
  { x: 24, y: 52 },
  { x: 310, y: 86 },
  { x: 48, y: 194 },
  { x: 332, y: 242 },
];
const STAGED_CARD_W = 252;
const STAGED_COL_X = [24, 310];
const STAGED_ROW_H = 108;
// Overflow grid starts one full row below the lowest designer slot so extra
// cards can never overlap the hand-tuned four.
const STAGED_ROW_Y0 = 350;

/**
 * Position for the i-th staged overview card. Returns the hand-tuned slot when
 * present, otherwise a two-column grid stepped by full card height — so extra
 * cards stack cleanly instead of colliding with the designer slots or with
 * each other, satisfying "variable data cannot silently overlap".
 */
export function stagedSlot(i: number): CardSlot {
  if (i < STAGED_SLOTS.length) return STAGED_SLOTS[i];
  const over = i - STAGED_SLOTS.length;
  const col = over % 2;
  const row = Math.floor(over / 2);
  return { x: STAGED_COL_X[col], y: STAGED_ROW_Y0 + row * STAGED_ROW_H, w: STAGED_CARD_W };
}

// ── bezier link geometry ────────────────────────────────────────────────────
//
// Pure SVG-path math for the live-traffic links (a README acceptance-criteria
// interaction). Lives here rather than in boardLayout so node:test can import
// it — only type imports, no runtime dependency on boardData.

export interface Point {
  x: number;
  y: number;
}

export interface Link {
  id: string;
  cls: "" | "risky" | "lease";
  d: string;
}

/** Bezier between two anchor points — horizontal control handles. */
export function linkCurve(a: Point, b: Point): string {
  const dx = Math.max(40, Math.abs(b.x - a.x) * 0.45);
  const h1 = a.x + (b.x >= a.x ? dx : -dx);
  const h2 = b.x + (b.x >= a.x ? -dx : dx);
  return `M ${a.x} ${a.y} C ${h1} ${a.y}, ${h2} ${b.y}, ${b.x} ${b.y}`;
}

/** Link curves between the live (dragged) zone rects, keyed for pulse lookup. */
export function computeLinks(
  Z: Record<ZoneId, ZoneDef>,
  agentIds: string[],
): Link[] {
  const L: Link[] = [];
  agentIds.forEach((id, i) => {
    L.push({
      id: "ag-" + id,
      cls: id === "grok" ? "risky" : "",
      d: linkCurve(
        { x: Z.agents.x + Z.agents.w, y: Z.agents.y + 30 + i * 120 + 33 },
        { x: Z.hub.x, y: Z.hub.y + 60 + i * 20 },
      ),
    });
  });
  L.push({ id: "hub-staged", cls: "", d: linkCurve({ x: Z.hub.x + Z.hub.w, y: Z.hub.y + 70 }, { x: Z.staged.x, y: Z.staged.y + 90 }) });
  L.push({ id: "hub-logs", cls: "", d: linkCurve({ x: Z.hub.x + Z.hub.w, y: Z.hub.y + 160 }, { x: Z.logs.x, y: Z.logs.y + 44 }) });
  L.push({ id: "hub-quarantine", cls: "risky", d: linkCurve({ x: Z.hub.x + Z.hub.w, y: Z.hub.y + 190 }, { x: Z.quarantine.x, y: Z.quarantine.y + 64 }) });
  L.push({ id: "hub-recall", cls: "", d: linkCurve({ x: Z.hub.x + Z.hub.w, y: Z.hub.y + 40 }, { x: Z.recall.x, y: Z.recall.y + 200 }) });
  L.push({
    id: "lease",
    cls: "lease",
    d: `M ${Z.agents.x + 218} ${Z.agents.y + 82} C ${Z.agents.x + 262} ${Z.agents.y + 120}, ${Z.agents.x + 262} ${Z.agents.y + 164}, ${Z.agents.x + 218} ${Z.agents.y + 202}`,
  });
  return L;
}

export function orderedAgentLinks(agentIds: string[]): string[] {
  return agentIds.map((id) => "ag-" + id);
}

// ── real-traffic flow mapping ───────────────────────────────────────────────
//
// Turns a raw audit-tail entry (markdown block, first line shaped like
// `## [timestamp] tool | summary`) into the flow a pulse should ride on the
// board: which agent link it leaves from, which daemon leg it lands on, and
// the ticker label/color. Pure so node:test can pin the classification.

/** Agents that have a link on the board (matches SAMPLE_AGENTS ids). */
export const FLOW_AGENTS = ["claude-code", "codex", "gemini", "antigravity", "grok"] as const;

/** Best-effort agent attribution from the entry text; the local vault's own
 * traffic carries no explicit agent marker, so claude-code is the default. */
export function agentFromAuditText(text: string): string {
  const t = text.toLowerCase();
  for (const a of FLOW_AGENTS) {
    if (a !== "claude-code" && t.includes(a)) return a;
  }
  return "claude-code";
}

/** Map one audit entry to the flow its pulse should travel. Order matters:
 * a denied recall ("recall guard denied") must land in quarantine, not on the
 * recall round-trip, so DENY is classified first. */
export function flowForAuditEntry(entry: string): BoardFlow {
  const first = (entry.split(/\r?\n/, 1)[0] || entry).trim();
  const m = /^##\s+\[([^\]]+)\]\s+([^|]+?)\s*(?:\|\s*(.*))?$/.exec(first);
  const tool = (m ? m[2] : first).trim();
  const summary = (m && m[3] ? m[3] : "").trim();
  const agent = agentFromAuditText(first);
  const ag = "ag-" + agent;

  // The tool name is authoritative; the free-text summary is only consulted
  // when the tool is generic (hook_*) — otherwise a recall QUERY that merely
  // mentions "leases" would ride the lease loop.
  const classify = (s: string): string | null => {
    if (/deny|denied|quarantine|do-not-store|blocked/.test(s)) return "DENY";
    if (/handoff|lease/.test(s)) return "LEASE";
    if (/learn|vault_write|promot/.test(s)) return "LEARN";
    if (/recall|route|drill|prepare[_-]?task/.test(s)) return "RECALL";
    if (/\blog\b|private|prepare_outcome/.test(s)) return "LOG";
    return null;
  };
  const toolKind = classify(tool.toLowerCase());
  const summaryKind =
    !tool || tool.startsWith("hook_") ? classify(summary.toLowerCase()) : null;
  const kind = toolKind ?? summaryKind ?? "PING";

  const mk = (steps: BoardFlow["steps"], color: string): BoardFlow => ({
    steps,
    color,
    label: `${kind} · ${agent} · ${(summary || tool).slice(0, 64).toUpperCase()}`,
  });

  switch (kind) {
    case "DENY":
      return mk([{ l: ag }, { l: "hub-quarantine" }], "var(--persimmon)");
    case "LEASE":
      return mk([{ l: "lease" }], "var(--bd-gold)");
    case "LEARN":
      return mk([{ l: ag }, { l: "hub-staged" }], "var(--verdigris)");
    case "RECALL":
      return mk(
        [{ l: ag }, { l: "hub-recall" }, { l: "hub-recall", rev: true }, { l: ag, rev: true }],
        "var(--blue)",
      );
    case "LOG":
      return mk([{ l: ag }, { l: "hub-logs" }], "var(--mustard)");
    default:
      return mk([{ l: ag }], "var(--bd-gold)");
  }
}
