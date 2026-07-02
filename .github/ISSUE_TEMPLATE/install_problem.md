---
name: Install / setup problem
about: make setup, the daemon, or platform wiring isn't working
title: ""
labels: install
assignees: ""
---

## What you were trying to do

e.g. `make setup`, `make daemon`, wiring a specific runtime via
`propagate.py update-plugin --platform ...`.

## What happened

The exact error or unexpected behavior. Paste full command output where
possible.

```
paste here
```

## Environment

- OS (and version):
- `python3 --version`:
- `node --version`:
- `make doctor` output (if available — `make doctor` is being added
  separately; skip this if your checkout doesn't have it yet):

```
paste here
```

## What you've already tried

Any steps you've taken to narrow this down (e.g. `rm -rf engine/.venv` and
re-running `make setup`, checking `.python-version`/`.nvmrc` against your
installed versions, etc.).

## Additional context

Anything else relevant — custom `MINNI_HOME`, non-default socket path,
corporate proxy/firewall, etc.
