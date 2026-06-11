**Convergence status:** NOT CONVERGED (round cap hit — open findings may remain) after 3 round(s). Method: code-grounded adversarial design converge (Ground 5 readers / opus design / 4-lens review / skeptic verify / revise loop).

---
title: /minni:handoff — Design
date: 2026-06-11
status: design (pre-implementation)
scope: V1 — Grok Build builder lane only
author: designer (orchestrated subagent)
evidence_legend: "[LOCAL-ONLY] = rests on local/runtime evidence not in official public docs; [DOCS] = confirmed by official xAI documentation; [SRC] = confirmed by reading Minni source/config."
---

# /minni:handoff — Design

## 1. What /minni:handoff is

`/minni:handoff` is a **thin command skill** — a single markdown file at
`plugins/minni/commands/handoff.md`, sitting next to the existing
`plugins/minni/commands/plan.md`. It contains **no code and no new MCP tools**. Its
entire job is to teach the agent, from the terminal, how to **dispatch a drafted
plan to a builder agent on another platform** (V1: the Grok Build CLI), and how to
**review the builder's result**. It changes the `/minni:plan` circle by **zero
percent** — it never calls into, wraps, patches, or re-implements any
`minni_plan_*` tool. The designer agent still drafts the plan and runs the normal
plan circle in its own vault if it wants a coordinator-side copy; the **builder
satisfies the plan by invoking `/minni:plan`'s own gated dynamic workflow inside
the builder's env-pinned vault**, driven by plan content that travels as plain
text in the dispatch prompt. The skill is documentation-of-folklore: codex (GPT-5)
and sonnet each already ran multi-hour sprints executing the `/minni:plan` circle
in their own vaults. This skill makes that lane official without inventing
mechanism.

---

## 2. The skill file: full proposed content of `commands/handoff.md`

> The block below is the **complete proposed file content**. Inline annotations in
> `> NOTE` lines are part of this design doc, not the file; the fenced markdown is
> the deliverable.

````markdown
---
name: handoff
description: Dispatch a drafted Minni plan to a builder agent on another platform (V1: Grok Build CLI). The builder runs the /minni:plan circle in its own env-pinned vault; you review the result. Read-only dispatch + review — never modifies the builder's vault, never writes cross-vault.
---

# /minni:handoff — dispatch a plan to a builder, then review

This command does NOT change /minni:plan. You draft a plan (normally via
/minni:plan in your own vault), then hand its CONTENT to a builder agent on
another terminal. The builder instantiates and drives that plan through the
gated /minni:plan circle in ITS OWN vault. You then review the builder's vault
artifacts and independently verify the work before trusting any "done".

V1 covers exactly one builder lane: the **Grok Build CLI** (`grok`). ACP and the
Antigravity/Codex builder lanes are noted but out of scope for V1.

---

## Step 0 — have a plan to hand off

Draft the plan first (goal, constraints, slices with ids + gate text + depends_on,
open_questions). The fastest path is to run /minni:plan yourself so the plan is
real and you hold its plan_id. You will serialize the plan CONTENT into the
dispatch prompt — the builder gets text, not a pointer. There is no cross-vault
reference; vaults are a hard isolation wall (G12/SEC-003).

Capture, from your active plan, the EXACT values you will paste into the template:
- full plan_id (verbatim, never truncated — see "Why the full plan_id" below)
- goal
- constraints[]
- slices[] : { id, title, gate, depends_on }
- open_questions[]
- any scar_tissue you want the builder to pre-seed (as a SEPARATE post-create step)

## Step 1 — choose the builder model

The Grok Build CLI exposes two coding models. Select with `-m <slug>`:

| Slug (`-m`)               | Display name   | Pick when…                                                        |
|---------------------------|----------------|-------------------------------------------------------------------|
| `grok-build`              | Grok Build     | DEFAULT. xAI-native agentic coder. Larger context, backend web   |
|                           |                | search available. Best general builder lane.                     |
| `grok-composer-2.5-fast`  | Composer 2.5   | Long-running, heavy multi-step / complex-instruction sprints;     |
|                           |                | Cursor's latest coding model (agent_type "cursor"). Smaller       |
|                           |                | context (~200k), no backend web search. Pick for long autonomous  |
|                           |                | loops where you don't need the model to browse.                   |

`-m grok-build` is the CLI default; omit `-m` to get it.
Verify the live lineup any time with: `grok models`.

## Step 2 — the builder ALREADY has /minni:plan loaded — but invoke it explicitly

