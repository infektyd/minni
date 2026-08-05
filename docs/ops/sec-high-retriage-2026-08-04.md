# Security HIGH re-triage (local) — 2026-08-04

Source: `evidence/codex-findings-triage-2026-07-02.md`  
Tip: main local `50cc719` (origin/main `dc80ffa` + residual stack)  
Method: explore agent re-check anchors only (no bulk fix PR)

## Result

**All 11 original HIGH items re-triage as FIXED.** No HIGH local fix required from that list.

| ID | Was | Now | Anchor |
|----|-----|-----|--------|
| P1 AFM compile write | HIGH | FIXED | `afm.py` operator gate |
| P2/P5 self-approve | HIGH | FIXED | `governance.py` resolve |
| P3 no-agent → main | HIGH | FIXED | `principal.py` strict |
| R4 wikilink leak | HIGH | FIXED | `retrieval.py` can_read |
| R5 contradictions leak | HIGH | FIXED | learn path agent_id |
| M2 page_type spoof | HIGH | FIXED | forced learning type |
| I1/I2 NULL privacy | HIGH | FIXED | consolidation privacy |
| M1 mig 013 | HIGH | FIXED | gated migration |
| X1 mcp wildcard | HIGH | FIXED | RO grants + strip |
| X4 console open | HIGH→MED | FIXED | ui-server auth |
| X5 vaultPath redirect | HIGH→LOW | FIXED | schema strip |

## Residual (not this HIGH set)

- POLICY §2 still PARTIAL for unknown-prefix high-entropy blobs and bare adapter/plist names; JSON-quoted + `/home` + common bare prefixes (`sk-`/`ghp_`/…) landed local 2026-08-04 (`34cdbe1`)
- MED/other codex items: re-open only if operator prioritizes
- Pure `gemini` wire remains provisional (`GEMINI_PROVISIONAL_REASON` — extension-manifest OQ8); day-to-day = antigravity

## Campaign closed (orthogonal)

- #237 redaction honesty, #261 deploy honesty, multi-host matrix — closed on main
