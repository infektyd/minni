---
name: readme-audit
description: Use when the repo README may have drifted from reality, before a release, or when someone asks whether the README is still true — verifies every factual claim and every Mermaid node/edge against the code, proposes worthy additions, enforces a line/word budget with a retirement rule so the README cannot grow forever, and ships the result as a branch + PR whose body is the audit report.
---

# README Audit

## Overview

A README is the only document in the repo that is read by people who have not
read the code. That makes it the document most expensive to get wrong and the
one nobody notices going stale. This skill audits it against the actual repo
state and lands the fixes as a reviewable PR.

Two laws govern everything below:

> **Nothing is CORRECT until it has been read in the code.** A claim that
> "sounds right" is UNVERIFIABLE, not CORRECT.
>
> **Nothing is added without something being retired.** A README with additions
> and no removals is a README on its way to being unread.

The skill produces a **report and a PR**. It never merges, and it never applies
a redesign proposal — layout changes are proposals for the operator, always.

## Scope and inputs

- Target: the repo root `README.md` (the operator may name another file).
- Ground truth, in descending authority: **source code** > tests > `CHANGELOG.md`
  and merged PRs > `docs/`. When docs and code disagree, the code wins and the
  disagreement is itself a finding.
- Everything is read-only until Phase 6. Do not edit the README while auditing;
  you will lose the line numbers your evidence is pinned to.

## Phase 0 — Watermark and measurement

1. **Content watermark.** Find the last commit that changed the README's
   *substance*, not its formatting:

   ```bash
   git log -15 --date=short --format='%ad %h %s' -- README.md
   ```

   Read the diffs of the recent ones. Hygiene, link, and typo passes do **not**
   move the watermark. The watermark is the date the README last said something
   new about the product, and it is the cutoff Phase 3 sweeps from. Record it in
   the report — a watermark two releases old is a headline finding by itself.

2. **Measure against budget.** Run the meter:

   ```bash
   python3 plugins/minni/skills/readme-audit/scripts/readme_budget.py README.md
   ```

   It reports per-section lines / prose words / table rows against the budget
   table, the Mermaid node and edge counts, and any section nobody budgeted. Do
   not estimate these by eye; eyeballing always errs toward "there's room".

3. **Number the claims.** Walk the README top to bottom and extract every
   *falsifiable* statement into a numbered list with its line number. A claim is
   falsifiable if a reader could be wrong by believing it. Sweep for these
   classes so none is skipped:

   | Class | Examples |
   |---|---|
   | Version / release | badges, "v0.4.0", "since v0.3", "current release" |
   | Platform / support matrix | supported runtimes, OSes, language versions |
   | Install & commands | every command in every code block, every flag |
   | Feature statements | "X does Y", "gated by default", "no cloud tier" |
   | Architecture assertions | component names, paths, storage layout, data flow |
   | Numbers & stats | download sizes, tool counts, test counts, timings |
   | Comparison claims | any cell in a comparison table about *this* project |
   | Links | every relative path and every `#anchor` |
   | Prose around the diagram | the sentence that narrates the flowchart |

   Marketing adjectives are not claims. "Local-first memory for AI agents" is a
   positioning statement; "no hosted dependency" is a claim.

## Phase 1 — Claim verification

Judge each numbered claim. One row per claim, no claim omitted:

| Verdict | Means | Requires |
|---|---|---|
| **CORRECT** | The code says this | `file:line` you actually read |
| **STALE** | The code says something else | the **correct current fact** + `file:line` |
| **UNVERIFIABLE** | Cannot be settled from this repo | *why* — external fact, no test, ambiguous wording |

Rules that keep this honest:

- **STALE requires a replacement.** "This is wrong" without "here is what is
  true" is half a finding and cannot be turned into a diff.
- **UNVERIFIABLE is a legitimate verdict, not a cop-out** — third-party claims,
  download sizes that depend on a network, "most tools do X". But
  UNVERIFIABLE-because-I-did-not-look is a lie. Say which it is.
- **Check commands by finding their implementation**, not by running them. A
  command that exists in the CLI's dispatch table is verified; a command that
  merely does not error is not.
- **Anchors count.** A link to `docs/foo.md#some-heading` is STALE if the heading
  was renamed, even though the file resolves.
- **Verify the whole claim, including its hedges.** "Wired for A, B, and C;
  D is provisional" is three sub-claims plus a caveat. Compound sentences hide
  the stale half.

