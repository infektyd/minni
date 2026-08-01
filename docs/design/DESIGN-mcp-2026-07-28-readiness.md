# MCP 2026-07-28 readiness for the Minni plugin server

Status: proposed, 2026-08-01
Scope: assess what the MCP `2026-07-28` specification revision requires of
Minni's MCP surface (`plugins/minni/src/server.ts`), decide what lands now and
what waits, and record the opportunities the new protocol opens.

This document deliberately does **not** migrate anything. The deliverable is
readiness: a decided plan, a tripwire that fires when the ecosystem catches up,
and an honest account of what Minni does not have to do.

## Summary

Minni's exposure to this revision is unusually small, and the reason is
architectural rather than lucky: the server is **stdio-only, tools-only, and
strictly request/response**. Almost every breaking change in `2026-07-28`
targets the HTTP transport, server-initiated messaging, or session state — three
things Minni has never used.

The revision cannot be adopted today regardless, for two independent reasons:
the host cannot speak it, and the SDK package Minni depends on does not
implement it. Both are established below with direct evidence.

## Evidence: the current state of the world

### The installed host does not negotiate `2026-07-28`

Claude Code `2.1.220`, the binary at
`~/.local/share/claude/versions/2.1.220`, is dated 2026-07-24 — four days before
the specification was published. Extracting protocol-version literals from the
bundle yields exactly five:

```
2024-10-07  2024-11-05  2025-03-26  2025-06-18  2025-11-25
```

That set is character-for-character the `SUPPORTED_PROTOCOL_VERSIONS` array of
MCP TypeScript SDK v1. `2026-07-28` does not appear. (Two unrelated 2026 date
strings, `2026-03-05` and `2026-07-25`, are present in the bundle; neither is an
MCP spec revision — no revision bears either date.)

So the highest protocol version any Minni-Claude Code pair can negotiate today
is `2025-11-25`.

### The SDK Minni depends on does not implement `2026-07-28`

`plugins/minni/package.json` depends on `@modelcontextprotocol/sdk` at
`^1.30.0`, and `1.30.0` is what the lockfile resolves and what is installed.
That package was published 2026-07-27 — one day *before* the spec — and it
declares:

```js
// node_modules/@modelcontextprotocol/sdk/dist/esm/types.js:2
export const LATEST_PROTOCOL_VERSION = '2025-11-25';
```

The string `2026-07-28` appears in no file of that package. `npm view
@modelcontextprotocol/sdk dist-tags` reports `latest: 1.30.0` and no `next` or
`beta` channel. Minni is already on the newest v1 release; there is no bump to
take.

### Support ships in a different package family

This is the finding that shapes the whole plan. `2026-07-28` support is **not** a
future v1 minor. It is a new set of packages, published 2026-07-27T23:55Z:

| package | version |
| --- | --- |
| `@modelcontextprotocol/core` | `2.0.0` |
| `@modelcontextprotocol/server` | `2.0.0` |
| `@modelcontextprotocol/client` | `2.0.0` |

Adopting the revision is therefore a **package migration**, not a version bump:
off `@modelcontextprotocol/sdk` and onto `@modelcontextprotocol/server`, with a
different entry point. The upstream migration guide — `docs/migration/support-2026-07-28.md`
in the **typescript-sdk** repository, not this one
(<https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/migration/support-2026-07-28.md>) — replaces

```ts
await server.connect(new StdioServerTransport());   // today, server.ts:1712-1713
```

with

```ts
serveStdio(() => buildServer());
```

where the connection's protocol era is pinned once at open, so handlers need no
per-request branching.

## What Minni actually uses

The SDK is imported at exactly two lines in the entire source tree, both in
`plugins/minni/src/server.ts`:

- `server.ts:6` — `@modelcontextprotocol/sdk/server/mcp.js` (`McpServer`)
- `server.ts:7` — `@modelcontextprotocol/sdk/server/stdio.js` (`StdioServerTransport`)

The server is constructed at `server.ts:158` with a name, a version, and an
`instructions` string. It registers 37 canonical tools via
`server.registerTool`, plus 11 backward-compatible aliases through the
`DEPRECATED_TOOL_ALIASES` loop at `server.ts:1685-1709` — 48 live tools. It
registers **no** resources and **no** prompts.