The Grok Build CLI discovers the installed Minni plugin because this machine's
`~/.claude/settings.json` registers it as a Claude-compat marketplace —
`extraKnownMarketplaces: { minni: { source: { source: "directory", path:
"/Users/hansaxelsson/Projects/Minni" } } }` with `enabledPlugins:
{ "minni@minni": true }`. Grok reads `~/.claude/settings.json` for
`extraKnownMarketplaces` and surfaces the plugin's SKILLS and COMMANDS (commands/,
skills/, including `plan`) directly from that SOURCE directory. The MCP SERVER
layer is NOT surfaced from the source directory: `grok inspect --json` shows the
minni MCP server `source.type = "configToml"`, loaded from a SEPARATE
`~/.grok/config.toml` `[mcp_servers.minni]` entry that points at the DEPLOYED copy
(`~/.agents/plugins/minni@minni/dist/server.js`) and carries the vault-pinning env
vars (`MINNI_VAULT_PATH` = grok-build-vault, etc.). The source plugin's own
`.mcp.json` has only relative paths and NO env vars — it does not pin the vault.
So vault pinning is NOT automatic from the source plugin registration; it lives
exclusively in the separately maintained `config.toml` override. `grok inspect
--json` confirms the plugin loads from
`/Users/hansaxelsson/Projects/Minni/plugins/minni` (scope user, enabled); its
`provides` block reports `{ skills: 7, agents: 0, hooks: true, mcpServers: 1 }`
(there is NO `commands` count field — commands from `commands/*.md` instead surface
in the GLOBAL skills list, where `plan` appears among the minni-sourced items), and
the minni MCP server (stdio, via the config.toml override) is present.

> Important: this is NOT the `[compat.claude].skills` mechanism, which only scans
> `~/.claude/skills/` for bare `SKILL.md` files and would NOT surface a plugin's
> `commands/`. Because skill/command discovery hinges on this machine's
> `~/.claude/settings.json` entry pointing at the Minni repo, a fresh builder
> environment or a different machine WITHOUT that `extraKnownMarketplaces` entry
> will NOT auto-surface the plugin — and SEPARATELY, a machine without the
> `~/.grok/config.toml` `[mcp_servers.minni]` entry will NOT have the vault-pinned
> MCP server. Verify BOTH with `grok inspect --json` before relying on
> `/minni:plan` being present, and register the marketplace and/or the config.toml
> MCP entry if either is missing.

There is a NAME COLLISION: Grok has a BUILT-IN `/plan` (its own plan mode), and
"built-in slash commands always take priority over skills with the same name."
So to reach the Minni plan skill you MUST use the plugin-qualified form
**`/minni:plan`** (the plugin name is the qualifier). Equivalently, you can tell
the builder to call the MCP tool `minni_plan_create` directly — which is exactly
what the /minni:plan skill body instructs anyway.

Because the skill is already surfaced, the dispatch prompt does NOT need to inline
the full /minni:plan protocol text. It only needs to (a) reference `/minni:plan`
by qualified name, (b) carry the plan CONTENT, and (c) state the gate discipline
the builder must honor. The template below does this.

## Step 3 — headless dispatch (JSON envelope)

Single-shot, unattended dispatch:

```bash
grok -p "$DISPATCH_PROMPT" \
  -m grok-build \
  --cwd /absolute/path/to/builder/project \
  --yolo \
  --output-format json \
  --effort high \
  --max-turns 200 \
  > /tmp/minni-handoff-<plan_id>.json
```

Flag notes:
- `-p "<prompt>"`     trigger headless (process, run tools, exit).
- `-m grok-build`     model (Step 1).
- `--cwd <path>`      working dir; Grok walks up to the enclosing `.git` for the project root.
- `--yolo`            auto-approve all tool executions — REQUIRED for unattended runs.
- `--output-format json`  emit one JSON object on completion:
                          { text, stopReason, sessionId, requestId }.
                          (Or `streaming-json` for an NDJSON live stream.)
- `--effort high`     reasoning effort; raise to xhigh/max for hard sprints. HEADLESS-ONLY.
- `--max-turns N`     hard cap on agentic turns — a safety stop for headless runs.

ALWAYS check `type` before reading `text`: an error surfaces as
`{"type":"error","message":"..."}`. Exit codes: 0 ok, 1 error, 130 SIGINT, 143 SIGTERM.

Capture the session id for continuation:

```bash
SID=$(jq -r '.sessionId' /tmp/minni-handoff-<plan_id>.json)
```

Auth is `XAI_API_KEY` (already set in this environment).
Sessions are stored per-CWD under `~/.grok/sessions/<encoded-cwd>/<session-id>/`.

## Step 4 — continue / resume the builder

To push the builder to the next slice (or to re-prompt after a partial run),
resume the SAME session so it keeps the plan state it already created in its vault:

```bash
grok -p "Continue the active /minni:plan. Take the next_action slice to done with substantive evidence, or set it blocked with a reason." \
  -r "$SID" \
  -m grok-build --yolo --output-format json
```

- `-r / --resume <ID>`  resume an exact session (errors if not found).
- `-c / --continue`     resume the most recent session in the current CWD (no id needed).

