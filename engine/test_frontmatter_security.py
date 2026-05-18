#!/usr/bin/env python3
"""G21: Adversarial frontmatter security tests (TDD - fail before YAML parser fix).

Replaces brittle re.search-over-whole-content in indexer.py _extract_frontmatter.

Corpus covers:
- Forged frontmatter in body (after real --- block) must NOT override agent/status/privacy
- Duplicate keys in YAML block (safe_load takes last; no crash, no body leak)
- Malformed YAML / missing --- / no frontmatter → safe defaults, no injection
- Body with --- lines inside code fences or paragraphs must be ignored
- Sigil, type, tags, sources parsed correctly only from block

Uses PyYAML safe_load for structured adversarial resistance (SEC-011/SEC-018).
"""

import pytest

from indexer import VaultIndexer


def test_body_forged_frontmatter_ignored():
    """Body content after real frontmatter must not spoof agent/status via regex."""
    content = """---
agent: "alice"
status: accepted
privacy: safe
type: wiki
sigil: "A"
---
# Real page

This is body text.
agent: "evil"
status: rejected
privacy: private
---
more body
"""
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "alice"
    assert meta["page_status"] == "accepted"
    assert meta["privacy_level"] == "safe"
    assert meta.get("page_type") == "wiki"
    # forged in body must not leak
    assert "evil" not in str(meta)


def test_duplicate_keys_in_frontmatter_block():
    """YAML duplicate key: safe_load behavior is last-wins; ensure no crash and consistent."""
    content = """---
agent: alice
status: draft
agent: bob
status: accepted
privacy: safe
---
body
"""
    meta = VaultIndexer._extract_frontmatter(content)
    # last wins for agent/status
    assert meta["agent"] == "bob"
    assert meta["page_status"] == "accepted"


def test_malformed_yaml_falls_back_safely():
    content = """---
agent: [unclosed
status: accepted
---
body with : colons and "quotes
"""
    meta = VaultIndexer._extract_frontmatter(content)
    # must not raise, must return clamped defaults or partial safe
    assert isinstance(meta, dict)
    assert meta.get("page_status") in {"draft", "candidate", "accepted", "superseded", "rejected", "expired"}
    assert meta.get("privacy_level") in {"safe", "local-only", "private", "blocked"}


def test_no_frontmatter_uses_defaults():
    content = "# Just a markdown body\nNo frontmatter here.\nagent: spoof"
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "unknown"
    assert meta["page_status"] == "candidate"
    assert meta["privacy_level"] == "safe"


def test_code_fence_with_fake_frontmatter_ignored():
    content = """---
agent: real
status: accepted
privacy: safe
---
# Title

```yaml
agent: forged
status: rejected
```
body text
"""
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "real"
    assert meta["page_status"] == "accepted"


def test_wiki_style_full_frontmatter_parsed():
    """Ensure YAML lists and strings work (converge toward wiki_indexer shape)."""
    content = """---
title: "My Wiki"
type: decision
tags: [governance, security]
sources: ["[[other]]", "doc.md"]
status: accepted
privacy: safe
agent: wiki-bot
sigil: "W"
---
Body content here.
"""
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "wiki-bot"
    assert meta["page_type"] == "decision"
    assert meta["page_status"] == "accepted"