(A bare `grep -c 'server.registerTool' src/server.ts` reports 39 and is wrong
twice over: one hit is a comment at `server.ts:1677`, and one is the alias
loop's single call site at `server.ts:1700`. The authoritative pin is
`plugins/minni/tests/tool-schema-boundary.test.mjs`, which asserts 38 schemas —
37 canonical plus the one literal the alias loop reuses for all 11 aliases.)

Transport is established at `server.ts:1712-1713` and nowhere else.

A search across `plugins/minni/src/*.ts` for every feature this revision
changes returns nothing:

| feature | uses in Minni |
| --- | --- |
| Sampling (`sampling/createMessage`) | none |
| Roots (`roots/list`, `notifications/roots/list_changed`) | none |
| Logging (`logging/setLevel`, `sendLoggingMessage`) | none |
| Elicitation | none |
| Server-initiated notifications, `listChanged` | none |
| Resource subscribe / unsubscribe | none |
| `ping` | none |
| `Mcp-Session-Id` / protocol session state | none |
| Registered resources / prompts | none |

Two grep results look like hits and are not:

- **`minni_subscribe_contradictions`** (`server.ts:1204`) is a *tool name*. It is
  a poll-style tool call, not `resources/subscribe`, and the removal of
  resource subscriptions does not touch it.
- **`MCP_PROCESS_SESSION_ID`** (`server.ts:172`) is a Minni-minted correlation id
  for daemon recall traces, generated once at module scope. It is not an MCP
  protocol session. Notably it *already* matches the shape the new spec
  prescribes — "servers that need cross-call state use explicit, server-minted
  handles" — so statelessness finds Minni compliant by construction rather than
  by migration.

## Gap table

Ordered by whether Minni must do anything at all.

| Spec change | Minni's current state | Required change | Risk |
| --- | --- | --- | --- |
| Remove `initialize`/`notifications/initialized` handshake; per-request `_meta` protocol version, `clientInfo`, `clientCapabilities` | Handled entirely inside the SDK; no application code touches the handshake | None in Minni code. Comes free with the SDK migration | None |
| Remove protocol sessions and `Mcp-Session-Id` | Never used one. Cross-call state already rides explicit server-minted handles | None | None |
| `server/discover` RPC — servers **MUST** implement | Not implemented; does not exist in SDK v1 | Supplied by `@modelcontextprotocol/server` v2. Not hand-written | None, once migrated |
| Required `resultType` on all results | Not emitted | SDK v2 adds it and strips it before handlers see it — the field is removed from public result types | None |
| Required `ttlMs` / `cacheScope` (`CacheableResult`) on `tools/list` et al. | Not emitted | SDK v2 defaults to `ttlMs: 0`, `cacheScope: 'private'` — valid but yields **no caching**. Opt in via `ServerOptions.cacheHints` | Low. See opportunity 2 |
| MRTR (`resultType: "input_required"`, `inputRequests`/`inputResponses`) replacing server-initiated requests | Minni issues no server-initiated requests, so nothing breaks | None required. Purely additive opportunity — see opportunity 1 | None |
| Tasks moved to `io.modelcontextprotocol/tasks` extension | Minni uses no MCP tasks. Its long-running work (AFM, handoff) is modelled as ordinary tools | None required. Additive — see opportunity 3 | None |
| Roots / Sampling / Logging deprecated, ≥12-month window | Zero uses of all three | None. **Minni's deprecation exposure is nil** | None |
| Required `Mcp-Method` / `Mcp-Name` headers; `x-mcp-header` | HTTP-only. Minni is stdio | Not applicable | None |
| Authorization: RFC 9207 `iss` validation, `application_type`, issuer-keyed credentials, DCR deprecated for CIMD | HTTP-only, and client-side. Minni is a local stdio server with no OAuth | Not applicable | None |
| HTTP+SSE transport reclassified Deprecated | Never used | Not applicable | None |
| SSE resumability / `Last-Event-ID` removed | Never used | Not applicable | None |
| `subscriptions/listen` replaces HTTP GET + `resources/subscribe` | No subscriptions, no resources | Not applicable | None |
| Resource-not-found error code `-32002` → `-32602`; error range repartitioned | Minni registers no resources and mints no MCP error codes in the reserved range | None | None |
| `inputSchema`/`outputSchema` loosened to full JSON Schema 2020-12 | Zod-derived schemas, already a subset | None. Strictly widening | None |
| Deterministic `tools/list` order (SHOULD) | Already satisfied: registration is static, top-to-bottom, in one module | None. Nothing currently asserts the ordering; an optional follow-up if we ever want it pinned | None |

