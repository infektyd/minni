# Security Plan — Marketable Minimum

Generated: 2026-04-26
Scope: `/Users/hansaxelsson/sovereignMemory` — Python engine daemon, retrieval, TypeScript MCP plugin, vault helpers, OpenClaw bridge, AFM prep.

This plan covers what Sovereign Memory must ship before public release. It is deliberately scoped to the threats a *local-first, single-user* memory daemon faces — not enterprise multi-tenant hardening. A longer "v2 hardening" backlog is preserved at the bottom; nothing there blocks v1.

## Executive Summary

Findings: 10 must-fix, 4 must-document, 6 deferred to v2.

The 10 must-fix items map directly to claims the README already makes: "local-first," "agent-owned vaults with privacy frontmatter," "auditable handoffs," "multi-agent shared daemon." A reviewer who tests those claims today will find each of them broken in at least one place.

Sequencing:

- **Phase A — Local perimeter** (parallel): close the world-writable socket, bound input sizes, escape the audit log, tighten launchd file permissions, document cloud-sync exclusion.
- **Phase B — Principal binding** (SEC-002 serial; rest parallel): server-stamp agent identity; remove caller control over vault roots, AFM URL, and writeback frontmatter.
- **Phase C — Read policy** (SEC-005 serial; rest parallel): one read policy across recall, expand, vault search, AFM prep; fence recalled content; parse vault frontmatter from the YAML block; contain handoff wikilinks.
- **Document or one-line fix**: remove HTTP fallback, add `pip-audit` target, set launchd `Umask`, document Assumptions + cloud-sync hygiene.

## Threat Model

### Assets

- `sovereign_memory.db` (with `-wal`/`-shm` sidecars) and FAISS index files.
- Codex-owned Obsidian vault content under `codex-vault/` — wiki, raw, logs, inbox, generated context packs.
- Runtime sockets: `/tmp/sovereign.sock`, `/tmp/sovrd.sock`, OpenClaw bridge socket.
- Cross-agent handoff envelopes and audit logs.
- AFM preparation payloads (task, recall, changed-files, outcome context).

### Trust boundaries

- Model/prompt text → MCP tool arguments.
- MCP plugin process → Python daemon over local socket.
- Local browser/process → loopback UI server.
- OpenClaw extension → Sovereign Memory bridge daemon.
- Vault markdown → AI context pack and AFM prep.
- Agent identity claims → cross-agent memory authorization.

### Adversaries in scope

- Prompt injection delivered via vault content, learned memory, or tool arguments.
- Lower-trust local processes that can reach a Sovereign Memory socket.
- Operator misuse of caller-supplied override fields (`agentId`, `vaultPath`, `afmPrepareUrl`).
- A user who unwittingly drops the vault into iCloud/Dropbox/Time Machine.

### Adversaries out of scope

- Other macOS user accounts on the same machine (perimeter is the user account).
- Internet-facing or remote MCP transports (stdio only at v1).
- Physical attacker against an unlocked machine (FileVault is the assumption).

## Assumptions (explicit)

These hold at v1. If any of them breaks, additional findings open.

1. Single-user macOS account is the security perimeter.
2. FileVault is enabled for at-rest protection of the DB and vault.
3. The vault and DB are **not** in iCloud Drive, Dropbox, Google Drive, or any sync root, and Time Machine excludes them. Sovereign Memory will warn if it detects a sync-root location at startup.
4. MCP transport is stdio only; the optional HTTP JSON-RPC fallback is removed (see SEC-008).
5. The model provider may cache or train on tool I/O. Anything recalled into context is treated as exported.
6. The host runtime (Codex, Claude Code, Gemini, OpenClaw) is trusted to stamp the effective principal honestly. A compromised in-process IDE extension breaks principal binding — out of scope.
7. The daemon does not run as root and does not require macOS Full Disk Access.
8. Dependency installs are from trusted registries with lockfile pinning.

## Phase Plan

### Phase A — Local Perimeter (parallel)

Goal: stop the obvious local-process and shape attacks before broader policy work.

Items: SEC-001, SEC-014, SEC-015, SEC-016 (one-line), cloud-sync doc.

Exit criteria:

