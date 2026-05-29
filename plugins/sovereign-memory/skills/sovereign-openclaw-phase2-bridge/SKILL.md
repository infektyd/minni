---
name: sovereign-openclaw-phase2-bridge
description: "Implement Phase 2 of the Sovereign Memory → OpenClaw plugin bridge — chunking strategy (512 tokens), metadata schema, layer filtering, and per-request agent routing."
category: openclaw
tags: [openclaw, sovereign-memory, plugin, chunking, metadata, layer-filtering, typescript, python]
---

# Sovereign OpenClaw Phase 2 Bridge Implementation

Implements chunking strategy, metadata schema, and layer filtering for the Sovereign Memory daemon (`sovrd`) and the OpenClaw adapter (`sovereign-memory` plugin).

## Architecture Overview

```
OpenClaw Agent → SovereignMemoryManager (TS) → bridge.ts → /tmp/sovereign.sock → sovrd.py (Python) → SovereignAgent (engine)
```

### Key Components

| Component | Location | Phase 2 Changes |
|-----------|----------|-----------------|
| Chunker config | `~/.openclaw/sovereign-memory-v3.1/config.py` | 512 tokens, 128 overlap, min 64, max 1024, sentence snap |
| Chunker | `~/.openclaw/sovereign-memory-v3.1/chunker.py` | Sentence snapping, code preservation, min/max filtering |
| Daemon | `~/.openclaw/plugins/sovereign-memory/sovrd.py` | Per-request agent_id, layer filtering, content-hash dedup |
| Bridge (TS) | `~/.openclaw/plugins/sovereign-memory/src/bridge.ts` | layer + workspace_id params on recall/learn |
| Manager (TS) | `~/.openclaw/plugins/sovereign-memory/src/sovereign-manager.ts` | workspaceId constructor, layer filter rules |
| Types | `~/.openclaw/plugins/sovereign-memory/src/types.ts` | MemoryLayer type, ChunkMetadata schema, LAYER_FILTER_RULES |

## Chunking Strategy (Phase 2)

### Config (`config.py`)

```python
chunk_size: int = 512              # Target tokens (word approximation)
chunk_overlap: int = 128           # 25% overlap
min_tokens: int = 64               # Drop undersized chunks
max_tokens: int = 1024             # Hard cap
sentence_snap: bool = True         # Snap to sentence boundaries
code_treatment: str = "single_chunk"  # Preserve code blocks intact
```

### Chunker Implementation Details

- **Sentence snapping**: Uses `(?<=[.!?])\s+` regex. After sliding-window split, looks ahead up to `overlap/2` words to find a sentence boundary. If found, extends the chunk to include the full sentence. Falls back to original window if no boundary found within lookahead.

- **Code blocks**: Detected by `'''` presence. Treated as single atomic chunk. If > max_tokens (1024), truncated with `<!-- TRUNCATED: exceeded max_tokens -->` sentinel.

- **Minimum filtering**: `_filter_and_finalize()` drops chunks < 64 tokens and re-indexes remaining chunks with sequential indices.

- **Metadata flags on Chunk dataclass**: `is_code: bool`, `truncated: bool`

## Metadata Schema

### ChunkMetadata Interface (`types.ts`)

```typescript
interface ChunkMetadata {
  agent_id: string;        // "forge" | "syntra" | "recon" | "pulse" | "hermes"
  workspace_id: string;    // e.g., "workspace-syntra"
  source_path: string;
  chunk_index: number;
  content_hash: string;    // SHA-256 of normalized text (dedup key)
  layer: "identity" | "episodic" | "knowledge" | "artifact";
  is_private: boolean;
  is_code: boolean;
  truncated?: boolean;
  learned_at: string;      // ISO 8601
  accessed_at?: string;
}
```

### DB Columns (additive, graceful if missing)

```sql
-- documents table
workspace_id TEXT DEFAULT ''
layer TEXT DEFAULT 'knowledge'

-- chunk_embeddings table  
content_hash TEXT DEFAULT ''
is_code INTEGER DEFAULT 0
truncated INTEGER DEFAULT 0
learned_at REAL DEFAULT 0
```

The `sovrd.py` `_write_chunk_metadata()` function wraps ALTER/UPDATE in try/catch — it works even if columns don't exist yet.

## Layer Filtering Rules

Implemented in `LAYER_FILTER_RULES` (`types.ts`):

| Layer | agentFilter | agentTagTransform | isShared |
|-------|-------------|-------------------|----------|
| identity | strict | `identity:{agentId}` | false |
| episodic | strict | - | false |
| knowledge | none | - | true |
| artifact | default | - | false |

### How it flows

1. `SovereignMemoryManager.search()` checks `opts.layer` against `LAYER_FILTER_RULES`
2. If `layer=knowledge`: `agentFilter=none` → no `agent_id` param sent to daemon
3. If `layer=identity`: `agentFilter=strict` + transform → `agent_id=identity:{agentId}`
4. `bridge.recall()` passes layer + workspace_id in query params
5. `sovrd.py` `/recall` endpoint reads params, calls `_recall_raw()` with layer
6. `_recall_raw()` routes to appropriate SovereignAgent method based on layer

## Content-Hash Dedup

In `sovrd.py` `/learn` handler:
1. Normalize text: `" ".join(text.lower().split())`
2. SHA-256 hash
3. Check SQLite: `SELECT COUNT(*) FROM chunk_embeddings WHERE content_hash = ? AND agent_matches`
4. If duplicate exists → return `{"status": "duplicate", "hash": "..."}` (not an error!)
5. If new → proceed with write + metadata tagging

