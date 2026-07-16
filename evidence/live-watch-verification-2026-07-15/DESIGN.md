# Live IRL test: does `minni watch` record the right data?

## Claim under test

While a real minnid daemon serves a real external client, `minni watch --json`
records **exactly** the memory operations that occurred: no missing events, no
phantom events, correct attribution (agent, session), correct content
(query text, hit counts), correct order.

## Topology

- **Daemon**: minnid from the PR branch, live under Python 3.13
  (3.14 unavailable in this container — proxy blocks the toolchain download;
  documented deviation), MINNI_HOME=/tmp/mlive (AF_UNIX 108-char limit forces
  the short path), strict identity mode (principals/ exists), recall_trace on.
- **External workload A (daemon lane)**: `triage_bot.py` — a standalone
  "support ticket triage bot" that does NOT import minni. Speaks raw
  line-delimited JSON-RPC over the socket as agent `loadgen`
  (operator-authored principal: search/read/learn/feedback/log_event).
  Per ticket: search (with session_id), sometimes log_event, sometimes learn.
  Writes its own ground-truth ledger (`ledger.jsonl`): one line per issued op
  with args and the daemon's response summary.
- **External workload B (plugin lane)**: `audit_writer.mjs` — drives the real
  plugin audit path (`dist/vault.js` recordAudit) against `loadgen-vault`,
  emitting a boot marker, stamped user_prompt_submit/guard/learn entries, and
  a stop marker for the same session id; writes `audit_ledger.jsonl` and
  finally `receipt.json` from `sessionReceipt()`.
- **Recorder**: `minni watch --json --interval 1 > watch.jsonl`, started
  before the workloads, killed after. This is the artifact under test.
- **Duration**: ~8 minutes wall clock (48 tickets, 1.5–4.5s think time;
  plugin lane on an 8s cadence). MINNI_BYPASS_AUDIT_LIMIT=true on the writer
  so hook-entry throttling (1/5s) doesn't hide ledgered entries.

## Verification (verify.py) — three-way cross-check

Ground truth = the workloads' own ledgers (written by non-minni code) plus
the raw stores (sqlite `episodic_events`/`candidate_packets`, vault log.md).
The artifact under test = watch.jsonl. All checks scoped to events after the
test-start timestamp (the preflight probe rows are excluded).

1. **Recall bijection**: every ledgered search appears exactly once in
   watch.jsonl (source=daemon, tool=recall) with matching query text and the
   hit count the daemon returned to the client; counts equal in both
   directions; every recall row in sqlite matches 1:1.
2. **log_event bijection**: same, for ledgered log_events.
3. **Learn side-channel**: ledgered learns == `candidate_packets` rows with
   status 'proposed' (learns are governance-staged, not episodic — watch is
   not expected to show them in the daemon lane; documented scope).
4. **Plugin-lane bijection**: every audit_ledger entry appears exactly once
   in watch.jsonl (source=plugin, agent=loadgen) with matching tool+summary;
   no watch plugin-lane event lacks a ledger counterpart.
5. **No phantoms**: every watch.jsonl line in the test window maps to a
   ledger line (either lane).
6. **Ordering**: daemon-lane watch events appear in ledger seq order.
7. **Session attribution**: all traced recalls carry thread_id == session id;
   `receipt.json` tallies equal the counts derivable from audit_ledger.

## Threats to validity (known, accepted or probed)

- watch reads the same stores the daemon writes — it cannot detect a daemon
  that lies to its own store. The ledger (independent client code) closes
  that gap for counts/content the client observes.
- Empty index ⇒ all recalls return 0 hits; hit-count *variance* is untested
  (bijection and count-faithfulness still are). Note in results.
- Python 3.13, not 3.14; single-host clock for ordering ties.
- The plugin-lane writer necessarily uses minni's own vault.js — that IS the
  surface being observed; its ledger is still written by test code.
