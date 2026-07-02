# Security Policy

## Reporting a vulnerability

Please report vulnerabilities **privately** via
[GitHub Security Advisories](https://github.com/infektyd/minni/security/advisories/new)
on this repository. Do not open a public issue for a suspected
vulnerability — that discloses it to everyone before there's a fix.

We'll acknowledge the report and follow up as the investigation progresses.

## Supported versions

Minni is **pre-v1**. Only the `main` branch is supported; there are no
released versions to backport fixes to.

## Threat model and known findings

This file is intentionally short. The actual threat model — assets, trust
boundaries, adversaries in and out of scope, and the tracked findings
(`SEC-001` through `SEC-022`) — lives in [`SECURITY_PLAN.md`](SECURITY_PLAN.md)
at the repo root. Read that document for what "secure" means for this
project and what's already known and tracked.
