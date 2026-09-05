"""#336: unusable compact_summary inbox files must have a drain.

A file the distillation pass cannot use is never archived or quarantined by
anything: `quarantine_stale_agent_mismatch` gates on STOP_KINDS (so an explicit
`compact_summary` kind is excluded) and `quarantine_afm_dead_letter` is
filename-scoped to afm-drafts-/afm-pruning-. So the file stays in the inbox and
is re-processed and re-dropped on every tick — measured 2, 4, 6 over three
passes with both files still on disk, ~96 re-drops/day/file at the 900s
consolidation interval.

Four cohorts, all deterministic (the same file produces the identical skip
forever): unreadable payload, non-object payload, agent mismatch, empty
summary.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

DAY = 86400.0


def _write(inbox: Path, name: str, payload, *, age_days: float = 61.0) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8",
    )
    old = time.time() - age_days * DAY
    os.utime(path, (old, old))
    return path


def _moved(inbox: Path):
    q = inbox / "quarantine"
    if not q.is_dir():
        return []
    return [p for p in q.glob("*.json") if not p.name.endswith(".reason.json")]


def _reasons(inbox: Path):
    return {
        json.loads(s.read_text())["reason"]
        for s in (inbox / "quarantine").glob("*.reason.json")
    }


def _summary(agent_id="unknown", text="a real summary"):
    return {
        "kind": "compact_summary",
        "agent_id": agent_id,
        "summary_text": text,
        "slug": "s",
    }


# ── all four cohorts drain ─────────────────────────────────────────────────


def test_all_four_unusable_cohorts_are_drained(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", '{"kind": "compact_summary", "sum')
    _write(inbox, "c-malformed.json", "[]")
    _write(inbox, "c-mismatch.json", _summary(agent_id="some-other-agent"))
    _write(inbox, "c-empty.json", _summary(text="   "))

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert result["quarantined"] == 4, result
    assert not list(inbox.glob("*.json"))
    assert len(_moved(inbox)) == 4
    assert _reasons(inbox) == {
        "_inbox_unreadable",
        "_inbox_malformed",
        "_compact_agent_mismatch",
        "_compact_empty_summary",
    }


def test_a_usable_compact_summary_is_never_touched(tmp_path):
    """The whole point is that only UNUSABLE files move. Draining a file the
    pass can still consume would destroy real memory input."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    good = _write(inbox, "c-good.json", _summary())

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert result["quarantined"] == 0
    assert good.exists()


def test_slug_alias_stamped_compact_is_not_drained(tmp_path):
    """Parity with test_slug_alias_stamped_agent_id_is_not_mismatch:
    leftover agent_id=agy in agy-vault (canonical gemini) is usable, so
    the compact drain must not classify it as _compact_agent_mismatch."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "agy-vault" / "inbox"
    good = _write(inbox, "c-agy.json", _summary(agent_id="agy"))

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert result["quarantined"] == 0
    assert good.exists()


def test_other_kinds_are_never_touched(tmp_path):
    """Other kinds share this inbox and are drained by their own pass."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    handoff = _write(inbox, "handoff.json", {"kind": "handoff", "task": "t"})
    stop = _write(inbox, "stop.json", {
        "kind": "stop_candidates", "candidates": ["x"], "agent_id": "unknown",
    })

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert result["quarantined"] == 0
    assert handoff.exists() and stop.exists()


# ── the established contract: move, never delete; grace window ─────────────


