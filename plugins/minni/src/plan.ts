import { createHash, randomUUID } from "node:crypto";
import { readFile, readdir, unlink } from "node:fs/promises";
import path from "node:path";

import { DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH } from "./config.js";
import { writeVaultPage, appendFileWithFsync, writeFileAtomic, type VaultWriteResult } from "./vault.js";
import type { PageStatus } from "./vault.js";
import type { ScarTissueEntry } from "./task.js";
import { stableStringify } from "./agent_envelope.js";
import { withThreadLock, ThreadBusyError } from "./thread-lock.js";
import {
  appendOrderedEventBatch,
  deriveSystemEventKey,
  ensureOrderedBaseline,
  readOrderedThreadEvents,
  reconcileThreadJournal,
} from "./thread-events.js";

// ---------------------------------------------------------------------------
// Types (exported per spec)
// ---------------------------------------------------------------------------

export type PlanSliceStatus = "pending" | "in_progress" | "done" | "blocked" | "superseded";

/**
 * Thread Phase 1 (worker slice metadata, digest v3): the private claim a
 * worker currently holds on a slice. NEVER carries the claim token itself —
 * the token lives only in the private mode-0600 envelope under
 * `.runtime/thread-claims/` (thread-claims.ts, a later task). This ref is
 * durable metadata (who/when/expiry) so ready-set and scope checks can run
 * from the plan note alone; it is intentionally silent on secret material.
 */
export interface ThreadClaimRef {
  claim_id: string;
  worker_agent_id: string;
  claimed_at: string;
  expires_at: string;
}

/**
 * Thread Phase 1: attributed worker proposals for topology change. Only the
 * orchestrator applies a proposal (through existing replan/supersession
 * behavior, per the V2 design's "Expansion and contraction" section) — a
 * proposal recorded on a slice is a durable request, never a mutation by
 * itself. `slices` reuses CreatePlanInput's slice shape so a proposal can be
 * fed straight into the existing replan() input without another mapping
 * layer.
 */
export type StructuralProposal =
  | { kind: "expand" | "split"; reason: string; slices: CreatePlanInput["slices"] }
  | { kind: "contract"; reason: string; slice_ids: string[] };

