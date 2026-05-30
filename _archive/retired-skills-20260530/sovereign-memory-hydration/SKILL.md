---
name: sovereign-memory-hydration
description: "Wire Sovereign Memory V3.1 into Hermes agent — startup hydration, runtime recall, and write-back learning. Use when setting up, debugging, or extending the sovereign memory bridge."
tags: [openclaw, memory, hermes-agent, faiss, sqlite, rag, hydration]
---

# Sovereign Memory Hydration

> **Plugin install status (2026-04-18):** Plugin is LIVE in OpenClaw 2026.3.23. Installed via `openclaw plugins install --link --dangerously-force-unsafe-install ~/.openclaw/plugins/sovereign-memory`. `definePluginEntry` does NOT exist in this SDK version — use default export with direct hook registration pattern. `openclaw.plugin.json` required at plugin root. Remaining: tool handler bridge to daemon at `/tmp/sovereign.sock`, Layer 2 RAG injection, real round-trip test.

 — Hermes Agent Bridge

Bridges the standalone Sovereign Memory V3.1 system into Hermes agent's system prompt
and tool registry, so Hermes "wakes up" hydrated with wiki/vault knowledge and can
do ad-hoc retrieval and write-back during sessions.

## Architecture

```
~/.openclaw/sovereign-memory-v3.1/     ← Sovereign Memory engine
  ├── agent_api.py                      ← SovereignAgent class (recall, learn, startup_context)
  ├── wiki_indexer.py                   ← Wiki ingestion
  ├── index_all.py                      ← Unified vault+wiki indexer
  ├── retrieval.py                      ← FAISS+FTS5 hybrid search
  └── config.py                         ← agent_colors, paths, budgets

~/.hermes/hermes-agent/
  ├── agent/sovereign_hydration.py      ← NEW: startup prompt builder + runtime recall
  ├── tools/sovereign_memory_tool.py    ← NEW: Hermes tool registrations
  ├── run_agent.py                      ← MODIFIED: import + inject in _build_system_prompt()
  ├── model_tools.py                    ← MODIFIED: added tools.sovereign_memory_tool to discovery
  └── toolsets.py                       ← MODIFIED: added sovereign_memory_recall/learn to core tools
```

## Two-Layer Boot Sequence

Identity is NOT chunked — it's loaded whole. Sovereign Memory fragments documents
for FAISS retrieval, which would break identity. So we use a two-layer architecture:

```
┌─────────────────────────────────────────────┐
│  AGENT BOOT SEQUENCE                        │
│                                             │
│  Layer 1: IDENTITY (whole-document load)    │
│  ~/.openclaw/identities/{agent}/            │
│  ├── IDENTITY.md  (who am I?)              │
│  └── SOUL.md      (how do I behave?)       │
│  Loaded IN FULL — never chunked             │
│                                             │
│  Layer 2: SOVEREIGN MEMORY (chunked RAG)    │
│  Wiki docs, vault files, learnings, events  │
│  Retrieved by relevance — supplementary     │
└─────────────────────────────────────────────┘
```

Identity files are indexed into Sovereign Memory with `whole_document=1` so the
`startup_context()` method filters them out (identity is loaded separately).
The `identity_context()` method loads them whole via `chunk_embeddings` (single chunk).

## Three Modes

1. **STARTUP** — Two-layer hydration via `build_sovereign_startup_prompt("hermes")`
   - Layer 1: `--identity` → IDENTITY.md + SOUL.md (whole document, ~4000 chars max)
   - Layer 2: `--context` → wiki/vault/learnings (chunked RAG, ~3000 chars max)
   - Both layers joined with `---` boundary in system prompt
   - Gated on `~/.openclaw/sovereign_memory.db` existence
   - Subprocess call with 15s timeout; graceful degradation on failure

2. **RECALL** — `sovereign_memory_recall` tool (runtime ad-hoc queries)
   - Direct in-process import first (fast path)
   - Falls back to CLI subprocess if import fails
   - Returns markdown-formatted results with score + heading context

