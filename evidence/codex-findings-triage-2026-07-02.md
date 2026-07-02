# Codex Security Findings — Triage vs Current HEAD (2026-07-02)

Branch: `agent/gifted-neumann-7ccc1a` (current with main). Source: `codex-security-findings-2026-07-02T01-47-13.487Z.csv` (48 findings, filed against older commits). Six parallel `plumb` triage agents verified each against current code. Paths below are repo-root relative (old `workspace/minni/*` → root; `engine/sovrd.py` → `engine/minnid.py`; `plugins/sovereign-memory` → `plugins/minni`; `sovereign_*` tools → `minni_*`).

Verdict legend: **PRESENT** = still exploitable; **PARTIAL** = primary vector closed, real residual remains; **FIXED** = closed (no work); **POLICY** = mechanism confirmed, fix is a product decision.

---

## FIXED — no work needed
- **P4** default-deny agents — dispatch `enforce_method_capability` now gates every DB RPC (`engine/minnid_runtime/dispatch.py:60-66`, `provenance.py:39-67`).
- **P6** platform principals overbroad — missing platform caps now default to `[]`, not `["*"]` (`engine/principal.py:452-461`).
- **R1** MCP drill — `handle_sm_drill` stamps principal, `expand_result` gates via `can_read_document` (`recall.py:721-780`).
- **R2** expand-by-id — `handle_expand` resolves principal + gates (`recall.py:438-466`). (Minor note: `expand_result` SELECT omits `privacy_level`; same-agent only, no cross-principal leak — folded into M3 fix.)

---

## MUST-FIX — engine (Python)

### Capability / principal gates
- **P1 (HIGH, PRESENT)** — Read-only principal can run write-capable AFM compile. `daemon.compile` requires only `read` (`provenance.py:65`); `dry_run=false` calls `submit_drafts`/`apply_consolidation_result` promoting candidates to durable learnings; handler never receives principal (`afm.py:309-328`). **Fix:** thread stamped principal into `handle_daemon_compile`; require `is_operator_principal` (or `govern`/`compile` cap) when `dry_run=false`.
- **P2 / P5 (HIGH, PARTIAL)** — Self-owned candidate self-approval. Cross-owner resolution now needs operator (`governance.py:740-746`), but `owner == principal.agent_id` reaches durable INSERT (`governance.py:781-790`) with only an `instruction_like` gate. A `learn`-capable surface stages then accepts its own candidate → durable learning. **Fix:** require operator/`govern` capability for the `accept`/`learn` branch even for self-owned candidates (owner-only may reject/redact, not promote).
- **P3 (HIGH, PRESENT)** — No-agent-id caller stamped as wide-open operator `main`. `author_principals` emits no canonical `main`/`operator` (`tools/author_principals.py:16-22`); `from_local_transport` synthesizes `main` with `capabilities=["*"]`, `allowed_vault_roots=[]` (`principal.py:261-267`); returned immediately when `supplied_agent_id is None` (`principal.py:399-400`) → `is_operator_principal`=True. **Fix:** author an operator-owned `main`/`operator` principal, and/or make synthesized `main` non-operator unless an authenticated local operator channel asserts it.

