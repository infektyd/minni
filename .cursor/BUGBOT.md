# Bugbot rules — minni

## Defect classes this repository treats as blockers
- Logged-then-orphaned: a result, counter, or field that is written but read
  by no production consumer.
- Silent-empty channel: an error path that returns empty/None/[] instead of
  failing loudly or surfacing a degraded flag.
- Capture-without-reassert: state captured (parked, queued, archived) with no
  path that re-delivers or drains it.
- Dead-letter/unbounded queue: any queue or directory with writers and no
  reader, reaper, cap, or age surface.
- Health-signal overstatement: a status/health/coverage report claiming more
  than the code measures (e.g. green when a subsystem was never checked).

## Test standards
- Tests must be behavioral. A test that greps source text or uses
  inspect.getsource to assert wiring is not coverage — flag it as a finding.
- A fix claiming to schedule/emit/record something needs a test that fails
  when the call is removed.

## Deployment standards
- Deployed artifacts are built, versioned copies. A symlink from a deployed
  location into a working tree is a defect.
- Any tool reporting fleet/deploy success must derive its verdict from real
  per-target exit status; success-for-work-not-done is a blocker.

## Security
- Any text that can reach a model-facing or public surface (status, audit
  headlines, review bodies, logs) must pass through redaction; raw exception
  strings and filesystem paths in such surfaces are findings.
- Never weaken the leak gates, defang steps, or workflow binary pinning.
- Credential-shaped strings outside clearly-FAKE test fixtures are blockers.