3. **LEARN** — `sovereign_memory_learn` tool (write-back)
   - Stores agent learnings for future startup hydration
   - Categories: pattern, fix, decision, preference, fact, procedure, general
   - Confidence scoring (0.0-1.0)

## Key Decisions

- **Subprocess for startup hydration**: The sovereign memory system lives in a different
  venv (~/.openclaw/venv) with FAISS/SBERT deps. Rather than forcing those into the
  Hermes venv, we call `agent_api.py` as a subprocess. Runtime recall tries direct
  import first (same-process, fast) and falls back to subprocess.

- **agent_context table**: Populated with relevance-scored doc_ids for each agent.
  Wiki docs get type-boosted scores (entity=20, concept=18, decision=15) so they
  appear above untagged vault docs in startup context.

- **Startup context query**: Updated `agent_api.py startup_context()` to use
  `agent_context` table when available, with fallback to `WHERE agent=? OR 'unknown' OR wiki:%`.

## Pitfalls & Troubleshooting

### DB Path Drift: Hydration Returns Empty
If `build_sovereign_startup_prompt('hermes')` returns an empty string, check the database path before debugging retrieval logic. On this user's machine the live repo and active populated DB may be `~/sovereignMemory/sovereign_memory.db`, while legacy/default config paths such as `~/.openclaw/sovereign_memory.db` can exist but contain only a minimal or stale database. Verify table counts before trusting a path:

```bash
python3 - <<'PY'
import os, sqlite3
for p in ['~/.openclaw/sovereign_memory.db', '~/sovereignMemory/sovereign_memory.db']:
    db=os.path.expanduser(p)
    if not os.path.exists(db):
        print(p, 'missing'); continue
    con=sqlite3.connect(db); cur=con.cursor()
    print('\n', p)
    for table in ['documents','agent_context','learnings','chunk_embeddings']:
        try:
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            print(table, cur.fetchone()[0])
        except Exception as e:
            print(table, e)
    con.close()
PY
```

Direct one-off hydration from the populated DB:

```bash
export SOVEREIGN_DB_PATH="$HOME/sovereignMemory/sovereign_memory.db"
cd "$HOME/sovereignMemory/engine"
python3 agent_api.py hermes --full
```

Treat a populated `agent_context` table and whole-document identity rows as the proof gate for Layer 1/Layer 2 availability.

### Council Tool ModuleNotFoundError
When calling council agents (Syntra/Forge/Recon) directly via their `.py` tool scripts (e.g., for ad-hoc reports), the script may fail to import `tools.registry`.
**Fix:** Explicitly set the `PYTHONPATH` to the hermes-agent root:
```bash
PYTHONPATH="~/.hermes/hermes-agent" python3 ~/.hermes/hermes-agent/tools/call_syntra_tool.py "message"
```

### Command Center vs. Unix Socket Transport
There is a known architectural tension between the "Option 2 Plan" (Unix Sockets) and the "Command Center" (WebSocket) transport. Always verify which transport is active in the spec at `~/SOVEREIGN-OPENCLAW-OPTION2-PLAN.md` before implementation.

### Incomplete Retrieval Snippets
`sovereign_recall` may return hits that are too thin for conversational hydration.
**Best Practice:** Use `sovereign_recall` for artifacts/specs and `session_search` (with OR-heavy queries) for the narrative thread memory.