## Phase 2 — Diagram audit

Mermaid diagrams age worse than prose because nobody re-reads them. Give every
diagram the same treatment as the claims list.

1. **Node by node.** Each node names something. Does it exist, and is the name
   in the diagram the name in the code? Rendered labels that encode facts —
   paths, directory inventories, scope enums, backend names — are claims and get
   `file:line` evidence like any other.
2. **Edge by edge.** Each edge asserts a real call, write, or data flow. Verify
   direction and mechanism. An edge whose label collapses two different
   mechanisms into one is STALE even if both mechanisms exist — the reader
   learns something false about how it works.
3. **Missing topology.** List shipping components absent from the diagram. Rank
   by how central they are to the story the diagram is *supposed* to tell. A
   component the README's own prose already names but the diagram omits is a
   self-contradiction and ranks HIGH.
4. **Diagram redesign.** If the topology has drifted — the diagram grew a
   dimension it was not designed for, or the real system now has two client
   surfaces where the diagram shows one — propose a redesign with reasoning
   (split into two diagrams, collapse a subgraph, re-layer). Proposal only.
5. **Names are re-verified every run.** Never carry a node name forward from a
   previous audit; renames are exactly what this phase catches.

The diagram has its own budget (nodes / edges) and the same one-in-one-out rule.
A diagram that no longer fits one screen has stopped being "at a glance".

## Phase 3 — Missing claims, and the worthiness bar

Sweep for capabilities that landed since the watermark and are absent from the
README:

```bash
gh pr list --state merged --limit 100 --json number,title,mergedAt
git log --since=<watermark> --diff-filter=A --name-only --format= -- docs/ | sort -u
```

Also read `CHANGELOG.md` from the watermark forward, and diff the skill and
tool/command registries for new entries.

Then **judge, do not include**. The default answer is no. A candidate earns a
place in the README only if it clears all three bars:

1. **New-reader test** — does someone who has never used this project need it to
   *evaluate* (is this for me? is it credible?) or to *install* it? If it only
   matters once you are already using the thing, it is a docs page.
2. **Category test** — does it change what the project *is*, or is it a better
   version of something the README already claims? The latter is at most an
   edit to an existing sentence, not a new one.
3. **Cost test** — name the words it displaces under the budget. A candidate
   nobody will pay for is not worthy; it is merely nice.

Rank surviving candidates HIGH / MEDIUM / LOW and record the *argument against*
each one alongside the argument for. A missing-claims list with no rejections in
it means the bar was not applied.

Special case worth checking every run: a capability that directly answers a pain
the README's own opening section names, but which the README never connects.
That is the highest-value addition a README audit finds.

## Phase 4 — Budget and retirement

### The budget

Ceilings live in `scripts/readme_budget.py` (`DEFAULT_BUDGETS`), with the
per-section reasoning in `references/budget-rationale.md`. In summary:

- **Prose words per section**, each set just above its size at budget adoption —
  thin headroom, so the cap bites on the next addition rather than in a year.
- **Line ceilings** on the sections whose cost is screen real estate (preamble,
  Quickstart, Architecture).
- **Row ceilings** on the table sections, which grow by accretion.
- **A total cap below the sum of the sections.** Local slack, global scarcity:
  you cannot max every section at once.
- **Diagram node/edge ceilings.**

Budgets are revised by the operator in a PR, never silently by the auditing
agent. If a budget is genuinely wrong, propose the new number *with the
reasoning* and leave the old one in force for this run.

### The retirement rule

> When a section is at or over budget, an addition of N prose words to it must
> retire at least N prose words **from that same section**. If the section
> cannot yield N words, the addition is rejected — it does not get to displace a
> different section's content.

Retire in this priority order:

| Order | What goes | Destination |
|---|---|---|
| 1 | Content that failed Phase 1 (stale, and superseded) | delete — it is already wrong |
| 2 | Release archaeology: how a past version got here | `CHANGELOG.md` (usually already there → delete the copy) |
| 3 | Detail a linked doc covers more fully | compress to the link, into `docs/` |
| 4 | Content whose audience is a contributor, not an evaluator or installer | `CONTRIBUTING.md` / `docs/` |
| 5 | A fact stated in two sections | keep the load-bearing instance, cut the echo |

Two hard constraints:

- **No silent deletion of still-true unique information.** Every retirement
  names a destination. If no destination covers it, the same PR must create or
  extend that destination. "It was not important" is not a destination.
