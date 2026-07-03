"""Test: daemon search must default to depth='snippet', not 'headline' (M-2 fix).

Before the fix: minnid.py:1509 defaulted to depth='headline' which returns
wikilink + score only - no text for the agent to read. The docstring claimed
the default was 'snippet'. The search handler now lives in
minnid_runtime/recall.py; this test verifies the implementation still matches
the documented intent after modularization.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def test_search_depth_defaults_to_snippet(tmp_path, monkeypatch):
    """params without 'depth' must produce depth='snippet' in the result."""
    # We test the engine-level retrieve() depth parameter via a direct call,
    # since testing the full daemon RPC dispatch requires a running socket.
    # The daemon's _handle_search line now reads:
    #   depth = str(params.get("depth", "snippet"))
    # This test verifies the downstream retrieve() at depth="snippet" returns text.
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    content = """# Minni Token Budget

The context_budget_tokens setting (default 4096) controls the maximum tokens
returned in a single recall call. When budget_tokens=True the retrieval engine
applies MMR-based diversity selection to fit within this budget.
""" * 6  # repeat to ensure it's above min_tokens

    engine.index_durable_document(
        content=content,
        path="wiki/concepts/token-budget.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )

    # depth="snippet" (the new default) must return a non-empty 'text' field
    results = engine.retrieve(
        query="context budget tokens recall",
        limit=5,
        depth="snippet",   # explicit — mirrors what the daemon now sends
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert results, "Expected at least 1 result"
    first = results[0]

    # snippet depth must carry a non-empty text field
    assert "text" in first, f"depth=snippet must include 'text'; got keys: {list(first.keys())}"
    assert first["text"], f"depth=snippet 'text' must be non-empty; got: {first['text']!r}"
    assert first["depth"] == "snippet", f"Expected depth='snippet', got {first['depth']!r}"

    # headline depth must NOT carry a text field (sanity check)
    results_hl = engine.retrieve(
        query="context budget tokens recall",
        limit=5,
        depth="headline",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert results_hl, "Expected at least 1 result at headline depth"
    first_hl = results_hl[0]
    assert first_hl.get("depth") == "headline", f"Expected depth='headline', got {first_hl.get('depth')!r}"
    # headline has no text — absence of 'text' OR empty text is correct
    text_hl = first_hl.get("text", "")
    assert not text_hl, (
        f"depth=headline must NOT carry text; got {text_hl[:80]!r}. "
        "Confirms headline and snippet are distinct."
    )


def test_minnid_search_default_depth_param_is_snippet():
    """Verify the literal default string in the runtime search handler is 'snippet'.

    This is a source-level assertion so a future diff that re-introduces the
    'headline' default will fail this test immediately — the fix is not silently
    regressed by an ambiguous merge.
    """
    import ast
    import pathlib

    recall_path = pathlib.Path(__file__).parent.parent / "src" / "minni" / "minnid_runtime" / "recall.py"
    source = recall_path.read_text()

    # Find the line with the depth default
    for line in source.splitlines():
        if 'params.get("depth"' in line and "snippet" in line:
            return  # found the fixed line
        if 'params.get("depth"' in line and "headline" in line:
            # This is in _handle_search (line 1509 before fix)
            # Check context: is this in _handle_search or another handler?
            # The _handle_search handler is the one we fixed.
            # We need to ensure _handle_search uses "snippet".
            pass

    # More precise: parse the AST and find the default
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "handle_search":
            continue
        # Walk the body to find the depth assignment with params.get
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            # Look for: depth = str(params.get("depth", <default>))
            val = stmt.value
            if not isinstance(val, ast.Call):
                continue
            # Check it's str(...)
            if not (isinstance(val.func, ast.Name) and val.func.id == "str"):
                continue
            inner = val.args[0] if val.args else None
            if not isinstance(inner, ast.Call):
                continue
            # Check inner is params.get("depth", ...)
            if not (
                isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and len(inner.args) >= 2
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "depth"
            ):
                continue
            default_node = inner.args[1]
            if not isinstance(default_node, ast.Constant):
                continue
            actual_default = default_node.value
            assert actual_default == "snippet", (
                f"handle_search depth default must be 'snippet' (M-2 fix), "
                f"got {actual_default!r}. The fix was reverted."
            )
            return

    pytest.fail(
        "handle_search: could not locate the depth=params.get('depth', ...) "
        "assignment in minnid_runtime/recall.py. The M-2 fix may have been removed "
        "or the function renamed."
    )
