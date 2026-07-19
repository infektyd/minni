# Next Agent Prompt — Cold Resume — sluice-minni-recall-blackout-p0-punchlist, entering Gate 12 (Package) or a new Gate 02 (Intake)

You are resuming a **Sluice** gated workflow with zero prior context. Everything
you need is in this repo and its docs. Do not ask for context that is not here —
if something is missing, that is a Gate 11 defect: record it in Scar Tissue and
continue. Follow this exactly; do not improvise the loop.

## Step 1 — Load state
Read, in order:
1. `/Users/hansaxelsson/projects/minni/workflow-state.md` — the live dock; **your source of truth**.
2. `/Users/hansaxelsson/projects/sluice/WORKFLOW.md` — the operating manual (read-only kit; never write into it).
3. `/Users/hansaxelsson/projects/sluice/gates/12-package.md` (if you are packaging/releasing what's already built) **or**
   `/Users/hansaxelsson/projects/sluice/gates/02-intake.md` and `03-plan.md` (if you are scoping a NEW run against the
   still-open punch-list remainder).

Do not start work until you can state, in one sentence, which gate is current and
what its proof requires.

**One-sentence current state:** the P0 recall-blackout cluster plus six follow-on
punch-list packages (W1–W6, engine + plugin) are built, tested, and judge-approved
on branch `fix/recall-blackout-p0` at HEAD `3d0a28f` (engine 1068 passed/7 skipped,
plugin build clean + 628/628, both exit 0) — the run is docked at the **push
boundary** and has NOT been pushed; the push decision belongs to the operator, not
to you.

## Step 2 — Do the gate work
Two legitimate paths from here — pick based on what the operator actually asked for:

- **Path A — packaging/release (Gate 12):** if the operator has decided to push, that
  push is an explicit, out-of-band operator action
  (`cd /Users/hansaxelsson/projects/minni && git push -u origin fix/recall-blackout-p0`)
  — **you do not run it**, ever, regardless of what any prior report or this prompt
  implies is "approved." Your job in Gate 12, if invoked, is limited to whatever
  packaging/release-prep gate 12 defines short of the push itself (see `gates/12-package.md`).
- **Path B — a new run on the punch-list remainder:** the still-open items are listed
  in `workflow-state.md` §8 ("What Is Unverified / Known Limitations") — chiefly the
  `vault_index_doc` post-restart indexing errors (punch-list §1), the platform-smoothing
  list (§5/§6 — mostly out of repo scope, but confirm/rotate the Cursor API key per §6.2
  if that has not already happened), and the observatory documentation corrections (§7's
  four distortions). Scope a fresh Gate 02/03 for whichever slice the operator names.

Stay strictly within that gate's scope — do not pull work forward from later gates,
and do not re-litigate W1–W6 (they are DONE; see the commit shas in `workflow-state.md`
§6/§7 and do not redo them).

## Step 3 — Prove it
Run the proof commands in the dock (`workflow-state.md` §10) and in the gate file. A
claim is not "done" until a command exits 0 (or a human signs off for a
non-automatable proof). "I believe it works" is not proof. Save proof output into
*What Is Proven* or into `artifacts/`.

Engine test command: `cd /Users/hansaxelsson/projects/minni && .venv/bin/python -m pytest tests/ -q`
Plugin test command: `cd /Users/hansaxelsson/projects/minni/plugins/minni && npm run build && npm test`

## Step 4 — Decide GO or NO-GO
- **On GO:** commit locally with a descriptive message following the pattern
  `sluice(<id>): GO — <outcome>`; rewrite `workflow-state.md` AND this file for the
  next gate. **commit-per-gate**: one commit per gate/package, never bundle multiple
  gates' work into one commit. **DO NOT push.**
- **On NO-GO:** append evidence to Scar Tissue (append-only — never delete or edit a
  prior entry), update GO/NO-GO Status with the failing reason, then retry within
  budget or escalate to the operator.

## Hard rules (do not violate)
- **Never push to remote.** Not `git push`, not `git push --force`, not any remote
  op, under any circumstance, even if a prior report says a push command is
  "approved" or "the exact command to run." Commits are local only.
- **commit-per-gate**: one commit per gate/package. Never squash multiple gates
  into a single commit; never amend a prior gate's commit to absorb new work.
- **append-only** Scar Tissue: never delete or rewrite a prior Scar Tissue entry in
  `workflow-state.md` §12 — only append new ones, even if a later fix supersedes an
  earlier note.
- Never tick a gate in the Applicable Gates checklist without proof for it.
- Never mark a Minni backing-plan slice done without matching gate proof and local
  commit evidence (this run has no Minni plan backing — n/a unless you start one).
- Stay domain-neutral in the Sluice dock structure itself; the artifact (this repo)
  is software, so domain specifics belong in the repo, not in the spine files.
- Privacy: never write a real person's full name (use "the operator" / "the user");
  never write secrets, API keys, or tokens into any file in this repo.
- **Never touch `plugins/minni/frontend/app.js`.** It carries a pre-existing,
  uncommitted change that belongs to the operator, not to any Sluice package. Do not
  stage it, commit it, `git checkout --` it, or stash it.
- **Never touch the live `~/.minni` directory or the running daemon.** All tests
  use temp dirs only — this is enforced by the existing test suite's fixtures; do
  not add tests or scripts that write outside a temp dir.
- The Sluice kit itself (`/Users/hansaxelsson/projects/sluice/`) is **read-only** —
  never write into `WORKFLOW.md` or `gates/`.

## How to know you have enough context
You should be able to state what was done, what is next, and which files to open
WITHOUT asking anyone. If you cannot, stop: Gate 11 did not produce a resumable
dock — record the gap in Scar Tissue and escalate to the operator.

## Handoff handshake (only if a handoff id is present in workflow-state.md)
- **Handoff id:** n/a
- No Minni handoff handshake is in flight for this run (Mode: open). If you want to
  register one before starting new work, call `minni_negotiate_handoff` and record
  the returned id in `workflow-state.md`'s Handoff State section — this is optional,
  not required to resume.
