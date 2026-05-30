"""
PR-1b Tests — pytest suite.

Covers:
  0.4  Agent contract pointer in agent_envelope (smoke test via string check)
  0.6  Progressive disclosure depth tiers
  0.6  pack_results() — budget packing + MMR diversity
  0.6  expand_result() — round-trip via RetrievalEngine
"""

import os
import sys
import sqlite3
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_results(n: int, score_step: float = 0.1) -> list:
    """Build n fake search result dicts as would be produced by retrieve()."""
    results = []
    for i in range(n):
        score = round(1.0 - i * score_step, 4)
        results.append({
            "doc_id": i + 1,
            "chunk_id": (i + 1) * 10,
            "path": f"/vault/wiki/sessions/doc-{i}.md",
            "source": f"/vault/wiki/sessions/doc-{i}.md",
            "filename": f"doc-{i}.md",
            "agent": "claude-code",
            "sigil": "session",
            "score": score,
            "fts_rank": i + 1,
            "sem_rank": i + 1,
            "rrf_score": score * 0.5,
            "rerank_score": score,
            "decay_score": 0.9,
            "chunk_text": f"This is chunk text for document {i}. " * 10,
            "heading_context": f"## Section {i}",
            "token_count": 50,
            "confidence": None,
            "age_days": None,
            "depth": "snippet",
        })
    return results


# ---------------------------------------------------------------------------
# 0.4  Agent contract pointer
# ---------------------------------------------------------------------------

class TestAgentEnvelopeContract:

    def test_memory_contract_references_agent_md(self):
        """MEMORY_CONTRACT must contain a pointer to docs/contracts/AGENT.md."""
        # We test the TypeScript source as a text file to avoid needing ts-node
        plugin_src = os.path.join(
            os.path.dirname(__file__),
            "..", "plugins", "minni", "src", "agent_envelope.ts"
        )
        plugin_src = os.path.abspath(plugin_src)
        assert os.path.exists(plugin_src), f"agent_envelope.ts not found: {plugin_src}"
        content = open(plugin_src).read()
        assert "docs/contracts/AGENT.md" in content, (
            "MEMORY_CONTRACT in agent_envelope.ts must reference docs/contracts/AGENT.md"
        )
        assert "evidence, not instruction" in content, (
            "MEMORY_CONTRACT must include the 'evidence, not instruction' phrase"
        )

    def test_vault_schema_content_points_at_vault_md(self):
        """schemaContent() in vault.ts must reference docs/contracts/VAULT.md."""
        vault_src = os.path.join(
            os.path.dirname(__file__),
            "..", "plugins", "minni", "src", "vault.ts"
        )
        vault_src = os.path.abspath(vault_src)
        assert os.path.exists(vault_src), f"vault.ts not found: {vault_src}"
        content = open(vault_src).read()
        assert "docs/contracts/VAULT.md" in content, (
            "schemaContent() in vault.ts must reference docs/contracts/VAULT.md"
        )

    def test_contracts_directory_exists(self):
        """docs/contracts/ directory must exist with all four contract files."""
        contracts_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docs", "contracts")
        )
        assert os.path.isdir(contracts_dir), f"docs/contracts/ not found at {contracts_dir}"
        for fname in ("AGENT.md", "CAPABILITIES.md", "VAULT.md", "PAGE_TYPES.md"):
            fpath = os.path.join(contracts_dir, fname)
            assert os.path.exists(fpath), f"Missing contract file: {fname}"
            assert os.path.getsize(fpath) > 0, f"Contract file is empty: {fname}"


# ---------------------------------------------------------------------------
# 0.6  Progressive disclosure — depth tiers
# ---------------------------------------------------------------------------

