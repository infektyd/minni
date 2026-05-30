---
name: sovereign-memory-packaging
description: Package Sovereign Memory as a pip-installable Python library and push to GitHub
version: 3.1.0
---

# Sovereign Memory Packaging Workflow

## Context
Sovereign Memory v3.1 was originally a loose collection of .py files in `~/.openclaw/sovereign-memory-v3.1/`. This workflow covers refactoring it into a proper Python package and shipping to GitHub.

## Steps

### 1. Audit existing files
- List all `.py` files, identify which are core, agent-facing, source integrations, or CLI
- Check for hardcoded paths (especially `~/.openclaw/`) that should use `SOVEREIGN_HOME`
- Verify no secrets/DB files/binaries are in the tree

### 2. Restructure into `src/sovereign_memory/` layout
- `core/` — db, chunker, faiss_index, retrieval, indexer, decay, episodic, writeback, graph_export, config
- `agents/` — agent_api, hydration (two-layer boot)
- `sources/` — wiki_indexer, obsidian
- `identities/` — template system with `_example/` agent

### 3. Use lazy imports in `__init__.py` files
Prevent crash on `import sovereign_memory` when numpy/faiss not installed:
```python
def __getattr__(name):
    if name in ("SovereignAgent", ...):
        from .agents import SovereignAgent
        return SovereignAgent
    raise AttributeError(...)
```

### 4. Write `pyproject.toml`
- Modern Python packaging (no setup.py)
- `SOVEREIGN_HOME` env var for user data path (default `~/.sovereign/`, fallback `~/.openclaw/`)
- CLI entry point: `sovereign-memory = sovereign_memory.cli:main`

### 5. Git infrastructure
- `.gitignore` — venv/, __pycache__/, *.db, *.index, .env, IDE, OS files
- `.gitattributes` — LF normalization, binary diff skip for .db/.index/.bin
- `.env.example` — all path overrides documented
- Identity templates only — actual agent souls stay in `~/.openclaw/identities/`

### 6. Push to GitHub
```bash
cd sovereign-memory-v3.1/
git init
git add .
git commit -m "v3.1.0: pip-installable sovereign memory with two-layer hydration"
gh repo create infektyd/sovereign-memory --public --source=. --push
```

## Key Decisions
- **No FAISS/DB in repo** — generated at runtime
- **Identity templates** — `_example/` ships with package, real souls are private
- **`SOVEREIGN_HOME`** — portable path for other users, backward-compatible fallback