- No Sovereign Memory socket is group/world writable.
- Audit log entries cannot be forged via `\n## …` injection.
- `learn` and socket reads have a hard size limit.
- `~/Library/Logs/sovrd.*.log` is mode 0600 via plist `Umask`.
- README documents cloud-sync exclusion and the daemon warns at startup if the vault path is under a known sync root.

Verification: `pytest engine/`, `npm test -w plugins/sovereign-memory`, plus a 30-second smoke that asserts `stat -f %A /tmp/sovrd.sock` returns `600`.

### Phase B — Principal Binding (SEC-002 serial; rest parallel)

Goal: make agent identity, vault root, AFM endpoint, and writeback content server-side facts.

Items: SEC-002 first, then SEC-003, SEC-004, SEC-018 in parallel.

Exit criteria:

- A caller cannot claim another agent's identity through normal MCP.
- A caller cannot point memory tools at another vault root.
- AFM prep cannot post to a caller-supplied URL.
- `learn` content cannot smuggle a forged frontmatter block into writeback files.

Verification: same as Phase A plus new tests under `engine/tests/test_principal_binding.py` and `plugins/sovereign-memory/tests/principal-binding.test.ts`.

### Phase C — Read Policy (SEC-005 serial; rest parallel)

Goal: one read-policy implementation, applied everywhere, on inputs that cannot be spoofed.

Items: SEC-005 first, then SEC-006, SEC-010, SEC-011, SEC-012 in parallel.

Exit criteria:

- `can_read_document(principal, workspace, doc_metadata)` is the single point of truth and is called from search, read, expand, neighborhood, and AFM prep paths.
- Vault frontmatter is parsed from the leading `---…---` YAML block, not by full-document regex.
- Recalled snippets are wrapped in an explicit "evidence-only" envelope with provenance before being shown to the model.
- Handoff `wikilink_refs` cannot resolve outside `<vault>/wiki/` (no `..`, symlink-aware).

Verification: extend `engine/test_pr2_envelope.py` and add `plugins/sovereign-memory/tests/read-policy.test.ts`. All findings have at least one negative-path test in `tests/regression/sec-NNN_*`.

## Findings

Each finding gets: severity, affected files, what/why, fix sketch.

### SEC-001 — World-writable OpenClaw socket exposes file-read and memory-write (HIGH)

Files: [openclaw-extension/sovrd.py:323,410,467,508](openclaw-extension/sovrd.py).

The legacy OpenClaw daemon chmods its Unix socket to `0o666`, exposes `/read` for absolute paths, and exposes `/learn` with no runtime auth.

Fix: move the socket into `~/.sovereign-memory/run/` mode `0700`, set the socket itself to `0600`, restrict `/read` to vault/project roots via realpath containment, require a runtime principal token for `/learn`, cap request bodies.

### SEC-002 — Caller-supplied agent identity (HIGH)

Files: [engine/sovrd.py:382,564,741,853,1025,1111](engine/sovrd.py); [plugins/sovereign-memory/src/server.ts:172,221,374](plugins/sovereign-memory/src/server.ts).

`agent_id`/`agentId`/`fromAgent` flow in from caller input and drive recall, learn, handoff, and audit logging. Any caller can claim another identity.

Fix: introduce `EffectivePrincipal` resolved from process/session config or local capability. Remove `agentId` from model-facing schemas. Reject caller-supplied identity that disagrees with the runtime principal.

### SEC-003 — Caller-controlled vaultPath (HIGH)

Files: [plugins/sovereign-memory/src/server.ts:37,88,115,132,153,178,223,286,310,332,348,368](plugins/sovereign-memory/src/server.ts); [plugins/sovereign-memory/src/vault.ts:309,362,472](plugins/sovereign-memory/src/vault.ts).

Many MCP tools accept optional `vaultPath`. Vault helpers create directories, write notes, append audit logs, and search markdown under whatever path is supplied.

Fix: map each principal to a configured vault root. Resolve via `realpath`. Reject paths outside the principal's allowlist. Remove `vaultPath` from model-facing schemas.

### SEC-004 — Caller-controlled AFM prepare URL (HIGH)

Files: [plugins/sovereign-memory/src/server.ts:39,88](plugins/sovereign-memory/src/server.ts); [plugins/sovereign-memory/src/task.ts:575,583,656,842](plugins/sovereign-memory/src/task.ts).