The pattern is worth stating plainly: of the sixteen changes catalogued above, Minni must act on
**zero** to remain correct. Everything in the "required change" column is either
SDK-supplied or optional upside.

## Q1 — Do we migrate to SDK v2 now?

**Decision: no. Not until the host negotiates `2026-07-28`.**

The tempting argument for migrating today is that SDK v2's `serveStdio` ships a
legacy shim enabled by default, so one handler body serves both eras and the
migration is allegedly free. Rejected, for three reasons.

1. **There is no benefit to collect.** The host tops out at `2025-11-25`. Every
   `2026-07-28` capability — MRTR, cache hints, `server/discover` — would be
   dead code on this machine the day it merged. We would take migration risk in
   exchange for behaviour no client can reach.

2. **The compatibility shim is exactly the untested surface.** Migrating now
   means Minni's only exercised code path is the *legacy* path through a
   brand-new major version — the path the SDK authors have the least field data
   on, one working day after `2.0.0` left beta. The modern path we actually
   migrated for would stay unexercised until the host updates. That inverts the
   risk we want: we would be first-in-production on the shim and still untested
   on the target.

3. **`2.0.0` is one day old.** Published 2026-07-27T23:55Z, following five betas
   through July. A major version this fresh, adopted into a 48-tool surface with
   no protocol-level need, is speculative work. The brief for this task was
   readiness, and readiness is not the same as being early.

The cost of waiting is close to zero, because the migration's surface area is
two import lines and one transport call. It does not get harder later; it gets
easier, as the SDK's modern path accumulates field use.

## Q2 — Is there any compatible change to land today?

**Decision: no. There is no safe forward-compatible change available, and in
particular there is nothing to bump.**

This section is written to be self-contained. A future session should be able to
read it alone, confirm the conclusion still holds in about two minutes, and not
re-derive any of it from scratch. The conclusion rests on three independent
pillars, each of which would have to change before the answer changes.

**Pillar 1 — the host cannot speak the protocol.** Claude Code `2.1.220` (binary
dated 2026-07-24, four days before the spec) contains exactly five MCP
protocol-version literals:

```
2024-10-07  2024-11-05  2025-03-26  2025-06-18  2025-11-25
```

`2026-07-28` is absent. Reproduce with:

```sh
grep -aoE '"20(2[4-9])-[0-9]{2}-[0-9]{2}"' \
  ~/.local/share/claude/versions/<version> | sort -u
```

Ignore `2026-03-05` and `2026-07-25` if they appear — neither is an MCP spec
revision; no revision bears either date.

**Pillar 2 — Minni is already on the newest v1.** `package.json` pins
`@modelcontextprotocol/sdk@^1.30.0`; the lockfile resolves `1.30.0`; `npm view
@modelcontextprotocol/sdk dist-tags` reports `latest: 1.30.0` with no `next` or
`beta` channel. The installed package declares `LATEST_PROTOCOL_VERSION =
'2025-11-25'` and contains the string `2026-07-28` in no file. There is no
higher v1 to move to.

**Pillar 3 — support is a package migration, not a version bump.** This is the
pillar most likely to be forgotten, because "bump the SDK" is the reflexive
answer and it is wrong here. `2026-07-28` support lives in a *different package
family* — `@modelcontextprotocol/{core,server,client}@2.0.0` — published
2026-07-27T23:55Z. `npm update` will not find it and should not. There is no
intermediate v1 release that adds `2026-07-28`, and given the package split
there will not be one.

**What this rules out.** Not just the dependency bump: any change that would
only pay off under `2026-07-28` (cache hints, MRTR gating, `server/discover`
handling) is unreachable code today, because no client on this machine can
exercise it. The one spec-aligned property that *is* satisfiable today —
deterministic `tools/list` ordering — is already satisfied by static
single-module registration and needs no change.