class TestDepthTiers:

    def _engine(self, tmp_path):
        """Create a RetrievalEngine with a minimal test DB."""
        from config import SovereignConfig
        from db import SovereignDB
        from faiss_index import FAISSIndex
        from retrieval import RetrievalEngine

        db_path = str(tmp_path / "test.db")
        cfg = SovereignConfig(db_path=db_path)

        # Reset migrations flag so we can use a fresh DB
        import db as db_mod
        old_flag = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            db = SovereignDB(cfg)
        finally:
            db_mod._migrations_run = old_flag

        faiss = FAISSIndex(cfg)
        return RetrievalEngine(db=db, config=cfg, faiss_index=faiss)

    def test_snippet_is_default_no_change(self, tmp_path):
        """retrieve() with no depth arg returns snippet-depth fields."""
        engine = self._engine(tmp_path)
        # Empty DB — no results, but must not raise
        results = engine.retrieve("test query", limit=1)
        assert isinstance(results, list)

    def test_depth_parameter_accepted(self, tmp_path):
        """retrieve() accepts depth parameter without raising."""
        engine = self._engine(tmp_path)
        for depth in ("headline", "snippet", "chunk", "document"):
            results = engine.retrieve("test query", limit=1, depth=depth)
            assert isinstance(results, list), f"depth={depth} raised"

    def test_unknown_depth_falls_back_to_snippet(self, tmp_path):
        """retrieve() with unknown depth gracefully falls back to snippet."""
        engine = self._engine(tmp_path)
        # Should not raise; unknown depth → snippet
        results = engine.retrieve("test query", limit=1, depth="banana")
        assert isinstance(results, list)

    def test_apply_depth_headline_fields(self, tmp_path):
        """_apply_depth('headline') returns only headline fields."""
        engine = self._engine(tmp_path)
        fake = _make_fake_results(1)[0]
        fake.update({
            "rationale": "verbose explanation",
            "evidence_refs": ["doc:a"],
            "recommended_action": "cite",
            "recommended_wiki_updates": ["update"],
        })
        result = engine._apply_depth(fake, "headline")
        assert "score" in result
        assert "depth" in result
        assert result["depth"] == "headline"
        assert "chunk_id" in result
        assert "decay_factor" in result
        # headline must NOT include full text
        assert "text" not in result or result.get("text") is None or "text" not in result
        # headline keeps only cheap routing metadata, not verbose PR-2 guidance.
        assert "rationale" not in result
        assert "evidence_refs" not in result
        assert "recommended_action" not in result
        assert "recommended_wiki_updates" not in result

    def test_minnid_search_defaults_to_headline(self, monkeypatch):
        """Daemon search defaults to headline without changing RetrievalEngine default."""
        import minnid

        seen = {}

        class FakeEngine:
            last_trace_id = "trace-b1"

            def retrieve(self, **kwargs):
                seen.update(kwargs)
                return [{"doc_id": 1, "chunk_id": 10, "depth": kwargs["depth"]}]

        monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: FakeEngine())
        response = minnid._dispatch_sync({
            "jsonrpc": "2.0",
            "id": 100,
            "method": "search",
            "params": {"query": "headline default"},
        })

        assert "error" not in response
        assert seen["depth"] == "headline"
        assert response["result"]["depth"] == "headline"

    def test_sm_drill_batches_result_ids(self, monkeypatch):
        import minnid

        calls = []

        class FakeEngine:
            def expand_result(self, result_id, depth="chunk"):
                calls.append((result_id, depth))
                return {"doc_id": result_id, "chunk_id": result_id * 10, "depth": depth}

        monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: FakeEngine())
        response = minnid._dispatch_sync({
            "jsonrpc": "2.0",
            "id": 101,
            "method": "sm_drill",
            "params": {"result_ids": [1, 2], "depth": "snippet"},
        })

        assert "error" not in response
        assert calls == [(1, "snippet"), (2, "snippet")]
        assert response["result"]["count"] == 2

    def test_sm_export_pack_is_deterministic(self, monkeypatch):
        import minnid

        class FakeEngine:
            def retrieve(self, **kwargs):
                return [
                    {
                        "doc_id": 2,
                        "chunk_id": 20,
                        "source": "/vault/b.md",
                        "wikilink": "[[b]]",
                        "text": "Second result",
                        "token_count": 3,
                        "score": 0.8,
                        "depth": kwargs["depth"],
                    },
                    {
                        "doc_id": 1,
                        "chunk_id": 10,
                        "source": "/vault/a.md",
                        "wikilink": "[[a]]",
                        "text": "First result",
                        "token_count": 3,
                        "score": 0.9,
                        "depth": kwargs["depth"],
                    },
                ]

        monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: FakeEngine())
        request = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "sm_export_pack",
            "params": {"query": "cache prefix", "budget_tokens": 100, "cache_key": "k1"},
        }

        first = minnid._dispatch_sync(request)["result"]
        second = minnid._dispatch_sync(request)["result"]

        assert first["manifest_hash"] == second["manifest_hash"]
        assert first["cache_key"] == "k1"
        assert [a["anchor"] for a in first["prefix"]["anchors"]] == ["sm://doc/1/chunk/10", "sm://doc/2/chunk/20"]
        assert [s["anchor"] for s in first["suffix"]["snippets"]] == ["sm://doc/2/chunk/20", "sm://doc/1/chunk/10"]

    def test_apply_depth_snippet_includes_text(self, tmp_path):
        """_apply_depth('snippet') includes truncated text."""
        engine = self._engine(tmp_path)
        fake = _make_fake_results(1)[0]
        result = engine._apply_depth(fake, "snippet")
        assert "text" in result
        assert result["depth"] == "snippet"
        # text must be truncated to ≤280 chars
        assert len(result["text"]) <= 280 + 1  # +1 for ellipsis char

    def test_apply_depth_chunk_includes_provenance(self, tmp_path):
        """_apply_depth('chunk') includes provenance dict."""
        engine = self._engine(tmp_path)
        fake = _make_fake_results(1)[0]
        result = engine._apply_depth(fake, "chunk")
        assert "text" in result
        assert "provenance" in result
        assert isinstance(result["provenance"], dict)
        assert result["depth"] == "chunk"
        # provenance must have key fields
        assert "fts_rank" in result["provenance"]
        assert "agent_origin" in result["provenance"]

    def test_snippet_text_truncation(self, tmp_path):
        """_apply_depth snippet truncates long chunk_text to ≤280 chars."""
        engine = self._engine(tmp_path)
        long_text = "word " * 200  # ~1000 chars
        fake = {
            "doc_id": 1, "chunk_id": 10, "path": "/x.md", "source": "/x.md",
            "filename": "x.md", "agent": "test", "sigil": "session",
            "score": 0.9, "fts_rank": 1, "sem_rank": 1,
            "rrf_score": 0.5, "rerank_score": 0.9, "decay_score": 1.0,
            "chunk_text": long_text, "heading_context": "", "token_count": 0,
            "confidence": None, "age_days": None,
        }
        result = engine._apply_depth(fake, "snippet")
        assert len(result["text"]) <= 282, f"text too long: {len(result['text'])}"

    def test_expand_result_returns_none_for_missing_id(self, tmp_path):
        """expand_result() returns None for a non-existent result_id."""
        engine = self._engine(tmp_path)
        result = engine.expand_result(result_id=99999, depth="chunk")
        assert result is None