Prefer `-r` over a fresh `grok -p` for slice 2 so the builder keeps the session
state (and the active plan) it already created. If you must start fresh, re-carry
the full plan content; id-less re-discovery is vault-safe (the builder's tools
resolve only its own vault's active plan), but a fresh session has lost its working
context (see "Why the full plan_id").

## Step 5 (optional) — isolate in a git worktree

For risky write-heavy sprints, run the builder in its own worktree so the diff is
contained and easy to review/discard:

```bash
grok -p "$DISPATCH_PROMPT" -m grok-build --yolo \
  --worktree minni-handoff-<plan_id> \
  --cwd /absolute/path/to/builder/project \
  --output-format json
```

`--worktree [NAME]` starts the headless session in a new isolated git worktree.

## ACP (noted for later, NOT V1)

A persistent agent lane exists via `grok agent stdio` (JSON-RPC: initialize →
session/new → session/prompt → session/update), with worktree extension methods
(`x.ai/git/worktree/create|apply|list`). This is the future "long-lived builder"
lane. V1 uses only headless `grok -p`. Do not use ACP for V1 handoffs.

---

## THE DISPATCH-PROMPT TEMPLATE

Fill the ALL-CAPS placeholders from your active plan (Step 0). Paste the whole
thing as `$DISPATCH_PROMPT`. It references `/minni:plan` explicitly (collision
avoidance) and carries the plan content; it does NOT inline the full plan
protocol because the builder already has the skill loaded.

```text
You are the BUILDER for a Minni plan handoff. Use the Minni plugin's plan tools
in YOUR OWN vault. Read-only on everything outside your working project; the only
state you mutate is (a) your own vault's plan artifacts via the minni_plan_* tools
and (b) the project files inside --cwd.

STEP 0 — Pre-flight: ensure YOUR vault is idle before creating anything.
Before you create the new plan, confirm your vault has no in-flight plan that this
handoff would clobber. minni_plan_create sets the new plan active UNCONDITIONALLY
(it overwrites _active_plan.json with no pre-check), so a pre-existing live plan
would be silently orphaned mid-run. Do this:
  1. Call minni_plan_status with NO plan_id. If it reports there is no active plan,
     you are idle — proceed to STEP 1.
  2. If it returns an active plan, read that plan's frontmatter status. If the
     status is terminal (accepted/rejected), the vault is idle for dispatch
     purposes — proceed to STEP 1 (the stale pointer is harmless; the new create
     will repoint it).
  3. If the status is NON-terminal (the plan is genuinely live), STOP and report:
     "vault busy — active plan <id> status <status>". Do NOT call
     minni_plan_deactivate or minni_plan_create to repoint a live plan; that would
     orphan an in-flight run. Surface this to the coordinator and wait.
Do NOT call minni_plan_activate with any plan_id in this whole run — it has no
terminal-status guard and re-activating a completed plan would hijack the vault
pointer onto a finished plan, routing your id-less tool calls to the wrong plan.

STEP 1 — Instantiate the plan in YOUR vault.
Invoke the plugin-qualified skill /minni:plan (NOT Grok's built-in /plan), or call
the MCP tool minni_plan_create directly with the plan below. Creating the plan
ALSO sets it active in your vault (the create call writes _active_plan.json) — do
not hand-edit any plan file.

  goal: <GOAL>
  constraints:
    - <CONSTRAINT_1>
    - <CONSTRAINT_2>
  slices:
    - id: <SLICE_ID_1>  title: <TITLE_1>  gate: <GATE_TEXT_1>  depends_on: []
    - id: <SLICE_ID_2>  title: <TITLE_2>  gate: <GATE_TEXT_2>  depends_on: [<SLICE_ID_1>]
  open_questions:
    - <OPEN_Q_1>

  Coordinator provenance (reference only — your vault will mint a DIFFERENT
  plan_id): coordinator_plan_id = <FULL_COORDINATOR_PLAN_ID>

STEP 1b — Pre-seed scars (ONLY if any are listed below; SEPARATE calls AFTER create).
minni_plan_create has NO scar parameter — it accepts only goal, constraints,
slices, open_questions (and seed_scar_from_audit, which reads the audit log, not
caller text). To pre-seed scars you MUST make a SEPARATE minni_plan_scar call for
EACH scar, AFTER minni_plan_create has succeeded (minni_plan_scar requires an
existing plan). If the list below says "none", skip this step.
  scars to pre-seed (one minni_plan_scar call each): <OPTIONAL_SCARS_OR_"none">

STEP 2 — Execute the circle, slice by slice, respecting depends_on order.
For each slice: do the work, then write evidence to
  ./evidence/<slice_id>/result.md   (what you did + how it was verified)
  ./evidence/<slice_id>/diff.patch  (git diff of the change, if any)
Then call minni_plan_update(slice_id=<id>, status="done", evidence="<substantive,
non-trivial summary referencing the evidence files and the gate>").
The evidence gate REJECTS trivial strings (e.g. "done","ok","lgtm","wip", and any
evidence under 8 characters) and REJECTS marking done without evidence — so write
real proof. If you cannot satisfy a slice's gate, call
minni_plan_update(status="blocked", evidence="<actionable reason>"). Do NOT round a
partial win up to done. Always address slices with their explicit slice_id; never
call minni_plan_activate to switch the active pointer.

STEP 3 — When the plan reaches terminal state (all slices done/superseded), STOP.
The plan auto-transitions to accepted (its note frontmatter status = accepted) and
injection stops; do not start new work.

STEP 4 — Report. Emit a final summary: your vault plan_id, per-slice status, and
the evidence/ paths. Do not git commit, do not switch branches, do not push.
```

> NOTE (design): STEP 0 is load-bearing. `minni_plan_create` calls `setActivePlan()`
> UNCONDITIONALLY — there is no pre-check for an existing `_active_plan.json`, so a
> create in a vault that already holds a live plan silently overwrites the pointer
> and orphans the in-flight plan. Because the builder vault is a SEPARATE,
> env-pinned vault the coordinator cannot read, the coordinator's own pre-dispatch
> pointer check (section 4) CANNOT cover the builder vault — only a builder-side
> pre-flight can. STEP 0 makes the idle-check structural rather than dependent on
> coordinator discipline. The explicit "do NOT call `minni_plan_activate`"
> instruction closes a second gap: `minni_plan_activate` has NO terminal-status
> guard, so re-activating an `accepted` plan would repoint the vault onto a finished
> plan and misroute every id-less tool call.

> NOTE (design): The template carries `coordinator_plan_id` for provenance only.
> The builder mints its OWN plan_id at create time; there is no shared id and no
> pointer between vaults. We still pass the coordinator's FULL id (never
> truncated) as belt-and-suspenders hygiene (see "Why the full plan_id" below) —
> id-less active-plan resolution already ships and is vault-safe.

