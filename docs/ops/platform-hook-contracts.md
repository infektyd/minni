# Platform hook contracts

Minni is multi-host memory. Operator docs and
[hook-platforms.md](../contracts/hook-platforms.md) advertise **which hosts
can deny PreToolUse and whether Minni’s s6 cold-tool guard is live**.

That matrix is a **contract**, not aspirational prose:

| Host | Contract (summary) | Implementation surface |
|------|--------------------|-------------------------|
| Claude Code | Deny all tools; guard live | Native Claude protocol + shared handlers |
| Codex | Deny **Bash only**; cold-file guard **not** live | Documented limitation — do not fake it |
| **Grok Build** | Deny broad tools; guard **live** | `grok-adapter.ts` + `grok-hook.ts` (camelCase + native tool names + `{decision,reason}` out) |
| Cursor | Deny broad; guard live | Cursor adapters |
| agy / Antigravity | Deny broad; guard live | `gemini-adapter.ts` |
| Kilo | Deny broad; guard live | Bridge plugin |

## Rule

If the matrix says **guard live** for a host, shipping without a working
adapter is a **defect**, not a docs problem. Prefer **`implement_now`** (code
up to contract) over cutting the matrix row.

If a host **cannot** meet the contract (vendor limit), the matrix must say so
explicitly (Codex Bash-only) — that is honesty, not abandoned ambition.

## After code changes

Redeploy the plugin to every wired host:

```bash
minni sync              # or minni sync --full on an editable dogfood checkout
```

Then restart agent apps so they reload `server.js` / hook entrypoints.

## Related

- Truth policy (honesty vs goals): [docs-truth-policy.md](docs-truth-policy.md)
- Fleet update: `minni sync` / [install.md](../install.md)
- Grok App (personal CI gate): [grok-reviewer-app.md](grok-reviewer-app.md)

## Verification bar (Grok cold-tool guard)

**2026-08-04 (local, post-#274):** Unit bar accepted for dogfood gate —

```bash
cd plugins/minni && node --test tests/grok-hook.test.mjs
# 8 pass, including: native Grok read_file denied when strong recall pending;
# stdout is {decision,reason} (not Claude permissionDecision).
```

Wet session proof (live Grok Build + pending strong recall → deny `read_file`)
remains optional follow-up; host restart after `minni sync` required first.