export interface PlanSlice {
  id: string;
  title: string;
  status: PlanSliceStatus;
  gate?: string;
  depends_on?: string[];
  evidence?: string;
  superseded_by?: string;
  /**
   * Exclusive split (drop + add in one replan) is replacement, not
   * drop-without-replacement. When set, unmetDependencies treats this
   * superseded slice as still unmet for anyone who still depends_on it —
   * orch remounts named depends_on onto the replacement ids (set_depends_on
   * on the existing replan surface) without restating every other slice.
   * Plain contract drop leaves this unset so superseded continues to
   * resolve dependents (disclosed residual).
   */
  replaced_by?: string[];
  // Thread Phase 1 (worker slice metadata, digest v3) — all optional so a
  // note persisted before this task, or a slice literal built by an older
  // test/caller, remains a valid PlanSlice with no backfill required on
  // read. `generation`/`attempt` are conceptually "0 unless recorded"; pure
  // helpers (e.g. computePlanDigestHexV3) default a missing value to 0
  // in-memory rather than writing a materialized 0 back into the note.
  requirements?: string[];
  assigned_to?: string;
  assignment_profile?: string;
  generation?: number;
  attempt?: number;
  claim?: ThreadClaimRef;
  proposals?: StructuralProposal[];
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

// #292 (June audit N5): "shelf_pulled" was declared here and never emitted —
// there is no code anywhere that resolves shelf_ref.pull_hint into an actual
// content fetch as a distinct action; it is only ever stored and rendered as
// a display string for an agent to read and manually follow. A declared
// event kind nothing can emit is a channel indistinguishable from "this
// never happens." Removed rather than wired up: re-grepped the whole repo
// (src, tests, docs) before removing and found zero downstream consumers —
// no renderer, no test, no other agent's hook parser referenced the kind.
export type PlanEvent =
  // #291: a hard-block override of depends_on must never be silent. Folded
  // into status_changed itself (rather than a second, separate journal
  // event/write) so the override record is atomic with the transition it
  // describes — a round-1 cassandra review found that two sequential
  // appendJournal calls left a crash window where "done" could land durably
  // with no override record at all, which is exactly the silence this fix
  // exists to prevent. depends_on_override is present only when force
  // actually mattered (unmet was non-empty at the time of the transition).
  | {
      kind: "status_changed";
      slice_id: string;
      from: PlanSliceStatus;
      to: PlanSliceStatus;
      at: string;
      evidence?: string;
      depends_on_override?: { unmet: string[]; reason?: string; forced_by: string };
    }
  // #291 (round-1 cassandra finding 1): replan() can silently rewrite an
  // existing slice's depends_on (including erasing it to []), which
  // defeats the hard block above with no journal trail at all — a plan
  // author never needs force/force_reason if they can just replan the
  // dependency away first. depends_on_changed makes that edit visible: any
  // slice whose depends_on differs before/after the replan is listed here.
  // This does not BLOCK the edit (replan's whole purpose is restructuring
  // the plan, and gating that is a larger, separate design decision) — it
  // only guarantees the edit can never be silent, which is the actual
  // invariant #291's design brief requires.
  | {
      kind: "replan";
      at: string;
      note?: string;
      depends_on_changed?: Array<{ slice_id: string; from: string[]; to: string[] }>;
      // #291 round-2 cassandra finding HIGH-1: a dependency slice being
      // superseded (the ordinary way to replan) satisfies any surviving
      // slice that depends on it just as much as depends_on being edited
      // directly — must be equally non-silent.
      depends_on_superseded?: Array<{ slice_id: string; depended_on_by: string[] }>;
      // Landed topology from orch apply (add/drop MCP args OR new_slices
      // full-set replan). Journal is SoT for what actually applied —
      // derived from before→after, never claim tokens. propose_structure
      // never writes these.
      add_slices?: CreatePlanInput["slices"];
      drop_slice_ids?: string[];
    }
  | { kind: "gate_passed"; slice_id: string; evidence: string; at: string }
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
 *
 * Exported (like computePlanDigestV1) so tests can stamp a note as declared
 * v2 — v2 is now itself a legacy algorithm superseded by v3 below, and the
 * declared-v2-stays-readable-without-mutation contract needs a real v2 hex
 * to fabricate a fixture with.
 */
export function computePlanDigestHexV2(plan: PlanArtifact): string {
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
 * Thread Phase 1 (Task 2, worker slice metadata): widens the H7 v2 payload
 * with every new PlanSlice field (requirements, assigned_to,
 * assignment_profile, generation, attempt, claim, proposals) so a vault edit
 * to any of them — an unauthorized reassignment, a forged claim, a silently
 * dropped structural proposal — is caught by digest verification exactly
 * like every pre-existing field is (Gate T2: every new durable field affects
 * v3). `generation`/`attempt` default to 0 in this pure computation only;
 * that default is never written back into the slice itself (see the
 * rehydratePlan declared-version gate below, which returns a declared-older
 * note unmodified rather than upgrading it on a mere read).
 */
function computePlanDigestHexV3(plan: PlanArtifact): string {
  const slices = plan.slices
    .map((sl) => ({
      id: sl.id,
      title: sl.title,
      status: sl.status,
      gate: sl.gate,
      depends_on: sl.depends_on ? [...sl.depends_on].sort() : undefined,
      evidence: sl.evidence,
      superseded_by: sl.superseded_by,
      replaced_by: sl.replaced_by ? [...sl.replaced_by].sort() : undefined,
      requirements: sl.requirements ? [...sl.requirements].sort() : undefined,
      assigned_to: sl.assigned_to,
      assignment_profile: sl.assignment_profile,
      generation: sl.generation ?? 0,
      attempt: sl.attempt ?? 0,
      claim: sl.claim
        ? {
            claim_id: sl.claim.claim_id,
            worker_agent_id: sl.claim.worker_agent_id,
            claimed_at: sl.claim.claimed_at,
            expires_at: sl.claim.expires_at,
          }
        : undefined,
      proposals: sl.proposals ?? undefined,
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
    v: 3,
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
 * #122 F-PLAN-DIGEST-CROSSPROC (revised after codex review on PR #130): the
 * persisted plan_digest VALUE stays a bare hex so pre-tagging readers on other
 * hosts keep validating it during a rolling update; the algorithm version
 * travels in the separate plan_digest_v frontmatter field (old readers ignore
 * unknown fields). Read-time recognition dispatches on the declared version
 * through a registry of every historical algorithm, so payload widening
 * (v2->v3, and any future version) cannot re-open the single-legacy-fn cliff
 * that transiently bricked plan tools during the v1->v2 rollout. Notes
 * without plan_digest_v are still recognized as bare v2-or-v1 exactly as
 * before, and "vN:<hex>" digest prefixes (written by interim builds of a
 * version bump) are accepted on read and normalized to bare hex on the next
 * write — but ONLY when the declared version is the CURRENT one. A note that
 * DECLARES an older version (v1 or v2) validates against that older
 * algorithm and is returned as-is: rehydratePlan must never write-on-read a
 * note a still-running older-plugin host declares itself the owner of during
 * a rolling upgrade (Task 2 / Thread Phase 1). The next explicit mutation
 * naturally advances such a note to v3 through the normal persistPlan path.
 */
export const PLAN_DIGEST_VERSION = 3;

const PLAN_DIGEST_ALGORITHMS: Record<number, (plan: PlanArtifact) => string> = {
  1: computePlanDigestV1,
  2: computePlanDigestHexV2,
  3: computePlanDigestHexV3,
};

/** Current digest (bare hex; the algorithm version is persisted separately as plan_digest_v). */
export function computePlanDigest(plan: PlanArtifact): string {
  return computePlanDigestHexV3(plan);
}

/**
 * Every PlanSlice key that ONLY v3 knows how to hash. Review finding
 * (Task 2 follow-up): the v1/v2 algorithms pick specific known keys into
 * their payload and silently ignore anything else — so a slice can carry
 * one of these keys (an unauthorized reassignment, a forged claim, an
 * injected structural proposal) while the note's DECLARED v1/v2 digest still
 * validates, because that older algorithm never looked at the key in the
 * first place. A genuine v1/v2 writer's PlanSlice type never had these
 * fields, so JSON.stringify of a real one never emits them — their mere
 * presence on a note that declares an older version is itself the tamper
 * signal, independent of what the declared algorithm's hash covers.
 */
const V3_ONLY_SLICE_FIELDS = [
  "requirements",
  "assigned_to",
  "assignment_profile",
  "generation",
  "attempt",
  "claim",
  "proposals",
  "replaced_by",
] as const;

function findV3OnlySliceField(
  slices: PlanSlice[],
): { sliceId: string; field: string } | undefined {
  for (const slice of slices) {
    const record = slice as unknown as Record<string, unknown>;
    for (const field of V3_ONLY_SLICE_FIELDS) {
      if (record[field] !== undefined) {
        return { sliceId: slice.id, field };
      }
    }
  }
  return undefined;
}

function parsePlanDigestTag(stored: string): { version: number; hex: string } | undefined {
  const m = stored.match(/^v(\d+):([0-9a-f]+)$/);
  return m ? { version: Number(m[1]), hex: m[2] } : undefined;
}

/**
 * Model-facing / `.message` text for a newer-than-supported digest.
 * Keep `notePath` as a typed field on PlanDigestVersionError — never
 * interpolate it here. Worker MCP surfaces this via threadWorkerErrorText.
 */
export function planDigestVersionErrorMessage(version: number): string {
  return `plan_digest version v${version} is newer than this plugin supports (max v${PLAN_DIGEST_VERSION}); update the minni plugin to read this note`;
}

/**
 * A note whose declared digest version is newer than this plugin understands.
 * Typed (not a bare Error) so recovery paths can tell it apart from a tamper:
 * minni_thread_restore must refuse to "heal" such a note — writing it back with
 * this plugin's older schema would silently downgrade newer fields.
 */
export class PlanDigestVersionError extends Error {
  readonly code = "PLAN_DIGEST_NEWER" as const;
  readonly version: number;
  readonly notePath: string;
  constructor(version: number, notePath: string) {
    super(planDigestVersionErrorMessage(version));
    this.name = "PlanDigestVersionError";
    this.version = version;
    this.notePath = notePath;
  }
}

/**
 * Active-plan pointer / view read failed for a filesystem or lock reason.
 * Distinct from "no active plan" (undefined) so hooks do not treat a failed
 * read as "nothing salient." Message is path-free — syscall code only.
 */
export class ActivePlanReadError extends Error {
  readonly code = "ACTIVE_PLAN_READ_FAILED" as const;
  readonly causeCode?: string;

  constructor(causeCode?: string, cause?: unknown) {
    super(
      causeCode
        ? `active plan read failed: ${causeCode}`
        : "active plan read failed",
      cause instanceof Error ? { cause } : undefined,
    );
    this.name = "ActivePlanReadError";
    this.causeCode = causeCode;
  }
}

const ACTIVE_PLAN_ERRNO = /^E[A-Z][A-Z0-9]{1,30}$/;

function activePlanErrnoCode(error: unknown): string | undefined {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error as { code: unknown }).code;
    if (typeof code === "string" && ACTIVE_PLAN_ERRNO.test(code)) {
      return code;
    }
  }
  return undefined;
}

/**
 * persistPlan writes the canonical vault note, then appends a history
 * snapshot as a second, separate durable step. If the note write succeeds
 * but the history append throws (e.g. EISDIR/EACCES on the history file),
 * the plan mutation is ALREADY durable — this is never a pre-commit
 * failure. Typed (not a bare Error) so a caller holding a freshly staged
 * secret (claimSlice) can tell "committed, journal degraded" apart from
 * "nothing was written" without re-deriving that distinction from message
 * text, and so it never mistakes this for a rollback signal that would
 * license deleting the only token for a now-durable claim.
 */
const SYS_ERR_CODE = /^[A-Z][A-Z0-9_]{1,31}$/;

/**
 * Model-facing / .message cause fragment for a failed history append.
 * Prefer a Node syscall `.code` (EISDIR, EACCES, …) so the operator still
 * sees the failure class. Never interpolate cause.message or cause.path:
 * real Node system errors embed the history file path
 * (`wiki/artifacts/…history.jsonl`), which is adjacent to — but distinct
 * from — the vault notePath. Without a syscall code, use a generic phrase.
 */
export function planHistoryAppendErrorCauseText(cause: unknown): string {
  if (cause && typeof cause === "object") {
    const code = (cause as { code?: unknown }).code;
    if (typeof code === "string" && SYS_ERR_CODE.test(code)) {
      return code;
    }
  }
  return "history append failed";
}

export function planHistoryAppendErrorMessage(rev: number, cause: unknown): string {
  return `persistPlan: note committed at rev ${rev}, but appending the history snapshot failed: ${planHistoryAppendErrorCauseText(cause)}`;
}

export class PlanHistoryAppendError extends Error {
  readonly code = "PLAN_HISTORY_APPEND_FAILED" as const;
  readonly notePath: string;
  readonly rev: number;
  constructor(notePath: string, rev: number, cause: unknown) {
    super(
      planHistoryAppendErrorMessage(rev, cause),
      cause instanceof Error ? { cause } : undefined,
    );
    this.name = "PlanHistoryAppendError";
    this.notePath = notePath;
    this.rev = rev;
  }
}

/**
 * #122 (codex round 5): single declared-version gate for EVERY path that reads
 * plan frontmatter — strict or lenient. A note declaring an unknown (newer)
 * digest version, via plan_digest_v or a "vN:<hex>" digest prefix, throws the
 * typed PlanDigestVersionError before this build judges the note against its
 * own schema in any way. Returns the parsed declaration for callers that go on
 * to verify the digest hex.
 *
 * Codex round 6: when BOTH declarations are present the note's effective
 * version is the NEWEST declared — a "v2:<hex>" prefix must not shadow a
 * plan_digest_v: 3 marker, or this older build would verify the note as v2
 * and the normalization rewrite would stamp plan_digest_v back to 2,
 * bypassing the downgrade guard. If both are known but disagree, the digest
 * is likewise verified under the newest declared version (a stale lower
 * declaration never weakens verification); a hex that fails under it is
 * reported as tampered.
 */
function assertKnownDigestVersion(
  fm: Record<string, unknown>,
  notePath: string,
): { storedTag?: { version: number; hex: string }; declaredVersion?: number } {
  const rawStoredDigest = typeof fm.plan_digest === "string" ? fm.plan_digest : "";
  const storedTag = parsePlanDigestTag(rawStoredDigest);
  const fmDigestV = typeof fm.plan_digest_v === "number" ? fm.plan_digest_v : undefined;
  const declared = [storedTag?.version, fmDigestV].filter(
    (v): v is number => typeof v === "number",
  );
  const declaredVersion = declared.length > 0 ? Math.max(...declared) : undefined;
  if (declaredVersion !== undefined && !PLAN_DIGEST_ALGORITHMS[declaredVersion]) {
    throw new PlanDigestVersionError(declaredVersion, notePath);
  }
  return { storedTag, declaredVersion };
}

/**
 * Statuses a plan never returns from. Mirrors resolveActivePlanView's
 * injection-suppression set; shared by createPlan's displacement warning and
 * the minni_thread_activate terminal guard (#122).
 */
export const TERMINAL_PLAN_STATUSES: ReadonlySet<string> = new Set([
  "accepted",
  "complete",
  "rejected",
  "superseded",
]);

/**
 * All-resolved predicate: every slice terminal (done/superseded) on a non-empty
 * slice list. Single source of truth for updateSlice's terminal-state
 * transition, resolveActivePlanView's honest-health self-heal, and the
 * activate guard — a plan in this shape is finished even when a stale deploy
 * left its status scalar at 'draft'/'candidate' (#122 review follow-up).
 */
export function allSlicesResolved(slices: PlanSlice[]): boolean {
  return slices.length > 0 && slices.every((s) => s.status === "done" || s.status === "superseded");
}

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

function assertUniqueSliceIds(
  slices: Array<{ id: string }>,
  context: string,
): void {
  const seen = new Set<string>();
  for (const slice of slices) {
    if (seen.has(slice.id)) {
      throw new Error(`${context}: duplicate slice id "${slice.id}"`);
    }
    seen.add(slice.id);
  }
}

function assertUniqueExplicitSliceIds(
  slices: Array<{ id?: string }>,
  context: string,
): void {
  const seen = new Set<string>();
  for (const slice of slices) {
    if (!slice.id) continue;
    if (seen.has(slice.id)) {
      throw new Error(`${context}: duplicate explicit slice id "${slice.id}"`);
    }
    seen.add(slice.id);
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
    // #122: version marker kept OUT of the digest string so pre-tagging
    // readers (which compare plan_digest byte-for-byte) never see a value
    // they cannot match; they simply ignore this extra field.
    plan_digest_v: PLAN_DIGEST_VERSION,
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

// Bugbot on #309 (campaign scar #3 — source-grep tests are false confidence:
// a mutant that RENAMES the call site trips a regex but proves nothing about
// behavior, and a mutant that swaps the durable helper's own internals for a
// plain write while keeping its name and signature would sail straight past
// a source-text assertion). Injectable seam so tests can spy on the ACTUAL
// call — path, content, and that it fires at all — rather than grepping
// plan.ts's source text for the helper's name.
export interface AppendJournalDeps {
  appendFileWithFsync?: typeof appendFileWithFsync;
  writeFileAtomic?: typeof writeFileAtomic;
}

function isJournalMissing(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code: unknown }).code === "ENOENT"
  );
}

/** Append a PlanEvent as a single JSON line. Creates header on first write. */
export async function appendJournal(
  journalPath: string,
  event: PlanEvent,
  deps: AppendJournalDeps = {},
): Promise<void> {
  // #293 (June audit N6, sibling of PLUMB-T4/#231): this used to be a plain
  // appendFile/writeFile, unlike history.jsonl's appendFileWithFsync
  // (plan.ts's historyFile write, below) — a crash could leave history
  // durable and the journal behind it or truncated. Same durability
  // guarantee as the sibling now: fsync'd append, atomic temp+rename init.
  //
  // Init (header + first line) is ENOENT-only. A catch-all rewrite after a
  // failed fsync append wipes this file, which is also the ordered Thread
  // event journal — real lost-events, not recovery.
  const doAppendWithFsync = deps.appendFileWithFsync ?? appendFileWithFsync;
  const doWriteAtomic = deps.writeFileAtomic ?? writeFileAtomic;
  const line = JSON.stringify(event) + "\n";

  let existing: string;
  try {
    existing = await readFile(journalPath, "utf8");
  } catch (error) {
    if (!isJournalMissing(error)) {
      throw error;
    }
    const header = `# Minni Plan Journal\n\n## events\n`;
    await doWriteAtomic(journalPath, header + line);
    return;
  }

  const prefix = existing.length > 0 && !existing.endsWith("\n") ? "\n" : "";
  await doAppendWithFsync(journalPath, prefix + line);
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

  assertUniqueExplicitSliceIds(input.slices ?? [], "createPlan");
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
  // Ids stay 'plan-' prefixed after the minni:threads rename — the prefix is baked
  // into existing filenames (wiki/artifacts/plan-<hex>.md), [[plan-<hex>]] wikilinks,
  // journals, history siblings and audit history. The rename is tool/command-layer
  // only. Changing this splits the vault's id space and orphans every inbound
  // wikilink; it needs a real migration, not an edit here. Guarded by the
  // "freeze guard" test in tests/plan.test.mjs.
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
    // Effectively-terminal predicate shared with resolveActivePlanView and
    // activatePlanChecked (codex round 4): a terminal status OR a stale
    // all-resolved shape (status scalar stuck at draft/candidate with every
    // slice done/superseded) is a finished plan — displace it silently rather
    // than warn the user toward re-activating it.
    let incumbentTerminal = false;
    try {
      const scalars = await rehydratePlanScalars(incumbent.notePath);
      incumbentTerminal =
        TERMINAL_PLAN_STATUSES.has(scalars.status) || allSlicesResolved(scalars.slices);
    } catch {
      // unreadable incumbent: conservatively report the displacement
    }
    if (!incumbentTerminal) {
      displaced_active = incumbent.plan_id;
    }
  }

  await setActivePlan(vaultPath, plan.plan_id, writeRes.notePath);

  const journalPath = journalPathFor(writeRes.notePath, plan.plan_id);
  await appendJournal(journalPath, { kind: "rehydrated", at: plan.created });

  return { plan, write: writeRes, displaced_active };
}

// #294 (June audit N7): history.jsonl stored one full plan snapshot per
// revision, unbounded — a long-running or frequently-updated plan grows this
// file forever, and a completed plan's history was never archived either
// (H4 unbounded growth). Cap it to the most recent N revisions on write;
// older snapshots are dropped. A revision-count cap alone satisfies the
// issue's acceptance criteria ("a bound ... exists"); archive-on-complete
// was considered and deliberately NOT added on top of it — it would need to
// decide where archived history lives, whether readHistory/minni_thread_history
// check both locations, and how it interacts with a plan being reopened
// after completion, none of which this issue's finding required solving.
// A bounded rolling window is the smaller, sufficient fix; full historical
// retention is a real but separate feature if an operator ever wants it.
// getRevision/minni_thread_revision already handle a rotated-out revision as
// a graceful "not found" (server.ts), so capping never surfaces as silently
// wrong data — checked, not assumed.
//
// Configurable via MINNI_PLAN_HISTORY_CAP, parsed the same defensively-never-
// breaks-a-write way as this codebase's other env-tunable caps (e.g.
// MINNI_RPC_WORKERS in minnid.py): malformed or non-positive values fall
// back to the default rather than failing the plan write that triggered it.
// Strict digit-only validation (not Number.parseInt's lenient prefix
// parsing) — a cassandra review round found parseInt silently reads
// "1e9"/"1e3" as 1, so an operator RAISING the cap via that env var would
// have gotten a cap of 1 instead — the opposite of their intent, and the
// most destructive possible misparse of a size knob.
const DEFAULT_PLAN_HISTORY_CAP = 200;
// Rotate only once meaningfully over the cap, trimming back down to the cap.
// Without this, a long-running plan sitting AT the cap triggers a full
// read+rewrite+fsync on every single subsequent append forever (measured:
// tens of KB to multi-MB per edit at realistic plan sizes) — hysteresis
// amortizes that to roughly one rotation per HYSTERESIS_MARGIN appends, and
// shrinks how often the read-then-rewrite race window (below) opens at all.
// One consequence, noted here rather than left implicit: the cap is a soft
// ceiling, not exact — between rotations a history file can hold up to
// cap + HISTORY_ROTATION_HYSTERESIS valid lines (250 at the default), and
// readHistory() will return all of them. That's the intended trade for
// amortizing rewrite cost, not a bug.
const HISTORY_ROTATION_HYSTERESIS = 50;
// Upper bound on an operator-supplied cap. Without this, a huge value (e.g.
// a typo'd extra digit) parses as a valid positive integer and silently
// disables the whole feature this issue exists to add — a round-2 cassandra
// finding. MAX is generous enough that no real deployment should hit it
// deliberately, while still bounding worst-case file growth.
const MAX_PLAN_HISTORY_CAP = 100_000;

function planHistoryCap(): number {
  const raw = (process.env.MINNI_PLAN_HISTORY_CAP ?? "").trim();
  if (!/^\d+$/.test(raw)) return DEFAULT_PLAN_HISTORY_CAP;
  const parsed = Number.parseInt(raw, 10);
  if (!(parsed > 0)) return DEFAULT_PLAN_HISTORY_CAP;
  return Math.min(parsed, MAX_PLAN_HISTORY_CAP);
}

/**
 * The same validity predicate readHistory() applies when parsing lines, used
 * here too so rotation's line count agrees with what readHistory actually
 * reports. A cassandra round found rotation originally counted ANY non-blank
 * line toward the cap while readHistory silently drops malformed/schema-
 * invalid ones — a garbage line could occupy a cap slot and evict a genuine
 * revision that readHistory would have returned. Filtering here the same way
 * also means a garbage line is correctly dropped by rotation rather than
 * preserved forever.
 */
function isValidHistoryLine(trimmed: string): boolean {
  if (!trimmed) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return (
      typeof parsed.rev === "number" &&
      typeof parsed.at === "string" &&
      typeof parsed.digest === "string" &&
      parsed.plan &&
      typeof parsed.plan.plan_id === "string"
    );
  } catch {
    return false;
  }
}

export interface AppendHistorySnapshotDeps {
  appendFileWithFsync?: typeof appendFileWithFsync;
  writeFileAtomic?: typeof writeFileAtomic;
  readFile?: typeof readFile;
}

// Per-history-file promise-chain lock, same shape as vault.ts's
// withAuditLock. A cassandra review round REPRODUCED data loss without this:
// appendHistorySnapshot's rotation does a separate read-then-rewrite: caller
// A appends (durably, fsync'd), reads the file, then WHILE A is deciding/
// writing the rotated file, caller B's append lands and fsyncs — A's stale
// read still wins the race and A's writeFileAtomic overwrites B's already-
// durable revision. persistPlan's call sites in server.ts are unserialized
// async RPC handlers, so two edits to the SAME plan_id landing back-to-back
// is a real, reachable interleaving within one daemon process, not a
// theoretical one. This lock closes that window for same-process callers,
// which is the reachable case in this codebase's concurrency model (one
// daemon process serving multiple RPC/MCP requests). It does NOT close a
// cross-process race (two independent OS processes editing the identical
// plan_id at the same instant) — closing that would need a real filesystem
// lock (flock/O_EXCL), a larger change this issue's scope does not require;
// documented here as a known, narrow, disclosed residual rather than
// silently assumed away.
const historyLocks = new Map<string, Promise<void>>();

async function withHistoryLock<T>(historyFile: string, fn: () => Promise<T>): Promise<T> {
  const key = path.resolve(historyFile);
  const previous = historyLocks.get(key) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => {
    release = resolve;
  });
  const tail = previous.then(() => current, () => current);
  historyLocks.set(key, tail);