### Retrieval read-policy leaks
- **R4 (HIGH, PRESENT)** — Wikilink neighborhood summaries leak restricted memory. `_fetch_linked_context` runs post-filter but ungated: `WHERE d.path LIKE '%'+candidate` with no agent/privacy/status/vault-root check; `[[%]]` matches every doc; first 700 chars → AFM + `{link,doc_id,path}` returned (`retrieval.py:1782-1837`, links from `1774-1780`). **Fix:** thread `principal`/`workspace` in; drop rows failing `can_read_document`; escape/reject LIKE metacharacters (`%`,`_`) or use exact-path equality.
- **R5 (HIGH read-leak, PARTIAL)** — `resolve_contradiction` ownership FIXED (`governance.py:288-304`), but `handle_learn` calls `detect_contradictions(..., agent_id=None)` (`governance.py:121-123`) and returns full cross-agent `content`/`assertion`/`agent_id` (`writeback.py:512-550`). Probing assertion exfiltrates other agents' learnings. **Fix:** pass `agent_id=principal.agent_id` (or operator-only for cross-agent; restrict returned fields).
- **R6 (MED, PARTIAL)** — `hygiene_report` containment FIXED (`health.py:307-309`); `health_report` still queries `documents`/`learnings` with no agent/privacy/status filter for any identified caller (`health.py:208-258`). **Fix:** add `can_read_document`-style filtering or operator-cap gate to health_report queries.
- **R8 (MED, PARTIAL)** — `feedback` FIXED (principal-derived agent_id, `recall.py:334-344`); `trace` ring keyed only by `trace_id`, no owner binding — any authenticated principal with a `trace_id` reads that trace (`recall.py:357-388`). **Fix:** store creating principal per ring entry; deny/scrub for non-owner.
- **M3 (MED, PARTIAL)** — Blocked-page reindex purge FIXED in indexers (`indexer.py:194-204`, `wiki_indexer.py:354-364`), but `expand_result` SELECT omits `privacy_level`/`page_status` (`retrieval.py:2598-2604,2610-2618`) → privacy defaults `"safe"`. **Fix:** add `d.privacy_level, d.page_type, d.page_status, d.workspace_id` to SELECT and populate `raw`.

### Lifecycle / indexing integrity
- **M2 (HIGH, PARTIAL)** — Durable-learn frontmatter spoof. Agent leg FIXED (server-stamped `doc_agent`, `minnid.py:428,438`), but `page_type = meta.get("page_type")` copied from model frontmatter (`minnid.py:446`); `can_read_document` treats `page_type in {wiki,handoff,synthesis,decision,session}` as cross-visible (`principal.py:630-637`). `type: wiki` learn → cross-agent-readable. **Fix:** force `page_type=None`/fixed `learning` for durable-learn synthetic docs.
- **M4 (MED, PRESENT)** — Superseded learnings still semantically searchable. Store-time synthetic doc `page_status='accepted'` (`minnid.py:430,440-444`); supersede updates only `learnings` columns (`governance.py:326-330`) — synthetic doc/FTS/chunks/FAISS untouched; doc-search filters on `documents.page_status` → stale chunks surface. **Fix:** on supersede/resolve/reject, purge/mark the synthetic doc (`_durable_doc_path(agent,"learning:{id}")`) + FAISS/FTS/chunks.
- **M5 (MED, PRESENT)** — `search_learnings` active = `superseded_by IS NULL` only, ignoring `status` (`retrieval.py:2799,2810`); rejected/expired (set without `superseded_by`, e.g. `afm.py:185`) still match FTS. Same in `writeback.py:279,356-357,515-516`. **Fix:** add `AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))`.
- **M6 (MED, PRESENT)** — Vault frontmatter spoofs identity layer. `_infer_layer(whole_document defaults 1)` returns `"identity"` for `agent.startswith("identity:")`; VaultIndexer never passes `whole_document`, so `agent: identity:codex` markdown → `layer='identity'` (`indexer.py:92,119,131-137,208-238`), a retrieval filter (`retrieval.py:1586-1622`). **Fix:** reject/strip frontmatter `agent` starting `identity:` in VaultIndexer (identity layer assignable only by trusted seed path).

### Inbox ingest
- **I1 / I2 (HIGH/MED, PRESENT)** — Forged/kind-less inbox file → durable memory. Agent-spoof FIXED (principal bounded to vault-dir owner, `inbox_ingest.py:184-234`), but rows inserted `privacy_level=None` (`inbox_ingest.py:308-326`); consolidation maps NULL→`""`∈`_SAFE_PRIVACY` (`consolidation.py:37,288-291`) → promotable to durable `learnings` under the vault's principal. **Fix:** treat NULL/unset privacy as NOT safe (drop `""` from `_SAFE_PRIVACY` or route NULL→review), or stamp a conservative non-safe privacy at ingest.
- **I3 (LOW, PRESENT)** — Unhashable `kind` DoS. `kind not in STOP_KINDS` raises `TypeError` for list/dict kind; try/except wraps only `json.loads`, aborting the whole `ingest()` pass; poison file never deleted → persistent inbox starvation (`inbox_ingest.py:215-220,276-281`). **Fix:** `if not isinstance(kind,str) and kind is not None: skip+count`, or per-file try/except.

