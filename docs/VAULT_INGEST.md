# Vault Ingest

`vault_ingest` is the deterministic AFM maintenance pass that indexes each
agent's Obsidian vault wiki into that vault's own local recall store.

## What It Indexes

- Source: `<agent>-vault/wiki/**/*.md`
- Store: `<agent>-vault/.index/vault.db`
- FAISS cache: `<agent>-vault/.index/vault.faiss` with
  `<agent>-vault/.index/vault.manifest.json`

The pass reads markdown, parses wiki frontmatter, chunks page bodies with the
existing markdown chunker, stores full chunk text and embeddings, and materializes
resolved `[[wikilink]]` edges in the per-vault `memory_links` table.

## What It Does Not Touch

- It does not add rows to the shared `~/.minni/minni.db` `documents` table.
- It does not create a shared-db migration.
- It does not delete, rename, move, rewrite, or normalize vault markdown.
- It does not index the legacy bare `~/.minni/vault` directory.

Pruning means removing stale rows from `<vault>/.index/vault.db` when a previously
indexed vault markdown file no longer exists. It never means deleting vault files.

## Agent Ownership

The owning agent is derived from the vault directory slug using the shared
slug-to-agent map:

- `codex-vault` -> `codex`
- `claudecode-vault` -> `claude-code`
- `gemini-vault` -> `gemini`
- `grok-build-vault` -> `grok-build`
- `kilocode-vault` -> `kilocode`

Unknown `*-vault` slugs are skipped with a warning. The pass does not invent
agent IDs.

## Recall Scoping

When the daemon handles `search` with a stamped caller principal:

- `scope="personal"`: search the caller's per-vault index only. If the caller's
  index does not exist or cannot be read, fall back to the shared legacy
  document layer.
- `scope="combined"`: search the pooled document view: all existing per-vault
  indexes, including the caller's when present, plus the shared legacy document
  layer. Results are merged by score.
- `scope="both"`: default when `scope` is absent. Merge the personal path with
  the combined path, de-duplicating the caller's own hit so it appears once as
  personal. Personal hits rank first on score ties.
- Back-compat: `cross_agent=true` maps the document leg to the
  `combined`-equivalent breadth. `cross_agent=false` or absent maps to the new
  default `both`. This alias does not change learnings recall scoping.
- No principal: preserve old behavior and search only the shared legacy document
  layer.

Each returned document hit carries one compact source marker:

- `src: "p"` means the hit came from the caller's personal path.
- `src: "c"` means the hit came from the combined/shared path.

The recall envelope does not add owning agent IDs, source vault paths, index DB
paths, or expanded score breakdowns inline. Use `minni_drill` / daemon
`sm_drill` with a returned hit reference (`references` / `refs`, or the existing
numeric `resultIds` / `chunkIds`) to retrieve full provenance on demand:
owning agent ID, source vault, index DB path, score components, and `indexed_at`.

Learnings recall still comes from the shared daemon DB. This change is only the
documents/semantic leg.

## Manual Run

From `engine/`:

```bash
python index_all.py --vault-ingest-all
```

Useful variants:

```bash
python index_all.py --vault-ingest-all --dry-run
python index_all.py --vault-ingest-all --verbose
python index_all.py --vault-ingest-all --minni-home /tmp/minni-home
```

The command enumerates `~/.minni/*-vault` directories and runs `vault_ingest`
once per vault. Existing legacy modes remain available:

```bash
python index_all.py
python index_all.py --vault-only
python index_all.py --wiki-only
```

## Shared DB Boundary

The shared `~/.minni/minni.db` keeps its existing legacy `documents`,
`chunk_embeddings`, and FAISS state. Those rows are still used as the fallback
and as the shared leg for `scope="combined"` / `scope="both"`. Agent vault
markdown lives in the per-vault `.index` stores only.
