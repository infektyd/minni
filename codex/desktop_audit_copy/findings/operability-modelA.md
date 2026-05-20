# Dimension 6: Operability & Lifecycle (Model A - Gemini 3.5 Flash)

## Operability & Maintenance Capabilities

### 1. Nightly Maintenance and Hygiene Reports
* **File:** [engine/hygiene.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/hygiene.py)
* **Summary:** The `engine/hygiene.py` script provides a read-only maintains routine that generates a detailed report (`logs/hygiene-YYYY-MM-DD.md` and `.json`) checking for:
  - Broken wikilinks
  - Missing sources in frontmatter
  - Status drift (superseded without pointer, rejected pages still linked)
  - Orphaned pages (no incoming links)
  - Invalid frontmatter keys/values
  - Privacy mismatches (safe pages containing words like 'api key', 'secret', etc.)
  - Contradiction occurrences
  - Index/log drift (pages not linked in `index.md`)

### 2. Guardrails and Capacity Limits
* **File:** [engine/test_size_caps_and_sync_warn.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/test_size_caps_and_sync_warn.py)
* **Summary:** The engine implements strict boundaries to prevent denial of service (DoS) and data corruption:
  - **Payload Caps:** Rejecting `learn` payloads $>64$ KiB or `summary` fields $>4$ KiB with RPC code `-32602`.
  - **Socket Read Limits:** 1 MiB `readuntil` limit at the Unix socket level.
  - **Cloud-Sync Warnings:** `_warn_if_sync_root` detects if vault roots are located in folders synchronized by iCloud (`Mobile Documents`) or Dropbox, logging a warning to prevent TOCTOU sync races or file corruption.
