# Security model

The current threat model is
[`contracts/THREAT_MODEL.md`](contracts/THREAT_MODEL.md) (trust layers, assets,
adversaries, residual stories, and criticality calibration), with
[`contracts/THREAT_MODEL_SCANNER.md`](contracts/THREAT_MODEL_SCANNER.md) as its
dense four-section skeleton for PR / cloud security agents.
[`SECURITY_PLAN.md`](archive/SECURITY_PLAN.md) is the findings ledger from the
v0.2 hardening pass (SEC-001…SEC-022, with their fixes) — archived as a
point-in-time record, not current guidance. Report vulnerabilities per
[`SECURITY.md`](../SECURITY.md) — privately, via GitHub Security Advisories.

This page is the orientation layer: what the local-first boundary actually
means in code. For the full multi-layer threat model (same-uid FS vs socket vs
daemon RPC vs plugin-local writes vs optional HTTP), read the threat model
contract first.

## Local-first boundaries

- The daemon listens on a **Unix socket** (`~/.minni/run/minnid.sock`, intended
  mode 0600 inside a 0700 run dir — SEC-001); there is no TCP listener by
  default. Socket `chmod` is best-effort (warn-and-continue); there is no
  peer-credential check — see the threat model.
- Vaults, per-vault `.index` stores, and the shared daemon DB are local
  filesystem paths. Nothing syncs anywhere. Same-uid processes can open those
  files directly; daemon authz does not apply on that plane.
- The localhost React web console (`ui-server` on `127.0.0.1:8765`) was retired
  in favor of CLI (`minni status`, `minni activity`, `minni watch`, `minni doctor`),
  the mode-`0600` symlink-guarded `~/.minni/TODAY.md` digest, and plain-Markdown
  vault status surfaces; no loopback web server ships on `main`.
- Optional daemon HTTP (`--port`) is off by default; enabling it is a Critical
  surface (no auth, unbounded body, local-operator stamp) — not mere
  configuration risk. See the threat model.
- Non-loopback model targets require explicit allowlisting **and** HTTPS.
- `~/.minni/providers.json` rejects inline cloud API keys; secrets resolve
  only from environment variables or 0600 files under the Minni secrets dir.

## Identity and capability gating

Every **daemon-mediated** durable write and cross-agent operation passes the
server-stamped `EffectivePrincipal` gate — identity is resolved daemon-side,
so a caller cannot claim capabilities it doesn't have. Candidate resolution is
owner-or-explicit-operator; the `force=true` durable-learn escape is
operator-only and audit-stamped (`FORCE_DURABLE_LEARN`).

The gate is **not** universal, and it is honest to say so. Local vault-note
and audit writes — `vaultFirstLearn`/`writeVaultPage`/`recordAudit` in
`plugins/minni/src/vault.ts`, which back the personal vault-note half of
governed `minni_learn` (while durable shared storage goes through daemon
`learn` / `minni_resolve_candidate` vault writeback), `minni_recall`'s audit
entry, and all hook-side vault/audit/inbox writes — are local-first filesystem
writes that never reach daemon dispatch and never pass this gate. Their
safety derives from the schema-pinned write target (the operator-controlled
agent id and vault path — SEC-003/G11/G12), not from the shared gate. On the
read side, daemon `search`, `read`, `minni_drill` (`sm_drill`/`expand`), and
`project_shelf` enforce principal, workspace, and `privacy_level` (`private`,
`local-only`, `blocked`) read authorization. See the
[provenance-gate findings](implementation/minni-provenance-gate-ground-and-resolve-findings.md)
for the grounded file-by-file split.

Two consequences worth knowing when auditing the trust boundary:

- **`gate.shared` is attribution, not authorization.** The daemon's
  `gate.shared` handler resolves the caller principal and returns a stamped
  envelope for provenance; it never performs (or blocks) the vault write
  itself.
- **Daemon-down behavior is asymmetric.** Gated tools (`candidates.list`,
  `candidates.resolve`, `audit.status`, `handoff.send`, `handoff.inbox`, and
  `plan.create`/`status`/`update`/`scar`) fail closed — they return structured
  `gate-unavailable` / `gate-rejected` payloads when the gate is unavailable —
  while the ungated local-write paths above proceed and write the caller's own
  pinned vault.

## Memory-poisoning defenses

- Recall is **evidence, not instruction**: results ship in an evidence
  envelope with provenance, and instruction-like stored content is detected
  and reversibly perturbed at the data layer before reaching a prompt.
- Learning is proposal-first on the default `learn` path: durable writes need
  an approval decision (see [concepts](concepts.md#the-four-verbs)). Exception:
  when `MINNI_AFM_LOOP` is enabled, consolidation may auto-promote *trusted*
  safe candidates (`resolved_by=afm-consolidation`) — inbox ingest and
  operator/govern staging only; learn-only `stage_candidate` clamps privacy to
  `review`. See the threat model.
- Health reporting is redacted to aggregate counts for non-operator callers.
  Secret redaction elsewhere is best-effort: keyword assignment + JSON-quoted
  labels, common bare prefixes (`sk-`/`ghp_`/`github_pat_`/`AKIA`/`xox*`), PEM,
  and common absolute paths — see `docs/contracts/POLICY.md` §2. Unknown-prefix
  high-entropy blobs and non-listed path layouts may still slip through.

## Audit trail

Learning, handoff, vault writes, and hooks leave vault audit entries;
candidate resolution records terminal database status and daemon log output.
