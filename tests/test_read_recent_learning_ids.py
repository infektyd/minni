"""S1a-D: the daemon ``read`` response carries structured recent-learning ids.

The plugin keys each boot learning by ``learning_id`` (use-detector, S1b), but
the ``read`` handler only returned the rendered text. The handler now adds two
ADDITIVE fields beside ``agent_id`` / ``context``:

* ``recent_learnings``: ``[{learning_id, category, line, content}]`` in exactly
  the rendered order. ``line`` is the exact rendered line; ``content`` is the
  stored content clipped to 4,000 characters.
* ``daemon_version``: the daemon's existing version source of truth.

The rendered ``context`` text is injected natively as Layer 1 by the codex and
grok hosts, so it must stay BYTE-IDENTICAL for the same DB state. The golden
string below is derived by hand from today's rendering rule
(``  - [category] content[:150] (conf=x.y)``) and was verified against the
pre-change handler before the fields were added.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_issue_437_learnings_dedup import (  # noqa: E402,F401
    _make_db,
    setup_hermetic_principals,
)

T0 = 1_700_000_000.0

_LONG_SENTENCE = "Long synthetic learning sentence. "  # 34 chars
_LONG_CONTENT = _LONG_SENTENCE * 200  # 6,800 chars
_MULTIBYTE_CONTENT = "é" * 4100  # 4,100 code points, 8,200 UTF-8 bytes
_WAL_STORED = "  use wal mode for concurrent readers. "

_HEADER = "## Learnings (main)"


def _insert(c, n, category, content, *, confidence=1.0, status="active",
            agent="main", content_hash=None, superseded_by=None):
    cols = ["agent_id", "category", "content", "confidence", "created_at",
            "status", "superseded_by"]
    vals = [agent, category, content, confidence, T0 + n, status, superseded_by]
    if content_hash is not None:  # column only exists once governance ensured it
        cols.append("content_hash")
        vals.append(content_hash)
    c.execute(
        f"INSERT INTO learnings ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    return c.lastrowid


def _seed_fixture(db_obj) -> dict:
    """Synthetic learnings covering every filter the boot render applies.

    Returns ``{"selected": [ids in rendered order], "dropped": [ids]}``.
    """
    from minni.minnid_runtime.governance import ensure_content_hash_column

    # Production state: governance adds learnings.content_hash on first learn,
    # so `read` takes its primary SELECT (which carries the stored hash).
    assert ensure_content_hash_column(db_obj) is True
    ids: dict[str, int] = {}
    with db_obj.cursor() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(learnings)").fetchall()}
        assert {"status", "content_hash"} <= cols, cols

        # Older whitespace/case duplicate: the NEWER row (WAL_NEW) is kept.
        ids["wal_old"] = _insert(c, 1, "fact", "Use WAL mode for concurrent readers.")
        ids["wal_new"] = _insert(c, 2, "fact", _WAL_STORED)
        # Non-active rows.
        ids["superseded_status"] = _insert(
            c, 3, "fact", "Obsolete status-superseded learning.", status="superseded"
        )
        # Low-signal rows: prompt echo, test-runner output, audit echo.
        ids["prompt_echo"] = _insert(
            c, 4, "general", "Executed prompt: check git status and run pytest"
        )
        ids["test_output"] = _insert(c, 5, "general", "=== 42 passed in 0.88s ===")
        ids["audit_echo"] = _insert(
            c, 6, "general", "## [2026-10-16T12:00:00Z] minni_recall | ok"
        )
        ids["arch"] = _insert(
            c, 7, "architecture",
            "Always bound cross-encoder batch size on CPU.", confidence=0.5,
        )
        ids["null_conf"] = _insert(
            c, 8, "fact", "Prefer explicit paths over cwd-relative paths.",
            confidence=None,
        )
        ids["long"] = _insert(c, 9, "lesson", _LONG_CONTENT)
        ids["multibyte"] = _insert(c, 10, "fact", _MULTIBYTE_CONTENT)
        # Different text, same STORED content_hash: deduped by the hash column.
        ids["hash_old"] = _insert(
            c, 11, "fact", "Pin interpreter version in CI matrix.",
            content_hash="synthetic-hash-pin",
        )
        ids["hash_new"] = _insert(
            c, 12, "fact", "Pin the interpreter version in the CI matrix.",
            content_hash="synthetic-hash-pin",
        )
        # Another agent's row, a rejected row, and an active-status row whose
        # superseded_by pointer is set: none may appear.
        ids["other_agent"] = _insert(
            c, 13, "fact", "Other agent private learning.", agent="codex"
        )
        ids["rejected"] = _insert(c, 14, "fact", "Rejected learning row.", status="rejected")
        ids["pointer_superseded"] = _insert(
            c, 15, "fact", "Superseded by pointer row.",
            superseded_by=ids["wal_old"],
        )

    selected = [
        ids["hash_new"], ids["multibyte"], ids["long"], ids["null_conf"],
        ids["arch"], ids["wal_new"],
    ]
    dropped = [v for k, v in ids.items() if v not in selected]
    return {"ids": ids, "selected": selected, "dropped": dropped}


# Hand-derived from the rendering rule; NOT produced by the handler under test.
_GOLDEN_LINES = [
    "  - [fact] Pin the interpreter version in the CI matrix. (conf=1.0)",
    "  - [fact] " + "é" * 150 + " (conf=1.0)",
    "  - [lesson] " + _LONG_SENTENCE * 4 + "Long synthetic" + " (conf=1.0)",
    "  - [fact] Prefer explicit paths over cwd-relative paths. (conf=1.0)",
    "  - [architecture] Always bound cross-encoder batch size on CPU. (conf=0.5)",
    "  - [fact]   use wal mode for concurrent readers.  (conf=1.0)",
]
_GOLDEN_CONTEXT = "\n" + _HEADER + "\n" + "\n".join(_GOLDEN_LINES)


def _read(minnid, agent="main"):
    resp = minnid._handle_read({"agent_id": agent}, 1)
    assert "error" not in resp, resp
    return resp["result"]


def _rendered_learning_lines(context: str) -> list[str]:
    """The lines of the Learnings section as the text consumer sees them."""
    rows = context.split("\n")
    start = rows.index(_HEADER) + 1
    out = []
    for row in rows[start:]:
        if not row.startswith("  - ["):
            break
        out.append(row)
    return out


def test_recent_learnings_match_rendered_lines_one_to_one_in_order(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    seeded = _seed_fixture(db_obj)

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    recent = result["recent_learnings"]
    rendered = _rendered_learning_lines(result["context"])

    assert [r["learning_id"] for r in recent] == seeded["selected"]
    assert [r["line"] for r in recent] == rendered
    assert [r["line"] for r in recent] == _GOLDEN_LINES
    assert [r["category"] for r in recent] == [
        "fact", "fact", "lesson", "fact", "architecture", "fact",
    ]
    for row in recent:
        assert set(row) == {"learning_id", "category", "line", "content"}
        assert isinstance(row["learning_id"], int)
        # The rendered line carries the stored category.
        assert row["line"].startswith(f"  - [{row['category']}] ")


def test_recent_learning_content_is_clipped_at_4000_characters(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    seeded = _seed_fixture(db_obj)

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    by_id = {r["learning_id"]: r for r in result["recent_learnings"]}
    ids = seeded["ids"]

    assert len(_LONG_CONTENT) > 4000
    assert by_id[ids["long"]]["content"] == _LONG_CONTENT[:4000]
    assert len(by_id[ids["long"]]["content"]) == 4000
    # Clip counts characters, not UTF-8 bytes.
    assert by_id[ids["multibyte"]]["content"] == _MULTIBYTE_CONTENT[:4000]
    assert len(by_id[ids["multibyte"]]["content"]) == 4000
    # Short content is the raw stored value, not stripped or normalized.
    assert by_id[ids["wal_new"]]["content"] == _WAL_STORED
    assert by_id[ids["arch"]]["content"] == "Always bound cross-encoder batch size on CPU."
    # A clipped `content` never alters the 150-char rendered line.
    assert len(by_id[ids["long"]]["line"]) < 200


def test_text_context_is_byte_identical_to_pre_change_rendering(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    _seed_fixture(db_obj)

    result = _read(minnid)

    assert result["context"] == _GOLDEN_CONTEXT
    assert result["context"].encode("utf-8") == _GOLDEN_CONTEXT.encode("utf-8")
    assert result["agent_id"] == "main"


def test_dedupe_and_low_signal_filtering_are_reflected_identically(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    seeded = _seed_fixture(db_obj)

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    structured_ids = [r["learning_id"] for r in result["recent_learnings"]]
    rendered = _rendered_learning_lines(result["context"])

    # Same cardinality in both channels, and no dropped row in either.
    assert len(structured_ids) == len(rendered) == len(seeded["selected"])
    assert not set(structured_ids) & set(seeded["dropped"])
    context = result["context"]
    for text in (
        "Executed prompt:",
        "42 passed in 0.88s",
        "minni_recall | ok",
        "Obsolete status-superseded learning.",
        "Rejected learning row.",
        "Superseded by pointer row.",
        "Other agent private learning.",
        "Pin interpreter version in CI matrix.",
    ):
        assert text not in context
    # Whitespace/case duplicate collapses to the NEWER row in both channels.
    ids = seeded["ids"]
    assert ids["wal_new"] in structured_ids
    assert ids["wal_old"] not in structured_ids
    assert context.lower().count("use wal mode for concurrent readers.") == 1
    # Stored-content_hash duplicate collapses to the NEWER row in both channels.
    assert ids["hash_new"] in structured_ids
    assert ids["hash_old"] not in structured_ids


def test_recent_learnings_cap_at_ten_newest_distinct(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    ids = []
    with db_obj.cursor() as c:
        for n in range(1, 15):
            ids.append(
                _insert(c, n, "fact", f"Distinct synthetic learning number {n:02d}.")
            )

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    newest_ten = list(reversed(ids))[:10]
    assert [r["learning_id"] for r in result["recent_learnings"]] == newest_ten
    assert [r["line"] for r in result["recent_learnings"]] == (
        _rendered_learning_lines(result["context"])
    )
    assert len(_rendered_learning_lines(result["context"])) == 10


def test_no_learnings_yields_empty_list_and_unchanged_text(tmp_path, monkeypatch):
    import minni.minnid as minnid

    _make_db(tmp_path, monkeypatch)

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    assert result["recent_learnings"] == []
    assert result["context"] == "No context for 'main'."
    assert result["agent_id"] == "main"


def test_all_learnings_low_signal_yields_empty_list(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        _insert(c, 1, "general", "Executed prompt: check git status and run pytest")
        _insert(c, 2, "general", "=== 42 passed in 0.88s ===")

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    assert result["recent_learnings"] == []
    assert "## Learnings" not in result["context"]


def test_recent_learnings_on_legacy_schema_without_content_hash(tmp_path, monkeypatch):
    """The fixture DB has no learnings.content_hash column until governance adds
    it; `read` then falls back to a narrower SELECT and hashes in Python. The
    new fields must ride that path too, byte-for-byte consistent with the text."""
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(learnings)").fetchall()}
        assert "content_hash" not in cols, cols
        old = _insert(c, 1, "fact", "Use WAL mode for concurrent readers.")
        new = _insert(c, 2, "fact", _WAL_STORED)
        low = _insert(c, 3, "general", "=== 42 passed in 0.88s ===")
        arch = _insert(
            c, 4, "architecture",
            "Always bound cross-encoder batch size on CPU.", confidence=0.5,
        )

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    recent = result["recent_learnings"]
    assert [r["learning_id"] for r in recent] == [arch, new]
    assert old not in [r["learning_id"] for r in recent]
    assert low not in [r["learning_id"] for r in recent]
    assert [r["line"] for r in recent] == _rendered_learning_lines(result["context"])
    assert result["context"] == (
        "\n" + _HEADER + "\n"
        "  - [architecture] Always bound cross-encoder batch size on CPU. (conf=0.5)\n"
        "  - [fact]   use wal mode for concurrent readers.  (conf=1.0)"
    )


def test_blob_stored_content_stays_json_serialisable(tmp_path, monkeypatch):
    """A learning stored as a BLOB (bytes in the TEXT column) already renders
    today as ``b'...'``. The structured fields must not turn that into a
    non-serialisable ``bytes`` value and break the whole ``read`` response."""
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        blob_id = _insert(c, 1, "fact", b"Blob stored learning.")

    resp = minnid._handle_read({"agent_id": "main"}, 1)

    assert "error" not in resp, resp
    result = json.loads(json.dumps(resp))["result"]  # raises on bytes
    expected_line = "  - [fact] b'Blob stored learning.' (conf=1.0)"
    assert result["context"] == "\n" + _HEADER + "\n" + expected_line
    assert result["recent_learnings"] == [
        {
            "learning_id": blob_id,
            "category": "fact",
            "line": expected_line,
            "content": "b'Blob stored learning.'",
        }
    ]


def test_learning_reads_rows_match_structured_ids(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    seeded = _seed_fixture(db_obj)

    result = _read(minnid)

    assert "recent_learnings" in result, sorted(result)
    with db_obj.cursor() as c:
        read_ids = sorted(
            r[0]
            for r in c.execute(
                "SELECT learning_id FROM learning_reads "
                "WHERE agent_id = 'main' AND source = 'minnid.read'"
            ).fetchall()
        )
    assert read_ids == sorted(r["learning_id"] for r in result["recent_learnings"])
    assert read_ids == sorted(seeded["selected"])


def test_daemon_version_is_the_existing_source_of_truth(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    _seed_fixture(db_obj)

    result = _read(minnid)

    assert "daemon_version" in result, sorted(result)
    assert isinstance(result["daemon_version"], str)
    assert result["daemon_version"].strip()
    assert result["daemon_version"] == minnid.VERSION
    assert minnid._recall_context().version == minnid.VERSION


def test_response_is_json_serialisable_and_additive(tmp_path, monkeypatch):
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    _seed_fixture(db_obj)

    resp = minnid._handle_read({"agent_id": "main"}, 1)

    wire = json.loads(json.dumps(resp))
    assert wire["result"]["context"] == _GOLDEN_CONTEXT
    assert {"agent_id", "context"} <= set(wire["result"])
    assert set(wire["result"]) == {
        "agent_id", "context", "recent_learnings", "daemon_version",
    }
