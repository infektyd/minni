"""Test: vault_write pages must be immediately semantically recall-able (M-4 fix).

Before the fix: minni_vault_write wrote pages to disk but did NOT call
index_durable_document — vault-write content was only recall-able after a
separate VaultIndexer run. This test verifies the in-process bridge works:
write → index_durable_document → retrieve all in one engine lifetime,
without an out-of-band indexer run.

This tests the ENGINE bridge (index_durable_document), not the full
MCP/TypeScript stack (which requires the daemon to be running).
The daemon's new vault_index_doc RPC calls this same engine method.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def test_vault_write_content_is_immediately_recall_able(tmp_path):
    """After index_durable_document, the page must appear in retrieve() results."""
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=False,   # fast; correctness not reranker-dependent
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    # Simulate what vault_write produces: a markdown page with frontmatter
    page_content = """---
title: Minni Architecture Decision: Dual-Write Mode
section: decisions
status: candidate
privacy: safe
---
# Minni Architecture Decision: Dual-Write Mode

The dual-write flag controls whether the daemon writes memories to both
the primary SQLite store and a secondary backup store simultaneously.
This is disabled by default because the secondary store adds ~15% latency
overhead per learn call.

## When to enable dual-write
Enable dual-write in production when you need high availability failover
without a daemon restart. Set MINNI_DUAL_WRITE=1 in the environment.
"""
    page_path = "wiki/decisions/dual-write-mode.md"

    # 1. Index the page (simulates what _handle_vault_index_doc / vault_write does)
    result = engine.index_durable_document(
        content=page_content,
        path=page_path,
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="candidate",
        layer="knowledge",
    )
    assert result["status"] == "ok", f"index_durable_document failed: {result}"

    # 2. Recall without any out-of-band indexer run
    results = engine.retrieve(
        query="dual-write flag minni latency overhead",
        limit=10,
        depth="snippet",
        expand=False,
        budget_tokens=False,
        update_access=False,
        # include_drafts=True because page_status="candidate" not "accepted"
        include_drafts=True,
    )

    sources = [r.get("source", "") for r in results]
    assert page_path in sources, (
        f"vault_write page not found in recall results. sources={sources}. "
        "The vault_index_doc bridge must call index_durable_document immediately."
    )


def test_vault_write_chunks_zero_still_findable_via_fts(tmp_path):
    """Very short pages that produce 0 semantic chunks should still be found
    via FTS5 (lexical fallback), not silently dropped."""
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test2.db"),
        faiss_index_path=str(tmp_path / "test2.faiss"),
        vault_path=str(tmp_path / "vault2/"),
        writeback_path=str(tmp_path / "learnings2/"),
        graph_export_dir=str(tmp_path / "graphs2/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    # Very short page — likely produces 0 chunks (below min_tokens=64)
    short_page = "# Decision\n\nUse sqlite WAL mode for concurrent reads.\n"
    short_path = "wiki/decisions/sqlite-wal.md"

    result = engine.index_durable_document(
        content=short_page,
        path=short_path,
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="candidate",
        layer="knowledge",
    )
    assert result["status"] == "ok", f"Failed: {result}"
    # We don't assert chunks > 0 — short pages may have 0 chunks (lexical only)
    # The test just confirms the index call doesn't fail.

    results = engine.retrieve(
        query="sqlite WAL mode concurrent",
        limit=10,
        depth="snippet",
        expand=False,
        budget_tokens=False,
        update_access=False,
        include_drafts=True,
    )
    # Short pages come back via FTS5 — verify they're findable
    sources = [r.get("source", "") for r in results]
    assert short_path in sources, (
        f"Short page not found in FTS results. sources={sources}."
    )