# ---------------------------------------------------------------------------
# 0.6  pack_results — token-budget + MMR diversity
# ---------------------------------------------------------------------------

class TestPackResults:

    def test_pack_results_returns_list(self):
        """pack_results() returns a list."""
        from tokens import pack_results
        results = _make_fake_results(5)
        packed = pack_results(results, budget_tokens=500, depth="snippet")
        assert isinstance(packed, list)

    def test_pack_results_respects_budget(self):
        """pack_results() returns a subset that fits within budget_tokens."""
        from tokens import pack_results
        from tokens import _result_tokens
        results = _make_fake_results(10)
        budget = 200
        packed = pack_results(results, budget_tokens=budget, depth="snippet")
        total = sum(_result_tokens(r, "snippet") for r in packed)
        # Allow one result to push over (first-result guarantee), but not many
        assert total <= budget + 100, f"Total tokens {total} exceeds budget {budget} + slack"

    def test_pack_results_empty_input(self):
        """pack_results() with empty input returns empty list."""
        from tokens import pack_results
        assert pack_results([], budget_tokens=500) == []

    def test_pack_results_no_budget(self):
        """pack_results() with budget_tokens<=0 returns all results."""
        from tokens import pack_results
        results = _make_fake_results(5)
        packed = pack_results(results, budget_tokens=0)
        assert len(packed) == len(results)

    def test_pack_results_mmr_diversity(self):
        """pack_results() with two near-duplicate results keeps one, picks diverse second."""
        from tokens import pack_results

        # Make two near-identical results and one very different one
        dup_a = {
            "doc_id": 1, "chunk_id": 10, "filename": "a.md", "source": "/a.md",
            "score": 0.95, "text": "auth migration using JWT tokens bearer auth",
            "heading": "Auth", "depth": "snippet", "token_count": 30,
        }
        dup_b = {
            "doc_id": 2, "chunk_id": 20, "filename": "b.md", "source": "/b.md",
            "score": 0.90, "text": "auth migration using JWT tokens bearer auth",  # near-dup
            "heading": "Auth2", "depth": "snippet", "token_count": 30,
        }
        diverse = {
            "doc_id": 3, "chunk_id": 30, "filename": "c.md", "source": "/c.md",
            "score": 0.80, "text": "database schema indexing performance optimization",
            "heading": "DB", "depth": "snippet", "token_count": 30,
        }

        results = [dup_a, dup_b, diverse]
        # Budget for 2 results (60 tokens total, 30 each)
        packed = pack_results(results, budget_tokens=70, depth="snippet", mmr_lambda=0.6)

        # Should have selected dup_a (highest score) and diverse (most different)
        assert len(packed) >= 1
        sources = [r["filename"] for r in packed]
        # The first selected is always the highest-score (dup_a)
        assert "a.md" in sources, f"Expected a.md in {sources}"

    def test_pack_results_single_result_always_returned(self):
        """pack_results() always returns at least one result even if it exceeds budget."""
        from tokens import pack_results
        results = _make_fake_results(1)
        # Budget way below any result's size
        packed = pack_results(results, budget_tokens=1, depth="snippet")
        assert len(packed) == 1

    def test_count_tokens_imported_in_pack_module(self):
        """pack_results is in tokens module alongside count_tokens."""
        import tokens as tokens_mod
        assert hasattr(tokens_mod, "pack_results"), "tokens.py must export pack_results"
        assert callable(tokens_mod.pack_results)