  await previous.catch(() => {});
  try {
    return await fn();
  } finally {
    release();
    if (historyLocks.get(key) === tail) {
      historyLocks.delete(key);
    }
  }
}

/**
 * Append one history snapshot line, then cap the file to the most recent
 * MINNI_PLAN_HISTORY_CAP (default 200) lines once meaningfully over the cap
 * (see HISTORY_ROTATION_HYSTERESIS). The append itself is always durable
 * (appendFileWithFsync) regardless of whether capping runs this call; capping
 * rewrites the file atomically (writeFileAtomic) so a crash mid-rotation
 * leaves the OLDER, uncapped file intact rather than a truncated one.
 * Serialized per history file (withHistoryLock) so two concurrent callers
 * for the same plan cannot interleave an append with another's rotation and
 * lose an already-durable revision — see withHistoryLock's comment for the
 * reproduced race this closes and the narrower cross-process gap it does not.
 * If the post-append read fails for any reason, the append has already
 * landed durably and capping is simply skipped for this call (self-heals on
 * the next successful append).
 */
export async function appendHistorySnapshot(
  historyFile: string,
  snapshot: unknown,
  deps: AppendHistorySnapshotDeps = {},
): Promise<void> {
  const doAppendWithFsync = deps.appendFileWithFsync ?? appendFileWithFsync;
  const doWriteAtomic = deps.writeFileAtomic ?? writeFileAtomic;
  const doReadFile = deps.readFile ?? readFile;

  await withHistoryLock(historyFile, async () => {
    await doAppendWithFsync(historyFile, JSON.stringify(snapshot) + "\n");

    const cap = planHistoryCap();
    let raw: string;
    try {
      raw = await doReadFile(historyFile, "utf8");
    } catch {
      return;
    }
    const lines = raw.split(/\r?\n/).filter((line) => isValidHistoryLine(line.trim()));
    if (lines.length <= cap + HISTORY_ROTATION_HYSTERESIS) return;
    const kept = lines.slice(lines.length - cap);
    await doWriteAtomic(historyFile, kept.join("\n") + "\n");
  });
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
  assertUniqueSliceIds(plan.slices, "persistPlan");

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
    // The write already landed. Do not interpolate either vault path into
    // `.message` — MCP surfaces persistPlan errors via threadWorkerErrorText.
    throw new Error(
      "persistPlan: durable write landed at a different notePath than the caller expected",
    );
  }

  // append snapshot line to history file, capped (#294) at MINNI_PLAN_HISTORY_CAP
  const historyFile = historyPathFor(writeRes.notePath);
  const snapshot = {
    rev: plan.rev,
    at: updated,
    digest: plan.plan_digest,
    plan,
  };
  try {
    await appendHistorySnapshot(historyFile, snapshot);
  } catch (cause) {
    // The note above is already durable — never swallow this into a bare
    // Error a caller could mistake for "nothing was written". See
    // PlanHistoryAppendError's doc comment.
    throw new PlanHistoryAppendError(writeRes.notePath, plan.rev, cause);
  }

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
    // Journals are `<planId>.log.md`. They are not plan notes; reading them
    // here used to abort discovery with a path-bearing Node EISDIR/EACCES
    // before MCP handlers reached threadWorkerErrorResult.
    if (!name.endsWith(".md") || name.endsWith(".log.md")) continue;
    const notePath = path.join(dir, name);
    try {
      const raw = await readFile(notePath, "utf8");
      const { frontmatter: fm } = parseFrontmatter(raw);
      if (String(fm.plan_id ?? "") === plan_id) return notePath;
    } catch {
      // Per-file: a sibling directory named *.md or an unreadable note must
      // not abort the scan or leak a vault path as a raw throw.
    }
  }
  return undefined;
}