> NOTE (design): STEP 1b is a SEPARATE step on purpose. `minni_plan_create` has no
> `scar_tissue`/scar-text parameter (its schema is goal, constraints, slices,
> open_questions, seed_scar_from_audit); a scar can only be added by a distinct
> `minni_plan_scar` call AFTER the plan exists. Embedding scar text in the create
> block would be silently dropped. The post-create ordering is load-bearing.

### Why the full plan_id (belt-and-suspenders hygiene) — [SRC]
Id-less plan addressing shipped across three commits on 2026-06-10:
`minni_plan_status` / `minni_plan_update` / `minni_plan_history` got optional
`plan_id` in `64bcd41` (feat(plan): C5 parity); `minni_plan_scar` joined in
`38ab638` (fix(security+plan+inbox): round-2 review-panel fixes); `minni_plan_replan`
joined in `03fc8d2` (fix(security+plan): review-panel hardening). All five now take
`plan_id` as `z.string().min(1).optional()`, and when it is omitted they resolve the
calling vault's active plan via `resolvePlanIdOrActive(DEFAULT_VAULT_PATH, …)` →
`getActivePlan(vaultPath)`, which reads ONLY that vault's `_active_plan.json`. There
is no cross-vault filesystem grep: the resolver is vault-pinned and structurally
cannot read a foreign vault's pointer, so id-less re-discovery is SAFE. Passing the
full, verbatim coordinator plan_id (provenance only) and continuing with
`-r <sessionId>` is therefore belt-and-suspenders hygiene — it keeps the builder on
the exact plan it created and gives a clean audit trail — not a workaround for an
open vulnerability.
````

> NOTE (design): the four-backtick fence above wraps the file body so the inner
> three-backtick code blocks render intact.

---

## 3. How the designer reviews

The builder drives `/minni:plan` in the **grok-build vault**. The designer reviews
by reading that vault's artifacts and then **independently verifying** — never by
trusting the builder's self-report.

### 3a. Builder vault artifact paths — [SRC]
Grok Build's MCP server is env-pinned (`~/.grok/config.toml`,
`MINNI_VAULT_PATH = /Users/hansaxelsson/.minni/grok-build-vault`). So
`DEFAULT_VAULT_PATH` at runtime = `~/.minni/grok-build-vault`, and all plan
artifacts land under:

```
~/.minni/grok-build-vault/wiki/artifacts/<builder_plan_id>.md            # plan note + frontmatter
~/.minni/grok-build-vault/wiki/artifacts/<builder_plan_id>.log.md        # append-only journal (gate_passed, scar_added, replan, rehydrated)
~/.minni/grok-build-vault/wiki/artifacts/<builder_plan_id>.history.jsonl # one snapshot per rev
~/.minni/grok-build-vault/wiki/artifacts/_active_plan.json              # { plan_id, notePath, set_at }
```

To find the builder's plan id without guessing: read
`~/.minni/grok-build-vault/wiki/artifacts/_active_plan.json` (while a plan is
active) or list `<vault>/wiki/artifacts/plan-*.md`. The designer reads these as
plain files — this is **read-only inspection of the builder's vault**, not a
cross-vault tool call (no tool can target a foreign vault anyway).

