"""P2.3 legacy alias repair diagnostic: focused read-only tests.

Disposable SQLite, no model/live/network. Proves: true 1:1 candidates with
exact identity and edge counts; ambiguous verdicts for N:1 aggregates,
restricted/retired nodes, owner mismatches, missing learning/canonical, and
absent join tables; malformed aliases; human-readable report contents; and
zero store mutation from the diagnostic.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.durable_projection import durable_doc_path
from minni.minnid_runtime.health import (
    diagnose_legacy_aliases,
    format_legacy_alias_report,
)


@pytest.fixture
def store(tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    config = SovereignConfig(
        db_path=str(tmp_path / "alias.db"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "notes"),
        faiss_index_path=str(tmp_path / "index.faiss"),
        reranker_enabled=False, attribution_enabled=False,
    )
    db = SovereignDB(config)
    run_migrations(db._get_conn())
    yield db
    db.close()


def _learning(db, content, agent="codex", status="active", superseded_by=None):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence,"
            " created_at, status, superseded_by) VALUES (?, 'general', ?,"
            " 1.0, 0.0, ?, ?)",
            (agent, content, status, superseded_by),
        )
        return c.lastrowid


def _alias_doc(db, learning_id, agent="codex", **over):
    cols = {
        "path": f"learning://{learning_id}",
        "agent": f"learning:{agent}",
        "sigil": "L",
        "page_status": "accepted",
        "privacy_level": "safe",
        "page_type": "learning",
    }
    cols.update(over)
    names = ", ".join(cols)
    with db.cursor() as c:
        c.execute(
            f"INSERT INTO documents ({names}) VALUES "
            f"({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
        return c.lastrowid


def _canonical_doc(db, agent, content, **over):
    from minni.durable_projection import durable_metadata

    meta = durable_metadata(content)
    cols = {
        "path": durable_doc_path(agent, "", db.config.vault_path, content),
        "agent": agent,
        "sigil": meta["sigil"],
        "page_status": "accepted",
        "privacy_level": "safe",
        "page_type": "learning",
        "memory_kind": "learning",
    }
    cols.update(over)
    names = ", ".join(cols)
    with db.cursor() as c:
        c.execute(
            f"INSERT INTO documents ({names}) VALUES "
            f"({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
        return c.lastrowid


def _join(db, learning_id, doc_id):
    with db.cursor() as c:
        c.execute(
            "INSERT OR IGNORE INTO learning_documents (learning_id, doc_id,"
            " created_at) VALUES (?, ?, 0.0)",
            (learning_id, doc_id),
        )


def _fts(db, doc_id, path, content, agent="codex"):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil)"
            " VALUES (?, ?, ?, ?, 'L')",
            (doc_id, path, content, agent),
        )


def _edge(db, source, target, link_type="derived_from"):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO memory_links (source_doc_id, target_doc_id,"
            " link_type, created_at) VALUES (?, ?, ?, 0.0)",
            (source, target, link_type),
        )


def _evidence_doc(db):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, agent, page_status, privacy_level,"
            " page_type) VALUES ('/vault/evidence.md', 'codex', 'accepted',"
            " 'safe', 'note')",
        )
        return c.lastrowid


def _fingerprint(db):
    """Full content hash over every diagnostic-relevant table."""
    with db.cursor() as c:
        parts = []
        for table in ("learnings", "documents", "learning_documents",
                      "memory_links", "vault_fts", "chunk_embeddings"):
            names = {r[1] for r in
                     c.execute(f"PRAGMA table_info({table})").fetchall()}
            if not names:
                continue
            for row in c.execute(f"SELECT * FROM {table}").fetchall():
                parts.append((table, tuple(row)))
    return hashlib.sha256(repr(sorted(
        parts, key=repr)).encode()).hexdigest()


def test_clean_store_has_no_findings(store):
    diag = diagnose_legacy_aliases(store)
    assert diag.scanned == 0 and diag.findings == ()
    assert (diag.candidates, diag.ambiguous, diag.malformed) == (0, 0, 0)
    assert diag.truncated is False


def test_true_candidate_with_edges(store):
    lid = _learning(store, "Lock code lives in the deploy checklist.")
    alias = _alias_doc(store, lid)
    content = "Lock code lives in the deploy checklist."
    canon = _canonical_doc(store, "codex", content)
    _join(store, lid, canon)
    _fts(store, canon, durable_doc_path(
        "codex", "", store.config.vault_path, content), content)
    target = _evidence_doc(store)
    _edge(store, alias, target)
    before = _fingerprint(store)

    diag = diagnose_legacy_aliases(store)
    assert diag.scanned == 1 and diag.candidates == 1
    (finding,) = diag.findings
    assert finding.kind == "migrate_candidate"
    assert finding.alias_doc_id == alias and finding.learning_id == lid
    assert finding.agent == "codex"
    assert finding.canonical_doc_id == canon
    assert finding.canonical_path == durable_doc_path(
        "codex", "", store.config.vault_path, content)
    assert finding.out_edge_count == 1 and finding.in_edge_count == 0
    assert "superseded" in finding.proposal
    assert str(canon) in finding.proposal
    assert _fingerprint(store) == before


def test_n1_aggregate_never_conflated(store):
    content = "Shared checklist content."
    lid1 = _learning(store, content)
    lid2 = _learning(store, content)
    alias = _alias_doc(store, lid1)
    canon = _canonical_doc(store, "codex", content)
    _join(store, lid1, canon)  # exact join proven...
    _join(store, lid2, canon)  # ...but a sibling claimant shares the node
    before = _fingerprint(store)

    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0 and diag.ambiguous == 1
    (finding,) = diag.findings
    assert finding.kind == "ambiguous"
    assert "N:1" in finding.reason and str(lid2) in finding.reason
    assert finding.proposal == ""
    assert _fingerprint(store) == before


def test_restricted_and_retired_stay_untouched(store):
    content = "Quarantined content."
    lid = _learning(store, content)
    _alias_doc(store, lid)
    _canonical_doc(store, "codex", content, privacy_level="blocked")
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    assert "restricted" in diag.findings[0].reason

    content2 = "Retired content."
    lid2 = _learning(store, content2, status="superseded", superseded_by=lid)
    _alias_doc(store, lid2)
    _canonical_doc(store, "codex", content2)
    diag = diagnose_legacy_aliases(store)
    retired = [f for f in diag.findings if f.learning_id == lid2][0]
    assert retired.kind == "ambiguous" and "retired" in retired.reason


def test_owner_mismatch_missing_and_malformed(store):
    lid = _learning(store, "Owned content.", agent="codex")
    _alias_doc(store, lid, agent="mallory")
    _alias_doc(store, 4242)  # parses, but no such learning
    _alias_doc(store, 0, **{"path": "learning://not-an-id"})
    _alias_doc(store, 0, **{"path": "learning://"})
    diag = diagnose_legacy_aliases(store)
    by_path = {f.alias_path: f for f in diag.findings}
    assert by_path[f"learning://{lid}"].kind == "ambiguous"
    assert "owner" in by_path[f"learning://{lid}"].reason
    assert by_path["learning://4242"].kind == "ambiguous"
    assert "no longer exists" in by_path["learning://4242"].reason
    assert by_path["learning://not-an-id"].kind == "malformed"
    assert by_path["learning://"].kind == "malformed"
    assert diag.malformed == 2


def test_missing_canonical_reports_repair_first(store):
    lid = _learning(store, "Unindexed content.")
    _alias_doc(store, lid)
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    (finding,) = diag.findings
    assert "index/repair" in finding.reason
    assert finding.canonical_path is not None


def test_report_text_explains_and_limits(store):
    lid = _learning(store, "Report content.")
    _alias_doc(store, lid)
    text = format_legacy_alias_report(diagnose_legacy_aliases(store))
    assert "read-only" in text and f"learning://{lid}" in text
    assert "Limitations" in text and "never merged" in text


def test_limit_truncates_deterministically(store):
    for i in range(3):
        lid = _learning(store, f"Bulk content {i}.")
        _alias_doc(store, lid)
    diag = diagnose_legacy_aliases(store, limit=2)
    assert diag.scanned == 3 and len(diag.findings) == 2
    assert diag.truncated is True
    ids = [f.alias_doc_id for f in diag.findings]
    assert ids == sorted(ids)


def test_canonical_wrong_owner(store):
    """Same durable path, different stored owner: collision, not proof."""
    content = "Colliding content."
    lid = _learning(store, content, agent="codex")
    _alias_doc(store, lid)
    same_path = durable_doc_path("codex", "", store.config.vault_path, content)
    canon = _canonical_doc(store, "mallory", content, path=same_path)
    _join(store, lid, canon)
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    (finding,) = diag.findings
    assert finding.kind == "ambiguous"
    assert "wrong-owner" in finding.reason


def test_missing_exact_join(store):
    """URI + owner + content evidence without the join row is unproven."""
    content = "Unjoined content."
    lid = _learning(store, content)
    _alias_doc(store, lid)
    canon = _canonical_doc(store, "codex", content)
    _fts(store, canon, durable_doc_path(
        "codex", "", store.config.vault_path, content), content)
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    (finding,) = diag.findings
    assert "no exact (learning, canonical) join" in finding.reason
    assert finding.proposal == ""


def test_alias_foreign_claim(store):
    """Alias mapped to another learning is a foreign claim, not private."""
    lid = _learning(store, "Private content.")
    other = _learning(store, "Other content.")
    alias = _alias_doc(store, lid)
    _join(store, other, alias)
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    (finding,) = diag.findings
    assert "foreign/aggregate claim" in finding.reason


def test_content_conflict_and_missing_evidence(store):
    """Stale FTS collision and absent FTS are ambiguity, never proof."""
    content = "Live content here."
    lid = _learning(store, content)
    _alias_doc(store, lid)
    canon = _canonical_doc(store, "codex", content)
    _join(store, lid, canon)
    _fts(store, canon, durable_doc_path(
        "codex", "", store.config.vault_path, content), "Stale other text.")
    diag = diagnose_legacy_aliases(store)
    assert diag.candidates == 0
    assert "conflicts" in diag.findings[0].reason

    content2 = "Unevidenced content."
    lid2 = _learning(store, content2)
    _alias_doc(store, lid2)
    canon2 = _canonical_doc(store, "codex", content2)
    _join(store, lid2, canon2)
    diag = diagnose_legacy_aliases(store)
    second = [f for f in diag.findings if f.learning_id == lid2][0]
    assert second.kind == "ambiguous"
    assert "no FTS content evidence" in second.reason


def test_newline_and_huge_ids_malformed(store):
    _alias_doc(store, 0, **{"path": "learning://7\n"})
    _alias_doc(store, 0, **{"path": "learning://99999999999999999999"})
    _alias_doc(store, 0, **{"path": "learning://" + "9" * 5000})
    diag = diagnose_legacy_aliases(store)
    assert diag.malformed == 3 and diag.candidates == 0
    by_path = {f.alias_path: f for f in diag.findings}
    assert by_path["learning://7\n"].kind == "malformed"
    assert "bounded" in by_path["learning://99999999999999999999"].reason


def test_offline_cli_uses_explicit_read_only_database(store, tmp_path):
    import subprocess
    from pathlib import Path

    lid = _learning(store, "CLI diagnostic content.")
    _alias_doc(store, lid)
    before = _fingerprint(store)
    script = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_legacy_aliases.py"
    command = [sys.executable, str(script), "--db", str(store.config.db_path),
               "--vault", str(store.config.vault_path)]
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "AMBIGUOUS" in result.stdout and "no canonical" in result.stdout
    assert _fingerprint(store) == before
    missing = tmp_path / "must-not-create.db"
    command[3] = str(missing)
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 1
    assert not missing.exists()


def test_diagnostic_preserves_outer_transaction(store):
    with store.transaction() as cursor:
        cursor.execute("INSERT INTO learnings (agent_id, content, category, created_at) VALUES ('codex', 'caller pending', 'general', 0)")
        before = cursor.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        diagnose_legacy_aliases(store)
        assert cursor.connection.in_transaction
        assert cursor.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == before
        cursor.connection.rollback()
    with store.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 0