function isTrivialEvidence(ev: string): boolean {
  const trimmed = ev.trim().toLowerCase();
  const trivial = new Set(["x", "ok", "done", "good", "looks good", "lgtm", "yes", "fine", "wip", "na", "n/a"]);
  return trivial.has(trimmed) || ev.trim().length < 8;
}

/**
 * #291 (audit N4): depends_on was advisory only — updateSlice never read it,
 * so a slice whose dependency was still open could be silently marked done.
 * Pure lookup used by the hard-block below: a dependency counts as unmet
 * unless it resolves to a slice that is itself "done" or "superseded"
 * (matching allSlicesResolved's definition of resolved). A depends_on id
 * that doesn't match any slice in the plan (typo, or a slice removed by
 * replan) is also unmet — it can never be resolved, so it must not be
 * silently ignored either.
 *
 * Exclusive split exception: a superseded slice with replaced_by is a
 * replacement, not drop-without-replacement. Dependents that still point at
 * it stay blocked until orch remounts depends_on onto the children.
 */
export function unmetDependencies(plan: PlanArtifact, slice_id: string): string[] {
  const slice = plan.slices.find((s) => s.id === slice_id);
  const depends_on = slice?.depends_on ?? [];
  return depends_on.filter((depId) => {
    const dep = plan.slices.find((s) => s.id === depId);
    if (!dep) return true;
    if (dep.status === "done") return false;
    if (dep.status === "superseded") {
      return Array.isArray(dep.replaced_by) && dep.replaced_by.length > 0;
    }
    return true;
  });
}

/**
 * #291 (round-1 cassandra finding 1): replan() can rewrite an existing
 * slice's depends_on with no journal trail — pure diff used by the
 * minni_thread_replan handler to make that edit visible instead of silent.
 * Compares by slice id present in BOTH before/after; a slice that no
 * longer exists after replan is out of scope here (it is superseded, which
 * unmetDependencies already treats as resolved — that's finding 2,
 * accepted as a disclosed residual, not this function's job). Order-
 * insensitive: [a, b] and [b, a] are not a change.
 */
export function diffDependsOn(
  before: PlanArtifact,
  after: PlanArtifact,
): Array<{ slice_id: string; from: string[]; to: string[] }> {
  const changes: Array<{ slice_id: string; from: string[]; to: string[] }> = [];
  for (const afterSlice of after.slices) {
    const beforeSlice = before.slices.find((s) => s.id === afterSlice.id);
    if (!beforeSlice) continue;
    const from = [...(beforeSlice.depends_on ?? [])].sort();
    const to = [...(afterSlice.depends_on ?? [])].sort();
    if (from.length !== to.length || from.some((v, i) => v !== to[i])) {
      changes.push({ slice_id: afterSlice.id, from: beforeSlice.depends_on ?? [], to: afterSlice.depends_on ?? [] });
    }
  }
  return changes;
}

