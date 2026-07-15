# Live watch verification — 2026-07-15

**Claim tested:** while a real `minnid` daemon serves a real external client,
`minni watch --json` records **exactly** the memory operations that occurred —
no missing events, no phantoms, correct attribution, content, and order.

**Verdict: PASS — 33/33 checks** (`VERIFY_OUTPUT.txt`). One first-run
verifier-expectation bug was fixed and re-run against the *same untouched
artifacts*: the harness normalized a newline in the adversarial summary by
stripping it, while `recordAudit` correctly escapes it to a literal `\n`
(SEC-014 single-line header contract) — watch had faithfully shown the escaped
form all along.

## Topology

- Daemon: `minnid` from this branch, live for the run under Python 3.13
  (3.14 unavailable in the sandbox), `MINNI_HOME=/tmp/mlive`, strict identity
  mode (unknown agents were capability-denied until an operator-authored
  `principals/loadgen.json` was added), `recall_trace` on.
- Workload A (daemon lane): `harness/triage_bot.py`, a third-party client
  that does not import minni — raw JSON-RPC over the socket. 48 support
  tickets with think time + a 250-search burst (exceeds watch's 200-rows/poll
  batch) = 298 searches, 16 log_events, 8 learns, all ledgered.
- Workload B (plugin lane): `harness/audit_writer.mjs`, driving the real
  `dist/vault.js` recordAudit/sessionReceipt: boot/stop markers, stamped
  turns, guard denials, a second vault remapped live via `MINNI_AGENT_VAULTS`,
  and one adversarial entry (ANSI/C1 escapes + forged `## [...]` audit header).
- Recorder: `minni watch --json --interval 1` (started before the workloads,
  ended via SIGINT to exercise the final-drain path).

## What passed (highlights)

- 298/298 recall bijection across ledger ↔ watch ↔ sqlite, each matched
  exactly once, positionally in ledger order, all stamped with the session id.
- 16/16 log_events; 8/8 learns staged as `proposed` candidates (governance
  side-channel — none became durable).
- Plugin lane: per-line multiset equality; the `MINNI_AGENT_VAULTS`-remapped
  vault attributed to `mapped-bot`, `--agent` filter returned only it.
- Adversarial entry: exactly one benign event; no phantom `fake_tool` entry;
  zero raw ESC/CSI/BEL bytes anywhere in JSON or text output.
- Session receipt tallies (6 strong / 3 weak recalls, 2 guard denials,
  1 learn, 2 drafted candidates) equal the independent ledger-derived counts.

## Process note

The test design itself was adversarially reviewed by three independent
reviewers **before** execution. That pre-review found 2 product bugs (fixed
on this branch with regression tests: one-shot vault discovery missing
late-created vaults; dropped final poll on interrupt), removed 4 false-fail
and 2 false-pass paths from the harness, and added the adversarial entry,
the vault remap, the burst, and the same-second collision coverage.

## Known limits (documented, accepted)

- Empty index: every search legitimately returned 0 hits; hit-count
  *variance* is untested (count faithfulness is tested).
- The ledger's hit-count field comes from the daemon's own RPC reply — a
  daemon lying consistently to both its reply and its store would pass.
- Rotation drain and hook throttling were not exercised live (unit-tested;
  throttle deliberately bypassed to keep ledgers exact).
- Python 3.13, not the project's 3.14 floor.
