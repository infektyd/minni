# Minni Vault Isolation & Mediated Access — Design Spec

- **Date:** 2026-05-30
- **Author:** claude-code (with operator)
- **Status:** Draft for review
- **Branch / worktree:** `feat/minni-vault-isolation` (worktree at `.claude/worktrees/minni-vault-isolation`, based on `rebrand/minni-deep-rename` @ `5333388` — depends on the renamed runtime: `minnid`, `~/.minni`, `engine/principal.py`)
- **Related memory:** `minni-enforcement-in-minnid-not-plugin`, `minni-vault-isolation-wide-open`, `minni-standalone-core-hermes-coupling`

## 1. Goal & Motivation

Make each agent's vault **actually isolated**: an agent reads/writes only its own vault, and any cross-agent knowledge transfer is an explicit, `minnid`-mediated, **audited** transaction — never a direct filesystem grab.

**Why (recorded so no future agent re-litigates):**
- The operator is "tired of agents looking in each other's vaults, or a lazy agent grabbing Claude's vault because they're trained to (look for Claude, copy all that, go to work)."
- The isolation **engine already exists and is tested** (`engine/principal.py`), but it is **deployed wide-open**: every agent aliases to one omniscient `main` with no vault-root scoping. The walls are built; the doors are propped open.
- This is the substrate for the operator's intended future: **knowledge requests with receipts/contracts for multi-agent work / swarms.** Getting the trust boundary right now makes that future additive instead of a rewrite.

## 2. Architecture Decision (settled — see `minni-enforcement-in-minnid-not-plugin`)

1. **Vault lives behind `minnid` ("option 2"), not at the agent ("option 1").** Flow: `agent → plugin → minnid → vault`. Agents never receive a filesystem path.
2. **Enforcement lives in `minnid` ONLY. The plugin is a *link*, not a *guard*.** The plugin is per-agent, runs inside the agent's process, and its env (`MINNI_AGENT_ID`, `MINNI_VAULT_PATH`) is client-side → **untrusted by design**. `minnid` is the single shared server that server-stamps identity and is the only thing that touches the vault.
3. **Obsidian/human access is compatible and not a leak.** The vault is Obsidian markdown under `~/.minni`. The **operator** (`main`) browses it directly in Obsidian — full cross-vault access is the operator's by design. **Agents** only ever get `minnid`-mediated, principal-gated results. Human direct-read ≠ agent direct-read.
4. **No direct cross-agent access, ever — only mediated grants that leave a receipt. The audit log is the contract ledger.**

## 3. Constraints & Hard Facts (verified 2026-05-30)

**The enforcement engine (`engine/principal.py`) — already built + tested:**
- `EffectivePrincipal` is **server-stamped**; "caller-supplied `agent_id` on the wire is NEVER authoritative."
- `resolve_effective_principal(supplied_agent_id, transport)` → stamps identity, raises `IdentityMismatchError` (JSON-RPC `-32000`) on mismatch.
- `allows_vault_root(path)` → **empty `allowed_vault_roots` means allow-everything** (line 98–99). This is the open door.
- `can_read_document(principal, workspace, doc_metadata)` (G19) → the centralized read gate: workspace scope + vault-root containment + privacy + agent-ownership, with a **default-deny** for foreign agents and an intentional cross-visible carve-out for `wiki`/`handoff`/`synthesis`/`decision`/`session` pages.
- Strict mode triggers if any of `CANONICAL_PRINCIPAL_NAMES = ("local","default","operator","main")` files exist under `~/.minni/principals/`.
- Tests already present: `test_principal_binding.py`, `test_vault_root_binding.py`, `test_can_read_document.py`, `test_retrieval_visibility.py`. **TDD against these.**