Task and outcome preparation accept `afmPrepareUrl` from the caller and POST task/recall/outcome context to that URL.

Fix: remove `afmPrepareUrl` from model-facing schemas. Use a configured endpoint (loopback default). Block non-loopback unless explicit operator config + host allowlist + TLS.

### SEC-005 — Read policy is not centralized; expand can bypass it (HIGH)

Files: [engine/retrieval.py:1570,1772](engine/retrieval.py); [engine/test_pr2_envelope.py:565](engine/test_pr2_envelope.py).

Privacy/status filtering exists in some paths but not all. `expand_result` fetches by id with no policy check. Drift-prone.

Fix: implement `can_read_document(principal, workspace, doc_metadata)` once. Call it from search, read, expand, neighborhood, HyDE, vault search, AFM prep, and audit presentation.

### SEC-006 — Vault context packs ignore privacy/status frontmatter (MED→HIGH for marketing)

Files: [plugins/sovereign-memory/src/vault.ts:472](plugins/sovereign-memory/src/vault.ts); [plugins/sovereign-memory/src/task.ts:246](plugins/sovereign-memory/src/task.ts); [plugins/sovereign-memory/src/sovereign.ts:278](plugins/sovereign-memory/src/sovereign.ts).

Vault search returns markdown snippets without parsing frontmatter; sensitivity inferred from string heuristics. Defeats a marketed feature.

Fix: parse YAML frontmatter; attach metadata to `VaultSearchResult`; gate via the SEC-005 policy function.

### SEC-010 — No prompt-injection fence between recalled memory and model context (HIGH)

Files: [plugins/sovereign-memory/src/task.ts:355,375,382](plugins/sovereign-memory/src/task.ts); [plugins/sovereign-memory/src/sovereign.ts:272](plugins/sovereign-memory/src/sovereign.ts); [engine/sovrd.py:347-374](engine/sovrd.py).

Snippets and wikilinks are interpolated raw into the model context. No fencing, no provenance markers, no escaping of `##` or backticks.

Fix: wrap each recalled snippet in an "EVIDENCE-ONLY" fenced envelope with `source=`, `agent=`, `status=` provenance. Strip or escape backticks and ATX headings inside the body.

### SEC-011 — Frontmatter regex matches the whole document (HIGH)

File: [engine/indexer.py:50-85](engine/indexer.py).

`_extract_frontmatter` does `re.search(r"agent:\s*(\S+)", content)` against the entire document. Body text like `agent: hermes\nprivacy: safe` reattributes the doc.

Fix: extract only the leading `---\n…\n---` block. Parse it as YAML. Apply per-field regex inside that range only.

### SEC-012 — Handoff wikilink path traversal (HIGH)

Files: [plugins/sovereign-memory/src/vault.ts:570-599](plugins/sovereign-memory/src/vault.ts); [plugins/sovereign-memory/src/hook.ts:102](plugins/sovereign-memory/src/hook.ts).

`normalizeWikilinkRef` strips `[[`, `]]`, `.md`, leading `/` — but not `..`. A handoff envelope with `wikilink_refs: ["../../../etc/passwd"]` reads arbitrary files into recipient context.

Fix: after resolving, `realpath` the candidate and verify containment within `path.join(vaultPath, "wiki")`. Reject otherwise.

### SEC-018 — Writeback content can forge its own frontmatter (MED→HIGH paired with SEC-011)

File: [engine/writeback.py:383-413](engine/writeback.py).

`_write_to_disk` interpolates `content` after the frontmatter block. A `learn` payload with `\n---\nagent: trusted\n---` creates a new frontmatter block that the indexer (SEC-011) reads as truth.

Fix: reject `---` or YAML-frontmatter-shaped lines in learn content, or write content inside a fenced code block beneath the real frontmatter.

### SEC-014 — Audit log injection (MED, must-fix for "auditable handoffs" claim)

Files: [plugins/sovereign-memory/src/vault.ts:336-348](plugins/sovereign-memory/src/vault.ts); [engine/sovrd.py:297-305](engine/sovrd.py).

Audit entries are formatted as `## [ts] tool | summary`. A `summary` containing `\n## […]` injects forged entries.