## Usage (Runtime Retrieval)
```bash
# Test startup hydration
cd ~/.hermes/hermes-agent && source venv/bin/activate
python -c "from agent.sovereign_hydration import build_sovereign_startup_prompt; print(build_sovereign_startup_prompt('hermes'))"

# Re-index wiki (after changes) — vault is now ~/wiki/ (unified since 2026-04-16)
# IMPORTANT: the venv lives at ~/.openclaw/sovereign-memory-v3.1/venv, NOT ~/.openclaw/venv
cd ~/.openclaw/sovereign-memory-v3.1 && source venv/bin/activate
python3 -c "from indexer import VaultIndexer; from db import SovereignDB; VaultIndexer(db=SovereignDB()).index_vault()"

# Repopulate agent_context (if DB changes significantly)
# NOTE: use the same venv as above — ~/.openclaw/sovereign-memory-v3.1/venv
cd ~/.openclaw/sovereign-memory-v3.1 && source venv/bin/activate
python3 -c "
import sqlite3, os, time
db = os.path.expanduser('~/.openclaw/sovereign_memory.db')
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute('DELETE FROM agent_context WHERE agent_id = ?', ('hermes',))
now = time.time()
cur.execute('SELECT doc_id, agent, access_count, decay_score FROM documents')
for row in cur.fetchall():
    agent = row[1]
    base = row[3] * row[2]
    boost = {'wiki:entity': 20, 'wiki:concept': 18, 'wiki:decision': 15, 'wiki:project': 14}.get(agent, 1)
    score = base + boost if agent.startswith('wiki:') else base + 1
    cur.execute('INSERT OR REPLACE INTO agent_context VALUES (?,?,?,?)', ('hermes', row[0], score, now))
conn.commit(); conn.close()
"
```

## Multi-Agent Context Population

By default, `agent_context` is only populated for hermes. The other 4 team agents
(Forge, Recon, Pulse, Syntra) need their own entries or they get zero startup context.

To populate all agents:

```bash
cd ~/.openclaw/sovereign-memory-v3.1 && source ../venv/bin/activate
python3 -c "
import sqlite3, os, time

db = os.path.expanduser('~/.openclaw/sovereign_memory.db')
conn = sqlite3.connect(db)
cur = conn.cursor()

# Wiki type boost table (higher = more important for startup context)
WIKI_BOOSTS = {
    'entity': 20.0, 'concept': 18.0, 'decision': 15.0,
    'project': 14.0, 'comparison': 12.0, 'tool': 10.0,
    'query': 8.0, 'meta': 5.0, 'unknown': 3.0,
}

# Agent-specific boosts (each agent should see its own docs first)
AGENT_BOOSTS = {
    'forge': 8.0, 'recon': 8.0, 'pulse': 5.0,
    'syntra': 15.0, 'hermes': 8.0,
}

for agent_id in ['hermes', 'forge', 'recon', 'pulse', 'syntra']:
    cur.execute('DELETE FROM agent_context WHERE agent_id = ?', (agent_id,))
    now = time.time()
    cur.execute('SELECT doc_id, agent, access_count, decay_score FROM documents')
    for row in cur.fetchall():
        doc_agent = row[1]
        base = row[3] * row[2]
        if doc_agent.startswith('wiki:'):
            wiki_type = doc_agent.replace('wiki:', '')
            score = base + WIKI_BOOSTS.get(wiki_type, 5.0)
        elif doc_agent == agent_id:
            score = base + AGENT_BOOSTS.get(agent_id, 3.0)
        elif doc_agent in AGENT_BOOSTS:
            score = base + 3.0  # Other agent docs are medium priority
        else:
            score = base + 1.0
        cur.execute('INSERT OR REPLACE INTO agent_context VALUES (?,?,?,?)',
                    (agent_id, row[0], score, now))
    conn.commit()
    cur.execute('SELECT COUNT(*) FROM agent_context WHERE agent_id = ?', (agent_id,))
    print(f'{agent_id}: {cur.fetchone()[0]} docs')
conn.close()
"
```

## Agent Workspace Audit

Most agents are missing IDENTITY.md and SOUL.md in `~/.openclaw/agents/*/agent/`.
Only Pulse has full identity files. Without these, agents don't know their own role,
personality, or boundaries. Check with:

```bash
for agent in forge recon pulse syntra hermes; do
  dir=~/.openclaw/agents/$agent
  [ -f "$dir/agent/IDENTITY.md" ] && echo "$agent: ✅ IDENTITY" || echo "$agent: ❌ IDENTITY"
  [ -f "$dir/agent/SOUL.md" ] && echo "$agent: ✅ SOUL" || echo "$agent: ❌ SOUL"
done
```

## Agent Identity System

