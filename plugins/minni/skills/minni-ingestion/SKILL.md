---
name: minni-ingestion
description: "Ingest content into Minni: auto-index new docs via Apple FM fact_extract into ~/wiki/auto-indexed/, and ingest LLM Wiki pages with agent-optimized enrichment (frontmatter metadata, wikilink graph extraction, source provenance tagging). Use when setting up, maintaining, or running the auto-indexing pipeline or wiki-aware Minni ingestion."
tags: [openclaw, memory, wiki, faiss, sqlite, rag, knowledge-graph]
version: 1
---

# Minni Ingestion

Two complementary ingestion paths that feed content into Minni:

1. **Auto-indexing** — scans new content (specs, docs, conversations, identities) every 30 min via cron, extracts facts using Apple Foundation Models, and writes to `~/wiki/auto-indexed/` where Minni auto-picks them up.
2. **Wiki ingestion** — ingests LLM Wiki pages with agent-optimized enrichment: frontmatter metadata baked into chunk heading context, `[[wikilinks]]` extracted as graph edges in `memory_links`, and source provenance tagging.

---

## Part 1: Auto-Indexing Pipeline

### The Chain

```
New content (specs, docs, conversations, identities)
    ↓ (every 30 min, cron job: minni-auto-index)
Apple Foundation Models fact_extract (on-device, M4 Neural Engine, no API)
    ↓
Write to ~/wiki/auto-indexed/
    ↓
Minni auto-indexes (already working)
```

### What Was Broken

Minni auto-indexes when it detects new docs in `~/wiki` — that part works. The missing link was **nothing wrote to `~/wiki` automatically**. Specs, conversations, and decisions stayed in their original locations and never got extracted.

### Cron Job

- Name: `minni-auto-index`
- Frequency: Every 30 minutes
- Scans: `~/.openclaw/project-console/`, `~/.hermes/hermes-agent/`, `~/.openclaw/identities/`
- Extracts facts via `fact_extract.swift` (Apple Foundation Models, local, on-device)
- Writes structured output to `~/wiki/auto-indexed/`

### fact_extract Tool

- Located: `~/.hermes/skills/apple/foundation-models-extraction/fact_extract` (Mach-O binary, not Swift source)
- Uses M4 Neural Engine — no API calls, no network
- Extracts structured facts from text as JSON: `{"facts": [...], "chunksProcessed": N}`
- Fast: 4-5 facts in under a second
- **Limitation:** Very short files (<2KB, dense bullet-format like IDENTITY.md) may produce 0 facts. Manual extraction needed as fallback.

### Scan Targets & Commands

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

### Large File Warning

AGENTS.md (35KB+) causes `fact_extract` to run for >300 seconds when called via `execute_code`. Use `terminal` directly with a 60s timeout for files over 20KB. If it times out, skip and rely on prior index.

### Timestamp-Aware Deduplication

A deduplication check by source path alone is not enough. Also verify the index file's mtime is **newer** than the source file's mtime:

```bash
file_mtime=$(stat -f '%m' "$source_file")
idx_mtime=$(stat -f '%m' "$index_file")
if [ "$idx_mtime" -gt "$file_mtime" ]; then
  echo "Already indexed (index is newer than source)"
fi
```

This avoids re-indexing files whose content hasn't changed since the last index, even if the `find -mmin` window catches them.

### Deduplication (critical)

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

### Exclusion Patterns (applied via grep)

Skip these low-value boilerplate files — they're generated documentation that mirrors actual skill files:
- `node_modules/` — 100+ README/CHANGELOG files, zero value
- `website/docs/` — auto-generated docs site from Docusaurus, mirrors source files
- `google-ai-edge-gallery/` — external gallery content (indexed separately if needed)

Apply all three in one grep chain:
```bash
find ~/.hermes/hermes-agent/ -name '*.md' -mmin -120 -type f | grep -v node_modules | grep -v 'website/docs/' | grep -v 'google-ai-edge-gallery/'
```

These directories alone account for 130+ of the ~154 modified files and produce redundant facts that add zero value.

### fact_extract Output Format

Returns JSON: `{"chunksProcessed": N, "facts": [{"content": "...", "confidence": 0.7, "ttlTier": "1h", "type": "fact"}]}`

Parse the JSON and count facts with `len(data.get('facts', []))` — don't count `###` headers or similar.

### Naming Convention

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

### Auto-Index Output Format

- Write to `~/wiki/auto-indexed/<sanitized-path>.md`
- Include: source path, timestamp, fact count, extracted facts list
- Minni auto-picks up files in `~/wiki/`
- Each fact: `### N. [TYPE] (confidence: X, TTL: tier)\n\n{content}\n\n`

---

## Part 2: Wiki Ingestion

Ingest LLM Wiki pages into Minni V3.1 with agent-optimized enrichment.
Each wiki page gets: frontmatter metadata baked into chunk heading context,
`[[wikilinks]]` extracted as graph edges in `memory_links`, and source provenance tagging.

### Architecture

```
~/wiki/ (LLM Wiki)
  ├── entities/*.md    → wiki:entity
  ├── concepts/*.md    → wiki:concept
  ├── decisions/*.md   → wiki:decision
  ├── projects/*.md    → wiki:project
  ├── queries/*.md     → wiki:query
  └── (meta files)     → wiki:meta

~/.openclaw/minni-v3.1/
  ├── wiki_indexer.py   ← NEW: wiki-aware indexer
  ├── index_all.py      ← NEW: unified vault+wiki indexer
  ├── config.py         ← MODIFIED: wiki_paths field
  ├── retrieval.py      ← MODIFIED: FTS5 hyphen fix
  └── db.py             ← Schema: memory_links FK → documents
```