- **Protected floor.** Honest caveats, known limitations, and the
  security/governance framing may be *compressed* but never retired to zero.
  Budget pressure must never end up quietly deleting the parts that under-claim
  in order to make room for the parts that sell. If those sections are the only
  place left to cut, the correct move is to reject the addition instead.

Retirement applies to **every** section, not just the ones the claims list
touched. Each run, name at least the top retirement candidates even when nothing
is being added — an audit that never finds anything to cut is not looking.

## Phase 5 — Redesign proposals (standing step)

Run this every audit, even when every claim is CORRECT. Claims can all be true
while the document fails its job. Evaluate:

- **First screen.** What does a reader learn in the first ~30 lines, before any
  scrolling? Is the strongest reason to care above that line, or below it?
- **Section order.** Does the order match a reader's questions (what is it? is
  it credible? is it for me? how do I start?), or the order things were built?
- **Redundancy.** Which facts appear in two or three sections? Which section
  should own each?
- **Audience fit.** Which sections serve an evaluator, an installer, or a
  contributor? Contributor-only material in a README is a retirement candidate
  by definition (Phase 4, order 4).
- **Density.** Which section carries the most claims per line — and is that the
  section a new reader hits first?
- **Proportion.** Is the space each section takes proportional to how much it
  matters? Compare the meter's numbers against your own ranking.

Output 2–5 proposals, each with the reasoning and the tradeoff it accepts.
**Never apply a redesign proposal in the audit PR.** Restructuring and
fact-fixing in one diff makes both unreviewable; the operator opens a separate
PR for any proposal they accept.

## Phase 6 — Output contract

A run ends in a branch and a PR, never a merge.

1. Branch: `docs/readme-audit-<YYYY-MM-DD>`.
2. Apply only the **verified** changes: corrected STALE claims, additions that
   cleared the worthiness bar, retirements per the budget (including writing
   content to its destination in the same commit).
3. Do **not** apply: redesign proposals, budget changes, or any fix whose
   evidence is UNVERIFIABLE.
4. Re-run the meter and confirm the result is within budget. Paste the output.
5. Open a non-draft PR whose **body is the full audit report**:
   - headline counts (claims checked / CORRECT / STALE / UNVERIFIABLE, additions
     proposed vs accepted, retirements, diagram findings);
   - the full claims table with evidence;
   - the missing-claims list with the rejections shown;
   - the retirement ledger — what was cut, from where, to where;
   - the diagram findings;
   - the redesign proposals, clearly labelled **PROPOSALS — NOT APPLIED**;
   - the before/after meter output.
6. Say plainly what was *not* verified. A report that reads as if everything was
   checked, when it was not, is worse than no report.

The operator merges. Always.

## Reporting discipline

- Every CORRECT verdict carries a `file:line` you opened. No `file:line`, no
  CORRECT.
- Report counts honestly, including the boring ones: "41 claims, 3 stale" is a
  finding. So is "0 stale" — say it and show the table.
- Distinguish "I checked and it is fine" from "I did not check". These are
  different and only one of them is work.
- When two sources in the repo disagree with each other, report the
  disagreement rather than picking the convenient one. Three files giving three
  different answers about the same directory layout is a real defect, and the
  README audit is often where it first surfaces.
- Under-claim. If the evidence is thin, say the evidence is thin.

## Under Claude Code (optional)

The core procedure above is portable — any agent with file reading, shell, and a
git host CLI can run it. Nothing in Phases 0–6 requires a specific runtime.

Under Claude Code, Phase 1 and Phase 2 fan out well: dispatch read-only
subagents over disjoint claim ranges (install/commands, features/status,
architecture + diagram, missing-claims sweep) in a single message so they run
concurrently, then merge their tables. Two cautions learned the hard way:

- Give each subagent the *numbered claims with line numbers*, not "audit the
  README" — otherwise coverage overlaps and gaps appear in the same run.
- The merge is yours. Subagents return evidence; the verdicts, the worthiness
  calls, and the retirement decisions stay with the agent that owns the report.

## Worked example

`references/example-audit-2026-08-01.md` is a real run of this procedure against
this repo's README, including the findings, the rejections, and the parts that
came back UNVERIFIABLE. Read it for calibration on how strict the worthiness bar
and the evidence standard are meant to be — not as a list of current defects,
which the next run re-derives from scratch.