Identity files live in `~/.openclaw/identities/{agent}/` — the canonical source of truth.
The old scattered `~/.openclaw/agents/{name}/agent/` files are replaced with symlinks
pointing to the identities directory. This ensures a single source of truth.

**All 5 agents** have IDENTITY.md + SOUL.md:
- `forge` — Builder/Modi, implementation agent
- `recon` — Scout/Modi, research agent
- `pulse` — Heartbeat, monitor (no cognitive role)
- `syntra` — Architect/Valon, moral+architectural agent
- `hermes` — Orchestrator/Drift, team lead + user interface

Each identity file is indexed into Sovereign Memory as `identity:{agent}/IDENTITY.md`
or `identity:{agent}/SOUL.md` with `whole_document=1` flag. The `agent_context` table
is populated for ALL 5 agents (119 docs each), ensuring every team member gets both
layers at boot.

**DB schema addition**: `documents` table now has `whole_document INTEGER DEFAULT 0`.
When `whole_document=1`, the doc is excluded from `startup_context()` (Layer 2) and
only loaded via `identity_context()` (Layer 1).

## Emergency Single-File Ingest (Ad-Hoc)

When a critical file exists on disk but isn't in sovereign memory (agents can't find it),
use direct SQL to insert it and link to agent contexts. This bypasses the full indexing
pipeline and is the right approach for one-off documents that need immediate availability.

**Use case:** User created a discussion spec, team agents tried to query it, found nothing.
The file needs to be discoverable NOW, not after a full re-index.

```python
import sqlite3, os, hashlib, time

db_path = os.path.expanduser("~/.openclaw/sovereign_memory.db")
file_path = os.path.expanduser("~/.hermes/hermes-agent/some-file.md")

with open(file_path) as f:
    content = f.read()

sigil = hashlib.md5(file_path.encode()).hexdigest()[:16]
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Insert document (whole_document=0 for normal, 1 for identity)
cur.execute("""
    INSERT INTO documents (path, agent, sigil, last_modified, indexed_at, access_count, last_accessed, decay_score, whole_document)
    VALUES (?, ?, ?, ?, ?, 0, 0, 1.0, 0)
""", (file_path, "wiki:project", sigil, time.time(), time.time()))

doc_id = cur.lastrowid

# Link to all agents that need it
for agent in ['hermes', 'syntra', 'forge', 'recon', 'pulse']:
    cur.execute("INSERT INTO agent_context (agent_id, doc_id, relevance_score) VALUES (?, ?, ?)",
                (agent, doc_id, 25.0))

conn.commit()
conn.close()
```

**Key details:**
- `agent` column determines type boosting: `wiki:project`, `wiki:entity`, `wiki:concept`, etc.
- `relevance_score` controls rank order in startup context — 25.0 is high priority (above most wiki docs)
- `whole_document=0` means it participates in Layer 2 chunked RAG retrieval
- Check before inserting: `SELECT doc_id FROM documents WHERE path = ?` to avoid duplicates
- To also insert chunks for FTS5 search, add rows to `chunk_embeddings` — but for short docs (<4K chars), the single-row approach is fine

**Verification:**
```python
cur.execute("SELECT doc_id, path, agent FROM documents WHERE path LIKE '%filename%'")
cur.execute("SELECT agent_id, relevance_score FROM agent_context WHERE doc_id = ?", (doc_id,))
```

## Multi-Agent & Profile Isolation Pitfalls

### Pitfall: Profile Isolation (Custom Providers)
When creating new Hermes profiles for a multi-agent fleet, custom providers (like local Ollama nodes) are NOT inherited from the main `config.yaml`. 
**Fix:** Manually inject the `custom_providers` block into `~/.hermes/profiles/<name>/config.yaml` to ensure local inference nodes are reachable by sub-agents.

## Implementation Status: COMPLETE (v2)

The approach described above was the original plan. **The actual implementation uses
the Hermes MemoryProvider plugin system**, which is cleaner and doesn't require
modifying core Hermes files.

### What Was Built