**What would change the answer.** Any one of: the host bundle gaining a
`2026-07-28` literal (pillar 1), the v1 line gaining `2026-07-28` in
`SUPPORTED_PROTOCOL_VERSIONS` (pillar 2 — the tripwire watches this), or a
decision to accept SDK v2's legacy shim for its own sake (pillar 3 — rejected in
Q1, and the reasoning there should be re-read rather than re-litigated).

## Q3 — What *does* land in this change?

**Decision: a protocol-version tripwire test, and this document.**

The single most valuable thing to add before a migration is a test that fails
when the assumption underneath the plan changes. `plugins/minni/tests/mcp-protocol-version.test.mjs`
asserts the shipped SDK's `LATEST_PROTOCOL_VERSION` is `2025-11-25` and that the
server module imports the v1 stdio entry point.

This is a **tripwire, not a bug fix**, and the distinction matters for how it
should be read in review: it locks in a fact rather than correcting a defect, so
it cannot "fail on old behaviour" — there is no old behaviour, the current state
is correct. What it does is guarantee that the day someone bumps the SDK, adds
`@modelcontextprotocol/server`, or the v1 line unexpectedly gains a newer
protocol version, the suite fails with a message pointing back at this document
instead of silently changing what Minni speaks on the wire.

That is worth more than a speculative migration, because the failure mode this
guards against — a transitive or inattentive SDK change quietly altering the
negotiated protocol under 48 tools — is both plausible and currently invisible.

## Opportunities

These are the reasons to care about this revision beyond compliance. All are
gated on Q1; none should be built before the host can negotiate the protocol.

### 1. MRTR for operator-gated writes

`resultType: "input_required"` lets a tool handler return a request for
information and be retried with the answer attached, with state threaded through
an opaque, HMAC-sealable `requestState` (`createRequestStateCodec({ key, ttlSeconds })`).

For Minni this is the first protocol-native way to put a human in the loop on a
write. `minni_learn` and `minni_vault_write` could return `input_required` with
a confirmation prompt describing exactly what would be persisted, and only write
on the retry carrying an affirmative `inputResponses`. Today the alternative is
either writing unconditionally or refusing — there is no "ask first" primitive
that survives across the call boundary.

**This does not close issue #251, and should not be proposed as its fix.** #251
is a scanner-coverage defect: `minni_vault_write` (`server.ts`) and `minni write`
(`cli.ts`) persist without calling `flagsSensitiveMaterial`, so a payload the
`minni_learn` gate blocks lands on disk through the other door. Its fix is the
mechanical one the issue already specifies — apply the existing exported scanner
to every persisted channel, pre-slugification, with per-writer regression tests.
Approval gating and credential scanning solve different problems: an operator
clicking "yes" does not make an unscanned secret safe, and a leak that requires
one careless confirmation is still a leak. Ship #251's scanner fix on its own
schedule. MRTR is a layer to add on top afterwards, not a substitute.

### 2. `cacheHints` on the tool list

Minni advertises 48 tools with full Zod-derived schemas — a large, near-static
`tools/list` payload re-sent on every client connection. `2026-07-28` makes
`ttlMs`/`cacheScope` required, and SDK v2's defaults (`ttlMs: 0`,
`cacheScope: 'private'`) are deliberately conservative: valid, but they disable
caching entirely.

**What bounds the number.** Under stdio the tool list *cannot* change during a
connection: Minni emits no `listChanged`, and the set is fixed at module load.
So the naive answer is "cache forever". The reason not to is the upgrade path —
a client that cached the list for a day and then saw the plugin upgraded
underneath it would dispatch against a stale surface, and the failure would look
like a missing or phantom tool rather than a stale cache. The right `ttlMs` is
therefore not "as long as possible" but "comfortably longer than a session,
comfortably shorter than the gap between deploys".

**Concrete recommendation, per list surface.** Declare these when the migration
lands (values in milliseconds):

| Surface | `ttlMs` | `cacheScope` | Reasoning |
| --- | --- | --- | --- |
| `tools/list` | `3_600_000` (1h) | `private` | The only surface Minni actually serves — 48 tools with full Zod schemas, fixed for the process lifetime. One hour eliminates virtually all in-session re-fetches while bounding post-upgrade staleness to something a restart clears. |
| `prompts/list` | `0` | `private` | Minni registers no prompts. Keep the conservative default rather than declaring a TTL for an empty surface; revisit only if prompts are ever added. |
| `resources/list` | `0` | `private` | Minni registers no resources. Same reasoning. |
| `resources/templates/list` | `0` | `private` | Same. |
| `resources/read` | `0` | `private` | Not served today. If resources are ever added they will be vault-backed and mutable, where a non-zero TTL would serve stale memory — the one place caching is actively wrong for Minni. |

