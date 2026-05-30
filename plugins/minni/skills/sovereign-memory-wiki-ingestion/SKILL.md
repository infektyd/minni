---
name: sovereign-memory-wiki-ingestion
description: "Ingest LLM Wiki pages into Sovereign Memory with agent-optimized enrichment: frontmatter metadata, wikilink graph extraction, source provenance tagging. Use when setting up or maintaining wiki-aware sovereign memory."
tags: [openclaw, memory, wiki, faiss, sqlite, rag, knowledge-graph]
---

# Sovereign Memory — Wiki Ingestion

Ingest LLM Wiki pages into Sovereign Memory V3.1 with agent-optimized enrichment.
Each wiki page gets: frontmatter metadata baked into chunk heading context,
`[[wikilinks]]` extracted as graph edges in `memory_links`, and source provenance tagging.

## Architecture

```
~/wiki/ (LLM Wiki)
  ├── entities/*.md    → wiki:entity
  ├── concepts/*.md    → wiki:concept
  ├── decisions/*.md   → wiki:decision
  ├── projects/*.md    → wiki:project
  ├── queries/*.md     → wiki:query
  └── (meta files)     → wiki:meta

~/.openclaw/sovereign-memory-v3.1/
  ├── wiki_indexer.py   ← NEW: wiki-aware indexer
  ├── index_all.py      ← NEW: unified vault+wiki indexer
  ├── config.py         ← MODIFIED: wiki_paths field
  ├── retrieval.py      ← MODIFIED: FTS5 hyphen fix
  └── db.py             ← Schema: memory_links FK → documents
```

## Key Files

### wiki_indexer.py
- `WikiPageParser` — parses frontmatter (YAML) + extracts `[[wikilinks]]`
- `WikiIndexer` — two-phase indexing:
  - Phase 1: Index all pages (content + chunks + FTS), queue wikilinks
  - Phase 2: Resolve wikilinks into `memory_links` (all pages are in DB)
- Enriched heading context: `[wiki:concept | tags: architecture, cognitive | Cognitive Architecture (Tri-Brain)]`

### index_all.py
- `index_all(vault=True, wiki=True, verbose=False)` — runs VaultIndexer + WikiIndexer, then rebuilds FAISS
- CLI: `python index_all.py [--vault-only|--wiki-only|--verbose]`

## Usage

```bash
cd ~/.openclaw/sovereign-memory-v3.1
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

## Agent-Optimized Features

### Heading Context Enrichment
Each chunk's heading breadcrumb includes wiki metadata:
```
[wiki:entity | tags: project, architecture, framework, swift | Syntra] What It Is > Overview
```
This helps agents understand what KIND of knowledge they're reading:
- `wiki:concept` → theoretical knowledge
- `wiki:entity` → specific thing (person, project, org)
- `wiki:decision` → recorded choice with rationale
- `wiki:project` → active project status

### Wikilink Graph Edges
`[[wikilinks]]` are resolved into `memory_links` table entries:
- `source_doc_id → target_doc_id` with `link_type='wikilink'`
- Two-phase resolution: index all pages FIRST, then resolve links
- Graph traversal: `SELECT * FROM memory_links WHERE source_doc_id = ?`

### Source Provenance
Documents tagged as `agent='wiki:entity'`, `agent='wiki:concept'`, etc.
Agents can filter: `WHERE agent LIKE 'wiki:%'` for wiki-only results.

## Config

In `config.py`:
```python
wiki_paths: list = field(default_factory=lambda: [
    os.path.expanduser("~/wiki"),
])
```

## Pitfalls

### FK Constraint: memory_links → documents
The `memory_links` table MUST have FK references to `documents` table, NOT `document_metadata`.
If you see `FOREIGN KEY constraint failed` on inserts, check:
```python
cursor.execute("PRAGMA foreign_key_list(memory_links)")
# Should show: target_doc_id -> documents.doc_id
# NOT:         target_doc_id -> document_metadata.doc_id
```
Fix: `DROP TABLE memory_links` and let `db.py` recreate it with correct schema.

### Wikilink Resolution: Two-Phase Required
You CANNOT resolve `[[wikilinks]]` during the same loop that indexes pages.
Target pages might not exist in DB yet when source page is processed.
Solution: Phase 1 queues `(doc_id, wikilinks)`, Phase 2 resolves after all pages are indexed.

### FTS5 Hyphen Handling
FTS5 treats `-` as a NOT operator. `multi-agent` becomes `multi NOT agent` → syntax error.
Fix in `retrieval.py` `_sanitize_fts_query`: replace ALL non-word chars including hyphens with spaces:
```python
cleaned = re.sub(r'[^\w\s]', ' ', query)  # removes hyphens
```

### Schema Migrations
If the DB was created by an older version of sovereign memory, columns may be missing.
Always check: `PRAGMA table_info(memory_links)` for expected columns.
Use `ALTER TABLE` for additive changes, `DROP TABLE` for structural changes.

### Frontmatter Parsing
No PyYAML dependency — uses simple line-by-line parser.
Handles: `key: value`, `[item1, item2]`, `"quoted values"`.
Does NOT handle nested YAML or multiline values.

## Testing

```bash
cd ~/.openclaw/sovereign-memory-v3.1
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
