# Minni as an interactive agent CLI — research findings & architecture proposal

**Status: research pass only — no implementation.** This document assesses how far
minni is from being a CLI you *work inside* (like Claude Code / Grok Build), where
the brains are the operator's existing OAuth'd subscriptions to Claude Code, Codex,
Grok, and Gemini — inverting today's relationship where those CLIs are the
front-end and minni is the plugin. Everything minni does today stays intact; the
proposed mode is purely additive.

## Context

Minni today is a governed local memory daemon (`minnid`, line-delimited JSON-RPC
2.0 over a 0600 Unix socket, ~30 methods) plus a Node MCP plugin
(`plugins/minni/src/server.ts`, 37 tools) that external agent runtimes connect to
via `minni wire <platform>`. The proposed evolution: launch `minni` itself,
converse with an agent in a streaming REPL, and have that agent be Claude Code /
Codex / Grok / Gemini running under the operator's own subscription login — with
minni memory (recall/learn/approve/handoff) woven through the session and the
governance model preserved. The Swift AFM helper stays on Swift 6.3+ and keeps its
current background-consolidation role.

## Key facts established (repo)

- **CLI** (`src/minni/minni_cli.py`, 385 LOC, stdlib argparse): lifecycle-only
  (`up/down/status/doctor/wire`). Deliberately never imports engine internals. No
  REPL, no TUI deps anywhere (`Environment :: No Input/Output (Daemon)`
  classifier; no rich/textual/prompt_toolkit).
- **Daemon protocol**: one JSON line request → one JSON line response; no
  streaming framing. Identity is **server-stamped** —
  `resolve_effective_principal` (`src/minni/principal.py`) resolves from
  operator-owned `~/.minni/principals/<agent>.json` files + `MINNI_AGENT_ID` env;
  wire claims are never trusted. Capability gate per method
  (`minnid_runtime/provenance.py`).
- **Principals already exist for all four target brains**:
  `principal_templates/{claude-code,codex,gemini,grok-build}.json`, each with
  standard caps `[search, read, learn, feedback, log_event, handoff, export]` and
  per-agent vault roots. `minni wire` already knows all four platforms
  (`src/minni/wire/platform.py`).
- **Governance**: learn → `stage_candidate` → `resolve_candidate`
  (operator/govern-gated, `minnid_runtime/governance.py`); the daemon-internal AFM
  loop precedent (`loop_principal()` with `["govern"]`, never mintable over the
  wire) shows how delegated approval works. Evidence envelopes +
  `is_instruction_like` defusal protect any agent reading recall output.
- **Swift AFM helper** (`src/minni/native_afm_helper.swift`, 615 LOC): one-shot
  subprocess (stdin JSON → one JSONL out), fresh `LanguageModelSession` per call,
  ~4K context, 12 ops, JIT-compiled by a zsh wrapper via `xcrun swiftc` with **no
  version pin** — already targets macOS 26 / FoundationModels, so Swift 6.3+ is a
  no-op constraint. It is *not* needed as the interactive brain and stays as-is.
- **Three clients already speak the same socket protocol** (CLI `_rpc`,
  `minnid_client.py`, Node `sovereign.ts`) — a REPL is just a fourth client.

## Key facts established (external, July 2026)

- **Anthropic bans third-party harnesses using subscription OAuth** — ToS updated
  Feb 20 2026, server-side blocking Feb–Apr 2026 (OpenCode, OpenClaw cut off).
  Minni must NOT implement its own Claude OAuth harness. The sanctioned route is
  driving the **official Claude Code harness**: `claude -p` / Claude Agent SDK
  under the user's existing login (subscription usage still flows through it as of
  July 2026, with the June 2026 credit-pool change for agent usage as a billing
  caveat to verify at build time).
- **Codex**: `codex exec` (headless), the official Codex SDK (TypeScript), device
  auth for headless login, and Codex-as-MCP-server — all sanctioned under ChatGPT
  plan sign-in.
- **Gemini CLI**: headless `-p` with `--output-format json`, reuses cached Google
  OAuth, generous free tier.
- **Grok Build**: headless `-p` with streaming JSON, and native **ACP** support
  (`grok agent stdio`), for SuperGrok / X Premium+ subscribers.
- **ACP (Agent Client Protocol, agentclientprotocol.com)** is the unifying seam:
  JSON-RPC 2.0 over stdio, streaming, permission-request flow. Official adapters
  exist for **all four**: `@agentclientprotocol/claude-agent-acp` (Claude Code),
  `@zed-industries/codex-acp`, `gemini --acp`, `grok agent stdio`. This is how
  Zed/JetBrains embed these agents; minni would be an ACP *client* in the
  terminal.

## Recommended architecture (future implementation)

**Minni becomes an ACP client host.** `minni agent [--brain
claude-code|codex|grok|gemini]` spawns the user's installed official CLI through
its ACP adapter as a subprocess (each CLI keeps its own OAuth login — minni never
touches tokens, staying ToS-clean), renders the streamed session in a REPL, and
injects minni memory around it.

Planned components, in control/data-flow order (all new code; existing symbols
referenced for reuse):

