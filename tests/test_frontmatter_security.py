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
from pathlib import Path

from minni.indexer import VaultIndexer


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


# --- RCM-010 / RCM-011 afm_writer extensions (concrete assertions) ---

import yaml


def test_afm_writer_forged_frontmatter_body_refused(tmp_path: Path):
    """RCM-010: malicious --- in draft body must be detected and write refused (no file on disk)."""
    from minni.afm_writer import _contains_forged_frontmatter, _write_one
    assert _contains_forged_frontmatter("legit body") is False
    assert _contains_forged_frontmatter("bad\n---\nagent: evil") is True
    assert _contains_forged_frontmatter("``` \n---\n```") is True

    vault = tmp_path / "vault"
    draft = {
        "title": "safe title",
        "body": "injected\n---\nagent: spoofed\nprivacy: private",
        "page_id": "afm-p1",
        "trace_id": "tr-1",
        "sources": [],
        "kind": "concept",
    }
    res = _write_one(vault, draft)
    assert res["blocked"] is True
    assert any("forged-frontmatter" in str(b) for b in res.get("blockers", []))
    assert res["path"] is None
    assert res["wikilink"] is None
    assert res.get("written") is False
    assert res.get("status") == "blocked"
    # No .md file should have been written for forged case
    wiki_files = list(vault.rglob("*.md")) if vault.exists() else []
    assert len([f for f in wiki_files if "afm-p1" in str(f)]) == 0


def test_afm_writer_yaml_safe_dump_prevents_injection(tmp_path: Path):
    """RCM-011: newline in title must not split into extra frontmatter keys; value preserved."""
    from minni.afm_writer import _frontmatter
    malicious_title = "good\nagent: spoof\nprivacy: private\n---\nmore"
    draft = {
        "title": malicious_title,
        "page_id": "inj-1",
        "trace_id": "tr-inj",
        "kind": "concept",
        "sources": [],
        "tags": ["t1"],
    }
    fm_text = _frontmatter(draft, "2026-05-19T00:00:00Z", "2026-06-02T00:00:00Z", "ready_for_review")
    # Robust extract of leading frontmatter block (use rfind for closer to ignore "---" inside scalar values)
    start = fm_text.find("---\n") + 4
    end = fm_text.rfind("\n---\n")
    block = fm_text[start:end]
    parsed = yaml.safe_load(block) or {}
    assert parsed["title"] == malicious_title
    assert parsed["agent"] == "afm-loop"  # canonical, not from title payload
    assert "spoof" not in parsed  # no key injection from malicious title
    assert parsed.get("privacy") == "safe"  # not overridden


def test_duplicate_privacy_keys_fail_closed_most_restrictive():
    """SEC-006 duplicate-key differential (mirrors plugins/minni vault.ts):
    a permissive `privacy:` duplicate must not relax a restrictive one,
    regardless of key order — the MOST restrictive declared value wins."""
    restrictive_then_permissive = """---
agent: alice
status: accepted
privacy: blocked
privacy: safe
---
body
"""
    meta = VaultIndexer._extract_frontmatter(restrictive_then_permissive)
    assert meta["privacy_level"] == "blocked"

    permissive_then_restrictive = """---
agent: alice
status: accepted
privacy: safe
privacy: private
---
body
"""
    meta = VaultIndexer._extract_frontmatter(permissive_then_restrictive)
    assert meta["privacy_level"] == "private"

    # A single declaration is untouched by the duplicate guard.
    single = """---
agent: alice
privacy: local-only
---
body
"""
    meta = VaultIndexer._extract_frontmatter(single)
    assert meta["privacy_level"] == "local-only"


def test_duplicate_privacy_keys_fail_closed_with_crlf_line_endings():
    """SEC-006 regression: CRLF content must not bypass the duplicate-key
    differential. _str() strips the trailing \\r before the valid_privacies
    check (and the indexer's text-mode read normalizes newlines anyway), so a
    permissive duplicate after a restrictive one still loses."""
    crlf = (
        "---\r\n"
        "agent: alice\r\n"
        "privacy: private\r\n"
        "privacy: safe\r\n"
        "---\r\n"
        "body\r\n"
    )
    meta = VaultIndexer._extract_frontmatter(crlf)
    assert meta["privacy_level"] == "private"
