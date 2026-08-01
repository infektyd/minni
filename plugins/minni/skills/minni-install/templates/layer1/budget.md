# Layer 1 token budget & curation contract

**Firm budget**: 4096 tokens for the combined content of **every** file under
this `layer1/` directory. Target under 3500 to leave headroom; 4096 is the hard
ceiling. Rough estimate: one token per four characters of English prose.

```bash
python3 -c "import sys; print(sum(len(open(f).read())//4 for f in sys.argv[1:]))" layer1/*.md
```

## Rules (you own this)

- **Full curation rights**: {{agent}} may add, edit, compact, rename, or delete
  files here at any time. `core.md` is the load-bearing file, read first on wake.
- **Sole responsibility**: nothing enforces the budget — no daemon check, no
  external gate. It is a trust contract with the operator. Re-estimate after
  every edit; if you are over, prune before the change lands.
- **Governance bypass is by design**: direct writes here skip the proposal
  pipeline, audit records, FTS/embedding indexing, frontmatter classification,
  and server-side redaction — and are delivered unredacted on wake. Use it for
  high-signal identity and map only. Sensitive material belongs in governed
  `wiki/` paths.
- **Ritual hygiene**: at each distill, review usage and prune to high signal.
  Keep identity, the critical map, and this budget; move anything ephemeral to
  `wiki/`, `inbox/`, or `logs/`.
- **Adding costs something**: a new file here requires pruning elsewhere in the
  directory.

## Current state (seeded {{timestamp}})

- `core.md` + `budget.md` only, both well under budget.
- `core.md` ships with empty Role / Operating notes / Scar tissue / Active
  surfaces sections — fill them from real experience, not from guesses.

If you are reading this, you now know how to own and protect your real Layer 1.
