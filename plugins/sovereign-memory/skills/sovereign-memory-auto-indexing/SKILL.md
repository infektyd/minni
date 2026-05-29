---
name: sovereign-memory-auto-indexing
description: Auto-indexing pipeline — new content → Apple FM fact_extract → ~/wiki/ → Sovereign Memory
version: 1
---

# Sovereign Memory Auto-Indexing Pipeline

## The Chain

```
New content (specs, docs, conversations, identities)
    ↓ (every 30 min, cron job: sovereign-memory-auto-index)
Apple Foundation Models fact_extract (on-device, M4 Neural Engine, no API)
    ↓
Write to ~/wiki/auto-indexed/
    ↓
Sovereign Memory auto-indexes (already working)
```

## What Was Broken
Sovereign Memory auto-indexes when it detects new docs in ~/wiki — that part works. The missing link was **nothing wrote to ~/wiki automatically**. Specs, conversations, and decisions stayed in their original locations and never got extracted.

## Cron Job
- Name: `sovereign-memory-auto-index`
- Frequency: Every 30 minutes
- Scans: `~/.openclaw/project-console/`, `~/.hermes/hermes-agent/`, `~/.openclaw/identities/`
- Extracts facts via `fact_extract.swift` (Apple Foundation Models, local, on-device)
- Writes structured output to `~/wiki/auto-indexed/`

## fact_extract Tool
- Located: `~/.hermes/skills/apple/foundation-models-extraction/fact_extract` (Mach-O binary, not Swift source)
- Uses M4 Neural Engine — no API calls, no network
- Extracts structured facts from text as JSON: `{"facts": [...], "chunksProcessed": N}`
- Fast: 4-5 facts in under a second
- **Limitation:** Very short files (<2KB, dense bullet-format like IDENTITY.md) may produce 0 facts. Manual extraction needed as fallback.

## Scan Targets & Commands

Scan for files modified in last 2 hours (-mmin -120):
```bash
# Project console markdown (~/.openclaw/project-console/)
find ~/.openclaw/project-console/ -name "*.md" -mmin -120 -type f

# Hermes agent markdown (~/.hermes/hermes-agent/) — exclude node_modules and website/docs (bulk-copy artifacts)
find ~/.hermes/hermes-agent/ -name "*.md" -mmin -120 -type f | grep -v node_modules | grep -v 'website/docs/'

# Identity files (~/.openclaw/identities/)
for d in ~/.openclaw/identities/*/; do
  for f in IDENTITY.md SOUL.md; do
    [ -f "$d/$f" ] && find "$d/$f" -mmin -120 -type f
  done
done
```

**Critical:** The `-mmin -120` flag catches bulk-updated files (e.g., git checkouts, rsync) that haven't actually changed content. If a large batch of files all have the same mtime within the last 2 hours, they're likely a bulk copy, not real edits. Consider tightening to `-mmin -30` for less noise, or cross-reference file contents with git diff to confirm real changes.

## Large File Warning

AGENTS.md (35KB+) causes `fact_extract` to run for >300 seconds when called via `execute_code`. Use `terminal` directly with a 60s timeout for files over 20KB. If it times out, skip and rely on prior index.

## Timestamp-Aware Deduplication

A deduplication check by source path alone is not enough. Also verify the index file's mtime is **newer** than the source file's mtime:

```bash
file_mtime=$(stat -f '%m' "$source_file")
idx_mtime=$(stat -f '%m' "$index_file")
if [ "$idx_mtime" -gt "$file_mtime" ]; then
  echo "Already indexed (index is newer than source)"
fi
```

This avoids re-indexing files whose content hasn't changed since the last index, even if the `find -mmin` window catches them.

## Deduplication (critical)
```python
# Parse existing indexed files for their Source lines
already_indexed = set()
for f in os.listdir('~/wiki/auto-indexed/'):
    if not f.endswith('.md'):
        continue
    with open(f) as fh:
        for line in fh:
            if line.startswith('**Source:** `'):
                already_indexed.add(line.split('`')[1])
                break

# Skip already-indexed sources
new_files = [f for f in modified_files if f not in already_indexed]
```

Without deduplication, a single `find` across hermes-agent/ returns 150+ files and most are already indexed from prior runs.

## Exclusion Patterns (applied via grep)

Skip these low-value boilerplate files — they're generated documentation that mirrors actual skill files:
- `node_modules/` — 100+ README/CHANGELOG files, zero value
- `website/docs/` — auto-generated docs site from Docusaurus, mirrors source files
- `google-ai-edge-gallery/` — external gallery content (indexed separately if needed)

Apply all three in one grep chain:
```bash
find ~/.hermes/hermes-agent/ -name '*.md' -mmin -120 -type f | grep -v node_modules | grep -v 'website/docs/' | grep -v 'google-ai-edge-gallery/'
```

These directories alone account for 130+ of the ~154 modified files and produce redundant facts that add zero value.

## fact_extract Output Format

Returns JSON: `{"chunksProcessed": N, "facts": [{"content": "...", "confidence": 0.7, "ttlTier": "1h", "type": "fact"}]}`

Parse the JSON and count facts with `len(data.get('facts', []))` — don't count `###` headers or similar.

## Naming Convention

Output files use this convention:
- Replace `/` with `_` in the source path
- Strip leading `_` if present, prepend `_` for consistency with existing files
- Single `.md` extension (NOT `.md.md` — avoid appending `.md` to paths that already end in `.md`)

Example: `/Users/hansaxelsson/.hermes/hermes-agent/foundation_models_research.md` → `_Users_hansaxelsson_.hermes_hermes-agent_foundation_models_research.md`

```python
name = filepath.replace('/', '_')
name = name.lstrip('_')
output_file = f"~/wiki/auto-indexed/_{name}.md"
```

## Output Format
- Write to `~/wiki/auto-indexed/<sanitized-path>.md`
- Include: source path, timestamp, fact count, extracted facts list
- Sovereign Memory auto-picks up files in ~/wiki/
- Each fact: `### N. [TYPE] (confidence: X, TTL: tier)\n\n{content}\n\n`
