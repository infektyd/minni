# Security Policy

## Reporting a vulnerability

Please report vulnerabilities **privately** via
[GitHub Security Advisories](https://github.com/infektyd/minni/security/advisories/new)
on this repository. Do not open a public issue for a suspected
vulnerability — that discloses it to everyone before there's a fix.

We'll acknowledge the report and follow up as the investigation progresses.

## Supported versions

Minni is **pre-v1**, with published releases including
[0.5.0](https://pypi.org/project/minni/0.5.0/). Security maintenance currently targets
the `main` branch; older release lines have no promised backport support. A
published package may lag fixes on `main`, even when its version string matches
the checkout. Check the relevant fix commit and release artifact before assuming
a deployed installation includes a security correction.

## Threat model and known findings

This file is intentionally short. The current threat model — assets, trust
boundaries, adversaries in and out of scope, and residual stories — lives in
[`docs/contracts/THREAT_MODEL.md`](docs/contracts/THREAT_MODEL.md). The
tracked findings from the v0.2 hardening pass (`SEC-001` through `SEC-022`,
with their fixes) are the findings ledger in
[`SECURITY_PLAN.md`](docs/archive/SECURITY_PLAN.md), archived as a
point-in-time record. Read both for what "secure" means for this project and
what's already known and tracked.