```
~/.hermes/hermes-agent/plugins/memory/sovereign/
├── __init__.py       ← SovereignMemoryProvider(MemoryProvider) + register()
└── plugin.yaml       ← Metadata
```

Set `memory.provider` to `sovereign` in the Hermes config to activate.

### Why CLI Subprocess (Not Direct Import)

Sovereign Memory lives in its own venv with FAISS, SentenceTransformers, and torch.
Importing those into Hermes's process causes venv conflicts. The provider shells out to
the Sovereign Memory venv's python to run agent_api.py.

First call is slow (~5-10s for model loading), then cached.

### Known Bug in agent_api.py (FIXED)

`SovereignAgent.recall()` called `self.retrieval.hybrid_search()` which doesn't exist.
The method is `retrieve()` and it returns `List[Dict]`, not a formatted string. The fix:

1. Change `hybrid_search` → `retrieve`
2. Remove the `format` parameter (not accepted by retrieve)
3. Format the results dict into markdown manually in recall()

If you rebuild from scratch, this bug will reappear — check agent_api.py line 102.

**Known Bug: Tool Schema Registration Race (FIXED)**

**Symptom:** `sovereign_recall`/`sovereign_learn`/`sovereign_log` appear in system prompt
("Active. Use sovereign_recall to search…") but calling them returns "Unknown tool" or
the tool names aren't in `valid_tool_names` at all.

**Root cause:** Initialization order in `run_agent.py`:
1. `add_provider(provider)` — indexes tool schemas via `get_tool_schemas()`
2. `initialize_all()` — sets `_active = True` on the provider
3. `get_tool_schemas()` returns `[]` when `_active = False`
4. So step 1 registers **zero** tools, and step 2 is too late

**The fix:** In `SovereignMemoryProvider.get_tool_schemas()`, always return the static
schema definitions regardless of `_active`. The `_active` gate should only apply to
`handle_tool_call()` execution, not schema advertisement:

```python
def get_tool_schemas(self) -> List[Dict[str, Any]]:
    """Expose sovereign_recall, sovereign_learn, sovereign_log tools.
    
    Always returns schemas — the _active gate is for handle_tool_call(),
    not for schema advertisement. If schemas are hidden before initialize(),
    the MemoryManager's add_provider() indexes zero tools and they're never
    registered in the agent's tool surface.
    """
    return ALL_SCHEMAS  # NOT gated on self._active
```

**Verification:**
```bash
cd ~/.hermes/hermes-agent && source venv/bin/activate
python -c "
from plugins.memory import load_memory_provider
p = load_memory_provider('sovereign')
print(f'Before init: {len(p.get_tool_schemas())} tools')  # Should be 3, NOT 0
"
```

## Runtime Hydration Playbook (Conversational Recall)

When a user says something like "hydrate from sovereign memory" and they mean
"pull our live project thread back into context," do **not** rely on
`sovereign_recall` alone.

### Observed behavior

`sovereign_recall` is good at surfacing the **right artifacts** (docs, wiki pages,
log files, known spec names), but in practice the returned snippets can be too thin
for user-facing conversational hydration. You may get filenames / headings without
enough narrative detail to rebuild the actual thread.

### Recommended retrieval sequence

1. **Load the sovereign-memory-hydration skill first** so the architecture and path
   assumptions are fresh.
2. **Query `sovereign_recall`** for canonical artifacts / concepts:
   - project names (`Project Console v1`)
   - known docs (`discussion-spec-console-redesign.md`)
   - architectural conclusions (`Command Center IS the group chat`)
   - infra terms (`~/wiki`, provider plugin, hydration)
3. **If results are empty OR snippet-poor, immediately pivot to `session_search`**
   using broad OR queries for conversational reconstruction.
4. **Use `session_search` summaries as the narrative spine** and use Sovereign Memory
   hits as grounding / artifact confirmation.
5. Present the hydration as:
   - what was recovered confidently
   - what files/specs are implicated
   - any gaps or uncertainty in recall quality

### Good `session_search` query style