1. **`src/minni/agent_cli/` (new package)** — keeps `minni_cli.py`'s engine-free
   contract intact; `minni_cli.py::main()` gains one `agent` subparser that lazily
   imports it.
   - `repl.py` — `run_agent_repl(brain: str, socket_path: str) -> int`: streaming
     prompt loop (stdlib/`prompt_toolkit` optional extra), slash-commands
     `/recall /learn /approve /candidates /handoff /brain /status`.
   - `acp_client.py` — `class ACPClient`: spawn adapter subprocess, speak JSON-RPC
     2.0 over stdio (`initialize`, `session/new`, `session/prompt`, streamed
     `session/update` notifications, `session/request_permission`). Same
     line-delimited JSON-RPC idiom the codebase already uses.
   - `brains.py` — `BrainSpec` registry mapping brain → adapter command +
     detection/preflight (is the CLI installed? logged in?), reusing
     `wire/platform.py::PlatformSpec` naming so brain ids match existing
     principals.
   - `memory_bridge.py` — thin wrapper over the socket protocol
     (reuse/consolidate the `_rpc` pattern from `minni_cli.py` /
     `minnid_client.py`) for `search`, `learn`, `stage_candidate`,
     `resolve_candidate`, `list_candidates`, `handoff`, `minni_await_handoff`.
2. **Memory weave** — on `session/new`, prepend `minni_prepare_task`-style recall
   (evidence envelopes via existing `retrieval.py::build_evidence_envelope` —
   poisoning defense applies unchanged); the spawned brain also gets the full MCP
   toolset if already wired (`minni wire` state is reused, nothing new needed
   there).
3. **Identity & governance** — the spawned brain runs with its existing stamped
   principal (`MINNI_AGENT_ID=claude-code` etc. via the wire config it already
   has). The REPL itself runs as the local operator (`MINNI_LOCAL_OPERATOR`).
   Design decision (operator's choice): **govern-delegated** — session learnings
   can be auto-resolved through `resolve_candidate` with `resolved_by` stamping,
   following the `afm-loop` precedent; implement as an operator-authored grant of
   `resolve_candidate` to a distinct `minni-agent-session` principal so it stays
   auditable and revocable (trade-off flagged: this weakens the human gate; keep a
   `--propose-only` flag).
4. **Streaming** — happens entirely REPL⇄adapter-subprocess; the daemon socket
   protocol is untouched (its one-line-per-response framing stays valid).
5. **Swift helper** — no changes required. Optional later phase: a fifth "brain"
   = resident AFM session server (FoundationModels supports multi-turn
   sessions/streaming/tools), but that is explicitly out of scope for now.

## Distance estimate ("how far off is it?")

Closer than it looks — the hard parts (governed memory substrate, per-platform
identity, wire/install machinery, evidence security) already exist. What's missing
is purely the front-end and orchestration:

| Gap | Size |
|---|---|
| ACP client (spawn/handshake/stream/permissions) | ~600–900 LOC, the core new work |
| REPL + slash-commands + rendering | ~400–600 LOC |
| Brain registry + preflight (`doctor`-style checks per CLI) | ~200 LOC |
| Memory bridge (mostly consolidation of existing `_rpc` triplication) | ~150 LOC |
| Governance delegation principal + `--propose-only` | ~100 LOC + one principal template |
| Tests (ACP fake adapter, provenance/gating, REPL smoke) | comparable to source |

No daemon protocol changes, no Swift changes, no MCP plugin changes required for
v1.

## Risks / open items to verify at implementation time

- Anthropic's June 2026 agent credit-pool billing: confirm current
  `claude -p`/Agent SDK subscription terms before shipping the Claude brain.
- ACP adapter maturity varies (Gemini's is flagged experimental); fall back to
  each CLI's native headless JSON streaming
  (`claude -p --output-format stream-json`, `codex exec`,
  `gemini -p --output-format json`, `grok -p`) behind the same `ACPClient`
  interface if an adapter misbehaves.
- Node ≥20 already required for the plugin; adapters add npm deps at wire-time,
  not to the Python wheel.
- Concurrent REPL + daemon is fine (REPL is a socket client, not a second engine —
  avoid `agent_api.SovereignAgent`, which self-grants `*` and opens the SQLite
  directly).

## Verification (when implemented)

`minni up && minni doctor`; `minni agent --brain gemini` (free tier) against a
fake-ACP fixture and a real CLI; confirm `/recall` returns evidence envelopes, a
session learning lands as `status=proposed`, `/approve` resolves it with correct
`resolved_by`, capability-denied on `daemon.endorse` for the brain principal;
handoff round-trip between two brains.

## Sources

- [Anthropic clarifies ban on third-party tool access to Claude (The Register)](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/) · [OpenCode blocked (ZBuild)](https://www.zbuild.io/resources/news/opencode-blocked-anthropic-2026) · [HN thread](https://news.ycombinator.com/item?id=46549823)
- [Run Claude Code programmatically (official docs)](https://code.claude.com/docs/en/headless) · [Use the Agent SDK with your Claude plan (help center)](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) · [Credit pool change](https://www.techtimes.com/articles/317625/20260602/anthropic-ends-subscription-subsidy-agents-june-15-credit-pool-replaces-flat-rate-access.htm)
- [Codex auth (official)](https://developers.openai.com/codex/auth) · [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) · [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) · [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [Gemini CLI headless mode (official)](https://google-gemini.github.io/gemini-cli/docs/cli/headless.html) · [Gemini CLI auth](https://geminicli.com/docs/get-started/authentication/)
- [Grok Build announcement (x.ai)](https://x.ai/news/grok-build-cli) · [Grok headless & scripting (docs.x.ai)](https://docs.x.ai/build/cli/headless-scripting)
- [Agent Client Protocol — agents registry](https://agentclientprotocol.com/get-started/agents) · [Zed ACP](https://zed.dev/acp) · [ACP adapters overview (ai-sdk.dev)](https://ai-sdk.dev/providers/community-providers/acp)
- [Apple FoundationModels docs](https://developer.apple.com/documentation/FoundationModels) · [WWDC26: What's new in Foundation Models](https://developer.apple.com/videos/play/wwdc2026/241/) · [Swift 6.3 released](https://www.swift.org/blog/swift-6.3-released/) *(background: confirms the Swift helper could later become a local brain; out of scope now)*
