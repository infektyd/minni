# Security model

The authoritative documents are [`SECURITY_PLAN.md`](../SECURITY_PLAN.md)
(the tracked findings, SEC-001…SEC-022, with their fixes) and
[`contracts/THREAT_MODEL.md`](contracts/THREAT_MODEL.md) (assets, trust
boundaries, adversaries in and out of scope). Report vulnerabilities per
[`SECURITY.md`](../SECURITY.md) — privately, via GitHub Security Advisories.

This page is the orientation layer: what the local-first boundary actually
means in code.

## Local-first boundaries

- The daemon listens on a **Unix socket** (`~/.minni/run/minnid.sock`, mode
  0600 inside a 0700 run dir — SEC-001); there is no TCP listener by default.
- Vaults, per-vault `.index` stores, and the shared daemon DB are local
  filesystem paths. Nothing syncs anywhere.
- The local console/API surface binds to **loopback only** by default; an
  optional configured bearer token is enforced fail-closed; deep-research is
  opt-in (`MINNI_CONSOLE_DEEP_RESEARCH=1`).
- Non-loopback model targets require explicit allowlisting **and** HTTPS.
- `~/.minni/providers.json` rejects inline cloud API keys; secrets resolve
  only from environment variables or 0600 files under the Minni secrets dir.

## Identity and capability gating

Every durable write and cross-agent operation passes the server-stamped
`EffectivePrincipal` gate — identity is resolved daemon-side, so a caller
cannot claim capabilities it doesn't have. Candidate resolution is
owner-or-explicit-operator; the `force=true` durable-learn escape is
operator-only and audit-stamped (`FORCE_DURABLE_LEARN`).

## Memory-poisoning defenses

- Recall is **evidence, not instruction**: results ship in an evidence
  envelope with provenance, and instruction-like stored content is detected
  and reversibly perturbed at the data layer before reaching a prompt.
- Learning is proposal-first: nothing external writes durable memory without
  an approval decision (see [concepts](concepts.md#the-four-verbs)).
- Health reporting is redacted to aggregate counts for non-operator callers.

## Audit trail

Learning, handoff, vault writes, and hooks leave vault audit entries;
candidate resolution records terminal database status and daemon log output.
