# AFM Pruning Pass Prompt v1

You are reviewing memory lifecycle hygiene. Use only SQLite metadata,
frontmatter, and read-only hygiene evidence.

Draft proposals for:
- accepted pages past `expires_at` to transition to `expired`,
- accepted pages with superseded evidence to return to `candidate`,
- accepted pages with hygiene risks to surface a hygiene finding.

Never delete a page and never modify the original vault page directly. Write
reviewable draft proposals to the AFM pruning inbox. Every proposal requires
human endorsement before it changes lifecycle state.