**The deployed config — why it's wide open:**
- The only principal file, `~/.minni/principals/local.json`, sets `agent_id="main"`, `capabilities=["*"]`, `legacy_agent_ids=[gemini, grok, claude-code, grok-build, kilocode]`, `platform_agent_ids=[codex]`, and **no `allowed_vault_roots`**.
- Effect 1 — **everyone is `main`:** all `legacy_agent_ids` alias to the single `main` operator; with empty `allowed_vault_roots`, `main` reads **every** vault. (Proven: a claude-code session's recall surfaced codex/grok/gemini/kilocode identity envelopes.)
- Effect 2 — **`resolve_effective_principal` stamps ONE canonical principal for the whole daemon.** `from_local_transport` returns the first of `local/default/operator/main` it finds; supplied `agent_id` is only checked *against* that one (match/alias → accept the SAME principal; else reject). **There is currently no mechanism to resolve caller "gemini" → a gemini-scoped principal.** This is the core code gap to close.
- Effect 3 — **Codex is rejected:** `codex` sits in `platform_agent_ids`, a field the resolver does **not** consult (it only checks `legacy_agent_ids`/`aliases`) → `identity_mismatch -32000`.
- `engine/config.py`: `CANONICAL_SOVEREIGN_HOME = ~/.minni` (env `MINNI_HOME`).
- Per-agent vault dirs exist: `~/.minni/{claudecode,codex,gemini,grok-build,grok-beta,kilocode}-vault`.
- Plugin manifests: `.claude-plugin` / `.gemini-plugin` / `.kilocode-plugin` each pin `MINNI_AGENT_ID` + `MINNI_VAULT_PATH`; **`.codex-plugin` has NO env block** → falls through to `config.ts` defaults (`DEFAULT_AGENT_ID`, `DEFAULT_VAULT_PATH → codex-vault`).

**Threat model (state it honestly):**
- All agents on this machine run as the **same macOS uid** as the operator. OS-level isolation between agent processes is therefore **impossible** — any process running as the user can read any file the user can (including 0600 principal files and any vault on disk). So perfect, hostile-process isolation is unachievable; the honest ceiling is **"pseudo-100%"** (operator's own word).
- **The adversary in scope is the *lazy / poorly-trained* agent** that takes the path of least resistance ("find Claude's vault, copy it, go"). It is NOT a malicious process actively exfiltrating (that is unstoppable at the same uid and out of scope).
- Therefore enforcement = **remove the easy path** (no filesystem paths handed to agents) + **default-scope every response to the caller's own vault** + **make every cross-agent transfer mediated and audited**. This defeats the lazy/accidental case, which is the real one.

## 4. Operating Rule (carry-over, do not violate)

- **Daemon DOWN for the whole migration** (operator: "treat it like a patient you're operating on"). Bring `minnid` (`com.minni.minnid`) down before changing principal resolution; bring it up only after all phases + tests pass.
- **No blanket `sed`. Targeted edits only. Never edit `docs/`** describing the rename. (Scar tissue from P1.)
- **TDD:** red test first against the existing principal/visibility tests, then implement.
- Keep the **public boundary clean** (no real name / secrets in committed content; git-guard active).

## 5. Target Model

### 5.1 Per-connection identity resolution (the core code change)
Extend `resolve_effective_principal` so that, on a local transport with a supplied `agent_id`, it resolves to a **per-agent principal file** `~/.minni/principals/<agent_id>.json` (operator-authored, `0600`) and stamps **that** scoped principal — instead of collapsing every caller into the single canonical operator.

- The operator authoring `principals/<id>.json` is the authorization: the id↔scope mapping is pre-declared by the operator, so loading the file that matches the claimed id is trustworthy *for the lazy threat model*.
- **Reserve operator scope.** A normal agent connection must **not** be able to obtain `main`/operator capabilities just by claiming `agent_id="main"`. Operator identity is granted only via the canonical operator file (`local`/`operator`/`main`) **and** a deliberate operator context (see 5.4) — not by an agent's self-declared id.
- Unknown / fileless `agent_id` on the agent path → **default-deny scope** (empty capabilities, no roots), not the wide-open operator.
- Preserve `IdentityMismatchError` semantics for the legitimate mismatch cases; add tests for the new resolution.

### 5.2 Per-agent principal files
One scoped file per agent under `~/.minni/principals/`:

```jsonc
// ~/.minni/principals/gemini.json   (0600)
{
  "agent_id": "gemini",
  "allowed_vault_roots": [
    "/Users/<operator>/.minni/gemini-vault",
    "/Users/<operator>/.minni/shared"          // shared wiki/handoff scope (read)
  ],
  "capabilities": ["search","read","learn","feedback","log_event","handoff","export"]
}
```
- Create for: `claude-code`, `codex`, `gemini`, `grok-build`, `kilocode` (one per real agent; drop `grok-beta` unless still used).
- **Codex gets its own file** (5.2) → fixes the rejection cleanly; retire the `platform_agent_ids` hack in `local.json`.
- Keep exactly **one** operator file (`local.json` → consider renaming to `main.json`/`operator.json` for clarity) with `capabilities:["*"]` and either no roots (full) or all roots explicitly. This is the only cross-vault identity (operator + consolidation/oversight).

### 5.3 Shared scope (intentional cross-visibility)
- Define a single shared root (e.g. `~/.minni/shared/` holding `wiki/`, `handoff/`, `synthesis/`) that every agent's `allowed_vault_roots` includes for **read**. `can_read_document`'s existing page-type carve-out already permits these when the path is within roots and the page isn't private.
- Private/`local-only` docs in another agent's vault remain default-denied (already implemented).

### 5.4 Plugin & transport hygiene (link layer — make correct, still untrusted)
- Fix `.codex-plugin` to include the env block (`MINNI_AGENT_ID=codex`, `MINNI_VAULT_PATH=~/.minni/codex-vault`, `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`).
- Fix the dangerous `config.ts` default: `DEFAULT_AGENT_ID`/`DEFAULT_VAULT_PATH` should be **fail-safe** (unknown agent → minimal/deny), not silently `codex`/`codex-vault`.
- Decide operator-context mechanism for 5.1's "reserve operator scope": options — (a) a dedicated operator socket / CLI path, (b) an operator token in `principals/main.json` injected only into the operator's own tooling, (c) `transport`-based (operator CLI on `stdio`, agents on the shared UDS). Pick the simplest that keeps agents off operator scope. **Open question for review.**

### 5.5 Verify the read path actually gates
- Confirm every read surface (`minnid._handle_search/_handle_read/_handle_expand`, retrieval, handoff wikilink expansion, `agent_api.recall`) constructs the per-request `EffectivePrincipal` and calls `can_read_document` on **every** surfaced doc/chunk/provenance/wikilink. The observed leak implies it currently runs as `main`/empty-roots so nothing is filtered. Add/confirm `test_retrieval_visibility` coverage that a scoped agent gets ONLY its own + shared docs.

## 6. North Star — Receipts / Contracts for swarms (out of scope to build, in scope to not paint into a corner)

The future the structure must stay compatible with: **request → grant → receipt.** Agent A asks `minnid` for a target scope + purpose; `minnid` evaluates a contract/policy; if granted, returns data **and** writes an immutable audit **receipt** (with a `contract_id`) that both agents can cite. Swarm = a team/workspace scope whose members get receipted grants; individual→team promotion is the same receipted transaction.

Seeds that already exist and should be treated as the prototypes to generalize — **not** replaced: `minni_negotiate_handoff` / `ack_handoff`, the audit log (the ledger), `can_read_document`'s shared carve-out, and the `team-*` code (shared scope + promotion).

**Structure rules to honor now so the future is additive:**
- Keep policy/audit/data authority in `minnid`; keep the plugin a thin transport. Any policy-looking plugin code (`src/policy.ts`, `src/handoff_guard.ts`) is advisory/early-UX; the authoritative copy is server-side.
- `can_read_document` is the single gate a future grant will also consult — do not fork read-authorization logic.
- Allow audit entries to carry optional `contract_id` / `receipt` fields (additive schema) so receipts ride the existing ledger.

## 7. Phases (each ≈ one PR, ascending risk; daemon down throughout)

1. **P0 — Reconcile stash + baseline.** Pop/merge the stashed `principal.py` WIP onto this branch; resolve conflicts; green the existing principal/visibility tests as the clean baseline. (Blocker: the stash predates the rename and will conflict.)
2. **P1 — Per-connection resolution (TDD).** Red tests for: caller `gemini` → gemini-scoped principal; unknown id → default-deny; agent claiming `main` does NOT get operator scope; codex no longer rejected. Then implement the `resolve_effective_principal` change.
3. **P2 — Per-agent principal files + shared scope.** Author `principals/<agent>.json` for each agent with scoped roots; create `~/.minni/shared/`; retire the `legacy_agent_ids`/`platform_agent_ids` collapse in the operator file. Migrate existing per-agent vault content if layout changes (copy + checksum-verify, never move — see rename-spec lesson).
4. **P3 — Read-path gating audit.** Confirm `can_read_document` runs on every read surface; add `test_retrieval_visibility` cases proving a scoped agent sees only its own + shared docs.
5. **P4 — Plugin/transport hygiene.** Fix `.codex-plugin` env, fail-safe `config.ts` defaults, decide + implement the operator-context mechanism (5.4).
6. **P5 — Receipts-readiness (additive only).** Add optional `contract_id`/`receipt` audit fields; document the request→grant→receipt RPC shape as the next spec. **No behavior change.**
7. **P6 — Bring daemon up + end-to-end.** Restart `com.minni.minnid`; verify in a fresh session per agent that each sees only its own vault; operator (Obsidian + `main`) still sees all.

## 8. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Stash conflicts on `principal.py` | P0 isolates the reconcile before any new logic; review the diff, don't blind-accept. |
| Over-tightening locks out a working agent | TDD each agent's happy path; P6 fresh-session verification per agent before declaring done. |
| Same-uid limitation oversold as "secure" | Spec states the threat model explicitly; claims are scoped to the lazy/accidental adversary. |
| Shared-scope becomes a backdoor (everything dumped in `shared/`) | Keep `shared/` to wiki/handoff/synthesis only; private docs stay in per-agent vaults; `can_read_document` still privacy-gates. |
| Breaking recall during migration | Daemon down throughout; copy-not-move for any vault relayout; checksum-verify. |
| Operator scope leaking to agents | 5.4 reserves operator scope behind a deliberate operator context, not a self-declared id; P1 test asserts an agent claiming `main` is denied operator caps. |

## 9. Out of Scope (captured follow-ups)
- Building the receipts/contracts RPC and swarm orchestration (separate spec; P5 only makes the ledger ready).
- Decoupling `minni-consolidation` from `~/.hermes` (`minni-standalone-core-hermes-coupling`) — related standalone-core work, tracked separately.
- Hostile-process / cross-uid sandboxing (impossible at same uid; explicitly excluded).
- Merging `rebrand/minni-deep-rename` to `main` (operator's call).

## 10. Success Criteria
- In a fresh session, **each agent's recall returns only its own vault + the shared scope** — no foreign agent identity envelopes leak (the current symptom is gone).
- **Codex works** (no `identity_mismatch`) and is scoped to `codex-vault`.
- An agent that supplies `agent_id="main"` does **not** receive operator/cross-vault access.
- The **operator** (via Obsidian and the operator context) still reads the whole `~/.minni`.
- All existing + new principal/visibility tests pass; daemon healthy after P6.
- Audit entries can carry `contract_id`/`receipt` (schema ready) with no behavior change — the receipts future is unblocked.
