import { createHash } from "node:crypto";
import { appendFile, readFile, readdir, writeFile, unlink } from "node:fs/promises";
import path from "node:path";

import { DEFAULT_VAULT_PATH } from "./config.js";
import { writeVaultPage, appendFileWithFsync, type VaultWriteResult } from "./vault.js";
import type { PageStatus } from "./vault.js";
import type { ScarTissueEntry } from "./task.js";
import { stableStringify } from "./agent_envelope.js";

// ---------------------------------------------------------------------------
// Types (exported per spec)
// ---------------------------------------------------------------------------

export type PlanSliceStatus = "pending" | "in_progress" | "done" | "blocked" | "superseded";

export interface PlanSlice {
  id: string;
  title: string;
  status: PlanSliceStatus;
  gate?: string;
  depends_on?: string[];
  evidence?: string;
  superseded_by?: string;
}

export interface ShelfRef {
  agent: string;
  wikilink: string;
  pull_hint: string;
  approx_tokens?: number;
  shelf_hash: string;
}

export interface PlanArtifact {
  plan_id: string;
  goal: string;
  status: PageStatus;
  constraints: string[];
  slices: PlanSlice[];
  open_questions: string[];
  scar_tissue: ScarTissueEntry[];
  next_action: string;
  shelf_ref?: ShelfRef;
  plan_digest: string;
  created: string;
  updated: string;
  rev: number;
}

export type PlanEvent =
  | { kind: "status_changed"; slice_id: string; from: PlanSliceStatus; to: PlanSliceStatus; at: string; evidence?: string }
  | { kind: "replan"; at: string; note?: string }
  | { kind: "gate_passed"; slice_id: string; evidence: string; at: string }
  | { kind: "shelf_pulled"; at: string; reason: string }
  | { kind: "rehydrated"; at: string }
  | { kind: "restored"; from_rev: number; at: string }
  | { kind: "scar_added"; signal: string; at: string }
  | { kind: "status_reconciled"; from: PageStatus; to: PageStatus; at: string };

// ---------------------------------------------------------------------------
// Supporting input/deps (for createPlan testability and callers)
// ---------------------------------------------------------------------------

export interface CreatePlanInput {
  goal: string;
  constraints?: string[];
  slices?: Array<{ id?: string; title: string; gate?: string; depends_on?: string[]; evidence?: string }>;
  open_questions?: string[];
  scar_tissue?: ScarTissueEntry[];
  shelf_ref?: Partial<ShelfRef> & { shelf_content?: string };
  vaultPath?: string;
  next_action?: string;
}

export interface CreatePlanDeps {
  writeVaultPage?: typeof writeVaultPage;
  now?: () => Date;
  vaultPath?: string;
}

// ---------------------------------------------------------------------------
// Pure helpers (no I/O)
// ---------------------------------------------------------------------------

function computeShelfHash(content: string): string {
  return createHash("sha256").update(content ?? "").digest("hex").slice(0, 16);
}

/**
 * Legacy (v1) digest: goal + (id,status,evidence) slice triplets only. Retained
 * so rehydratePlan can recognize plans persisted before H7 and upgrade them in
 * place rather than hard-failing them as "tampered". Exported so the H7
 * migration regression test can stamp a plan with a pre-H7 digest.
 */