### Key Files

#### wiki_indexer.py
- `WikiPageParser` — parses frontmatter (YAML) + extracts `[[wikilinks]]`
- `WikiIndexer` — two-phase indexing:
  - Phase 1: Index all pages (content + chunks + FTS), queue wikilinks
  - Phase 2: Resolve wikilinks into `memory_links` (all pages are in DB)
- Enriched heading context: `[wiki:concept | tags: architecture, cognitive | Cognitive Architecture (Tri-Brain)]`

#### index_all.py
- `index_all(vault=True, wiki=True, verbose=False)` — runs VaultIndexer + WikiIndexer, then rebuilds FAISS
- CLI: `python index_all.py [--vault-only|--wiki-only|--verbose]`

### Usage

```bash
cd ~/.openclaw/minni-v3.1
source venv/bin/activate

# Index everything
python sovereign_memory.py index

# Index only wiki
python sovereign_memory.py index --wiki-only

# Query (searches both vault + wiki)
python sovereign_memory.py query "syntra cognitive architecture" --limit 5

# Agent context
python agent_api.py hermes --context
```

### Agent-Optimized Features

#### Heading Context Enrichment
Each chunk's heading breadcrumb includes wiki metadata:
```
[wiki:entity | tags: project, architecture, framework, swift | Syntra] What It Is > Overview
```
This helps agents understand what KIND of knowledge they're reading:
- `wiki:concept` → theoretical knowledge
- `wiki:entity` → specific thing (person, project, org)
- `wiki:decision` → recorded choice with rationale
- `wiki:project` → active project status

#### Wikilink Graph Edges
`[[wikilinks]]` are resolved into `memory_links` table entries:
- `source_doc_id → target_doc_id` with `link_type='wikilink'`
- Two-phase resolution: index all pages FIRST, then resolve links
- Graph traversal: `SELECT * FROM memory_links WHERE source_doc_id = ?`

#### Source Provenance
Documents tagged as `agent='wiki:entity'`, `agent='wiki:concept'`, etc.
Agents can filter: `WHERE agent LIKE 'wiki:%'` for wiki-only results.

### Config

In `config.py`:
```python
wiki_paths: list = field(default_factory=lambda: [
    os.path.expanduser("~/wiki"),
])
```

### Testing

```bash
cd ~/.openclaw/minni-v3.1
source venv/bin/activate

# Full re-index
python index_all.py --verbose

# Verify wikilinks resolved
python -c "
from db import SovereignDB
db = SovereignDB()
with db.cursor() as c:
    c.execute('SELECT COUNT(*) FROM memory_links WHERE link_type = \"wikilink\"')
    print(f'Wikilinks: {c.fetchone()[0]}')
    c.execute(\"SELECT COUNT(*) FROM documents WHERE agent LIKE 'wiki:%'\")
    print(f'Wiki docs: {c.fetchone()[0]}')
"

# Test recall
python sovereign_memory.py query "syntra cognitive architecture" --limit 3
```

---

## Pitfalls

### Auto-Indexing

- Very short files (<2KB dense bullet format) may produce 0 facts — use manual fallback.
- Use `terminal` for files >20KB (>300s timeout via `execute_code`).
- Deduplication is critical: always check both source path and mtime.
- `-mmin -120` catches bulk copies; tighten to `-mmin -30` if noisy.

### Wiki Ingestion

#### FK Constraint: memory_links → documents
The `memory_links` table MUST have FK references to `documents` table, NOT `document_metadata`.
If you see `FOREIGN KEY constraint failed` on inserts, check:
```python
cursor.execute("PRAGMA foreign_key_list(memory_links)")
# Should show: target_doc_id -> documents.doc_id
# NOT:         target_doc_id -> document_metadata.doc_id
```
Fix: `DROP TABLE memory_links` and let `db.py` recreate it with correct schema.

#### Wikilink Resolution: Two-Phase Required
You CANNOT resolve `[[wikilinks]]` during the same loop that indexes pages.
Target pages might not exist in DB yet when source page is processed.
Solution: Phase 1 queues `(doc_id, wikilinks)`, Phase 2 resolves after all pages are indexed.

#### FTS5 Hyphen Handling
FTS5 treats `-` as a NOT operator. `multi-agent` becomes `multi NOT agent` → syntax error.
Fix in `retrieval.py` `_sanitize_fts_query`: replace ALL non-word chars including hyphens with spaces:
```python
cleaned = re.sub(r'[^\w\s]', ' ', query)  # removes hyphens
```

#### Schema Migrations
If the DB was created by an older version of Minni, columns may be missing.
Always check: `PRAGMA table_info(memory_links)` for expected columns.
Use `ALTER TABLE` for additive changes, `DROP TABLE` for structural changes.

#### Frontmatter Parsing
No PyYAML dependency — uses simple line-by-line parser.
Handles: `key: value`, `[item1, item2]`, `"quoted values"`.
Does NOT handle nested YAML or multiline values.

## Related Skills

- `minni-engine` — Development reference for the Minni engine pipelines
