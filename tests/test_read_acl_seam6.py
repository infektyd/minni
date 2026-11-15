"""Reality-bench seam 6: read-ACL tightening.

1. can_read_document granted cross-agent reads to ANY author id that merely
   contained "wiki"/"handoff" as a substring. Only the "wiki:" / "handoff"
   prefixes (and the page_type set) are shared surfaces.
2. expand_result bumped documents.access_count BEFORE the ACL check, so a
   denied expand still moved decay/ranking stats.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from minni.principal import EffectivePrincipal, can_read_document
from minni.retrieval import RetrievalEngine
from test_drill_identifier_kind import _make_db, _seed


def _codex():
    return EffectivePrincipal(
        agent_id="codex", capabilities=["search", "read"], allowed_vault_roots=["/tmp/v"]
    )


def _doc(agent, page_type="knowledge"):
    return {"path": "/tmp/v/x.md", "agent": agent, "privacy_level": "safe",
            "page_type": page_type, "page_status": "accepted"}


def test_substring_author_is_not_a_shared_surface():
    p = _codex()
    assert can_read_document(p, "*", _doc("kiwiki-bot")) is False
    assert can_read_document(p, "*", _doc("nohandoff-agent")) is False


def test_real_shared_surfaces_still_readable():
    p = _codex()
    assert can_read_document(p, "*", _doc("wiki:concept")) is True
    assert can_read_document(p, "*", _doc("handoff")) is True
    assert can_read_document(p, "*", _doc("gemini", page_type="wiki")) is True


def test_denied_expand_does_not_bump_access_count(tmp_path):
    db_obj, cfg = _make_db(tmp_path, "acl6.db")
    conn = db_obj._get_conn()
    secret = _seed(conn, "/tmp/v/secret.md", "gemini", ["gemini private body"],
                   privacy="private", page_type="knowledge")
    eng = RetrievalEngine(db_obj, cfg)
    out = eng.expand_result(secret, principal=_codex(), id_kind="doc")
    assert out is None
    n = conn.execute("SELECT access_count FROM documents WHERE doc_id=?", (secret,)).fetchone()[0]
    assert (n or 0) == 0, "denied expand must not touch access stats"