**Why `private` everywhere, including `tools/list`.** The tool list is byte-identical
across users, which superficially argues for `'public'`. It should still be
`'private'`: `'public'` only buys anything when a shared intermediary sits in the
request path, and a stdio deployment has none — the client speaks to a
subprocess it launched. `'public'` would add cache-sharing semantics with no
beneficiary, and would become actively wrong the moment any Minni surface is
served over HTTP with per-agent vault scoping.

**Free adjacent win.** The spec notes deterministic `tools/list` ordering
improves LLM prompt-cache hit rates. Minni already orders deterministically
(static top-to-bottom registration in one module), so this benefit arrives with
the migration at no cost — provided nothing refactors registration into a
`Map`/glob iteration in the meantime.

### 3. Tasks extension for AFM and handoff

The `io.modelcontextprotocol/tasks` extension now offers polling via `tasks/get`
and client-to-server input via `tasks/update`, and lets servers return task
handles unsolicited without per-request opt-in. Minni's genuinely long-running
operations — AFM distillation, `minni_await_handoff`, vault compilation — are
currently modelled as ordinary blocking tools.

The unsolicited-handle affordance is the interesting one: a tool that discovers
mid-call that it will take a while could hand back a task handle rather than
holding the call open. This is the largest of the three opportunities and the
least urgent; it is also the one most likely to change again, having just been
redesigned on its way out of the core protocol.

## Deprecation exposure

**None.** Every feature entering the ≥12-month deprecation window — Roots,
Sampling, Logging, HTTP+SSE transport, and the `includeContext` values
`"thisServer"`/`"allServers"` — has zero uses in `plugins/minni/src`. Minni
requires no deprecation-driven work on any timeline.

## Sequencing

1. **Now** — this document; the protocol-version tripwire. No behaviour change.
2. **On host support** — Claude Code negotiating `2026-07-28`, established by
   re-running the bundle check in this document (or an equivalent probe of the
   host). **The tripwire does not detect this and must not be waited on for
   it:** it reads the installed SDK's constants and greps `server.ts`, and never
   inspects the host binary. A tripwire failure means "the SDK or the server
   binding moved — re-read this plan", which is a different event that can occur
   before, after, or without host support. Then
   migrate `server.ts:6-7` and `server.ts:1712-1713` to
   `@modelcontextprotocol/server`'s `serveStdio`. Two imports and one call.
   Verify all 48 tools still list and dispatch.
3. **After the migration is stable** — `cacheHints` on the tool list
   (opportunity 2, smallest and highest-certainty payoff).
4. **Then** — MRTR gating on writes (opportunity 1), *after* #251's scanner fix
   has landed independently.
5. **Unscheduled** — tasks extension (opportunity 3), pending the extension
   settling post-redesign.

## Execution trigger — when to actually do step 2

Being ready is only useful if the READY state is actionable. This section is the
executable form of step 2: the observable signal that fires it, and the shape of
the diff it produces.

### The signal

**Primary (necessary and sufficient): the host negotiates `2026-07-28`.** Check
by re-running the pillar-1 command from Q2 against the *current* Claude Code
build:

```sh
grep -aoE '"20(2[4-9])-[0-9]{2}-[0-9]{2}"' \
  ~/.local/share/claude/versions/<version> | sort -u
```

Migrate when `2026-07-28` appears in that output. Until it does, migrating buys
nothing that any client can reach.

**Secondary (necessary, not sufficient): an SDK that implements it.** Already
true — `@modelcontextprotocol/server@2.0.0` is published. Re-confirm it is still
the intended path rather than a superseded beta line before starting.

**Explicitly NOT the signal: the tripwire failing.** Stated again here because
it is the easiest mistake to make from this document. The tripwire reads the
installed SDK's constants and greps `server.ts`; it never inspects the host. It
will stay green through a host upgrade that adds `2026-07-28`, and it will go
red on an SDK change while the host is still on `2025-11-25`. It is a
change-detector for *our* dependencies, not a readiness detector for the
ecosystem.