### DoS / misc engine
- **R7 (MED, PRESENT/by-design)** — HyDE default-on posts recall query to `http://127.0.0.1:11437/...` unauthenticated, no response cap (`config.py:149`, `hyde.py:23,66,83`); port-squat leaks queries. **Fix:** loopback token/shared secret or default `hyde_enabled=False`; add response byte cap.
- **R9 (MED, PRESENT)** — Backend fan-out. Wire `backend` list iterated with no cap/dedup; `["faiss-disk"]*N` builds N disk-loading backends (`recall.py:116`, `retrieval.py:1379-1399`). **Fix:** dedup + cap length (≤4), reject unknown members loudly.
- **R10 (LOW, PRESENT)** — `subscribe_contradictions` returns unscoped `SELECT COUNT(*) FROM contradiction_events` as `contradiction_events_in_window` (`governance.py:447-450,477`). **Fix:** drop the global count or gate behind operator cap.
- **X7 (MED, PRESENT)** — `functools.lru_cache(maxsize=10000)` on `_content_hash(text)` retains up to 10k full payloads (`openclaw-extension/sovrd.py:190-194`). **Fix:** drop the cache (hashing is cheap) or key on a short digest / cap text length.

### Migration (special handling)
- **M1 (HIGH, PARTIAL)** — Migration 013 unconditionally rewrites `main`→`claude-code` for `candidate_packets.principal` and `learnings.agent_id` (`migrations/013_*.sql:9-14`); runs on any un-migrated DB. Foreign-principal resolution now blocked, but rewritten rows are *owned by* `claude-code` (recall + owner-check resolvable). Can't unship an applied migration. **Fix:** guard fresh/un-migrated installs (skip UPDATE unless a legacy-alias config asserts `main`≡`claude-code`, or operator-confirm); document pre-013 backup/restore for installs with genuine operator `main` memory.

---

## MUST-FIX — plugin (TypeScript)

### Hooks / recall-state
- **H2 (MED, PRESENT)** — Recall-state written via bare `writeFile` to `<vault>/.runtime/recall-state.json`, no symlink guard (`recall-state.ts:191-207,242-270`; called `hook-handlers.ts:389`). Same pattern in `lifecycle-state`/`setActivePlan`. **Fix:** `lstat`/`O_NOFOLLOW` or `realpathSync(dirname)`+containment (reuse `assertUnder`, `vault.ts:1279-1292`) before writing.
- **H3 (MED, PRESENT)** — Recall guard interpolates raw `hit.title`/`hit.wikilink` into `permissionDecisionReason` with imperative framing (`recall-guard.ts:155-177`; titles unsanitized from daemon/vault via `recall-state.ts:118-130,166-170`). Note-title prompt injection into hook denial reasons. **Fix:** strip newlines/control chars, clamp length, quote as inert data; keep imperative text a fixed template.
- **H5 (MED, PRESENT)** — `extractIdentityBody` unanchored regex over whole read context, no stamped-agent match (`sovereign.ts:236-245`; injected Layer 1 `hook-handlers.ts:306-343`); learning content rendered unescaped (`agent_api.py:273-279`) can smuggle `## Agent Identity:`. **Fix:** anchor to leading block + validate matched name == stamped agent; strip newlines in learning content before rendering.
- **I4 (MED, PRESENT)** — Deferred inbox-tail rewrite `writeFile(tail.filePath,...)` no lstat/realpath/atomic (`vault.ts:1252-1263`). Symlink swap TOCTOU. **Fix:** `assertUnder` + `writeFileAtomic` (both already exist in `vault.ts`).
- **I5 (MED, PRESENT)** — All-malformed inbox entries never consumed (`vault.ts:1217-1223`); newest-3 timestamp window (`readInboxStatus`, `vault.ts:1046-1084`) → three recent malformed files crowd out valid correction reasserts indefinitely. **Fix:** quarantine/archive unconsumed all-malformed entries after N boots/age, or exclude zero-valid-event entries from the window.
- **I6 (MED, PRESENT)** — `isValidStaleBeliefEvent` validates types but keeps unknown props; raw event pushed (`vault.ts:1105-1122,1186,1199`) into boot envelope (`hook.ts:199`, `hook-handlers.ts:265`). Free-form injected strings reach model context. **Fix:** return a NEW object with only allowlisted fields (`event_id`, `superseded_learning_id`, `new_learning_id`, `originating_agent`, `created_at`).