Fix: in `recordAudit`, replace `\n` with `\\n` and prefix-escape leading `#` in `summary` and `tool`. Cap line length.

### SEC-015 — No socket body cap; embedding DoS (MED)

File: [engine/sovrd.py:1488-1517,825-930](engine/sovrd.py).

`reader.readuntil(b"\n")` has no `limit`. `_handle_learn` runs the embedder on caller-controlled content of arbitrary size.

Fix: pass `limit=1_048_576` (1 MiB) to `readuntil`. Enforce `len(content) <= MAX_LEARN_CHARS` (default 64 KiB).

### SEC-016 — launchd log files world-readable (LOW, one-line)

File: `~/Library/LaunchAgents/com.openclaw.sovrd.plist`.

Daemon stderr (containing learning excerpts) is mode 0644.

Fix: add `<key>Umask</key><integer>0077</integer>` to the plist. Document the change.

### Cloud-sync hygiene (DOC + startup warning)

If the vault or DB lives under iCloud Drive, Dropbox, Google Drive, or OneDrive sync roots, "local-first" is false. Time Machine likewise snapshots the data.

Fix: at daemon startup, detect known sync-root prefixes (`~/Library/Mobile Documents/`, `~/Dropbox`, `~/Google Drive`, `~/OneDrive`) and emit a warning. Document the recommendation in README and `docs/CANONICAL-PATHS.md` to set `com.apple.metadata:com_apple_backup_excludeItem` on the vault and DB.

## Document, Don't Fix (one-commit each)

- **SEC-008** — JSON-RPC HTTP fallback: remove the code path. It was off by default; v1 has no remote transport.
- **SEC-009** — Python dependency audit: add a `make audit` target running `pip-audit -r engine/requirements.txt`. Document in README.
- **SEC-016** — already covered above as a one-line plist change.
- **Cloud-sync hygiene** — already covered above as a startup warning + README note.

## Deferred to v2

These are valid but do not block marketability for a local-first single-user tool. Tracked here so reviewers see they've been considered.

- SEC-007 — Full audit-log redaction framework (we do credential-shaped escaping in SEC-014, full retention/redaction policy is post-launch).
- SEC-013 — Handoff envelope replay protection (single-user local; replay implies the attacker already has FS access).
- SEC-017 — FAISS ghost vectors after row deletion (matters with long retention; v1 ships with periodic full-rebuild guidance).
- Operational artifacts: PR templates, incident-response runbook, observability dashboards, alert thresholds — enterprise theatre at v1 scale.
- Asset expansions: Apple Keychain entries, browser-extension storage, in-process IDE extension threats — not on v1's surface.
- Subprocess environment inheritance audits.

## Verification

The release gate before marketing this:

```bash
cd engine && pytest -q              # baseline 213 passed, 3 skipped
cd ../plugins/sovereign-memory
npm test                             # baseline 32 passed
npm run smoke:hook
make -C .. audit                     # SEC-009; pip-audit clean
```

Plus a manual:

- Start daemon on a temp socket; verify `stat -f %A` returns `600`.
- Submit a handoff with `wikilink_refs: ["../../etc/passwd"]`; verify rejection.
- Submit a `learn` with body `\n---\nagent: trusted\n---`; verify rejection or fenced storage.
- Submit a recall whose result contains `## EVIDENCE`; verify the model-facing pack quotes it inside the evidence envelope, not as a heading.
- Set `vaultPath` to `~/Library/Mobile Documents/...`; verify a startup warning appears.

## Decision Log

- 2026-04-26 — Initial broad audit produced SEC-001..SEC-009.
- 2026-04-26 — Second-pass review added SEC-010..SEC-018 and cloud-sync hygiene.
- 2026-04-26 — Plan trimmed to marketable-minimum scope: 10 must-fix + 4 must-document, rest deferred to v2. Rationale: this is a single-user local tool; v1 hardening must close embarrassing-to-demo issues and back the marketing claims, not deliver enterprise audit posture.
- 2026-05-02 — Deep audit addendum added SEC-019..SEC-022 for cross-agent information requests. Implemented a vault-backed request/approval contract in the TypeScript plugin; cryptographic principal binding remains a future remote/multi-user requirement.