### The diff shape

Small and almost entirely mechanical. Estimated at four edits:

1. **Two import lines** — `server.ts:6-7`, from
   `@modelcontextprotocol/sdk/server/{mcp,stdio}.js` to
   `@modelcontextprotocol/server`.
2. **One entry point** — `server.ts:1712-1713`, replacing
   `server.connect(new StdioServerTransport())` with
   `serveStdio(() => buildServer())`. This requires wrapping the current
   module-scope `server` construction (`server.ts:158`) in a factory, which is
   the only structural change in the migration and the only part that is not a
   find-and-replace.
3. **`cacheHints`** — per the table in opportunity 2. Optional at migration
   time; without it the SDK's `ttlMs: 0` defaults apply and behaviour is correct
   but uncached.
4. **The tripwire** — `plugins/minni/tests/mcp-protocol-version.test.mjs`, rewritten
   to lock the *new* protocol facts. Update it; do not delete it. Its four
   assertions are the record of what Minni deliberately speaks.

`package.json` also swaps one dependency for one to three (`@modelcontextprotocol/server`,
plus `core`/`client` only if directly imported).

### Acceptance check

The migration is done when all 48 tools (37 canonical + 11 aliases) still appear
in `tools/list` and dispatch correctly, `npm run test` is green, and the tripwire
asserts the new protocol version rather than `2025-11-25`. Because Minni
registers no resources, prompts, sampling, roots, or logging, there is no other
protocol surface to re-verify — which is the practical payoff of the narrow
architecture this document documents.

## Out of scope

The duplicate MCP registration carried forward from PR #221 —
`mcp__minni__*` and `mcp__plugin_minni_minni__*` both live, because `minni`
appears both in the host's top-level `mcpServers` and in
`plugins/minni/.claude-plugin/plugin.json` — is host *configuration*, not server
code, and is unaffected by this revision. It is noted here only so a future
reader does not mistake it for a protocol problem. It stays with #221. See the
appendix for why the obvious cleanup does not hold.

## Appendix — duplicate MCP registration (operator note)

Recorded here because it surfaced during this audit and is easy to misdiagnose,
**not** because this revision changes it.

**Symptom.** Every Minni tool is registered twice in Claude Code, once as
`mcp__minni__*` and once as `mcp__plugin_minni_minni__*`. Two independent
registrations of the same stdio server: one from the host's top-level
`mcpServers` block in `~/.claude.json`, one from the plugin manifest at
`plugins/minni/.claude-plugin/plugin.json`. Confirmed live during this audit.

**The obvious operator step, and why it does not hold.** The intuitive fix is to
delete the `minni` key from the top-level `mcpServers` object in
`~/.claude.json` and restart the host, leaving the plugin-provided registration
as the single source. That does work — until the next wire run.

`update_claude_config` in `src/minni/wire/writers.py:195` does:

```python
data.setdefault("mcpServers", {})["minni"] = { ... }
```

unconditionally, against `~/.claude.json`. So the top-level entry is **generated
configuration, not hand-written state**, and `minni wire claude-code`
re-creates it every time. A manual deletion is undone by the next wire, which
means an operator who "fixes" this will see it silently return and reasonably
conclude the fix did not work.

**Recommended handling.**

- *Immediate relief (operator, reversible):* remove the `minni` key from
  top-level `mcpServers` in `~/.claude.json` and restart Claude Code. Valid for
  verifying the duplication is the cause of something, and for a clean session
  now. Expect it to come back after any `minni wire claude-code`.
- *Durable fix (code, belongs to #221):* decide which registration is
  authoritative and stop emitting the other. Since the plugin manifest travels
  with the versioned plugin payload and the wire pipeline already owns the
  plugin surface, the plugin registration is the better survivor, and
  `update_claude_config` should stop writing `mcpServers.minni` when the plugin
  provides it — with a wire-level test asserting only one registration results.

This is deliberately *not* proposed as work for this branch. It is a
configuration-generation defect with its own issue, and the only reason it
appears in an MCP-protocol document is to keep the next person from filing it as
a protocol bug or from trusting the one-line cleanup.