### Policy / plan / server
- **H1 (MED, PRESENT/by-design)** — Learn-question routing: `isQuestion` (`policy.ts:40-47`) sends `"Can you learn this: ...?"` to recall `automaticAllowed:true` (`policy.ts:65-74`), skipping write-intent suppression (`hook-handlers.ts:359`). **Fix:** gate learn-question branch on `!includesAny(text, LEARN_TERMS)` / no imperative write marker.
- **H6 (MED, PRESENT)** — `updateSlice` auto-promotes plan draft→accepted when slices done, gated only by `isTrivialEvidence` (≥8-char non-stock string) via model-callable MCP (`plan.ts:487-538,1162-1168`); accepted → default-recallable. **Fix:** don't treat model evidence as sufficient to `accept`; require operator approval or a terminal non-recallable status.
- **H7 (MED, PRESENT)** — `computePlanDigest` hashes only goal + slice id/status/evidence (`plan.ts:90-98`), but `compactPlanView`/`compactPlanPointer` inject `next_action`, `open_questions`, `constraints`, `scar_tissue`, slice `title` etc. (`plan.ts:691-742,1091-1116`); vault tampering of those passes digest validation. **Fix:** extend digest to cover all injected fields (stored-format change; re-persist).
- **X8 (MED, PRESENT)** — `getLeasePath` path.joins caller `requestId` with no `REQUEST_ID_PATTERN` check (`agent_ping.ts:207-210`); reached by `checkAndReapLease`/`readContract` before validation (`agent_ping.ts:152-158,217-226`, `getAgentPingStatus:459`, `decideAgentPingRequest:369`); schema only `min(8)` (`server.ts:1016,1042`). `../` traversal reads/unlinks contract `.json`. **Fix:** validate `REQUEST_ID_PATTERN` at top of `getLeasePath`.
- **X9 (MED, PRESENT)** — `getAgentPingStatus` reads requester outbox first, trusts first readable contract when actor is `fromAgent` OR `toAgent` (`agent_ping.ts:459-479`); `withExpiry` doesn't re-validate. Requester forges `status:"approved"` in own outbox. **Fix:** resolve authoritative status from lease/recipient copy; verify `response.decidedBy === toAgent` against lease nonce.
- **X10 (MED, PRESENT)** — Audit intent routes `automaticAllowed:true` (`policy.ts:95-104`); `minni_audit_report` returns `latest: tail.entries.at(-1)` full markdown entry incl. paths/metadata/errors (`vault.ts:899-920`, `server.ts:790-795`). **Fix:** return aggregate-only (drop/redact `latest`) or require confirmation.

### Policy decision (not a mechanical fix)
- **H4 (MED, POLICY)** — Hosted-agent Layer 1 sets `minni_layer_1_persona: agent_authored`, agent-authored persona slot written as `accepted`/`safe` identity doc + "invoke capabilities without asking" (`propagate.py:790,840-850,896-921`). Confirmed present; feature-vs-defect is an operator call. **Action:** surface to operator; if defect, keep persona out of the `accepted` identity row / require operator approval.

---

