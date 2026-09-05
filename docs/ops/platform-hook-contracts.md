# Platform hook contracts

Minni is multi-host memory. Operator docs and
[hook-platforms.md](../contracts/hook-platforms.md) advertise **which hosts
can deny PreToolUse and whether Minni’s s6 cold-tool guard is live**.

That matrix is a **contract**, not aspirational prose:

| Host | Contract (summary) | Implementation surface |
|------|--------------------|-------------------------|
| Claude Code | Deny all tools; guard live | Native Claude protocol + shared handlers |
| Codex | Deny **Bash only**; cold-file guard **not** live | Documented limitation — do not fake it |
| **Grok Build** | Deny broad tools; guard **PARTIAL** (host deny ≠ s6 liveness; UPS inject dropped; leftover cannot deny) | `grok-adapter.ts` + `grok-hook.ts` map camelCase + native tool names + `{decision,reason}` out — **capability, not liveness** |
| Cursor | Deny broad; guard **PARTIAL** (UPS inject dropped; leftover cannot deny) | Cursor adapters (host deny capability; s6 not live) |
| agy / Antigravity | Deny broad; guard live | `gemini-adapter.ts` |
| Kilo | Deny broad; guard live | Bridge plugin |

## Rule

If the matrix says **guard live** for a host, shipping without a working
adapter is a **defect**, not a docs problem. Prefer **`implement_now`** (code
up to contract) over cutting the matrix row.

If the matrix says **PARTIAL**, host deny / adapter mapping is capability, not
s6 liveness — do not treat that row as live, and do not expand
`GROK_INJECTABLE` to fake it. Goal (`honesty_partial` + `goal_next_pr`): live
deny-to-surface once UPS (or equivalent) actually delivers the envelope.

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

**2026-09-01 (local, PR #45):** Unit bar pins **allow**, not deny —

```bash
cd plugins/minni && node --test tests/grok-hook.test.mjs
# native Grok read_file **allows** leftover consumed=false — UPS inject is dropped;
# leftover file is not consumed; stdout is {decision,reason} (not Claude permissionDecision).
```

s6 deny-to-surface is **not live** on Grok: PreToolUse allows immediately when
UPS cannot inject, so a leftover file cannot deny. `grok-adapter.ts` mapping
remains host-deny **capability**. A wet session that expects deny `read_file`
on pending strong recall is not current and is not the bar.
