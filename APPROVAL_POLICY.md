# Approval policy — repository default

Approve only on machine-verifiable evidence on the PR's CURRENT head commit,
never on plausibility. ALL of the following must hold:

1. Every required status check is SUCCESS on this exact head.
2. A Bugbot review has run on this exact head with zero unresolved findings of
   Medium or higher severity. If Bugbot has not reviewed this head, do not
   approve.
3. No review in state CHANGES_REQUESTED from any reviewer (human, bot, or app)
   is outstanding.
4. The PR body documents the local pre-PR review rounds this repository's
   process requires (reviewer used, rounds, residual findings).
5. The diff and PR body contain no credential-shaped strings (token prefixes,
   private keys, base64 blobs decoding to credentials).

Additional review standards for this codebase:
- Tests asserting on source text (grep/inspect.getsource) instead of behavior
  do not count as coverage; flag them.
- Deployed artifacts must be built, versioned copies — a symlinked dist or
  payload is a defect, never a convenience.
- New silent-failure patterns are blockers: results logged but read by nothing,
  channels returning empty on error, queues without drains, health signals
  overstating coverage.

If any criterion is uncertain, request human reviewers; never approve.