/**
 * #291 (round-2 cassandra finding HIGH-1, confirmed by independent
 * reproduction against dist/plan.js before trusting the review):
 * diffDependsOn only catches edits to a slice's depends_on ARRAY. It does
 * NOT catch a dependency being satisfied "for free" by superseding the
 * dependency slice itself — which is the ordinary, common way to replan
 * (a slice omitted from new_slices, or listed in drop_slice_ids, becomes
 * superseded, and unmetDependencies already treats superseded as
 * resolved). That path is cheaper than editing depends_on directly and,
 * before this function, produced NO journal trail at all — the plain
 * "replan" event previously carried only depends_on diffs. Orch apply
 * also journals landed add_slices / drop_slice_ids on that same event
 * (see landedReplanTopology). Pure diff used by the
 * minni_thread_replan handler to make that supersession visible: for every
 * slice that newly became superseded in this operation, list which
 * still-open (not done/superseded) slices depend on it.
 */
export function diffSupersededDependencies(
  before: PlanArtifact,
  after: PlanArtifact,
): Array<{ slice_id: string; depended_on_by: string[] }> {
  const newlySuperseded = new Set(
    after.slices
      .filter((s) => s.status === "superseded")
      .map((s) => s.id)
      .filter((id) => {
        const wasSlice = before.slices.find((b) => b.id === id);
        return !!wasSlice && wasSlice.status !== "superseded";
      }),
  );
  const result: Array<{ slice_id: string; depended_on_by: string[] }> = [];
  for (const id of newlySuperseded) {
    const dependedOnBy = after.slices
      .filter((s) => s.status !== "done" && s.status !== "superseded")
      .filter((s) => (s.depends_on ?? []).includes(id))
      .map((s) => s.id);
    if (dependedOnBy.length > 0) {
      result.push({ slice_id: id, depended_on_by: dependedOnBy });
    }
  }
  return result;
}

/**
 * Landed add/drop for a replan apply, derived from before→after — not from
 * which MCP args the orch used. Closes the hole where new_slices (full-set
 * replan) can land adds and supersessions while the ordered replan event
 * still omits add_slices / drop_slice_ids because those MCP fields were
 * absent. Named remount is set_depends_on (edge edit; omitted live ids
 * stay); it journals depends_on_changed, not add/drop. Echoing request
 * args would miss generated ids; journaling landed state keeps the
 * ordered journal SoT.
 *
 * add_slices: newly appended slices (id + proposal fields only; no claim
 * token, status, proposals, or evidence). Ordered mirrors elsewhere omit
 * evidence (status_changed is status enums only). drop_slice_ids: ids that
 * newly became superseded. Empty sides are omitted.
 */
export function landedReplanTopology(
  before: PlanArtifact,
  after: PlanArtifact,
): {
  add_slices?: NonNullable<CreatePlanInput["slices"]>;
  drop_slice_ids?: string[];
} {
  const beforeIds = new Set(before.slices.map((s) => s.id));
  const add_slices = after.slices
    .filter((s) => !beforeIds.has(s.id))
    .map((s) => {
      const landed: {
        id: string;
        title: string;
        gate?: string;
        depends_on?: string[];
      } = { id: s.id, title: s.title };
      if (s.gate !== undefined) landed.gate = s.gate;
      if (s.depends_on !== undefined) landed.depends_on = [...s.depends_on];
      return landed;
    });
  const drop_slice_ids = after.slices
    .filter((s) => s.status === "superseded")
    .map((s) => s.id)
    .filter((id) => {
      const was = before.slices.find((b) => b.id === id);
      return !!was && was.status !== "superseded";
    })
    .sort();
  const out: {
    add_slices?: NonNullable<CreatePlanInput["slices"]>;
    drop_slice_ids?: string[];
  } = {};
  if (add_slices.length > 0) out.add_slices = add_slices;
  if (drop_slice_ids.length > 0) out.drop_slice_ids = drop_slice_ids;
  return out;
}

export interface UpdateSliceOptions {
  /**
   * Bypass the depends_on hard block for a transition to "done". Requires
   * forceReason (below) — force alone is not enough to bypass, by design:
   * the point of a journaled override is that it can never be silent, and a
   * caller that can't articulate why shouldn't be able to force it either.
   * The caller (server.ts's minni_thread_update) is responsible for
   * recomputing unmetDependencies against the pre-update plan and appending
   * a depends_on_override journal event when force actually mattered —
   * updateSlice itself is pure and does no I/O, so it cannot journal.
   */
  force?: boolean;
  forceReason?: string;
}