## MUST-FIX — installer / bench / CI

- **X1 (HIGH, PRESENT)** — `--platform antigravity` writes wildcard `mcp(minni/*)` into Gemini/Antigravity allow-lists, covering write/export tools (`propagate.py:430,487-494`). **Fix:** grant per-tool read-only set only; leave write/export to per-call confirmation.
- **X4 (HIGH→MED, PARTIAL)** — Console `/api/audit-tail` now token-gated, but `/api/prepare-task|prepare-outcome|status|candidates` require token only if `MINNI_CONSOLE_TOKEN` set (default unset) — reachable by any local caller, returns vault-derived packets (`ui-server.ts:132-143,549-556`). **Fix:** make `consoleAuthRequired` true for all `/api/prepare-*` and `/api/status` unconditionally (auto-generate token if unset) or bind to unix socket.
- **X2 (MED, PRESENT)** — `update-plugin` preserves existing `MINNI_AGENT_ID`/`VAULT_PATH`/`SOCKET_PATH` env verbatim, no validation (`propagate.py:200-257`); consumer trusts them (`config.ts:49-58,236-239`). **Fix:** assert `MINNI_VAULT_PATH == vault_for(agent)` (non-symlink, user-owned, under `~/.minni/`) and expected socket; drop on mismatch.
- **X3 (MED, PARTIAL)** — `--agent` unvalidated: `vault_for` `../` traversal (`propagate.py:133,687-691`); `_toml_basic_str` escapes `"`/`\` but not `\n`/`\r` → newline TOML section injection (`propagate.py:498-522`). **Fix:** validate `--agent` against `^[a-z0-9][a-z0-9-]{0,63}$` at parse.
- **X6 (MED, PRESENT)** — Bench scrubber rewrites text but not filename-derived `doc_id`; PII in filenames persists in `manifest.json`/`scrub_spans.jsonl`/gold labels (`corpus.py:60`, `snapshot.py:64,118`, `scrub.py:396-455`, `label_cli.py:85-86`). **Fix:** alias/rename doc_ids; scan doc_ids in `verify_scrubbed`.
- **X11 (LOW, PRESENT)** — `check-public-boundary.sh` redirects to hardcoded `/tmp/*.txt` (symlink truncation) (`scripts/check-public-boundary.sh:7,13`, `ci.yml:48`). **Fix:** `mktemp` + trap cleanup or `$RUNNER_TEMP`.
- **X5 (HIGH→LOW, PARTIAL)** — `vaultPath` removed from recall schema (`server.ts:490-495`); `searchVaultNotes` pre-scan now confined to `DEFAULT_VAULT_PATH` + `filterSafeVaultResults` (fails closed on undeclared privacy). Low residual (no daemon workspace scoping on safe notes). **Fix (optional):** gate pre-scan on daemon reachability or workspace subtree.

---

## Suggested slice grouping for implementation
- **Slice A — engine capability/principal gates:** P1, P2/P5, P3
- **Slice B — engine retrieval read-policy:** R4, R5, R6, R8, M3
- **Slice C — engine lifecycle/indexing:** M2, M4, M5, M6
- **Slice D — engine inbox ingest:** I1/I2, I3
- **Slice E — engine DoS/misc:** R7, R9, R10, X7
- **Slice F — plugin hooks (symlink/injection):** H2, H3, H5, I4, I5, I6
- **Slice G — plugin policy/plan/ping/audit:** H1, H6, H7, X8, X9, X10
- **Slice H — installer/propagate + console:** X1, X2, X3, X4
- **Slice I — bench + CI:** X6, X11
- **M1** — migration guard + docs (careful, standalone)
- **H4** — operator policy decision (no code until decided)
- **X5** — optional low-residual hardening

Guardrails (from Minni plan discipline): dedicated branch only, never commit/push to main, never merge, keep both suites green each slice (`cd engine && pytest -q`; `npm test --prefix plugins/minni`), evidence-gated (no "done" without machine-verifiable evidence), scope = these findings only.