def test_the_drain_never_deletes_the_payload(tmp_path):
    """os.replace only — an operator has to be able to inspect what was
    dropped and why."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-mismatch.json", _summary(agent_id="other", text="keep me"))

    quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    moved = _moved(inbox)
    assert len(moved) == 1
    assert json.loads(moved[0].read_text())["summary_text"] == "keep me"


def test_a_fresh_file_stays_inside_the_grace_window(tmp_path):
    """The TTL is a grace window: a file written minutes ago might still be
    corrected upstream before the next sweep."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    fresh = _write(inbox, "c-unreadable.json", "{oops", age_days=0.5)

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert result["quarantined"] == 0
    assert fresh.exists()


def test_dry_run_moves_nothing(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")

    result = quarantine_unusable_compact_summaries(None, [inbox], dry_run=True)

    assert result["would_quarantine"] == 1
    assert result["quarantined"] == 0
    assert list(inbox.glob("*.json"))


def test_the_drain_is_idempotent(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")

    quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)
    again = quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert again["quarantined"] == 0


# ── #335 linkage: the health count returns to zero after a real drain ──────


def test_health_unusable_count_returns_to_zero_after_a_drain(tmp_path):
    """The acceptance test tying #336 back to #335: the live count that #335
    put on the health surface must actually clear once a drain runs. Before
    this drain existed it could only ever stay non-zero."""
    from minni.afm_passes.compact_distillation import count_unusable_compact_files
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")
    _write(inbox, "c-malformed.json", "[]")
    _write(inbox, "c-mismatch.json", _summary(agent_id="other"))

    assert count_unusable_compact_files([inbox], fallback_principal="unknown")["files"] == 3

    quarantine_unusable_compact_summaries(None, [inbox], dry_run=False)

    assert count_unusable_compact_files([inbox], fallback_principal="unknown")["files"] == 0


def test_the_count_and_the_drain_agree_on_the_cohort(tmp_path):
    """If health counted a file the drain would not take, the number could
    never reach zero — a signal that permanently overstates the problem."""
    from minni.afm_passes.compact_distillation import count_unusable_compact_files
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")
    _write(inbox, "c-malformed.json", "[]")
    _write(inbox, "c-mismatch.json", _summary(agent_id="other"))
    _write(inbox, "c-empty.json", _summary(text=""))
    _write(inbox, "c-good.json", _summary())
    _write(inbox, "handoff.json", {"kind": "handoff", "task": "t"})

    counted = count_unusable_compact_files([inbox], fallback_principal="unknown")["files"]
    drained = quarantine_unusable_compact_summaries(None, [inbox], dry_run=True)

    assert counted == drained["would_quarantine"] == 4


# ── the drain must be WIRED, not merely importable ─────────────────────────


def test_afm_loop_drains_unusable_compact_summaries(tmp_path, monkeypatch):
    """A drain disconnected from the loop is a fix nothing can reach — the
    exact shape Bugbot caught on #305."""
    import asyncio

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cons = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons["distill_compact_summaries"] = True
    cons["inbox_quarantine_ttl_days"] = 14

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")
    _write(inbox, "c-mismatch.json", _summary(agent_id="some-other-agent"))

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        assert not list(inbox.glob("*.json")), "the loop must drain them"
        assert len(_moved(inbox)) == 2
        snap = obs.metrics_snapshot()
        assert snap.get("compact_summary_quarantined_total") == 2, snap
    finally:
        obs.METRICS.reset()


def test_a_loop_tick_leaves_a_usable_summary_in_place(tmp_path, monkeypatch):
    """Wiring a drain into the hot loop must not eat live input."""
    import asyncio

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cons = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons["distill_compact_summaries"] = True

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")
    good = _write(inbox, "c-good.json", _summary())

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        # A usable summary is CONSUMED and archived by the pass — it must not
        # be swept into quarantine, which is where dropped input goes.
        assert (inbox / ".archive" / good.name).is_file(), sorted(
            str(p.relative_to(inbox)) for p in inbox.rglob("*")
        )
        assert not (inbox / "quarantine" / good.name).exists()
    finally:
        obs.METRICS.reset()


# ── the count and the drain must agree AT THE CALL SITES, not just in theory ─
#
# Every test above uses an `unknown-vault` inbox, where _principal_for_inbox
# returns "unknown" whatever the fallback is — which is exactly why a health
# caller that let fallback_principal default was invisible. A BARE vault/inbox
# takes the fallback directly, so it discriminates.


def _bare_vault_inbox(tmp_path: Path) -> Path:
    return tmp_path / "vault" / "inbox"


def test_a_bare_vault_classifies_by_the_fallback_principal(tmp_path):
    """Divergence proof: with fallback 'unknown' the classifier flags the
    codex-owned file (usable to a codex-principal pass) and misses the
    unknown-owned one that actually drains."""
    from minni.afm_passes.compact_distillation import count_unusable_compact_files

    inbox = _bare_vault_inbox(tmp_path)
    _write(inbox, "a.json", _summary(agent_id="codex"))
    _write(inbox, "b.json", _summary(agent_id="unknown"))

    as_unknown = count_unusable_compact_files([inbox], fallback_principal="unknown")
    as_codex = count_unusable_compact_files([inbox], fallback_principal="codex")

    assert as_unknown["files"] == 1
    assert as_codex["files"] == 1
    # Same total, DIFFERENT file — which is why the totals alone hid this.
    drained = quarantine_for(inbox, fallback_principal="codex")
    assert [p.name for p in drained] == ["b.json"]


def quarantine_for(inbox: Path, *, fallback_principal: str):
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    quarantine_unusable_compact_summaries(
        None, [inbox], fallback_principal=fallback_principal, dry_run=False,
    )
    return _moved(inbox)


def test_health_count_uses_the_same_fallback_as_the_drain(tmp_path, monkeypatch):
    """Kills the shipped bug: the health caller defaulted fallback_principal
    while the drain passed the configured value, so health flagged a file the
    pass consumes and missed the one that drains — and never reached zero."""
    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    from test_inbox_quarantine import _real_lifecycle_db  # noqa: E402

    _real_lifecycle_db(tmp_path, monkeypatch)
    import minni.config as cfg_mod

    cons = (
        (getattr(cfg_mod.DEFAULT_CONFIG, "afm_loop_schedule", {}) or {})
        .setdefault("passes", {})
        .setdefault("consolidation", {})
    )
    monkeypatch.setitem(cons, "inbox_fallback_principal", "codex")

    inbox = _bare_vault_inbox(tmp_path)
    _write(inbox, "usable-for-codex.json", _summary(agent_id="codex"))

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    files = rep["memory_lifecycle"]["compact_distillation_unusable"]["files"]
    assert files == 0, (
        "a file the pass can consume under the configured principal must not "
        f"be counted unusable: {rep['memory_lifecycle']}"
    )


def test_the_loop_passes_the_configured_ttl_to_the_drain(tmp_path, monkeypatch):
    """The TTL is the guard against sweeping a file mid-write; a loop that
    stopped forwarding it would be invisible."""
    from minni.afm_passes import inbox_quarantine as iq

    seen = {}

    def _spy(config, inboxes=None, **kwargs):
        seen.update(kwargs)
        return {"quarantined": 0, "would_quarantine": 0, "quarantined_files": []}

    monkeypatch.setattr(iq, "quarantine_unusable_compact_summaries", _spy)

    import asyncio

    from minni.minnid_runtime.afm import afm_loop_runner

    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    db_obj, cfg = _make_loop_db(tmp_path)
    cons = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons["distill_compact_summaries"] = True
    cons["inbox_quarantine_ttl_days"] = 3
    cons["inbox_fallback_principal"] = "codex"

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-unreadable.json", "{oops")

    ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
    asyncio.run(afm_loop_runner(ctx))

    assert seen.get("ttl_days") == 3, seen
    assert seen.get("fallback_principal") == "codex", seen


def test_a_mismatch_only_backlog_still_reaches_the_drain(tmp_path, monkeypatch):
    """The loop gate must watch every drop cohort: a backlog of only
    agent-mismatch files would otherwise never drain and the count never
    clear."""
    import asyncio

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    db_obj, cfg = _make_loop_db(tmp_path)
    cons = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons["distill_compact_summaries"] = True

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write(inbox, "c-mismatch.json", _summary(agent_id="some-other-agent"))

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))
        assert not list(inbox.glob("*.json")), "mismatch-only backlog must drain"
    finally:
        obs.METRICS.reset()


def test_a_file_written_seconds_ago_is_never_swept(tmp_path):
    """A summary being written is transiently truncated; os.replace would move
    the inode out from under the writer's open fd and the completed file would
    land in quarantine/."""
    from minni.afm_passes.inbox_quarantine import quarantine_unusable_compact_summaries

    inbox = tmp_path / "unknown-vault" / "inbox"
    mid_write = _write(inbox, "c-partial.json", '{"kind": "compact_su', age_days=0.0)

    result = quarantine_unusable_compact_summaries(
        None, [inbox], fallback_principal="unknown", ttl_days=0.0, dry_run=False,
    )

    assert result["quarantined"] == 0, "TTL=0 must not defeat the mid-write floor"
    assert mid_write.exists()