/** Immutable update of one slice. Evidence is mandatory to reach "done". Recomputes next_action + digest. */
export function updateSlice(
  plan: PlanArtifact,
  slice_id: string,
  to: PlanSliceStatus,
  evidence?: string,
  options?: UpdateSliceOptions,
): PlanArtifact {
  assertUniqueSliceIds(plan.slices, "updateSlice");
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
    const unmet = unmetDependencies(plan, slice_id);
    if (unmet.length > 0) {
      if (!options?.force) {
        throw new Error(
          // #291 round-1 cassandra finding 8: this message is the only guidance
          // an MCP-calling model sees on refusal — name the MCP-facing field
          // (force_reason on minni_thread_update), not the internal TS option
          // name, so a model retrying doesn't pass a parameter that doesn't exist.
          `updateSlice: cannot mark "${slice_id}" done — depends_on unmet: ${unmet.join(", ")} (must be "done", or "superseded" without replacement, first). Pass force + a non-empty force reason to override.`
        );
      }
      if (!options.forceReason || !options.forceReason.trim()) {
        throw new Error(
          `updateSlice: force override of depends_on requires a non-empty force reason explaining why (unmet: ${unmet.join(", ")})`
        );
      }
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
  const allResolved = allSlicesResolved(newSlices);
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

function sameStringSet(
  left: string[] | undefined,
  right: string[] | undefined,
): boolean {
  const a = [...(left ?? [])].sort();
  const b = [...(right ?? [])].sort();
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * A structural edit invalidates any work issued for the old slice meaning.
 * Clearing the public claim ref removes it from every worker authority path;
 * incrementing generation also prevents an orphaned private envelope from
 * becoming authoritative again if the slice is later reassigned.
 */
function invalidateSliceGeneration(slice: PlanSlice): PlanSlice {
  return {
    ...slice,
    generation: (slice.generation ?? 0) + 1,
    claim: undefined,
  };
}

/** Replan: preserve superset (never drop history). Mark no-longer-proposed non-final slices superseded; append unmatched new ones. Pure. */
export function replan(
  plan: PlanArtifact,
  newSlices: Array<{ id?: string; title: string; gate?: string; depends_on?: string[]; evidence?: string }>,
): PlanArtifact {
  if (!Array.isArray(newSlices)) {
    return { ...plan, updated: new Date().toISOString() };
  }
  assertUniqueSliceIds(plan.slices, "replan");
  assertUniqueExplicitSliceIds(newSlices, "replan");
  const updated = new Date().toISOString();
  // Deterministic marker (no clock in id)
  const titlesKey = stableStringify(newSlices.map((s) => (s.title ?? s.id ?? "")).sort());
  const supersededMarker = `replan-${createHash("sha256").update(titlesKey).digest("hex").slice(0, 10)}`;

  // Supersede old non-final that are absent from the proposed set (match by id or title)
  const newlySupersededIds = new Set<string>();
  let nextSlices: PlanSlice[] = plan.slices.map((slice) => {
    const stillProposed = newSlices.some(
      (ns) =>
        (ns.id && ns.id === slice.id) ||
        ((ns.title ?? "").trim().toLowerCase() === slice.title.trim().toLowerCase()),
    );
    if (!stillProposed && slice.status !== "done" && slice.status !== "superseded") {
      newlySupersededIds.add(slice.id);
      return invalidateSliceGeneration({
        ...slice,
        status: "superseded",
        superseded_by: supersededMarker,
      });
    }
    return slice;
  });

  const usedIds = new Set(nextSlices.map((s) => s.id));
  const addedIds: string[] = [];

  // Append truly new (no id or title match among current non-superseded)
  for (const ns of newSlices) {
    const hasMatch = nextSlices.some((s) => {
      if (s.status === "superseded") return false;
      if (ns.id && s.id === ns.id) return true;
      return s.title.trim().toLowerCase() === (ns.title ?? "").trim().toLowerCase();
    });
    if (!hasMatch) {
      // #291 (round-2 cassandra finding HIGH-2): applySliceDelta's sibling
      // loop below has this exact collision independently reproduced and
      // live in two real vaults (a slice dropped and re-added with the
      // same explicit id in one call). In replan()'s own new_slices path
      // specifically, I could NOT reproduce a live duplicate: the
      // "stillProposed" check above already keys off `ns.id === slice.id`
      // across every newSlices entry, so any entry carrying id "a"
      // unconditionally keeps the original "a" alive (never superseded) —
      // there is no newSlices shape that both supersedes an id and
      // introduces a fresh entry under that same id in one replan() call.
      // Kept here as defense-in-depth against that coupling changing later,
      // not as a claim this branch is reachable today.
      if (ns.id && usedIds.has(ns.id)) {
        throw new Error(
          `replan: cannot add slice with id "${ns.id}" — a slice with that id already exists in this plan (even if superseded). Choose a different id, or omit id to let one be generated.`,
        );
      }
      const id = ns.id || slugifySliceId(ns.title, usedIds);
      usedIds.add(id);
      addedIds.push(id);
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
        const refreshed: PlanSlice = {
          ...cur,
          title: ns.title || cur.title,
          gate: ns.gate ?? cur.gate,
          depends_on: ns.depends_on ?? cur.depends_on,
        };
        const meaningChanged =
          refreshed.title !== cur.title ||
          refreshed.gate !== cur.gate ||
          !sameStringSet(refreshed.depends_on, cur.depends_on);
        nextSlices[idx] = meaningChanged
          ? invalidateSliceGeneration(refreshed)
          : refreshed;
      }
    }
  }

  // Same exclusive-split honesty as applySliceDelta: supersede + add in one
  // call is replacement. Omit-only (no adds) stays drop-without-replacement.
  if (newlySupersededIds.size > 0 && addedIds.length > 0) {
    nextSlices = nextSlices.map((slice) => {
      if (
        newlySupersededIds.has(slice.id) &&
        slice.status === "superseded" &&
        slice.superseded_by === supersededMarker
      ) {
        return { ...slice, replaced_by: [...addedIds] };
      }
      return slice;
    });
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

export interface RehydratePlanDeps {
  persistPlan?: typeof persistPlan;
  beforeUpgradePersist?: (plan: PlanArtifact) => Promise<void>;
}

/** Rehydrate snapshot from vault note (frontmatter + body). Read-only: does not append journal events. */
export async function rehydratePlan(
  notePath: string,
  deps: RehydratePlanDeps = {},
): Promise<PlanArtifact> {
  const raw = await readFile(notePath, "utf8");
  const { frontmatter: fm } = parseFrontmatter(raw);

  const plan_id = String(fm.plan_id ?? "");
  if (!plan_id) {
    throw new Error(`rehydratePlan: note ${notePath} missing plan_id in frontmatter`);
  }

  // #122 (codex re-review round 3): the declared-digest-version gate runs
  // BEFORE any current-schema validation (done-slice evidence, digest
  // verification). A note declaring a NEWER version must throw the typed
  // PlanDigestVersionError immediately — this plugin cannot judge a newer
  // schema, and a generic validation error thrown first would be misread by
  // recovery paths (minni_thread_restore) as recoverable corruption, letting an
  // older plugin downgrade-write the newer note.
  const { storedTag, declaredVersion } = assertKnownDigestVersion(fm, notePath);

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

  assertUniqueSliceIds(plan.slices, "rehydratePlan");

  // Validate that any 'done' slice has non-empty evidence
  for (const s of plan.slices) {
    if (s.status === "done" && (!s.evidence || !s.evidence.trim())) {
      throw new Error(`rehydratePlan: slice ${s.id} is 'done' without evidence (note tampered or corrupt)`);
    }
  }

  // Check for digest mismatch instead of silent repair.
  //
  // #122 F-PLAN-DIGEST-CROSSPROC (revised after codex review on PR #130,
  // extended by Task 2 / digest v3): dispatch on the DECLARED algorithm
  // version (resolved above, before any current-schema validation) through
  // the algorithm registry. A KNOWN version verifies with that exact
  // algorithm; a note with NO declared version validates against bare
  // v2-or-v1 exactly as before.
  //
  // Task 2: a declared version OLDER than current (v1 or v2) that verifies
  // is returned UNCHANGED — no persistPlan side effect on a mere read. A
  // rolling upgrade can have an older-plugin host still reading/writing that
  // note at its own declared version; silently rewriting it to the current
  // schema here would race that host and, for v1/v2, would also strip the
  // newer-only slice fields (assigned_to, generation, claim, ...) that
  // reader has never written, corrupting data it doesn't yet know exists.
  // The next EXPLICIT mutation (updateSlice/replan/persistPlan) is what
  // naturally advances such a note to v3. Only an interim "vN:<hex>" TAG on
  // an ALREADY-current declaration is normalized to bare hex here, mirroring
  // the pre-existing v1->v2 interim-tag behavior.
  const storedHex = storedTag ? storedTag.hex : plan.plan_digest;
  const recomputed = computePlanDigest(plan);
  let needsUpgrade = false;
  if (declaredVersion !== undefined) {
    const algo = PLAN_DIGEST_ALGORITHMS[declaredVersion];
    if (algo(plan) !== storedHex) {
      throw new Error(`rehydratePlan: plan_digest mismatch (stored=${plan.plan_digest} computed=${recomputed}); note may be tampered`);
    }
    // Review finding (Task 2 follow-up): a declared OLDER algorithm having
    // validated proves only that the fields IT covers are intact — it says
    // nothing about a v3-only slice key, which that algorithm never hashed
    // and therefore could not have caught being added, changed, or removed.
    // A genuine v1/v2 writer's slices can never contain one of these keys at
    // all, so their mere presence on a declared-older note is tampering,
    // not a legitimate value this build should trust or silently accept.
    if (declaredVersion < PLAN_DIGEST_VERSION) {
      const v3Field = findV3OnlySliceField(plan.slices);
      if (v3Field) {
        throw new Error(
          `rehydratePlan: slice "${v3Field.sliceId}" carries v3-only field "${v3Field.field}" outside declared digest v${declaredVersion}'s coverage; note may be tampered`,
        );
      }
    }
    // Normalize the RETURNED in-memory digest to bare hex regardless of the
    // write decision below — an interim "vN:<hex>" tag is an on-disk
    // encoding detail, never something a caller of rehydratePlan should see
    // in plan.plan_digest. This does not touch the note file; needsUpgrade
    // (just below) is the only thing that decides whether persistPlan runs.
    plan.plan_digest = storedHex;
    needsUpgrade = declaredVersion === PLAN_DIGEST_VERSION && storedTag !== undefined;
  } else if (plan.plan_digest !== recomputed) {
    // No declared version: legacy recognition. This has ALWAYS meant "bare
    // v2-or-v1" (see the doc comment on PLAN_DIGEST_VERSION above) — a note
    // with no plan_digest_v field at all predates the tagging field itself,
    // and could genuinely have been written by EITHER a pre-H7 v1 host or a
    // v2-era host that predated plan_digest_v (the tagging field and v1->v2
    // both landed together; a note written between the v2 payload widening
    // and the tagging field's own rollout has a bare v2 hex and no version
    // marker). Task 2's second follow-up regressed this to "v1 only",
    // which made a genuine bare-v2 note fail closed as tampered. Fixed by
    // trying every REGISTERED algorithm older than current, not just v1 —
    // this also means any future intermediate version added to
    // PLAN_DIGEST_ALGORITHMS is automatically covered here too.
    const legacyVersions = Object.keys(PLAN_DIGEST_ALGORITHMS)
      .map(Number)
      .filter((v) => v < PLAN_DIGEST_VERSION)
      .sort((a, b) => a - b);
    let matchedLegacyVersion: number | undefined;
    for (const v of legacyVersions) {
      if (PLAN_DIGEST_ALGORITHMS[v](plan) === plan.plan_digest) {
        matchedLegacyVersion = v;
        break;
      }
    }
    if (matchedLegacyVersion !== undefined) {
      // Review finding (Task 2 second follow-up): this path used to skip the
      // v3-only-field tamper check entirely, because that check originally
      // lived only inside the `declaredVersion !== undefined` branch above.
      // But an undeclared note validating against a legacy algorithm is
      // exactly as blind to the v3-only slice keys as a DECLARED
      // older note is — and, worse, it was about to be upgraded (persisted)
      // below, which would silently BLESS an injected
      // assigned_to/claim/proposals/replaced_by/etc. as legitimate v3 data. So the same
      // guard applies here too, before needsUpgrade is set.
      const v3Field = findV3OnlySliceField(plan.slices);
      if (v3Field) {
        throw new Error(
          `rehydratePlan: slice "${v3Field.sliceId}" carries v3-only field "${v3Field.field}" outside undeclared-v${matchedLegacyVersion} digest coverage; note may be tampered`,
        );
      }
      needsUpgrade = true;
    } else {
      throw new Error(`rehydratePlan: plan_digest mismatch (stored=${plan.plan_digest} computed=${recomputed}); note may be tampered`);
    }
  }
  if (needsUpgrade) {
    plan.plan_digest = recomputed;
    // Best-effort re-persist so the note carries the current bare-hex digest
    // plus plan_digest_v going forward. Never a direct file write (persistPlan
    // journals); a write failure leaves the in-memory upgrade intact so this
    // read still succeeds.
    try {
      const vaultPath = path.resolve(path.dirname(notePath), "..", "..");
      await deps.beforeUpgradePersist?.(plan);
      await (deps.persistPlan ?? persistPlan)(plan, { vaultPath, notePath });
    } catch {
      // advisory: the in-memory upgraded digest is enough for this read to proceed
    }
  }

  return plan;
}

/**
 * #122 F-PLAN-RESTORE-SELFBLOCK: bare-scalar read for recovery paths. Returns a
 * skeleton PlanArtifact carrying the frontmatter scalars that restorePlan
 * consumes from `current` (plan_id, status, created, updated, plan_digest, rev)
 * plus leniently-parsed slices for the activate guard, with NO digest or
 * evidence validation — so minni_thread_restore can heal a note
 * whose strict rehydratePlan throws (the exact bricked state it exists to fix).
 * Every digest-covered field comes from the history snapshot, and persistPlan
 * recomputes the digest on write, so nothing corrupt survives the restore.
 *
 * Lenient does NOT mean version-blind (codex round 5): a note declaring a
 * NEWER digest version still throws the typed PlanDigestVersionError — this
 * build cannot judge (or safely operate on) a newer writer's note, and e.g.
 * activating one would strand the host with an active plan no reader can
 * rehydrate. Only current-schema validation is skipped, never the version gate.
 */
export async function rehydratePlanScalars(notePath: string): Promise<PlanArtifact> {
  const raw = await readFile(notePath, "utf8");
  const { frontmatter: fm } = parseFrontmatter(raw);
  const plan_id = String(fm.plan_id ?? "");
  if (!plan_id) {
    throw new Error(`rehydratePlanScalars: note ${notePath} missing plan_id in frontmatter`);
  }
  assertKnownDigestVersion(fm, notePath);
  const created = typeof fm.created === "string" ? fm.created : new Date().toISOString();
  const revVal = fm.plan_rev;
  const rev = typeof revVal === "number" ? revVal : (typeof revVal === "string" ? parseInt(revVal, 10) : 0) || 0;
  // Slices are carried leniently (no evidence validation) so the activate
  // guard can apply the all-resolved terminal check; restorePlan ignores them
  // (every digest-covered field comes from the history snapshot).
  const slices: PlanSlice[] = Array.isArray(fm.plan_slices)
    ? (fm.plan_slices as PlanSlice[])
    : safeParse(fm.plan_slices, []);
  return {
    plan_id,
    goal: "",
    status: (fm.status as PageStatus) || "draft",
    constraints: [],
    slices: slices.map((s) => ({ ...s })),
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

/** Adjacent append-only journal for a plan artifact note. */
export function journalPathFor(notePath: string, planId: string): string {
  return path.join(path.dirname(notePath), `${planId}.log.md`);
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
      // Shared with appendHistorySnapshot's rotation cap so the two agree on
      // which lines count — a round-2 cassandra finding caught this as two
      // independently-maintained copies of the same check (drift risk, not a
      // live bug at the time) and asked for one real shared predicate.
      if (!isValidHistoryLine(trimmed)) continue;
      try {
        results.push(JSON.parse(trimmed));
      } catch {
        // unreachable: isValidHistoryLine already proved this parses
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
  assertUniqueSliceIds(current.slices, "restorePlan current");
  assertUniqueSliceIds(snapshot.slices, "restorePlan snapshot");
  const generations = [...current.slices, ...snapshot.slices]
    .map((slice) => slice.generation)
    .filter(
      (generation): generation is number =>
        Number.isSafeInteger(generation) && (generation ?? -1) >= 0,
    );
  // A forward restore must never roll claim identity backward. current.rev is
  // monotonic across durable mutations, while the global generation maximum
  // carries the high-water mark even when the restored snapshot omitted a
  // currently claimed slice. Every restored slice advances beyond both.
  const generationFloor = Math.max(
    Number.isSafeInteger(current.rev) && current.rev >= 0
      ? current.rev + 1
      : 1,
    ...generations.map((generation) => generation + 1),
  );
  return {
    ...current,
    goal: snapshot.goal,
    constraints: [...snapshot.constraints],
    slices: snapshot.slices.map((slice) => ({
      ...slice,
      generation: generationFloor,
      claim: undefined,
    })),
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
    set_depends_on?: Array<{ slice_id: string; depends_on: string[] }>;
  },
): PlanArtifact {
  assertUniqueSliceIds(plan.slices, "applySliceDelta");
  assertUniqueExplicitSliceIds(delta.add_slices ?? [], "applySliceDelta");
  const deltaKey = stableStringify({
    add: (delta.add_slices ?? []).map((s) => s.title ?? s.id ?? "").sort(),
    drop: (delta.drop_slice_ids ?? []).sort(),
  });
  const supersededMarker = `replan-${createHash("sha256").update(deltaKey).digest("hex").slice(0, 10)}`;

  const dropSet = new Set(delta.drop_slice_ids ?? []);
  const addList = delta.add_slices ?? [];
  // drop+add is exclusive-split shape: replacement, not drop-without-replacement.
  const isReplacement = dropSet.size > 0 && addList.length > 0;

  let nextSlices: PlanSlice[] = plan.slices.map((slice) => {
    if (dropSet.has(slice.id) && slice.status !== "done" && slice.status !== "superseded") {
      return invalidateSliceGeneration({
        ...slice,
        status: "superseded",
        superseded_by: supersededMarker,
      });
    }
    return slice;
  });

  const usedIds = new Set(nextSlices.map((s) => s.id));
  const addedIds: string[] = [];

  for (const ns of addList) {
    // #291 (round-2 cassandra finding HIGH-2): same collision as replan()'s
    // sibling loop, reached here via drop_slice_ids + add_slices in a
    // single call — e.g. dropping "a" and re-adding a slice explicitly
    // id'd "a" in the same delta creates two slices sharing an id, and
    // enforcement resolves against whichever .find() hits first.
    if (ns.id && usedIds.has(ns.id)) {
      throw new Error(
        `applySliceDelta: cannot add slice with id "${ns.id}" — a slice with that id already exists in this plan (even if superseded/done, including one just dropped in this same call). Choose a different id, or omit id to let one be generated.`,
      );
    }
    const id = ns.id || slugifySliceId(ns.title, usedIds);
    usedIds.add(id);
    addedIds.push(id);
    nextSlices.push({
      id,
      title: ns.title,
      status: "pending",
      gate: ns.gate,
      depends_on: ns.depends_on ? [...ns.depends_on] : undefined,
      evidence: ns.evidence,
    });
  }

  if (isReplacement && addedIds.length > 0) {
    nextSlices = nextSlices.map((slice) => {
      if (
        dropSet.has(slice.id) &&
        slice.status === "superseded" &&
        slice.superseded_by === supersededMarker
      ) {
        return { ...slice, replaced_by: [...addedIds] };
      }
      return slice;
    });
  }

  const remounts = delta.set_depends_on ?? [];
  if (remounts.length > 0) {
    const seen = new Set<string>();
    for (const entry of remounts) {
      const id = entry.slice_id;
      if (seen.has(id)) {
        throw new Error(`applySliceDelta: duplicate set_depends_on slice_id "${id}"`);
      }
      seen.add(id);
      const target = nextSlices.find((s) => s.id === id);
      if (!target || target.status === "superseded") {
        throw new Error(
          `applySliceDelta: cannot remount depends_on on "${id}" — missing or not live`,
        );
      }
    }
    nextSlices = nextSlices.map((slice) => {
      const entry = remounts.find((r) => r.slice_id === slice.id);
      if (!entry) return slice;
      const nextDepends = [...entry.depends_on];
      if (sameStringSet(slice.depends_on, nextDepends)) return slice;
      return invalidateSliceGeneration({
        ...slice,
        depends_on: nextDepends,
      });
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

/**
 * Landed add_slices for a replan apply, derived from before→after — not
 * from the request list. applySliceDelta generates ids when callers omit
 * them; journaling the request would miss those ids and leave the ordered
 * journal no longer SoT for what applied.
 *
 * Proposal fields only (id, title, gate, depends_on). No claim token,
 * status, proposals, or evidence — same as other ordered mirrors.
 */
export function landedAddSlices(
  before: PlanArtifact,
  after: PlanArtifact,
): NonNullable<CreatePlanInput["slices"]> {
  const beforeIds = new Set(before.slices.map((s) => s.id));
  return after.slices
    .filter((s) => !beforeIds.has(s.id))
    .map((s) => {
      const landed: {
        id: string;
        title: string;
        gate?: string;
        depends_on?: string[];
      } = { id: s.id, title: s.title };
      if (s.gate !== undefined) landed.gate = s.gate;
      if (s.depends_on !== undefined) landed.depends_on = [...s.depends_on];
      return landed;
    });
}

/**
 * Map a worker StructuralProposal onto minni_thread_replan's existing
 * add_slices / drop_slice_ids surface. Not a second apply tool and not a
 * replan kind enum — orch still calls replan with add/drop.
 *
 *   expand   = add only; proposer stays
 *   split    = supersede claimed parent + add children; no parent-id reuse.
 *              Does not remount dependents' depends_on — orch remounts
 *              named live slices via set_depends_on on the existing replan
 *              surface (edge edit; unnamed live slices stay). Split is
 *              replacement; contract is drop-without-replacement.
 *   contract = drop named ids only (supersede, never delete)
 */
export function structuralProposalDelta(
  proposal: StructuralProposal,
  proposerSliceId: string,
): {
  add_slices?: NonNullable<CreatePlanInput["slices"]>;
  drop_slice_ids?: string[];
} {
  const parentId = proposerSliceId.trim();
  if (!parentId) {
    throw new Error("structuralProposalDelta: proposer slice id is required");
  }
  if (proposal.kind === "expand" || proposal.kind === "split") {
    const slices = proposal.slices;
    if (!slices || slices.length === 0) {
      throw new Error(
        `structuralProposalDelta: ${proposal.kind} requires slices`,
      );
    }
    if (proposal.kind === "expand") {
      return {
        add_slices: slices.map((slice) => ({ ...slice })),
      };
    }
    if (slices.some((slice) => slice.id === parentId)) {
      throw new Error(
        `structuralProposalDelta: split cannot reuse parent id "${parentId}"`,
      );
    }
    return {
      add_slices: slices.map((slice) => ({ ...slice })),
      drop_slice_ids: [parentId],
    };
  }
  if (proposal.kind === "contract") {
    return {
      drop_slice_ids: [...proposal.slice_ids],
    };
  }
  throw new Error("structuralProposalDelta: unknown proposal kind");
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
  // PLUMB-T4 / #231: atomic temp+rename so a crash mid-write cannot leave a
  // truncated active-thread pointer. writeFileAtomic already lives in vault.ts.
  await writeFileAtomic(pointerPath, data);
}

/**
 * #122 F-PLAN-ACTIVATE-NO-TERMINAL-GUARD: setActivePlan gated on the plan's
 * status — a terminal plan (resolveActivePlanView's suppression set) must not
 * be re-activated. A stale note with every slice done/superseded but a status
 * scalar stuck at 'draft'/'candidate' (the shape resolveActivePlanView
 * self-heals) counts as terminal too. Fields are read via the lenient
 * bare-scalar path so a digest-bricked but non-terminal plan can still be
 * activated (as before this guard).
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
      error: `plan ${plan_id} has terminal status '${scalars.status}' and cannot be re-activated; create a new plan (minni_thread_create) or restore a prior revision (minni_thread_restore) instead`,
    };
  }
  if (allSlicesResolved(scalars.slices)) {
    return {
      ok: false,
      error: `plan ${plan_id} has every slice resolved (done/superseded) and is effectively complete despite its '${scalars.status}' status; it cannot be re-activated — create a new plan (minni_thread_create) or restore a prior revision (minni_thread_restore) instead`,
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
        // PLUMB-T4 remainder / #231: this is a security-relevant refusal (a
        // tampered or corrupted pointer trying to point outside the vault),
        // not routine "no active plan" — audit it instead of discarding
        // silently. stderr only: hook stdout is the JSON protocol channel.
        console.error(
          `minni: getActivePlan rejected a notePath outside the vault root (${vaultPath}): ${parsed.notePath}`,
        );
        return undefined;
      }
      return parsed;
    }
  } catch (error) {
    const errno = activePlanErrnoCode(error);
    // Missing pointer is empty (no active plan). Other FS failures must not
    // look like empty — hooks treat undefined as "nothing salient."
    if (errno === "ENOENT") {
      return undefined;
    }
    if (errno) {
      throw new ActivePlanReadError(errno, error);
    }
    // Corrupt JSON / shape — no usable active plan.
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
 * build their UserPromptSubmit `active_thread_ref` through this function so the
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
    pull: "Full plan (goal, open_questions, slices) omitted to save context. Call minni_thread_status for detail on demand.",
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
        "no plan_id provided and no active plan is set; pass plan_id explicitly or activate one with minni_thread_activate",
    };
  }
  return { plan_id: active.plan_id };
}

// #295 (June audit N8): shelfDrift() was reachable only by explicitly calling
// minni_thread_status with live_shelf_content supplied by hand — drift was
// found only when someone thought to check. resolveActivePlanView is the one
// function both hook.ts (claude-code) and hook-handlers.ts (every other
// wired platform) already call at SessionStart to inject the active plan, so
// wiring the check in here — rather than adding a new call site per
// platform, which #296's hook.ts/hook-handlers.ts duplication makes
// expensive — covers every platform uniformly regardless of the #283
// migration. `liveShelfContent` is the SessionStart handler's own
// already-read layer1/core.md body (readLayer1Shelf), passed in rather than
// re-read here: this function does no I/O beyond the plan note itself, and
// the whole point of shelfDrift's "never pulls" contract is that it compares
// against content the caller already has, not content it goes and fetches.
export async function resolveActivePlanView(
  vaultPath: string,
  liveShelfContent?: string,
): Promise<{
  plan_id: string;
  rev: number;
  view: ReturnType<typeof compactPlanView>;
  shelf_drift?: ReturnType<typeof shelfDrift>;
} | undefined> {
  try {
    const active = await getActivePlan(vaultPath);
    if (!active) return undefined;
    return await withThreadLock(
      vaultPath,
      active.plan_id,
      `active-plan-view:${randomUUID()}`,
      async () => {
        // The active pointer can change while this reader queues. Never use a
        // stale pointer after acquiring the old plan's lock.
        const lockedActive = await getActivePlan(vaultPath);
        if (
          !lockedActive ||
          lockedActive.plan_id !== active.plan_id ||
          lockedActive.notePath !== active.notePath
        ) {
          return undefined;
        }
        const plan = await rehydratePlan(active.notePath);
        if (TERMINAL_PLAN_STATUSES.has(plan.status)) {
          return undefined;
        }
        // Honest-health self-heal (audit C4): plans completed under a stale
        // plugin deploy can be stuck with every slice terminal but status
        // still 'draft'/'candidate'. Lock before strict rehydrate so this
        // repair cannot overwrite a concurrent worker mutation.
        const allResolved = allSlicesResolved(plan.slices);
        if (
          allResolved &&
          (plan.status === "draft" || plan.status === "candidate")
        ) {
          const from = plan.status;
          // H6: terminal, non-recallable completion (not "accepted").
          plan.status = "complete";
          await persistPlan(plan, { vaultPath, notePath: active.notePath });
          const journalPath = journalPathFor(active.notePath, plan.plan_id);
          try {
            // Ordered cursor (not legacy appendJournal): status_reconciled
            // without seq was invisible to minni_thread_events. Same actor
            // stamp as other server-side Thread mutations.
            const now = new Date();
            const readySummary = {
              slices: plan.slices
                .filter(
                  (slice) =>
                    slice.status === "pending" ||
                    slice.status === "in_progress" ||
                    slice.status === "blocked",
                )
                .map((slice) => ({ id: slice.id, title: slice.title })),
            };
            const ordered = await readOrderedThreadEvents(journalPath);
            await reconcileThreadJournal(
              {
                journalPath,
                notePath: active.notePath,
                planId: plan.plan_id,
                rev: plan.rev,
                actor: DEFAULT_AGENT_ID,
                at: now.toISOString(),
                readySummary,
                orderedSnapshot: ordered,
              },
            );
            await ensureOrderedBaseline(
              {
                journalPath,
                planId: plan.plan_id,
                rev: plan.rev,
                actor: DEFAULT_AGENT_ID,
                at: now.toISOString(),
                readySummary,
                orderedSnapshot: ordered,
              },
            );
            await appendOrderedEventBatch({
              journalPath,
              planId: plan.plan_id,
              rev: plan.rev,
              actor: DEFAULT_AGENT_ID,
              at: now.toISOString(),
              orderedSnapshot: ordered,
              events: [
                {
                  idempotencyKey: deriveSystemEventKey(
                    "status_reconciled",
                    plan.plan_id,
                    from,
                    "complete",
                    String(plan.rev),
                  ),
                  kind: "status_reconciled",
                  payload: { from, to: "complete" },
                },
              ],
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
          // #295: omitted when the caller did not provide live shelf content.
          ...(liveShelfContent !== undefined && plan.shelf_ref
            ? { shelf_drift: shelfDrift(plan, liveShelfContent) }
            : {}),
        };
      },
    );
  } catch (error) {
    // FS failures and lock contention must not look like "no active plan."
    if (error instanceof ActivePlanReadError) {
      throw error;
    }
    if (error instanceof ThreadBusyError) {
      throw error;
    }
    const errno = activePlanErrnoCode(error);
    if (errno) {
      throw new ActivePlanReadError(errno, error);
    }
    return undefined;
  }
}

