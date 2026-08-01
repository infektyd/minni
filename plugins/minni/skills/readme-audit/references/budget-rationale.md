# Budget rationale

Why these numbers and not others. The budget table lives in
`../scripts/readme_budget.py` (`DEFAULT_BUDGETS`); this file is the argument for
it. Operators change the numbers in a PR — not the auditing agent, and not
silently.

## The method

Every ceiling is **the section's measured size at adoption, rounded up to the
next sensible stop**. That is deliberate, and it is the opposite of how budgets
usually get set.

A budget picked from an ideal ("a README should be 300 lines") is a budget with
a year of slack in it, and a cap with a year of slack does nothing for a year —
it is a deferral wearing a rule's clothes. A budget set just above today's size
starts working on the next PR. The README is not currently too long; the point
is to make sure it never becomes too long, and that only happens if the cap
binds now.

Measured at adoption (2026-08-01, README @ 095c264):

```
175 lines · 1,429 prose words · 13 table rows · one 17-node / 13-edge diagram
```

"Prose words" excludes headings, fenced code blocks, and table rows — a
Quickstart is mostly commands, and counting them would make the number track
copy-paste bulk instead of reading cost. Code and table lines still count
against the *line* ceilings, because they still cost screen.

## Total cap: 1,600 words / 190 lines

The section ceilings sum to 1,670. The total cap is 1,600 — **below the sum on
purpose**.

Per-section slack lets a section absorb a genuinely better sentence without a
fight. A total cap below the sum means you cannot cash in every section's slack
at once: the README as a whole has ~170 words (12%) of growth left, and after
that every addition is strictly one-in-one-out. Local slack, global scarcity.

The line cap (190 vs 175 today) is tighter in proportion than the word cap
because lines are what a reader actually scrolls, and the two grow at different
rates — a table row is one line and three words.

## Per section

| Section | At adoption | Ceiling | Why |
|---|---|---|---|
| (preamble) | 23w / 8 lines | 40w / 10 lines | Title, one-line pitch, badges. The line cap is the real one: badge rows breed. |
| The problem | 100w | 110w | It has already made its point. Growth here means the pitch is being restated, not sharpened. |
| What Minni is | 271w | 300w | The densest load-bearing section — most new capability claims land here, so it gets the largest absolute headroom of the small sections. |
| Recall is evidence, not instruction | 125w | 140w | Protected floor (below). Enough room to sharpen, not to expand into a security doc. |
| How it compares | 115w / 5 rows | 140w / 6 rows | The row cap matters more than the word cap: comparison tables grow by competitor accretion, and a 9-competitor table is a survey, not a positioning. One spare row. |
| Quickstart | 491w / 66 lines | 550w / 70 lines | The largest section and the one with the strongest excuse to grow ("but users need to know"). They usually need `docs/install.md` to know. Thin headroom is the point. |
| Architecture at a glance | 58w prose / 40 lines | 90w prose / 45 lines | The diagram is the section; prose is a caption. If the caption needs 200 words, the diagram has failed and belongs in `docs/architecture.md`. |
| Status | 220w | 240w | Rots fastest — every release adds a "now shipping" sentence and removes none. Tight cap forces retirement of the previous release's news. |
| Documentation | 0w / 8 rows | 20w / 10 rows | A link table needs no prose. Two spare rows; past that, `docs/` needs an index page of its own, not more README. |
| Support | 26w | 40w | Fixed-size by nature. |

## Diagram: 18 nodes / 16 edges

At adoption: 17 nodes, 13 edges — one spare node, three spare edges.

The ceiling is not aesthetic. "At a glance" is a claim about reading time, and a
flowchart stops being glanceable somewhere around twenty nodes; past that the
reader is doing graph traversal, which is what `docs/architecture.md` is for.
One spare node means the next architectural addition has to argue for itself
against an existing one — which is exactly the conversation that keeps the
diagram a *summary* instead of an inventory.

Edges are capped tighter than nodes (16 vs 18) because edge density, not node
count, is what makes a diagram unreadable.

## The protected floor

Budget pressure has a bias: under scarcity, the content that gets cut is the
content that does not sell. In a README that means the caveats.

So these may be **compressed but never retired to zero**:

- honest caveats and known limitations (the "early, tiny adoption, no published
  benchmarks, no hosted option" material);
- the security and governance framing, including the evidence-not-instruction
  property;
- the statement of what is *not* verified or *not* yet real.

This repo's stated value is that "when in doubt, this project under-claims"
(README Status). A budget that could quietly delete the under-claiming in order
to fit more features would invert that value while appearing to enforce
discipline. If the only remaining place to cut is the protected floor, the
correct outcome is **the addition is rejected**, not the caveat.

## Revising these numbers

Legitimate reasons: the README's job changed (new audience, project left
pre-1.0), or a section was split or merged. Not a legitimate reason: this run
wants to add something and the cap is in the way. That is the cap working.

Propose the new number with its reasoning in the audit report, leave the old one
in force for that run, and let the operator decide in the PR.