## Per-Request Agent Routing (Critical Fix)

The Phase 1 daemon had `AGENT_ID = "hermes"` hardcoded on module load. Phase 2 replaces this with an LRU cache:

```python
_agent_instances: dict[str, SovereignAgent] = {}
_agent_instances_lock = threading.Lock()

def get_agent(agent_id: str = "hermes") -> SovereignAgent:
    if agent_id not in _agent_instances:
        with _agent_instances_lock:
            if agent_id not in _agent_instances:
                _agent_instances[agent_id] = SovereignAgent(agent_id=agent_id)
    return _agent_instances[agent_id]
```

This means Forge's adapter calls get `SovereignAgent(agent_id="forge")`, Syntra's gets `agent_id="syntra"`, etc., all sharing one daemon process.

## Pitfalls

### Token Counting Approximation
The chunker uses `text.split()` (word splitting) as a proxy for token count. 512 words ≈ ~512 tokens for English text, but over-counts for code and under-counts for languages with long compound words. This matches the existing engine behavior and is acceptable.

### Schema Migration
New columns are added via `ALTER TABLE` or handled by try/catch in the write path. Existing indexed chunks remain valid — only new ingests get Phase 2 metadata. Do NOT drop/recreate tables.

### Agent Factory Function
After adding `workspaceId` to `SovereignMemoryManager` constructor, the `getMemorySearchManager()` factory in `sovereign-manager.ts` MUST be updated to accept and pass `workspaceId`. Otherwise OpenClaw's plugin loader will crash on instantiation.

### TypeScript Imports
The bridge.ts import of `MemoryLayer` from `./types.js` requires the `.js` extension (not `.ts`) because the compiled output is `.js`. This is required by Node.js ESM resolution.

## Verification Steps

```bash
# 1. TypeScript build/check
# Current live plugin path is ~/.openclaw/extensions/sovereign-memory.
# Old notes may still say ~/.openclaw/plugins/sovereign-memory — treat that as stale unless the directory exists.
cd ~/.openclaw/extensions/sovereign-memory && npm run build

# 2. Daemon health (after restart)
curl --unix-socket /tmp/sovereign.sock http://localhost/health

# 3. Recall with layer filtering
curl --unix-socket /tmp/sovereign.sock "http://localhost/recall?q=test&agent_id=syntra&layer=knowledge"

# 4. Learn with dedup (call twice — second should return "duplicate")
curl --unix-socket /tmp/sovereign.sock -X POST -H "Content-Type: application/json" \
  -d '{"content":"use vapor for APIs","agent_id":"forge","category":"pattern"}' http://localhost/learn

# 5. Day 5 end-to-end smoke test: OpenClaw plugin bridge -> Unix socket -> sovrd -> Sovereign DB
cd ~/.openclaw/extensions/sovereign-memory && npm run smoke:day5

# 6. Python chunker unit test
cd ~/.openclaw/sovereign-memory-v3.1 && python3 -c "
from chunker import MarkdownChunker; c = MarkdownChunker()
chunks = c.chunk_document('# Test\\nSome text here. More text. Even more.')
print(f'Chunks: {len(chunks)}, min_token: {c.min_tokens}')
"
```

## Day 5 Completion Notes (2026-04-23)

Live Day 5 verification found several reusable fixes:

- **Actual plugin path:** `~/.openclaw/extensions/sovereign-memory`, not the stale `~/.openclaw/plugins/sovereign-memory` path. Update launchd plists, log messages, and commands accordingly.
- **launchd path drift:** `~/Library/LaunchAgents/com.openclaw.sovrd.plist` can keep pointing at the stale plugin path even after OpenClaw loads the extension from the global extension path. Verify `WorkingDirectory`, `ProgramArguments`, and `PYTHONPATH` before debugging daemon failures.
- **Engine contamination blocker:** if `sovrd.py` crashes before binding `/tmp/sovereign.sock`, inspect `~/.openclaw/sovereign-memory-v3.1/config.py` for leading `declare -x` contamination. In the Day 5 run, 96 env-dump lines at the top of `config.py` caused `SyntaxError` during `from config import SovereignConfig`.
- **`/identity` crash:** `sovrd.py` had `query.get("agent_id", [AGENT_ID])` but `AGENT_ID` no longer existed after per-request routing. Fix default to `["hermes"]`.
- **Duplicate learn bug:** checking only `chunk_embeddings.content_hash` misses writes that land only in the `learnings` table. Dedup must also compare normalized hashes of existing `learnings.content` for the same `agent_id`.
- **Fresh learn recall gap:** vector recall may not surface a just-written exact marker immediately. `/recall` should prepend exact `learnings` table matches before vector/wiki hits so write -> recall round-trips pass deterministically.
- **Module mismatch:** plugin `package.json` declared `"type": "module"` while `tsconfig.json` emitted CommonJS. Node failed to load `dist/index.js`. Either compile real ESM or set `"type": "commonjs"`; Day 5 used CommonJS to match the existing build.
- **Layer 1 identity verification:** do not trust prior memory that identities are seeded. Verify live DB rows with `SELECT agent, COUNT(*) FROM documents WHERE agent LIKE 'identity:%' GROUP BY agent`. If missing, rerun `~/.openclaw/sovereign-memory-v3.1/seed_identity.py`, then restart `sovrd` to clear cached `SovereignAgent` instances.
- **Smoke test behavior:** `npm run smoke:day5` should verify health, identity, full hydration, learn, duplicate learn, Forge exact recall, Syntra cross-agent knowledge recall, manager vector probe, and manager search exact recall.
