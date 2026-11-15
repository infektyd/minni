"""Reality-bench seam 4: a consolidation batch whose ONLY decisions are
reject/redact must still be applied.

handle_daemon_compile gated apply_consolidation_result on promote/dedup/review
ids only, so a reject-only batch (no drafts, no promotes) left every rejected
candidate `proposed` forever — re-judged on every loop tick.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_afm_loop_promotion import _loop_context, _make_db, _seed_safe_candidate  # noqa: E402


def test_reject_only_batch_terminalizes_candidate(tmp_path, monkeypatch):
    from minni.minnid_runtime.afm import afm_loop_runner

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_db(tmp_path)
    # Deterministic auto-reject: "too short / trivial" — no model, no drafts.
    cid = _seed_safe_candidate(db_obj, "ok")
    ctx, _ = _loop_context(db_obj, cfg, ticks=1)

    asyncio.run(afm_loop_runner(ctx))

    with db_obj.cursor() as c:
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (cid,))
        status = c.fetchone()["status"]
        c.execute("SELECT COUNT(*) AS n FROM learnings")
        n_learn = c.fetchone()["n"]

    assert status != "proposed", "reject-only batch was never applied"
    assert status == "rejected", status
    assert n_learn == 0


def test_writer_lifecycle_accounting_counts_rejects():
    from minni.afm_writer import _lifecycle_apply_complete

    life = {"reject_candidate_ids": [7], "redact_candidate_ids": [8]}
    applied = {"rejected": 1, "redacted": 1, "errors": 0, "remaining_proposed": []}
    assert _lifecycle_apply_complete(applied, life) == (True, True)
    # Legacy shape (no remaining_proposed): requested must include reject/redact.
    legacy = {"rejected": 1, "redacted": 0, "errors": 0,
              "requested": {"reject": 1, "redact": 1}}
    assert _lifecycle_apply_complete(legacy, life) == (False, True)