Use OR-heavy queries, not narrow AND phrasing. Examples:
- `Project Console OR multi-agent coordination OR Sovereign Memory OR Command Center`
- `Discord OR Command Center OR transport OR mention tags`
- `Tower OR Vidar OR Ollama OR custom_providers`

### When this matters most

This combined strategy is especially important when the user wants:
- a project-state refresher
- cross-session continuity
- reconstruction of decisions, pivots, or frustrations
- the "thread" rather than raw document retrieval

### Rule of thumb

- Use **Sovereign Memory** to find the durable artifacts and decisions.
- Use **session_search** to recover the human/story layer and session chronology.
- Best hydration usually comes from **both**, not either alone.

## Session Extraction Pipeline (Built — Cron + AFM → Wiki)

Automatically extracts facts from ended Hermes sessions using Apple Foundation
Models, writes them as wiki pages that Sovereign Memory indexes on its next
wiki scan.

### Architecture Decision: Cron → Wiki, NOT on_session_end hook

Initially planned to wire extraction into `SovereignMemoryProvider.on_session_end()`.
Pivoted to cron+wiki approach because:
- `on_session_end` blocks session teardown (even in a thread, it's fragile)
- Writing directly to wiki lets Sovereign Memory's existing wiki indexer handle it
- Cron decouples extraction from the agent runtime entirely
- Wiki pages are inspectable/curable before Sovereign Memory ingests them

### Pipeline

```
Hermes state.db → session-extract.py → fact_extract (AFM) → ~/wiki/auto-indexed/sessions/ → Sovereign Memory wiki indexer
```

| Piece | Location | Status |
|-------|----------|--------|
| `fact_extract` binary | `~/.hermes/skills/apple/foundation-models-extraction/fact_extract` | ✅ Built |
| `session-extract.py` | `~/.hermes/scripts/session-extract.py` | ✅ Built |
| Extraction tracking DB | `~/.hermes/extraction-tracking.db` | ✅ Tracks processed sessions |
| Cron (launchd) | `~/Library/LaunchAgents/ai.hermes.session-extract.plist` | ✅ Every 30 min |
| Wiki output dir | `~/wiki/auto-indexed/sessions/` | ✅ Created |
| Sovereign Memory wiki indexer | `~/.openclaw/sovereign-memory-v3.1/index_all.py` | ✅ Auto-indexes wiki |

### Usage

```bash
# Process all unprocessed (default: max 10 per run)
python3 ~/.hermes/scripts/session-extract.py

# Specific session
python3 ~/.hermes/scripts/session-extract.py --session SESSION_ID

# Dry run
python3 ~/.hermes/scripts/session-extract.py --dry-run

# Re-index Sovereign Memory after extraction
python3 ~/.hermes/scripts/session-extract.py --reindex
```

### Known Issue: Env Var Noise

The AFM model extracts environment variable lines (e.g. `HERMES_INTERACTIVE="1"`)
as "preferences." Needs a post-extraction filter to skip facts matching `^[A-Z_]+=`.

## Critical: Vault Path Config

**As of 2026-04-16**, the vault is unified at `~/wiki/`. The Sovereign Memory config
at `~/.openclaw/sovereign-memory-v3.1/config.py` was updated:

```python
vault_path: str = os.environ.get(
    "SOVEREIGN_VAULT_PATH",
    os.path.expanduser("~/wiki/")  # Was ~/obsidian/openClaw/ — OLD, DO NOT REVERT
)
```

If you ever see `~/obsidian/openClaw/` in the config, it's wrong — that directory
was deleted during the vault unification. The env var `SOVEREIGN_VAULT_PATH` can
override if needed.

### Re-indexing after vault changes

After any structural changes to ~/wiki/ (new directories, moved files, merges):

```bash
source ~/.hermes/hermes-agent/venv/bin/activate
cd ~/.openclaw/sovereign-memory-v3.1
python3 -c "from indexer import VaultIndexer; from db import SovereignDB; VaultIndexer(db=SovereignDB()).index_vault()"
```

This does incremental indexing — skips unchanged files, indexes new/modified ones,
and deletes stale entries.