## 2026-05-02 Deep Audit Addendum — Cross-Agent Request Contracts

### Threat Model Update

- Asset: agent-owned vaults, private recalled memory, cross-agent handoff/request metadata, audit logs, local daemon socket, and browser-reachable console endpoints.
- Attacker: prompt-injected model output, a lower-trust local process that can call plugin/daemon surfaces, or a compromised/over-eager agent attempting to get another agent's private context.
- Deployment posture: localhost/local-first, single-user macOS by default. If exposed on LAN or public internet, this plan is incomplete and auth must be added first.
- Trust boundaries: model text to MCP tool arguments, MCP plugin to daemon socket, browser to local UI bridge, vault markdown to model context, and one runtime agent principal to another.
- Agent boundary concerns: runtime identity is still mostly environment/config stamped; display names in prompts remain insufficient. Cross-agent information must flow through attributed inbox/outbox contracts, never direct private memory reads.

### Findings

#### SEC-019 — Direct handoff delivery bypasses recipient consent (HIGH)

**Where:** `plugins/sovereign-memory/src/server.ts`, `plugins/sovereign-memory/src/sovereign.ts`, `engine/sovrd.py`

**What it is:** `sovereign_negotiate_handoff` and `daemon.handoff` can deliver a packet into a recipient inbox without a recipient-side approval decision. It is attributed and audited, but still operates as delivery, not consent.

**Why it matters here:** The product goal is model-agnostic, scale-agnostic memory sharing. Without a consent gate, one agent can push context at another and increase prompt-injection or memory-poisoning risk.

**How to fix:** Add a separate request lifecycle where the requester can ask for information but cannot receive content until the recipient approves or denies under its own runtime principal. Keep direct handoff for handoff envelopes, not private information retrieval.

**Status:** Done in the TypeScript plugin via `sovereign_ping_agent_request`, `sovereign_ping_agent_inbox`, `sovereign_ping_agent_decide`, and `sovereign_ping_agent_status`.

#### SEC-020 — Cross-agent requests need replay and lifecycle state (MEDIUM)

**Where:** `plugins/sovereign-memory/src/agent_ping.ts`

**What it is:** Cross-agent requests need stable IDs, nonces, TTLs, terminal statuses, and audit entries. Otherwise a stale or duplicated request can be approved later without context.

**Why it matters here:** Local-first does not remove replay risk; stale JSON in an inbox/outbox can be re-read by an online agent or copied by another process.

**How to fix:** Persist a pseudo-contract with `pending`, `approved`, `denied`, and `expired` states; reject decisions on anything other than `pending`; include TTL and nonce; sync both sender and recipient copies.

**Status:** Done for plugin-level request contracts.

#### SEC-021 — Agent identity is config-stamped but not cryptographically authenticated (MEDIUM)

**Where:** `plugins/sovereign-memory/src/config.ts`, `plugins/sovereign-memory/src/agent_ping.ts`, `engine/sovrd.py`

**What it is:** The implementation stamps the active principal from environment/config, not from a cryptographic session credential. That is appropriate for the current single-user local plugin, but insufficient for LAN/public or multi-user deployment.

**Why it matters here:** Model-agnostic and scale-agnostic implies future agents and possibly multiple runtimes. A caller that can start the plugin with another `SOVEREIGN_*_AGENT_ID` can impersonate that principal.

**How to fix:** Before remote/multi-user use, add per-agent local capabilities or signed session credentials and reject decisions whose credential does not bind to the recipient principal.

**Status:** Remaining risk; documented as local-only assumption.

#### SEC-022 — Browser UI remains read/prepare only (INFO)

**Where:** `plugins/sovereign-memory/src/ui-server.ts`, `plugins/sovereign-memory/tests/ui-server.test.mjs`

**What it is:** The browser console does not expose learn, vault-write, or the new agent-ping decision endpoints.

**Why it matters here:** Local web pages and extensions can try to hit loopback services. Keeping approval and memory-sharing tools on MCP/stdin reduces drive-by browser risk.

**How to fix:** Preserve the current local-origin checks and avoid adding decision endpoints to the browser bridge without CSRF/session auth.

**Status:** Verified by existing UI tests and unchanged by this addendum.
