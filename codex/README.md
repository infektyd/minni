# Codex Master-Plan Package

A self-contained prompt package that gives Codex everything it needs to
synthesize the three independent Sovereign Memory audits (Grok Build, Antigravity
Desktop, Antigravity CLI) into a release-candidate master plan.

This is the **4th orchestrator** in an ongoing comparison test. The output
will sit alongside `docs/RC_MASTER_PLAN.md` (produced via Claude Code + a
general-purpose subagent) so you can compare what Codex's reasoning surfaces
differently from what Claude surfaced.

---

## How to invoke

### Option A — single-shot (recommended)

From the repo root (`/Users/hansaxelsson/Projects/sovereignMemory`):

```sh
codex < codex/package/PROMPT.md
```

Or paste the entire contents of `codex/package/PROMPT.md` into a fresh Codex
session as the first message.

### Option B — interactive

```sh
codex
```

Then, at the first prompt, type:

> "Read `codex/package/PROMPT.md` and execute the task it describes. Do not
> ask clarifying questions — the prompt is self-contained. When done, return
> the report described under § Report-back."

---

## What the package contains

| File | Purpose |
|------|---------|
| `README.md` | This file — invocation + meta |
| `package/PROMPT.md` | The full self-contained task prompt |

`PROMPT.md` is intentionally portable. The audit paths are absolute, but if
you want to run this against a different repo or different audits, swap the
three `INPUTS` paths and the output path.

---

## Expected output

Codex will write **one file**: `docs/RC_MASTER_PLAN_CODEX.md`.

It will NOT modify any source. The audit dirs are read-only inputs.

After writing the file, Codex returns a short summary (total RCM-NNN count,
severity distribution, top-5 critical items, open questions). Keep that
summary — it's the comparison surface against the Claude run's summary.

---

## Why the path is `RC_MASTER_PLAN_CODEX.md`

So the Claude run's `RC_MASTER_PLAN.md` is not overwritten. The two should
sit side by side until you decide which to canonicalize (or merge into a
combined plan).

---

## Notes for the comparison

When Codex finishes, the interesting comparisons against the Claude plan
are:

- **Severity calibration** — does Codex agree on the 11 P0s, or does it call
  some of them P1?
- **Cross-audit dissent resolution** — Codex sees the same five open
  questions Claude flagged; how does it resolve them?
- **Phase ordering** — Codex may sequence the work differently. Where it
  diverges is interesting.
- **Findings Codex flags that Claude missed** — and vice versa. Different
  reasoning styles will surface different things even from the same source
  material.
- **Format adherence** — Codex tends to be more literal than Claude; the
  output spec in `PROMPT.md` is precise to reduce variance.

---

## Reproducibility

The three input audits are:

- `grok/worktrees/grok audit-2026-05-19/grok/audits/2026-05-19/` (Grok Build, `/implement effort=4`)
- `/Users/hansaxelsson/.gemini/antigravity/audits/3.5Flash_2026-05-19/` (Antigravity 2.0 Desktop)
- `antigravity/audits/3.5FlashCLI_2026-05-19/` (Antigravity 2.0 CLI)

All three were generated on 2026-05-19 from the same source audit prompt
with different orchestrators. Re-running this package against newer audits
requires only swapping the three input paths in `package/PROMPT.md`.