### 3b. `minni_plan_status` semantics — [SRC]
`minni_plan_status` returns an envelope `{ view, drift, status, rev, active }`; the
`compactPlanView` fields live UNDER `view`, not flat at top level. So read
`result.view.headline`, `result.view.next_action`, etc. — `result.headline` is
`undefined`. `view` carries `goal`, `headline`, `progress`, `next_action`,
`pending`, `open_questions`, `scar_tissue` (a count), `scars` (last-3 array),
`shelf`, and `rev`. The **headline carries plan-not-complete
salience** (post-fix `92a2bea`): a plan is not done until every slice is
done/superseded. `next_action` points at the blocking slice (respecting
`depends_on`) until that slice is `done`. `plan_id` is OPTIONAL on this tool
(`z.string().min(1).optional()`, made optional in `64bcd41`); when omitted it
resolves the calling vault's active plan via `resolvePlanIdOrActive(DEFAULT_VAULT_PATH,
…)`. The designer can run `minni_plan_status` **only inside a process pinned to the
grok-build vault** (e.g. inside a builder session), because every plan tool resolves
against `DEFAULT_VAULT_PATH` (the id-less resolver reads only that vault's pointer —
it cannot reach a foreign vault). From the coordinator side, the equivalent read is
parsing the vault artifact files directly.

### 3c. What "done" evidence looks like — [SRC]
- The `minni_plan_update` gate enforces substance: marking a slice `done` requires
  non-empty, **non-trivial** evidence (`isTrivialEvidence` rejects the generic set
  — {"x","ok","done","good","looks good","lgtm","yes","fine","wip","na","n/a"} —
  and strings < 8 chars). A passing slice therefore has a real evidence string in
  the plan note, plus the `evidence/<slice_id>/result.md` + `diff.patch` files the
  template mandates.
- A slice with a non-empty `gate` that reaches `done` appends a `gate_passed`
  journal event in `<plan_id>.log.md` — audit trail only; the gate text is a label,
  not an automated check.
- Terminal "done" for the whole plan is reached when all slices reach done/superseded.
  At that point, the plan auto-transitions to `accepted` frontmatter status. Verify
  terminal state by reading the note frontmatter status (`accepted` = done), not by
  the pointer's presence, because the pointer can be stale (see below).
- The pointer-staleness mechanics: `_active_plan.json` is reliably cleared only on the
  `minni_plan_update` code path that drives the accepted transition (`clearActivePlan`
  is called there). ONE rare path can independently leave a stale pointer at a
  now-`accepted` plan: the self-heal path in `resolveActivePlanView`, which mutates
  `plan.status` to `accepted` and calls `persistPlan` but NOT `clearActivePlan`.
  `minni_plan_restore` and `minni_plan_replan` do NOT set status to `accepted`
  themselves (restore spreads the live current plan, whose status is not overridden
  by the snapshot; replan/applySliceDelta never touch status) — they can only
  PERPETUATE a pointer that was already stale because the self-heal path fired
  first. The stale pointer does NOT leak into injection: `resolveActivePlanView`
  returns `undefined` for any plan it finds in `accepted` status, so a completed
  handoff cannot leak into the next session's injected context regardless of the
  pointer. The only consequence is a potentially stale pointer file. Verify terminal
  state by reading the plan note frontmatter, not the pointer.

### 3d. Post-build independent verification (from prior art — NON-NEGOTIABLE)
Builder self-report is never sufficient; evidence files without re-execution are
"theater" (an adversarial panel killed tamper-resistance claims 0-3). So, before
the designer trusts any slice:
1. Read `evidence/<slice_id>/result.md` and `diff.patch`.
2. **Independently re-run** the slice's verification (tests, build, the literal
   gate criterion) against the **PLAN's** acceptance criteria — not just the slice
   in isolation. The principal failure is a locally-correct change that violates a
   plan-level interface/handoff expectation; per-slice checks miss this.
3. Risk-stratify: read-only/reversible slices fast-path; high-risk writes (merges,
   schema changes) get a mandatory checkpoint before they're accepted.
4. If verification fails, treat the slice as blocked/scarred and replan — do not
   accept the builder's `done`.

A cheap verifier tier (e.g. a haiku verifier with a schema-forced verdict) is the
recommended economics; reserve heavier models for split/high-risk slices. (Prior
art; verifier-budget question stays open.)

---

## 4. Hygiene — no stale-plan leakage

The anti-leak mechanism is the existing per-vault `_active_plan.json` pointer; the
skill adds discipline around it and invents nothing. — [SRC]

- **One pointer per vault.** `_active_plan.json` lives at
  `<vault>/wiki/artifacts/_active_plan.json` and is local to whichever vault the
  server process is env-pinned to. The grok-build vault has exactly one; it cannot
  read or write the coordinator's.
- **Activation is automatic at create — and UNCONDITIONAL.** `minni_plan_create`
  calls `setActivePlan()` in the same operation (the pointer is written at creation,
  not as a separate step), with NO pre-check for a pre-existing `_active_plan.json` —
  it overwrites whatever pointer was there. The dispatch prompt therefore tells the
  builder simply to **create the plan via `/minni:plan` / `minni_plan_create`** —
  that single call both writes the artifact and sets it active. The prompt must
  explicitly forbid hand-editing plan files (the `plan_digest` check throws "note
  may be tampered" on hand edits).
- **The builder's PREVIOUS active plan — guarded builder-side AND coordinator-side.**
  Because `minni_plan_create` overwrites the pointer unconditionally, a pre-existing
  live plan in the builder vault would be silently orphaned. The coordinator CANNOT
  guard this: the builder vault is a separate env-pinned vault the coordinator cannot
  read, so the coordinator's pointer check only ever covers its OWN vault. The
  structural guard therefore lives in the dispatch template's STEP 0 (builder-side
  pre-flight): the builder must call `minni_plan_status` (no id), and if an active
  plan exists with a NON-terminal frontmatter status, STOP and report "vault busy"
  rather than creating over it. Only an absent pointer or a pointer at a terminal
  (`accepted`/rejected) plan is safe to create over. As a coordinator-side
  belt-and-suspenders, the designer should also read its OWN `_active_plan.json`
  before dispatching and refuse to dispatch if its own vault is mid-run — but must
  NOT treat a present pointer as proof of a live plan: because the self-heal path can
  leave a stale pointer at a now-`accepted` plan, the designer must follow the
  pointer to the plan note and check its frontmatter `status`. Only a non-terminal
  status (not `accepted`/rejected) counts as "live".
- **No re-activating completed plans.** `minni_plan_activate` has NO terminal-status
  guard: it calls `setActivePlan()` for any `plan_id` that exists in the vault,
  including an `accepted` one. Re-activating a completed plan repoints
  `_active_plan.json` at it, after which `resolvePlanIdOrActive` routes every id-less
  call (`minni_plan_update`, `minni_plan_scar`, `minni_plan_replan`) to that finished
  plan rather than any new one (this tool-dispatch path does NOT consult
  `resolveActivePlanView`, so the `accepted`-suppression of injection does not protect
  it). The dispatch template therefore forbids the builder from calling
  `minni_plan_activate` with any `plan_id` for the duration of the run; the builder
  addresses slices by explicit `slice_id` only.
- **Terminal plans stop injecting — but the pointer is not always cleared.** When
  all slices reach done/superseded, the plan auto-transitions to `accepted`.
  `_active_plan.json` is reliably cleared only on the `minni_plan_update` code path
  that drives the accepted transition (`clearActivePlan` is called there). ONE rare
  path can independently produce a stale pointer at a now-`accepted` plan: the
  self-heal path in `resolveActivePlanView`, which mutates `plan.status` to
  `accepted` and calls `persistPlan` but NOT `clearActivePlan`. `minni_plan_restore`
  and `minni_plan_replan` do NOT themselves set status to `accepted` (restore spreads
  the live current plan, whose status is not overridden by the snapshot; replan
  never touches status), so they cannot independently create this condition — they can
  only perpetuate a pointer that was already stale because the self-heal path fired
  first. This does NOT leak into injection: `resolveActivePlanView` returns
  `undefined` for any plan it finds in `accepted` status, so a completed handoff
  cannot leak into the next session's injected context regardless of the pointer. The
  only consequence is a potentially stale pointer file — see the previous bullets for
  how the designer must read it.
- **No cross-vault transfer.** The ONLY thing that moves between vaults is the
  human-readable plan content in the dispatch prompt. There is no pointer sync, no
  merge, no copy of `_active_plan.json` across vaults. The builder mints a fresh
  plan_id; the coordinator id travels as provenance text only.

---

## 5. Failure modes & mitigations

| # | Failure | Why | Mitigation |
|---|---------|-----|------------|
| F1 | **Builder ignores the plan tools** (the open trial) | A headless model may "just code" and never call `minni_plan_create`/`_update`, leaving no gated evidence trail. | Dispatch prompt makes plan-tool use STEP 1 and ties "done" to `minni_plan_update`. Designer verifies by checking the grok-build vault actually grew `<plan_id>.md` + `_active_plan.json`; if absent, the run is rejected regardless of code output. Re-dispatch with stronger explicit instruction or fall back to a Claude-side builder. This is the explicitly-acknowledged open trial of V1. |
| F2 | **Headless error** | tool failure, auth, model error. | Output is `{"type":"error","message":...}`; ALWAYS check `type` before `text`. Exit codes: 1 error, 130 SIGINT, 143 SIGTERM. `--max-turns N` caps runaway loops. On error, read the message, fix env (`XAI_API_KEY`), re-dispatch fresh (the failed session created no usable plan). |
| F3 | **Session loss / can't continue** | wrong CWD (sessions are per-CWD), expired/missing session id. | Capture `sessionId` from the JSON immediately; continue with `-r <id>` (or `-c` for most-recent-in-CWD). If `-r` errors (session missing), do NOT blind-restart — re-dispatch the FULL plan (with full plan_id) so the builder rebuilds state cleanly. Sessions live at `~/.grok/sessions/<encoded-cwd>/<id>/`; keep `--cwd` stable across steps. |
| F4 | **Fresh session loses working context** — [SRC] | Id-less active-plan resolution already ships (`64bcd41` for status/update/history; `38ab638` for scar; `03fc8d2` for replan): the five plan tools take `plan_id` optional and resolve via `resolvePlanIdOrActive` → `getActivePlan(vaultPath)`, which reads ONLY the calling vault's `_active_plan.json` — no cross-vault grep is possible. The residual risk is operational, not a leak: a brand-new session has lost the prior session's working context. | Continue with `-r <sessionId>` so the builder keeps its session and active plan; pass the FULL plan_id as provenance/hygiene. If a fresh session is unavoidable, re-carry the full plan content. No cross-vault leak can occur through the MCP layer. |
| F5 | **Model-slug drift (beta platform)** — [LOCAL-ONLY for Composer slug] | Grok Build is early-access/beta; `grok-composer-2.5-fast` is confirmed only via the live `models_cache.json` from `cli-chat-proxy.grok.com` (which records its description as "Cursor's latest coding model", agent_type "cursor"), NOT in indexed public docs (the Composer 2.5 announcement page is 403-gated). Slugs can change. | Skill instructs `grok models` to re-list before relying on a slug. `grok-build` (the default; live `models_cache.json` reports its display name as "Grok Build") is stable. If a `-m` slug errors, re-probe and update the skill table. Treat the Composer slug as runtime-verified-but-not-doc-pinned. |
| F6 | **Long-run compaction context loss** — [SRC] | A builder compaction CAN occur on long sprints. The builder DOES have a compaction hook: `~/.grok/hooks/minni.json` (created 2026-06-07) registers a PreCompact hook running `dist/hook.js PreCompact` pinned to the grok-build vault; `handlePreCompact` stashes stale-belief/contradiction reassert events in the inbox. Plan-context re-injection is a SEPARATE, always-on path: the post-compaction `SessionStart` calls `resolveActivePlanView(vaultPath)`, which reads `_active_plan.json` directly from the vault — it does NOT consume the PreCompact inbox entry. So corrections come from the PreCompact stash, and plan context comes from the vault file; both recover, via independent mechanisms. | Rely on the registered SessionStart plan re-injection (vault-file-driven) plus the PreCompact + SessionStart correction re-injection as the primary recovery; belt-and-suspenders, continue with `-r <session>` so the builder keeps its session and active plan, and keep the (re)prompt's carried plan content size-bounded. |
| F7 | **Builder rounds a partial up to done** | optimism / gate evasion. | The `isTrivialEvidence` gate hard-throws on trivial evidence; the prompt explicitly directs `blocked`-with-reason for partials. Designer's independent verification (3d) is the real backstop — re-run before accepting. |

---

## 6. Propagation — exact files and paths

### 6a. File added (one file)
```
/Users/hansaxelsson/Projects/Minni/plugins/minni/commands/handoff.md
```
This is the single source-of-truth file. It is plain markdown — command `.md`
files are **copied as-is, not compiled** (they do not go through the TypeScript
build / `dist/`). No `plan.ts` / `server.ts` change; no new MCP tool.

### 6b. How it reaches each platform — [SRC: propagate.py]
Propagation uses the existing `minni-install` script:
```
/Users/hansaxelsson/Projects/Minni/plugins/minni/skills/minni-install/scripts/propagate.py
```
The `update-plugin` subcommand "Build/copy the canonical plugin and stamp
platform-specific agent/vault/socket config." It `copytree`s the whole plugin
package (commands/ included, ignoring `node_modules`/`.git`) into each platform's
install/cache location, preserving each surface's per-agent env.

Run from `/Users/hansaxelsson/Projects/Minni`:
```bash
# 1. write the file (done in 6a)
# 2. rebuild dist (server.js is currently ~2h stale vs repo — propagate it too)
npm --prefix plugins/minni run build
# 3. propagate everything to every surface
python3 plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform all
```
`--platform` accepts: `grok`, `claude-code`, `codex`, `antigravity` (alias
`gemini`), `kilocode`, or `all`.

### 6c. Where each platform reads `handoff.md`
| Platform | command read from | propagation needed for the command? |
|----------|-------------------|-------------------------------------|
| Grok Build | the SOURCE repo: `/Users/hansaxelsson/Projects/Minni/plugins/minni/commands/handoff.md` (via `extraKnownMarketplaces` in `~/.claude/settings.json`) | NO — visible to grok the moment the file exists in the source repo |
| Claude Code | `/Users/hansaxelsson/.claude/plugins/cache/minni/minni/0.1.0/commands/handoff.md` | yes |
| Codex | `/Users/hansaxelsson/.codex/plugins/cache/minni/minni/0.1.0/commands/handoff.md` | yes |
| Antigravity (gemini surface) | `/Users/hansaxelsson/.gemini/extensions/minni/commands/handoff.md` | yes |

Note — [SRC]: For Grok Build, commands and skills are read from the SOURCE repo
(`grok inspect --json` shows the plugin loading from
`/Users/hansaxelsson/Projects/Minni/plugins/minni`, registered via the Claude-compat
marketplace entry in `~/.claude/settings.json`). So `/minni:handoff` is available to
grok as soon as `handoff.md` exists in `plugins/minni/commands/` — BEFORE any
propagation. Propagation to `~/.agents/plugins/Minni@Minni/commands/` does NOT make
the command available in grok, because grok does not read commands/skills from
there. What grok DOES load from the deployed copy is the MCP server: per
`~/.grok/config.toml` `[mcp_servers.minni]`, `dist/server.js` is loaded from
`~/.agents/plugins/minni@minni/dist/server.js` (a hardcopy, realpath
`…/Minni@Minni`, NOT a symlink to the repo). So `update-plugin` is still required to
refresh the TOOLS layer — e.g. the stale `dist/server.js` (deployed mtime 2026-06-11
13:22 vs repo 15:38) — across all surfaces, and to land the command on the
cache-based platforms (Claude Code, Codex, Antigravity). After propagation, the
qualified command `/minni:handoff` is available on every surface that surfaces Minni
commands (the Grok lane is the only one the V1 body documents how to USE).

---

## 7. Explicitly out of scope (V1)

- **Cross-vault writes of any kind.** No tool can target a foreign vault
  (G12/SEC-003: every plan handler hardcodes `DEFAULT_VAULT_PATH`; no `vaultPath`
  parameter exists). The handoff transfers plan CONTENT as prompt text only. We do
  NOT design `_active_plan.json` copy, plan-file byte-copy between vaults, pointer
  sync, or merge.
- **Any change to `plan.ts` / `server.ts` / the `/minni:plan` circle.** Zero
  percent. The optional-`plan_id` fix and id-less active-plan resolution
  (`resolvePlanIdOrActive`) already shipped (`64bcd41` for status/update/history,
  `38ab638` for scar, `03fc8d2` for replan) and are vault-safe; this skill neither
  adds to nor depends on changing them. It carries the full plan_id and uses `-r`
  purely as continuation hygiene.
- **New MCP tools.** No `minni_plan_adopt`, no `minni_plan_export/import`, no
  `minni_dispatch`. `minni_negotiate_handoff` (task-packet oriented) is explicitly
  NOT repurposed for full plan-state transfer.
- **ACP / persistent agent lane** (`grok agent stdio|serve`). Noted in the skill
  for later; V1 uses only headless `grok -p`.
- **Antigravity and Codex builder lanes.** Roles are symmetric and these platforms
  can be builders, but V1 documents the Grok Build lane only. (Gemini `--resume`
  also has a known full-history-restore overflow bug — another reason to defer.)
- **Coordinator-side automated verification orchestration.** The verification
  step (3d) is documented as operator discipline; a packaged verifier-agent harness
  is future work.

---

## Evidence provenance summary

- **[DOCS]** `grok-build` is the default CLI slug; headless
  flags (`-p`, `-m`, `--output-format`, `--yolo`, `-r`, `-c`, `--cwd`,
  `--max-turns`, `--effort` (headless-only — ignored with a warning in the interactive TUI), `--worktree`); session storage; built-in `/plan`
  priority over same-named skills; that grok reads `extraKnownMarketplaces` from
  `~/.claude/settings.json`; ACP method shape.
- **[LOCAL-ONLY]** `grok-composer-2.5-fast` slug → "Composer 2.5" (description
  "Cursor's latest coding model", agent_type "cursor"), and `grok-build` display
  name "Grok Build" (runtime `models_cache.json` from `cli-chat-proxy.grok.com`,
  not in indexed public docs; Composer announcement page 403-gated). `grok inspect
  --json` showing the plugin loading from the source repo via
  `extraKnownMarketplaces`, with skills (provides.skills = 7) and commands surfacing
  via the global skills list; the minni MCP server appearing with `source.type =
  "configToml"` (NOT surfaced from the source plugin's `.mcp.json`). This machine's
  `~/.claude/settings.json` marketplace registration and `~/.grok/config.toml`
  `[mcp_servers.minni]` vault-pinning entry.
- **[SRC]** plan circle mechanics, vault pinning (every plan handler resolves
  against `DEFAULT_VAULT_PATH`); id-less addressing shipped in `64bcd41`
  (status/update/history), `38ab638` (scar), and `03fc8d2` (replan) — `plan_id`
  optional on the five plan tools, `resolvePlanIdOrActive` →
  `getActivePlan(vaultPath)` reads only the calling vault's pointer (no cross-vault
  grep); `minni_plan_create` calling `setActivePlan()` UNCONDITIONALLY with no
  pre-check for an existing `_active_plan.json`; `minni_plan_activate` calling
  `setActivePlan()` for any existing `plan_id` with NO terminal-status guard, and the
  tool-dispatch path (`resolvePlanTarget` / `resolvePlanIdOrActive`) NOT consulting
  `resolveActivePlanView`; `minni_plan_status` envelope shape `{ view, drift, status,
  rev, active }`; `isTrivialEvidence` rejected set {"x","ok","done","good","looks good","lgtm","yes","fine","wip","na","n/a"}
  plus the < 8-char guard; `minni_plan_create` schema (goal, constraints, slices,
  open_questions, seed_scar_from_audit) with NO scar-text parameter, and
  `minni_plan_scar` requiring a pre-existing plan; artifact layout;
  `_active_plan.json` lifecycle, including that `clearActivePlan` runs only on the
  `minni_plan_update` accepted-transition path, that the self-heal path in
  `resolveActivePlanView` is the SOLE mechanism that independently mutates
  `plan.status` to `accepted` and `persistPlan`s without `clearActivePlan` (leaving a
  stale pointer), and that `minni_plan_restore` (status taken from the live current
  plan, not the snapshot) and `minni_plan_replan`/`applySliceDelta` (never touch
  status) can only perpetuate an already-`accepted` stale pointer, never originate
  one — while `resolveActivePlanView` still returns `undefined` for `accepted`); the
  post-compaction `SessionStart` calling `resolveActivePlanView(vaultPath)` to
  re-inject plan context directly from the vault file, INDEPENDENT of the PreCompact
  inbox stash (which carries only stale-belief/contradiction reassert events);
  evidence/gate enforcement, `plan_digest` tamper check, auto-transition;
  propagate.py `update-plugin` copytree behavior and landing paths; that grok reads
  commands/skills from the source repo and the MCP server (`dist/server.js`) from the
  `~/.agents/plugins/minni@minni` hardcopy.
