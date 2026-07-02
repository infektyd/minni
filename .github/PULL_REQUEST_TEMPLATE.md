## What

What does this PR change? Keep it to one concern (see `CONTRIBUTING.md`).

## Why

Why is this change needed? Link an issue if there is one.

## What I verified

- [ ] `make check` passes locally
- [ ] `make smoke` passes locally (if the daemon or its startup path is
      touched)
- [ ] Manually exercised the change (describe how, if not covered by the
      above)

```
paste relevant command output here
```

## Memory firewall

- [ ] This PR does **not** change memory storage, retrieval, scoring, or
      governance logic — under `engine/` or in the plugin's recall/privacy/
      model-facing-context gates (see the "memory firewall" section of
      `CONTRIBUTING.md`).
- [ ] This PR **does** touch that surface, and it was discussed with a
      maintainer first — link the issue/discussion: ...

## Additional context

Anything a reviewer should know: follow-ups you intentionally left out,
trade-offs, screenshots, etc.