export function computePlanDigestV1(plan: PlanArtifact): string {
  const sliceInfo = plan.slices
    .map((s) => ({ id: s.id, status: s.status, evidence: s.evidence }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const payload = { goal: plan.goal, slices: sliceInfo };
  const str = stableStringify(payload);
  return createHash("sha256").update(str).digest("hex").slice(0, 16);
}

/**
 * H7: the digest must cover EVERY field that compactPlanView / compactPlanPointer
 * inject into agent-visible envelopes — otherwise a vault edit to an uncovered
 * field (slice title, next_action, open_questions, constraints, scar_tissue,
 * shelf_ref, gate/depends_on/superseded_by) passes digest validation and reaches
 * the model unnoticed. Hash the full slice records plus all injected plan-level
 * fields. Sorted + stable keys for determinism.
 *
 * The payload is versioned ("v2") so rehydratePlan can distinguish a genuine
 * tamper from a pre-H7 plan (which validates against computePlanDigestV1) and
 * upgrade the latter gracefully.
 */
function computePlanDigestHexV2(plan: PlanArtifact): string {
  const slices = plan.slices
    .map((sl) => ({
      id: sl.id,
      title: sl.title,
      status: sl.status,
      gate: sl.gate,
      depends_on: sl.depends_on ? [...sl.depends_on].sort() : undefined,
      evidence: sl.evidence,
      superseded_by: sl.superseded_by,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const scar_tissue = (plan.scar_tissue ?? []).map((sc) => ({
    kind: sc.kind,
    signal: sc.signal,
    resolution: sc.resolution,
  }));
  const shelf_ref = plan.shelf_ref
    ? {
        agent: plan.shelf_ref.agent,
        wikilink: plan.shelf_ref.wikilink,
        pull_hint: plan.shelf_ref.pull_hint,
        approx_tokens: plan.shelf_ref.approx_tokens,
        shelf_hash: plan.shelf_ref.shelf_hash,
      }
    : undefined;
  const payload = {
    v: 2,
    goal: plan.goal,
    next_action: plan.next_action,
    constraints: plan.constraints ?? [],
    open_questions: plan.open_questions ?? [],
    scar_tissue,
    shelf_ref,
    slices,
  };
  const str = stableStringify(payload);
  return createHash("sha256").update(str).digest("hex").slice(0, 16);
}

/**
 * #122 F-PLAN-DIGEST-CROSSPROC: the stored digest is version-tagged
 * ("v<N>:<hex>") and read-time recognition dispatches on the tag through a
 * registry of every historical algorithm, so a future payload widening (v3)
 * cannot re-open the single-legacy-fn cliff that transiently bricked plan
 * tools during the v1->v2 rollout. Untagged stored digests (written by
 * pre-tagging builds) are still recognized as v2-or-v1 exactly as before.
 */
export const PLAN_DIGEST_VERSION = 2;

const PLAN_DIGEST_ALGORITHMS: Record<number, (plan: PlanArtifact) => string> = {
  1: computePlanDigestV1,
  2: computePlanDigestHexV2,
};

/** Current digest, version-tagged for cross-version read dispatch. */
export function computePlanDigest(plan: PlanArtifact): string {
  return `v${PLAN_DIGEST_VERSION}:${computePlanDigestHexV2(plan)}`;
}

function parsePlanDigestTag(stored: string): { version: number; hex: string } | undefined {
  const m = stored.match(/^v(\d+):([0-9a-f]+)$/);
  return m ? { version: Number(m[1]), hex: m[2] } : undefined;
}

/**
 * Statuses a plan never returns from. Mirrors resolveActivePlanView's
 * injection-suppression set; shared by createPlan's displacement warning and
 * the minni_plan_activate terminal guard (#122).
 */
export const TERMINAL_PLAN_STATUSES: ReadonlySet<string> = new Set([
  "accepted",
  "complete",
  "rejected",
  "superseded",
]);

export function slugifySliceId(title: string, taken: Set<string>): string {
  let slug = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!slug) {
    slug = "slice";
  }
  if (slug.length > 40) {
    const lastDash = slug.slice(0, 40).lastIndexOf("-");
    if (lastDash > 0) {
      slug = slug.slice(0, lastDash);
    } else {
      slug = slug.slice(0, 40);
    }
  }
  if (!taken.has(slug)) return slug;
  let i = 2;
  while (true) {
    const cand = `${slug}-${i}`;
    if (!taken.has(cand)) return cand;
    i += 1;
  }
}

function computeNextAction(slices: PlanSlice[]): string {
  const active = slices.find(
    (s) => s.status === "pending" || s.status === "in_progress" || s.status === "blocked",
  );
  if (!active) {
    const allResolved = slices.every((s) => s.status === "done" || s.status === "superseded");
    return allResolved ? "complete" : "review superseded slices";
  }
  let desc = `${active.id}: ${active.title}`;
  if (active.gate) desc += ` (verify: ${active.gate})`;
  if (active.depends_on && active.depends_on.length > 0) {
    desc += ` depends:${active.depends_on.join(",")}`;
  }
  return desc;
}

function normalizeShelfRef(input?: CreatePlanInput["shelf_ref"]): ShelfRef | undefined {
  if (!input) return undefined;
  const agent = (input.agent ?? "unknown").trim() || "unknown";
  const wikilink = (input.wikilink ?? "[[unknown]]").trim() || "[[unknown]]";
  const pull_hint = (input.pull_hint ?? "manual").trim() || "manual";
  let shelf_hash = input.shelf_hash ?? "";
  if (!shelf_hash && input.shelf_content) {
    shelf_hash = computeShelfHash(input.shelf_content);
  }
  if (!shelf_hash) {
    shelf_hash = computeShelfHash(wikilink);
  }
  return {
    agent,
    wikilink,
    pull_hint,
    approx_tokens: input.approx_tokens,
    shelf_hash,
  };
}

/** Render human-readable markdown body for the vault artifact note. */
export function renderPlanNote(plan: PlanArtifact): string {
  const lines: string[] = [];
  lines.push(`**Goal:** ${plan.goal}`);
  if (plan.constraints.length > 0) {
    lines.push("");
    lines.push("**Constraints:**");
    for (const c of plan.constraints) lines.push(`- ${c}`);
  }
  lines.push("");
  lines.push(`**Status:** ${plan.status}  |  **Plan:** ${plan.plan_id}  |  **Digest:** ${plan.plan_digest}`);
  if (plan.shelf_ref) {
    const sh = plan.shelf_ref;
    const tok = sh.approx_tokens ? ` (~${sh.approx_tokens}t)` : "";
    lines.push(`**Shelf:** ${sh.agent} ${sh.wikilink} — ${sh.pull_hint}${tok} hash=${sh.shelf_hash}`);
  }
  lines.push("");
  lines.push("## Slices");
  if (plan.slices.length === 0) {
    lines.push("- (none)");
  } else {
    lines.push("| ID | Title | Status | Gate | Depends | Evidence | Superseded |");
    lines.push("|----|-------|--------|------|---------|----------|------------|");
    for (const sl of plan.slices) {
      const deps = (sl.depends_on ?? []).join(",") || "";
      const ev = sl.evidence ? sl.evidence.replace(/\s+/g, " ").slice(0, 48) : "";
      const sup = sl.superseded_by || "";
      lines.push(`| ${sl.id} | ${sl.title} | ${sl.status} | ${sl.gate ?? ""} | ${deps} | ${ev} | ${sup} |`);
    }
  }
  if (plan.open_questions.length > 0) {
    lines.push("");
    lines.push("## Open Questions");
    for (const q of plan.open_questions) lines.push(`- ${q}`);
  }
  if (plan.scar_tissue.length > 0) {
    lines.push("");
    lines.push("## Scar Tissue");
    for (const sc of plan.scar_tissue) {
      const res = sc.resolution ? ` → ${sc.resolution}` : "";
      lines.push(`- [${sc.kind}] ${sc.signal}${res}`);
    }
  }
  lines.push("");
  lines.push(`**Next Action:** ${plan.next_action}`);
  lines.push("");
  lines.push(`*Created:* ${plan.created}  *Updated:* ${plan.updated}`);
  return lines.join("\n");
}

function planFrontmatterFields(
  plan: PlanArtifact,
): Record<string, string | number | boolean | undefined> {
  const fmExtras: Record<string, string | number | boolean | undefined> = {
    minni_plan: true,
    plan_id: plan.plan_id,
    plan_rev: plan.rev,
    plan_digest: plan.plan_digest,
    plan_goal: plan.goal,
    plan_constraints: JSON.stringify(plan.constraints),
    plan_slices: JSON.stringify(plan.slices),
    plan_open_questions: JSON.stringify(plan.open_questions),
    plan_scar_tissue: JSON.stringify(plan.scar_tissue),
    plan_next_action: plan.next_action,
    created: plan.created,
    updated: plan.updated,
  };
  if (plan.shelf_ref) {
    fmExtras.plan_shelf_ref = JSON.stringify(plan.shelf_ref);
  }
  return fmExtras;
}

// ---------------------------------------------------------------------------
// Tiny frontmatter parser (no deps; sufficient for our controlled writes)
// ---------------------------------------------------------------------------

function parseFrontmatter(raw: string): { frontmatter: Record<string, unknown>; body: string } {
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!m) return { frontmatter: {}, body: raw };
  const fmBlock = m[1];
  const body = m[2].trimStart();
  const fm: Record<string, unknown> = {};
  for (const rawLine of fmBlock.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf(":");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let valStr = line.slice(eq + 1).trim();
    if (!key) continue;
    let value: unknown = valStr;
    // strip outer quotes if yaml-stringified.
    // The writer (vault.ts `yamlValue`) emits any non-trivial scalar via JSON.stringify,
    // so a double-quoted scalar MUST be decoded with its exact inverse — JSON.parse —
    // not a partial hand-rolled unescape. The previous code only reversed \" and \n and
    // left \\ (plus \t, \r, \uXXXX) un-decoded, which doubled every backslash on each
    // write->read round-trip and produced false-positive plan_digest mismatches for any
    // evidence containing regex/path backslashes (e.g. rg 'malloc\(|free\('). Observed
    // live 2026-06-05 in codex's Runtime V4 plan (uart-rx-driver evidence).
    if (valStr.startsWith('"') && valStr.endsWith('"')) {
      try {
        valStr = JSON.parse(valStr) as string;
      } catch {
        // defensive fallback for malformed scalars: reverse the writer's escapes,
        // backslash LAST so it does not corrupt the \" and \n sequences.
        valStr = valStr
          .slice(1, -1)
          .replace(/\\n/g, "\n")
          .replace(/\\"/g, '"')
          .replace(/\\\\/g, "\\");
      }
      value = valStr;
    } else if (valStr.startsWith("'") && valStr.endsWith("'")) {
      valStr = valStr.slice(1, -1);
      value = valStr;
    }
    // parse json-ish or primitives (our pre-stringified arrays/objects land here)
    const trimmed = valStr;
    if (/^[\[{]/.test(trimmed) || /^(true|false|null|-?\d(\.\d+)?([eE][+-]?\d+)?$)/.test(trimmed)) {
      try {
        value = JSON.parse(trimmed);
      } catch {
        value = valStr;
      }
    } else if (trimmed === "true") {
      value = true;
    } else if (trimmed === "false") {
      value = false;
    } else if (trimmed !== "" && !Number.isNaN(Number(trimmed))) {
      const n = Number(trimmed);
      if (Number.isFinite(n)) value = n;
    }
    fm[key] = value;
  }
  return { frontmatter: fm, body };
}

function safeParse<T>(val: unknown, fallback: T): T {
  if (typeof val !== "string") return fallback;
  try {
    return JSON.parse(val) as T;
  } catch {
    return fallback;
  }
}

function extractGoalFromBody(body: string): string {
  let m = body.match(/\*\*Goal:\*\*\s*(.+?)(?:\n|$)/i);
  if (m?.[1]) return m[1].trim();
  m = body.match(/^Goal:\s*(.+?)(?:\n|$)/im);
  if (m?.[1]) return m[1].trim();
  m = body.match(/^#\s*[^\n]+\n\n(.+?)(?:\n|$)/);
  if (m?.[1]) return m[1].trim();
  return "unknown";
}

// ---------------------------------------------------------------------------
// Journal (append-only, replayable NDJSON lines; tolerant parser)
// ---------------------------------------------------------------------------

/** Append a PlanEvent as a single JSON line. Creates header on first write. */
export async function appendJournal(journalPath: string, event: PlanEvent): Promise<void> {
  const line = JSON.stringify(event) + "\n";
  try {
    // exists -> append
    await readFile(journalPath, "utf8");
    await appendFile(journalPath, line, "utf8");
  } catch {
    // missing or unreadable -> init
    const header = `# Minni Plan Journal\n\n## events\n`;
    await writeFile(journalPath, header + line, "utf8");
  }
}

/** Parse NDJSON-ish events from journal text (ignores header/markdown). */
export function parseJournal(journalText: string): PlanEvent[] {
  const events: PlanEvent[] = [];
  for (const ln of journalText.split(/\r?\n/)) {
    const t = ln.trim();
    if (!t || !t.startsWith("{") || !t.endsWith("}")) continue;
    try {
      const ev = JSON.parse(t) as PlanEvent;
      if (ev && typeof ev.kind === "string" && typeof (ev as any).at === "string") {
        events.push(ev);
      }
    } catch {
      // ignore bad line
    }
  }
  return events;
}

// ---------------------------------------------------------------------------
// The 8 functions
// ---------------------------------------------------------------------------

/** Create a draft plan, persist via writeVaultPage to artifacts/, init adjacent journal. */
export async function createPlan(
  input: CreatePlanInput,
  deps: CreatePlanDeps = {},
): Promise<{ plan: PlanArtifact; write: VaultWriteResult; displaced_active?: string }> {
  if (!input.goal?.trim()) {
    throw new Error("plan requires non-empty goal");
  }
  const writeFn = deps.writeVaultPage ?? writeVaultPage;
  const nowFn = deps.now ?? (() => new Date());
  const vaultPath = deps.vaultPath ?? input.vaultPath ?? DEFAULT_VAULT_PATH;

  const used = new Set<string>();
  const initialSlices: PlanSlice[] = (input.slices ?? []).map((s) => {
    const id = s.id || slugifySliceId(s.title, used);
    used.add(id);
    return {
      id,
      title: s.title,
      status: "pending",
      gate: s.gate,
      depends_on: s.depends_on ? [...s.depends_on] : undefined,
      evidence: s.evidence,
    };
  });

  const nowDate = nowFn();
  const created = nowDate.toISOString();
  const plan_id = `plan-${createHash("sha256").update(input.goal + created).digest("hex").slice(0, 16)}`;

  const shelf_ref = normalizeShelfRef(input.shelf_ref);

  const basePlan: PlanArtifact = {
    plan_id,
    goal: input.goal.trim(),
    status: "draft",
    constraints: (input.constraints ?? []).filter(Boolean),
    slices: initialSlices,
    open_questions: (input.open_questions ?? []).filter(Boolean),
    scar_tissue: input.scar_tissue ?? [],
    next_action: input.next_action ?? computeNextAction(initialSlices),
    shelf_ref,
    plan_digest: "",
    created,
    updated: created,
    rev: 0,
  };
  basePlan.plan_digest = computePlanDigest(basePlan);

  const plan: PlanArtifact = basePlan;
  const writeRes = await persistPlan(plan, { vaultPath, writeVaultPage: writeFn });

  // #122 F-PLAN-CREATE-OVERWRITES-ACTIVE: auto-activate still wins the pointer,
  // but displacing a non-terminal in-flight plan must be surfaced, not silent —
  // otherwise subsequent id-less plan_update/plan_status calls retarget the
  // wrong plan without notice. First-plan and terminal-incumbent cases stay
  // silent. An unreadable incumbent note is treated as in-flight (warn).
  let displaced_active: string | undefined;
  const incumbent = await getActivePlan(vaultPath);
  if (incumbent && incumbent.plan_id !== plan.plan_id) {
    let incumbentStatus = "";
    try {
      const raw = await readFile(incumbent.notePath, "utf8");
      incumbentStatus = String(parseFrontmatter(raw).frontmatter.status ?? "");
    } catch {
      // unreadable incumbent: conservatively report the displacement
    }
    if (!TERMINAL_PLAN_STATUSES.has(incumbentStatus)) {
      displaced_active = incumbent.plan_id;
    }
  }

  await setActivePlan(vaultPath, plan.plan_id, writeRes.notePath);

  const journalPath = path.join(path.dirname(writeRes.notePath), `${plan.plan_id}.log.md`);
  await appendJournal(journalPath, { kind: "rehydrated", at: plan.created });

  return { plan, write: writeRes, displaced_active };
}

/** Write plan artifact back to vault (create or update). Recomputes updated + plan_digest. */
export async function persistPlan(
  plan: PlanArtifact,
  opts: {
    vaultPath: string;
    notePath?: string;
    writeVaultPage?: typeof writeVaultPage;
  },
): Promise<VaultWriteResult> {
  const writeFn = opts.writeVaultPage ?? writeVaultPage;
  const updated = new Date().toISOString();

  // mutate in-place so caller gets updated rev, updated time and digest
  plan.rev = (plan.rev ?? 0) + 1;
  plan.updated = updated;
  plan.plan_digest = computePlanDigest(plan);

  const writeRes = await writeFn({
    vaultPath: opts.vaultPath,
    title: plan.plan_id,
    content: renderPlanNote(plan),
    section: "artifacts",
    type: "artifact",
    status: plan.status,
    frontmatter: planFrontmatterFields(plan),
  });

  if (opts.notePath && writeRes.notePath !== opts.notePath) {
    throw new Error(
      `persistPlan: expected notePath ${opts.notePath}, got ${writeRes.notePath}`,
    );
  }

  // append snapshot line to history file
  const historyFile = historyPathFor(writeRes.notePath);
  const snapshot = {
    rev: plan.rev,
    at: updated,
    digest: plan.plan_digest,
    plan,
  };
  await appendFileWithFsync(historyFile, JSON.stringify(snapshot) + "\n");

  return writeRes;
}

/** Locate artifacts note for plan_id by scanning wiki/artifacts frontmatter. */
export async function findPlanNote(
  vaultPath: string,
  plan_id: string,
): Promise<string | undefined> {
  const dir = path.join(vaultPath, "wiki", "artifacts");
  let names: string[];
  try {
    names = await readdir(dir);
  } catch {
    return undefined;
  }
  for (const name of names) {
    if (!name.endsWith(".md")) continue;
    const notePath = path.join(dir, name);
    const raw = await readFile(notePath, "utf8");
    const { frontmatter: fm } = parseFrontmatter(raw);
    if (String(fm.plan_id ?? "") === plan_id) return notePath;
  }
  return undefined;
}

function isTrivialEvidence(ev: string): boolean {
  const trimmed = ev.trim().toLowerCase();
  const trivial = new Set(["x", "ok", "done", "good", "looks good", "lgtm", "yes", "fine", "wip", "na", "n/a"]);
  return trivial.has(trimmed) || ev.trim().length < 8;
}

/** Immutable update of one slice. Evidence is mandatory to reach "done". Recomputes next_action + digest. */
export function updateSlice(
  plan: PlanArtifact,
  slice_id: string,
  to: PlanSliceStatus,
  evidence?: string,
): PlanArtifact {
  const idx = plan.slices.findIndex((s) => s.id === slice_id);
  if (idx < 0) {
    throw new Error(`updateSlice: no slice with id ${slice_id}`);
  }
  const from = plan.slices[idx].status;
  if (to === "done") {
    if (!evidence || isTrivialEvidence(evidence)) {
      throw new Error(
        `updateSlice: substantive evidence is required before a slice may become "done" (e.g. refer to a file, command output, test ID, etc.)`
      );
    }
  } else if (to === "blocked") {
    if (!evidence || !evidence.trim()) {
      throw new Error(`updateSlice: blocked requires a reason in \`evidence\``);
    }
  }
  const updatedSlice: PlanSlice = {
    ...plan.slices[idx],
    status: to,
  };
  if (evidence?.trim()) {
    updatedSlice.evidence = evidence.trim();
  }
  const newSlices = plan.slices.map((s, i) => (i === idx ? updatedSlice : s));
  const updated = new Date().toISOString();

  // P10 (terminal-state transition): when every slice is resolved (done/superseded), move the
  // plan to a terminal status so resolveActivePlanView stops injecting a finished plan into
  // future sessions.
  //
  // H6: this auto-promotion is driven entirely by model-supplied evidence
  // (isTrivialEvidence is a weak floor). It must NOT land the plan in "accepted"
  // — that is an operator/approval outcome and is default-recallable, so a model
  // could self-promote its own plan into recallable memory. Use the terminal,
  // NON-recallable "complete" status instead (resolveActivePlanView skips it the
  // same way). Reopening a slice un-finishes the plan, so revert a
  // model-completed plan back to draft.
  const allResolved =
    newSlices.length > 0 &&
    newSlices.every((s) => s.status === "done" || s.status === "superseded");
  let nextStatus: PageStatus = plan.status;
  if (allResolved && (plan.status === "draft" || plan.status === "candidate")) {
    nextStatus = "complete";
  } else if (!allResolved && plan.status === "complete") {
    nextStatus = "draft";
  }

  const nextPlan: PlanArtifact = {
    ...plan,
    slices: newSlices,
    status: nextStatus,
    next_action: computeNextAction(newSlices),
    updated,
  };
  nextPlan.plan_digest = computePlanDigest(nextPlan);
  return nextPlan;
}

export function addScar(plan: PlanArtifact, entry: ScarTissueEntry): PlanArtifact {
  const updated = new Date().toISOString();
  const kind = entry.kind;
  const signal = entry.signal;
  const resolution = entry.resolution;

  const existsIdx = plan.scar_tissue.findIndex(
    (s) => s.kind === kind && s.signal === signal,
  );
  let nextScarTissue: ScarTissueEntry[];
  if (existsIdx >= 0) {
    nextScarTissue = plan.scar_tissue.map((s, idx) => {
      if (idx === existsIdx) {
        return { ...s, resolution };
      }
      return s;
    });
  } else {
    nextScarTissue = [...plan.scar_tissue, { kind, signal, resolution }];
  }

  const nextPlan: PlanArtifact = {
    ...plan,
    scar_tissue: nextScarTissue,
    updated,
  };
  nextPlan.plan_digest = computePlanDigest(nextPlan);
  return nextPlan;
}

/** Replan: preserve superset (never drop history). Mark no-longer-proposed non-final slices superseded; append unmatched new ones. Pure. */
export function replan(
  plan: PlanArtifact,
  newSlices: Array<{ id?: string; title: string; gate?: string; depends_on?: string[]; evidence?: string }>,
): PlanArtifact {
  if (!Array.isArray(newSlices)) {
    return { ...plan, updated: new Date().toISOString() };
  }
  const updated = new Date().toISOString();
  // Deterministic marker (no clock in id)
  const titlesKey = stableStringify(newSlices.map((s) => (s.title ?? s.id ?? "")).sort());
  const supersededMarker = `replan-${createHash("sha256").update(titlesKey).digest("hex").slice(0, 10)}`;

  // Supersede old non-final that are absent from the proposed set (match by id or title)
  let nextSlices: PlanSlice[] = plan.slices.map((slice) => {
    const stillProposed = newSlices.some(
      (ns) =>
        (ns.id && ns.id === slice.id) ||
        ((ns.title ?? "").trim().toLowerCase() === slice.title.trim().toLowerCase()),
    );
    if (!stillProposed && slice.status !== "done" && slice.status !== "superseded") {
      return { ...slice, status: "superseded", superseded_by: supersededMarker };
    }
    return slice;
  });

  const usedIds = new Set(nextSlices.map((s) => s.id));

  // Append truly new (no id or title match among current non-superseded)
  for (const ns of newSlices) {
    const hasMatch = nextSlices.some((s) => {
      if (s.status === "superseded") return false;
      if (ns.id && s.id === ns.id) return true;
      return s.title.trim().toLowerCase() === (ns.title ?? "").trim().toLowerCase();
    });
    if (!hasMatch) {
      const id = ns.id || slugifySliceId(ns.title, usedIds);
      usedIds.add(id);
      nextSlices = [
        ...nextSlices,
        {
          id,
          title: ns.title,
          status: "pending",
          gate: ns.gate,
          depends_on: ns.depends_on ? [...ns.depends_on] : undefined,
          evidence: ns.evidence,
        },
      ];
    } else if (ns.id) {
      // Refresh fields on the matched entry (title/gate/deps may evolve)
      const idx = nextSlices.findIndex((s) => s.id === ns.id);
      if (idx >= 0) {
        const cur = nextSlices[idx];
        nextSlices[idx] = {
          ...cur,
          title: ns.title || cur.title,
          gate: ns.gate ?? cur.gate,
          depends_on: ns.depends_on ?? cur.depends_on,
        };
      }
    }
  }

  const nextAction = computeNextAction(nextSlices);
  const nextPlan: PlanArtifact = {
    ...plan,
    slices: nextSlices,
    next_action: nextAction,
    updated,
  };
  nextPlan.plan_digest = computePlanDigest(nextPlan);
  return nextPlan;
}

/** Surface-only drift check. Never pulls. */
export function shelfDrift(
  plan: PlanArtifact,
  liveShelfContent: string,
): {
  drifted: boolean;
  stored: string;
  live: string;
  recommendation?: string;
  configured: boolean;
  note?: string;
} {
  const live = computeShelfHash(liveShelfContent);
  if (!plan.shelf_ref) {
    return {
      configured: false,
      drifted: false,
      stored: "",
      live,
      recommendation: undefined,
      note: "no shelf attached",
    };
  }
  const stored = plan.shelf_ref.shelf_hash;
  const drifted = stored !== live;
  return {
    configured: true,
    drifted,
    stored,
    live,
    recommendation: drifted ? "drifted, pull recommended" : undefined,
  };
}

/** Bounded view suitable for injection into agent envelopes (small, no full slices). */
export function compactPlanView(plan: PlanArtifact): {
  headline: string;
  progress: { done: number; total: number; remaining: number; complete: boolean };
  goal: string;
  next_action: string;
  pending: Array<{ id: string; title: string; status: PlanSliceStatus }>;
  open_questions: string[];
  scar_tissue: number;
  scars: string[];
  shelf: string | undefined;
  rev: number;
} {
  const pending = plan.slices
    .filter((s) => s.status === "pending" || s.status === "in_progress")
    .map((s) => ({ id: s.id, title: s.title, status: s.status }));
  const shelf = plan.shelf_ref
    ? `${plan.shelf_ref.agent} ${plan.shelf_ref.wikilink} (${plan.shelf_ref.pull_hint})`
    : undefined;
  const scars = (plan.scar_tissue ?? [])
    .slice(-3)
    .map((s) => `${s.kind}: ${s.signal}`);

  // P3 (progress salience): make plan-level progress the headline so closing one slice is
  // never misread as closing the whole plan. A done/superseded slice counts as resolved.
  const total = plan.slices.length;
  const done = plan.slices.filter(
    (s) => s.status === "done" || s.status === "superseded",
  ).length;
  const remaining = total - done;
  const complete = total > 0 && remaining === 0;
  const activeSlice = plan.slices.find(
    (s) => s.status === "pending" || s.status === "in_progress" || s.status === "blocked",
  );
  const headline = complete
    ? `PLAN COMPLETE — all ${total} slice(s) resolved. No further action; this plan is finished.`
    : `Progress: ${done}/${total} slices done, ${remaining} remaining. ` +
      `NEXT: ${activeSlice ? activeSlice.id : plan.next_action}. ` +
      `The plan is NOT complete until all ${total} slices are done — do not stop after one slice.`;

  return {
    headline,
    progress: { done, total, remaining, complete },
    goal: plan.goal,
    next_action: plan.next_action,
    pending,
    open_questions: plan.open_questions,
    scar_tissue: plan.scar_tissue.length,
    scars,
    shelf,
    rev: plan.rev,
  };
}

/** Rehydrate snapshot from vault note (frontmatter + body). Appends a rehydrated journal event as side effect. */
export async function rehydratePlan(notePath: string): Promise<PlanArtifact> {
  const raw = await readFile(notePath, "utf8");
  const { frontmatter: fm } = parseFrontmatter(raw);

  const plan_id = String(fm.plan_id ?? "");
  if (!plan_id) {
    throw new Error(`rehydratePlan: note ${notePath} missing plan_id in frontmatter`);
  }

  const status = (fm.status as PageStatus) || "draft";
  const goal = typeof fm.plan_goal === "string" ? fm.plan_goal : extractGoalFromBody(raw);
  const constraints: string[] = Array.isArray(fm.plan_constraints)
    ? (fm.plan_constraints as unknown[]).filter((x): x is string => typeof x === "string")
    : safeParse(fm.plan_constraints, []);
  const slices: PlanSlice[] = Array.isArray(fm.plan_slices)
    ? (fm.plan_slices as PlanSlice[])
    : safeParse(fm.plan_slices, []);
  const open_questions: string[] = Array.isArray(fm.plan_open_questions)
    ? (fm.plan_open_questions as unknown[]).filter((x): x is string => typeof x === "string")
    : safeParse(fm.plan_open_questions, []);
  const scar_tissue: ScarTissueEntry[] = Array.isArray(fm.plan_scar_tissue)
    ? (fm.plan_scar_tissue as ScarTissueEntry[])
    : safeParse(fm.plan_scar_tissue, []);

  let shelf_ref: ShelfRef | undefined;
  const sr = fm.plan_shelf_ref;
  if (sr) {
    if (typeof sr === "object" && sr !== null && !Array.isArray(sr)) {
      shelf_ref = sr as ShelfRef;
    } else {
      shelf_ref = safeParse(sr as string, undefined);
    }
  }

  const next_action = typeof fm.plan_next_action === "string" ? fm.plan_next_action : computeNextAction(slices);
  let plan_digest = typeof fm.plan_digest === "string" ? fm.plan_digest : "";
  const created = typeof fm.created === "string" ? fm.created : new Date().toISOString();
  const updated = typeof fm.updated === "string" ? fm.updated : created;
  const revVal = fm.plan_rev;
  const rev = typeof revVal === "number" ? revVal : (typeof revVal === "string" ? parseInt(revVal, 10) : 0) || 0;

  const plan: PlanArtifact = {
    plan_id,
    goal,
    status,
    constraints,
    slices: slices.map((s) => ({ ...s })),
    open_questions: [...open_questions],
    scar_tissue: scar_tissue.map((s) => ({ ...s })),
    next_action,
    shelf_ref: shelf_ref ? { ...shelf_ref } : undefined,
    plan_digest,
    created,
    updated,
    rev,
  };

  // Validate that any 'done' slice has non-empty evidence
  for (const s of plan.slices) {
    if (s.status === "done" && (!s.evidence || !s.evidence.trim())) {
      throw new Error(`rehydratePlan: slice ${s.id} is 'done' without evidence (note tampered or corrupt)`);
    }
  }

  // Check for digest mismatch instead of silent repair.
  const recomputed = computePlanDigest(plan);
  if (plan.plan_digest !== recomputed) {
    // #122 F-PLAN-DIGEST-CROSSPROC: dispatch on the stored digest's version tag
    // through the algorithm registry. Any KNOWN prior version (or an untagged
    // stamp from a pre-tagging build, which validates against v2-or-v1 exactly
    // as before) is a legitimate plan and gets UPGRADED in place (re-persist
    // with the current tagged digest) rather than hard-failed as "tampered".
    // An UNKNOWN (newer) version degrades gracefully instead of claiming
    // tamper; only a stamp matching no known algorithm is a genuine tamper.
    const tagged = parsePlanDigestTag(plan.plan_digest);
    let recognized: boolean;
    if (tagged) {
      const algo = PLAN_DIGEST_ALGORITHMS[tagged.version];
      if (!algo) {
        throw new Error(
          `rehydratePlan: plan_digest version v${tagged.version} on ${notePath} is newer than this plugin supports (max v${PLAN_DIGEST_VERSION}); update the minni plugin to read this note`,
        );
      }
      recognized = algo(plan) === tagged.hex;
    } else {
      recognized =
        plan.plan_digest === computePlanDigestHexV2(plan) ||
        plan.plan_digest === computePlanDigestV1(plan);
    }
    if (recognized) {
      plan.plan_digest = recomputed;
      // Best-effort re-persist so the note carries the current tagged digest
      // going forward. Never a direct file write (persistPlan journals); a write
      // failure leaves the in-memory upgrade intact so this read still succeeds.
      try {
        const vaultPath = path.resolve(path.dirname(notePath), "..", "..");
        await persistPlan(plan, { vaultPath, notePath });
      } catch {
        // advisory: the in-memory upgraded digest is enough for this read to proceed
      }
    } else {
      throw new Error(`rehydratePlan: plan_digest mismatch (stored=${plan.plan_digest} computed=${recomputed}); note may be tampered`);
    }
  }

  // Record access (best-effort, append-only journal lives next to the note)
  const journalPath = path.join(path.dirname(notePath), `${plan_id}.log.md`);
  try {
    await appendJournal(journalPath, { kind: "rehydrated", at: new Date().toISOString() });
  } catch {
    // journal is advisory; do not fail rehydrate
  }

  return plan;
}

/**
 * #122 F-PLAN-RESTORE-SELFBLOCK: bare-scalar read for recovery paths. Returns a
 * skeleton PlanArtifact carrying only the frontmatter scalars that restorePlan
 * consumes from `current` (plan_id, status, created, updated, plan_digest, rev)
 * with NO digest or evidence validation — so minni_plan_restore can heal a note
 * whose strict rehydratePlan throws (the exact bricked state it exists to fix).
 * Every digest-covered field comes from the history snapshot, and persistPlan
 * recomputes the digest on write, so nothing corrupt survives the restore.
 */
export async function rehydratePlanScalars(notePath: string): Promise<PlanArtifact> {
  const raw = await readFile(notePath, "utf8");
  const { frontmatter: fm } = parseFrontmatter(raw);
  const plan_id = String(fm.plan_id ?? "");
  if (!plan_id) {
    throw new Error(`rehydratePlanScalars: note ${notePath} missing plan_id in frontmatter`);
  }
  const created = typeof fm.created === "string" ? fm.created : new Date().toISOString();
  const revVal = fm.plan_rev;
  const rev = typeof revVal === "number" ? revVal : (typeof revVal === "string" ? parseInt(revVal, 10) : 0) || 0;
  return {
    plan_id,
    goal: "",
    status: (fm.status as PageStatus) || "draft",
    constraints: [],
    slices: [],
    open_questions: [],
    scar_tissue: [],
    next_action: "",
    plan_digest: typeof fm.plan_digest === "string" ? fm.plan_digest : "",
    created,
    updated: typeof fm.updated === "string" ? fm.updated : created,
    rev,
  };
}

export function historyPathFor(notePath: string): string {
  const ext = path.extname(notePath);
  const dir = path.dirname(notePath);
  const base = path.basename(notePath, ext);
  return path.join(dir, `${base}.history.jsonl`);
}

export async function readHistory(
  notePath: string,
): Promise<Array<{ rev: number; at: string; digest: string; plan: PlanArtifact }>> {
  const historyFile = historyPathFor(notePath);
  try {
    const raw = await readFile(historyFile, "utf8");
    const lines = raw.split(/\r?\n/);
    const results: Array<{ rev: number; at: string; digest: string; plan: PlanArtifact }> = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed = JSON.parse(trimmed);
        if (
          typeof parsed.rev === "number" &&
          typeof parsed.at === "string" &&
          typeof parsed.digest === "string" &&
          parsed.plan &&
          typeof parsed.plan.plan_id === "string"
        ) {
          results.push(parsed);
        }
      } catch {
        // tolerate malformed/blank lines
      }
    }
    return results;
  } catch {
    return [];
  }
}

export async function getRevision(
  notePath: string,
  rev: number,
): Promise<PlanArtifact | undefined> {
  const history = await readHistory(notePath);
  const entry = history.find((h) => h.rev === rev);
  return entry?.plan;
}

export interface PlanDiff {
  added: PlanSlice[];
  dropped: PlanSlice[];
  status_changed: Array<{ id: string; from: PlanSliceStatus; to: PlanSliceStatus }>;
  evidence_changed: Array<{ id: string; title: string }>;
  goal_changed?: { from: string; to: string };
  constraints_changed?: boolean;
  open_questions_changed?: boolean;
}

export function diffPlans(a: PlanArtifact, b: PlanArtifact): PlanDiff {
  const added: PlanSlice[] = [];
  const dropped: PlanSlice[] = [];
  const status_changed: Array<{ id: string; from: PlanSliceStatus; to: PlanSliceStatus }> = [];
  const evidence_changed: Array<{ id: string; title: string }> = [];

  const aMap = new Map<string, PlanSlice>();
  for (const s of a.slices) {
    aMap.set(s.id, s);
  }

  const bMap = new Map<string, PlanSlice>();
  for (const s of b.slices) {
    bMap.set(s.id, s);
  }

  for (const sB of b.slices) {
    const sA = aMap.get(sB.id);
    if (!sA) {
      added.push(sB);
    } else {
      if (sA.status !== sB.status) {
        status_changed.push({ id: sB.id, from: sA.status, to: sB.status });
      }
      if (sA.evidence !== sB.evidence) {
        evidence_changed.push({ id: sB.id, title: sB.title });
      }
    }
  }

  for (const sA of a.slices) {
    if (!bMap.has(sA.id)) {
      dropped.push(sA);
    }
  }

  const diff: PlanDiff = {
    added,
    dropped,
    status_changed,
    evidence_changed,
  };

  if (a.goal !== b.goal) {
    diff.goal_changed = { from: a.goal, to: b.goal };
  }

  const constraintsChanged =
    a.constraints.length !== b.constraints.length ||
    a.constraints.some((c, i) => c !== b.constraints[i]);
  if (constraintsChanged) {
    diff.constraints_changed = true;
  }

  const openQuestionsChanged =
    a.open_questions.length !== b.open_questions.length ||
    a.open_questions.some((q, i) => q !== b.open_questions[i]);
  if (openQuestionsChanged) {
    diff.open_questions_changed = true;
  }

  return diff;
}

export function restorePlan(current: PlanArtifact, snapshot: PlanArtifact): PlanArtifact {
  return {
    ...current,
    goal: snapshot.goal,
    constraints: [...snapshot.constraints],
    slices: snapshot.slices.map((s) => ({ ...s })),
    open_questions: [...snapshot.open_questions],
    scar_tissue: snapshot.scar_tissue.map((s) => ({ ...s })),
    shelf_ref: snapshot.shelf_ref ? { ...snapshot.shelf_ref } : undefined,
    plan_id: current.plan_id,
    created: current.created,
    next_action: snapshot.next_action,
    updated: current.updated,
    plan_digest: current.plan_digest,
    rev: current.rev,
  };
}

export function applySliceDelta(
  plan: PlanArtifact,
  delta: {
    add_slices?: Array<{
      id?: string;
      title: string;
      gate?: string;
      depends_on?: string[];
      evidence?: string;
    }>;
    drop_slice_ids?: string[];
  },
): PlanArtifact {
  const deltaKey = stableStringify({
    add: (delta.add_slices ?? []).map((s) => s.title ?? s.id ?? "").sort(),
    drop: (delta.drop_slice_ids ?? []).sort(),
  });
  const supersededMarker = `replan-${createHash("sha256").update(deltaKey).digest("hex").slice(0, 10)}`;

  const dropSet = new Set(delta.drop_slice_ids ?? []);
  let nextSlices: PlanSlice[] = plan.slices.map((slice) => {
    if (dropSet.has(slice.id) && slice.status !== "done" && slice.status !== "superseded") {
      return { ...slice, status: "superseded", superseded_by: supersededMarker };
    }
    return slice;
  });

  const usedIds = new Set(nextSlices.map((s) => s.id));

  for (const ns of delta.add_slices ?? []) {
    const id = ns.id || slugifySliceId(ns.title, usedIds);
    usedIds.add(id);
    nextSlices.push({
      id,
      title: ns.title,
      status: "pending",
      gate: ns.gate,
      depends_on: ns.depends_on ? [...ns.depends_on] : undefined,
      evidence: ns.evidence,
    });
  }

  const nextAction = computeNextAction(nextSlices);
  const nextPlan: PlanArtifact = {
    ...plan,
    slices: nextSlices,
    next_action: nextAction,
    updated: new Date().toISOString(),
  };
  nextPlan.plan_digest = computePlanDigest(nextPlan);
  return nextPlan;
}

export function activePointerPath(vaultPath: string): string {
  return path.join(vaultPath, "wiki", "artifacts", "_active_plan.json");
}

export async function setActivePlan(
  vaultPath: string,
  plan_id: string,
  notePath: string
): Promise<void> {
  const pointerPath = activePointerPath(vaultPath);
  const data = JSON.stringify(
    {
      plan_id,
      notePath,
      set_at: new Date().toISOString(),
    },
    null,
    2
  );
  await writeFile(pointerPath, data, "utf8");
}

/**
 * #122 F-PLAN-ACTIVATE-NO-TERMINAL-GUARD: setActivePlan gated on the plan's
 * status — a terminal plan (resolveActivePlanView's suppression set) must not
 * be re-activated. Status is read via the bare-scalar path so a digest-bricked
 * but non-terminal plan can still be activated (as before this guard).
 */
export async function activatePlanChecked(
  vaultPath: string,
  plan_id: string,
  notePath: string,
): Promise<{ ok: true } | { ok: false; error: string }> {
  const scalars = await rehydratePlanScalars(notePath);
  if (TERMINAL_PLAN_STATUSES.has(scalars.status)) {
    return {
      ok: false,
      error: `plan ${plan_id} has terminal status '${scalars.status}' and cannot be re-activated; create a new plan (minni_plan_create) or restore a prior revision (minni_plan_restore) instead`,
    };
  }
  await setActivePlan(vaultPath, plan_id, notePath);
  return { ok: true };
}

export async function getActivePlan(
  vaultPath: string
): Promise<{ plan_id: string; notePath: string; set_at: string } | undefined> {
  const pointerPath = activePointerPath(vaultPath);
  try {
    const raw = await readFile(pointerPath, "utf8");
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed.plan_id === "string" &&
      typeof parsed.notePath === "string" &&
      typeof parsed.set_at === "string"
    ) {
      // Defense-in-depth containment (review panel): the pointer is only ever
      // written via the assertUnder-guarded writeVaultPage, but its notePath
      // is consumed by bare readFile/persistPlan calls downstream — a
      // tampered pointer must not be able to traverse outside the vault.
      const rel = path.relative(path.resolve(vaultPath), path.resolve(parsed.notePath));
      if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) {
        return undefined;
      }
      return parsed;
    }
  } catch {
    // undefined if absent/corrupt (never throw)
  }
  return undefined;
}

export async function clearActivePlan(vaultPath: string): Promise<void> {
  const pointerPath = activePointerPath(vaultPath);
  try {
    await unlink(pointerPath);
  } catch (err: any) {
    if (err.code !== "ENOENT") {
      throw err;
    }
  }
}

/**
 * Compact plan POINTER for per-turn injection (Option C). Keeps only the
 * actionable one-liners (headline, next_action, progress) plus counts, and tells
 * the agent how to pull the rest on demand. Drops the full goal text,
 * open_questions array (~1.8 KB, static) and pending-slice list.
 *
 * Plan parity (audit C5): ALL hooks (claude-code, codex, grok, kilocode) MUST
 * build their UserPromptSubmit `active_plan_ref` through this function so the
 * budget discipline cannot drift per hook.
 */
export function compactPlanPointer(active: {
  plan_id: string;
  rev: number;
  view: ReturnType<typeof compactPlanView>;
}): {
  plan_id: string;
  rev: number;
  headline: string;
  next_action: string;
  progress: ReturnType<typeof compactPlanView>["progress"];
  open_questions_count: number;
  scar_tissue: number;
  pull: string;
} {
  const v = active.view;
  return {
    plan_id: active.plan_id,
    rev: active.rev,
    headline: v.headline,
    next_action: v.next_action,
    progress: v.progress,
    open_questions_count: Array.isArray(v.open_questions) ? v.open_questions.length : 0,
    scar_tissue: v.scar_tissue,
    pull: "Full plan (goal, open_questions, slices) omitted to save context. Call minni_plan_status for detail on demand.",
  };
}

/**
 * Id-less active-plan addressing (audit C5 / plan-N3): resolve an explicit
 * plan_id, or fall back to the vault's active plan when none is supplied —
 * so hookless agents can address "the active plan" without knowing its id.
 * Returns a clear error when neither is available.
 */
export async function resolvePlanIdOrActive(
  vaultPath: string,
  planId?: string,
): Promise<{ plan_id: string } | { error: string }> {
  const explicit = planId?.trim();
  if (explicit) return { plan_id: explicit };
  const active = await getActivePlan(vaultPath);
  if (!active) {
    return {
      error:
        "no plan_id provided and no active plan is set; pass plan_id explicitly or activate one with minni_plan_activate",
    };
  }
  return { plan_id: active.plan_id };
}

export async function resolveActivePlanView(
  vaultPath: string
): Promise<{ plan_id: string; rev: number; view: ReturnType<typeof compactPlanView> } | undefined> {
  try {
    const active = await getActivePlan(vaultPath);
    if (!active) return undefined;
    const plan = await rehydratePlan(active.notePath);
    if (TERMINAL_PLAN_STATUSES.has(plan.status)) {
      return undefined;
    }
    // Honest-health self-heal (audit C4): plans completed under a stale plugin
    // deploy can be stuck with every slice terminal but status still
    // 'draft'/'candidate' (live evidence: plan-3da1b00ca39d2500,
    // plan-512ee7225dbb1c6f, plan-9fd20af5bc87bee2 — all 100% done, status
    // draft). Re-derive the terminal status at load time, persist it through
    // persistPlan (journaled; never a direct file write) and stop injecting
    // the finished plan.
    const allResolved =
      plan.slices.length > 0 &&
      plan.slices.every((s) => s.status === "done" || s.status === "superseded");
    if (allResolved && (plan.status === "draft" || plan.status === "candidate")) {
      const from = plan.status;
      // H6: terminal, non-recallable completion (not the recallable "accepted").
      plan.status = "complete";
      await persistPlan(plan, { vaultPath, notePath: active.notePath });
      const journalPath = path.join(
        path.dirname(active.notePath),
        `${plan.plan_id}.log.md`,
      );
      try {
        await appendJournal(journalPath, {
          kind: "status_reconciled",
          from,
          to: "complete",
          at: new Date().toISOString(),
        });
      } catch {
        // journal is advisory; the persisted status is the durable fix
      }
      return undefined;
    }
    return {
      plan_id: active.plan_id,
      rev: plan.rev,
      view: compactPlanView(plan),
    };
  } catch {
    return undefined;
  }
}

