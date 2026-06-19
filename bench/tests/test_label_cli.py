"""Labeling/review CLI + Drafter tests (§5.2, s2(a)).

Covers:
- StubDrafter is deterministic, offline (no network), derives a single-hop
  question per doc from its first heading, leaves drafts UNAPPROVED;
- review approve/edit/reject stamps approved_by and never auto-promotes a draft
  with no decision;
- the draft writer routes through the private-path guard (off-private raises).
"""

import json

import pytest

from membench import config
from membench.goldset import (
    BAND_NEGATIVE,
    BAND_SINGLE_HOP,
    GoldSetError,
    load_jsonl,
    validate_set,
)
from membench.label_cli import StubDrafter, apply_review, build_parser, main
from membench.paths import PrivatePathError, assert_private_path


def _fixture_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "membench/fixtures/corpus_synthetic"


def test_stub_drafter_is_deterministic_and_offline(corpus):
    d = StubDrafter()
    a = d.draft(corpus)
    b = d.draft(corpus)
    assert [i.to_json() for i in a] == [i.to_json() for i in b]
    # One draft per corpus doc, all single-hop, all unapproved.
    assert len(a) == len(corpus.doc_ids())
    for item in a:
        assert item.band == BAND_SINGLE_HOP
        assert item.approved_by is None
        assert item.drafted_by == "stub_drafter"
        assert len(item.gold_doc_ids) == 1


def test_stub_drafter_uses_first_heading(corpus):
    d = StubDrafter()
    by_doc = {i.gold_doc_ids[0]: i for i in d.draft(corpus)}
    # 01-aurora-protocol.md heading is "# Aurora Protocol".
    item = by_doc["01-aurora-protocol.md"]
    assert "Aurora Protocol" in item.question


def test_drafts_validate_as_clean_against_corpus(corpus):
    d = StubDrafter()
    drafts = d.draft(corpus)
    report = validate_set(drafts, set(corpus.doc_ids()))
    assert report.ok, report.errors()


def test_review_approve_stamps_approved_by(corpus):
    drafts = StubDrafter().draft(corpus)
    target = drafts[0].id
    approved = apply_review(
        drafts, {target: {"action": "approve"}}, approved_by="operator"
    )
    assert len(approved) == 1
    assert approved[0].id == target
    assert approved[0].approved_by == "operator"


def test_review_no_decision_is_not_auto_promoted(corpus):
    drafts = StubDrafter().draft(corpus)
    # Empty decisions -> nothing approved (no silent auto-promotion, §5.2).
    assert apply_review(drafts, {}, approved_by="operator") == []


def test_review_reject_excludes_item(corpus):
    drafts = StubDrafter().draft(corpus)
    out = apply_review(
        drafts, {drafts[0].id: {"action": "reject"}}, approved_by="operator"
    )
    assert out == []


def test_review_edit_applies_changes(corpus):
    drafts = StubDrafter().draft(corpus)
    target = drafts[0].id
    out = apply_review(
        drafts,
        {target: {"action": "edit", "question": "edited question?", "notes": "fixed"}},
        approved_by="operator",
    )
    assert out[0].question == "edited question?"
    assert out[0].notes == "fixed"
    assert out[0].approved_by == "operator"


def test_draft_writer_off_private_raises(tmp_path):
    """The private-path guard the draft CLI uses raises off-private by default."""
    with pytest.raises(PrivatePathError):
        assert_private_path(tmp_path / "gold_drafts.jsonl")  # allow_public False


def test_review_rejects_invalid_edit(corpus):
    """apply_review runs check_item on edits: a bogus band / non-existent doc id
    is REJECTED, never written silently (item 4)."""
    drafts = StubDrafter().draft(corpus)
    target = drafts[0].id
    with pytest.raises(GoldSetError):
        apply_review(
            drafts,
            {target: {"action": "edit", "band": "bogus_band"}},
            approved_by="operator",
            corpus_doc_ids=set(corpus.doc_ids()),
        )
    with pytest.raises(GoldSetError):
        apply_review(
            drafts,
            {target: {"action": "edit", "gold_doc_ids": ["nonexistent.md"]}},
            approved_by="operator",
            corpus_doc_ids=set(corpus.doc_ids()),
        )


def test_review_cli_guards_non_dict_decisions(corpus, tmp_path):
    """A decisions file that is a top-level array (or a non-dict per-item value)
    raises a clear error, not a deep traceback (item 5)."""
    from membench.goldset import dump_jsonl

    drafts = StubDrafter().draft(corpus)
    drafts_path = tmp_path / "drafts.jsonl"
    dump_jsonl(drafts, drafts_path, allow_public=True)
    decisions_path = tmp_path / "decisions.json"

    # Top-level array.
    decisions_path.write_text(json.dumps([{"action": "approve"}]), encoding="utf-8")
    with pytest.raises(GoldSetError):
        main(
            [
                "review",
                "--drafts", str(drafts_path),
                "--decisions", str(decisions_path),
                "--out", str(tmp_path / "out.jsonl"),
                "--approved-by", "operator",
                "--allow-public",
            ]
        )

    # Non-dict per-item value.
    decisions_path.write_text(
        json.dumps({drafts[0].id: "approve"}), encoding="utf-8"
    )
    with pytest.raises(GoldSetError):
        main(
            [
                "review",
                "--drafts", str(drafts_path),
                "--decisions", str(decisions_path),
                "--out", str(tmp_path / "out.jsonl"),
                "--approved-by", "operator",
                "--allow-public",
            ]
        )


def test_draft_cli_end_to_end_writes_jsonl(tmp_path):
    """main(['draft', ...]) with --allow-public on the public synthetic fixture
    writes a JSONL of stub drafts (item 7)."""
    out_path = tmp_path / "drafts.jsonl"
    rc = main(
        [
            "draft",
            "--corpus-dir", str(_fixture_dir()),
            "--hash", config.FIXTURE_CORPUS_HASH,
            "--out", str(out_path),
            "--no-scrubbed",
            "--allow-public",
        ]
    )
    assert rc == 0
    drafts = load_jsonl(out_path)
    assert len(drafts) > 0
    for d in drafts:
        assert d.band == BAND_SINGLE_HOP
        assert d.approved_by is None


def test_draft_cli_off_private_raises(tmp_path):
    """main(['draft', ...]) WITHOUT --allow-public to a non-private out raises
    the private-path guard before any write (item 7)."""
    with pytest.raises(PrivatePathError):
        main(
            [
                "draft",
                "--corpus-dir", str(_fixture_dir()),
                "--hash", config.FIXTURE_CORPUS_HASH,
                "--out", str(tmp_path / "leak.jsonl"),
                "--no-scrubbed",
            ]
        )


def test_validate_cli_end_to_end(tmp_path, capsys):
    """main(['validate', ...]) prints per-band counts and returns 0 on a clean
    set, and --finalized fails (rc=1) on an under-floor set (item 10)."""
    from membench.goldset import GoldItem, dump_jsonl

    items = [
        GoldItem(
            id=f"v-{i}",
            question="q?",
            band=BAND_SINGLE_HOP,
            gold_doc_ids=["doc-001.md"],
            gold_fact="fact",
            drafted_by="stub",
            approved_by="operator",
        )
        for i in range(3)
    ]
    gold_path = tmp_path / "gold.jsonl"
    dump_jsonl(items, gold_path, allow_public=True)

    # Plain validate (no corpus): clean set -> rc 0, per-band counts printed.
    rc = main(["validate", "--gold", str(gold_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "per-band counts" in out
    assert "single_hop" in out

    # --finalized on a 3-item set must FAIL (rc 1) — nowhere near 150/floors.
    rc = main(["validate", "--gold", str(gold_path), "--finalized"])
    assert rc == 1


def test_review_unknown_action_raises(corpus):
    """apply_review raises ValueError on an unknown action (item 13)."""
    drafts = StubDrafter().draft(corpus)
    with pytest.raises(ValueError):
        apply_review(
            drafts, {drafts[0].id: {"action": "skip"}}, approved_by="operator"
        )


def test_review_cli_corpus_dir_rejects_nonexistent_gold_doc(corpus, tmp_path):
    """The 'review' CLI --corpus-dir branch loads the corpus and validates edited
    gold_doc_ids against it: an edit referencing a NON-existent doc id is rejected
    through the CLI path (item 8)."""
    from membench.goldset import dump_jsonl

    drafts = StubDrafter().draft(corpus)
    drafts_path = tmp_path / "drafts.jsonl"
    dump_jsonl(drafts, drafts_path, allow_public=True)

    # Edit the first draft to point at a doc id that is NOT in the corpus.
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            {drafts[0].id: {"action": "edit", "gold_doc_ids": ["nonexistent.md"]}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(GoldSetError):
        main(
            [
                "review",
                "--drafts", str(drafts_path),
                "--decisions", str(decisions_path),
                "--out", str(tmp_path / "out.jsonl"),
                "--approved-by", "operator",
                "--allow-public",
                "--corpus-dir", str(_fixture_dir()),
                "--hash", config.FIXTURE_CORPUS_HASH,
                "--no-scrubbed",
            ]
        )


def test_review_subcommand_is_wired():
    """The 'review' subcommand exists in the CLI parser (item 4/12)."""
    parser = build_parser()
    ns = parser.parse_args(
        [
            "review",
            "--drafts",
            "d.jsonl",
            "--decisions",
            "dec.json",
            "--out",
            "out.jsonl",
            "--approved-by",
            "operator",
        ]
    )
    assert ns.cmd == "review"
    assert ns.func.__name__ == "cmd_review"


def test_review_cli_writes_approved_private_guarded(corpus, tmp_path, monkeypatch):
    """End-to-end 'review' CLI: stamps approved_by, and the writer is private-path
    guarded (off-private raises without --allow-public) (item 4/12)."""
    # Stage drafts on disk (allow_public for the tmp write).
    from membench.goldset import dump_jsonl

    drafts = StubDrafter().draft(corpus)
    drafts_path = tmp_path / "drafts.jsonl"
    dump_jsonl(drafts, drafts_path, allow_public=True)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps({drafts[0].id: {"action": "approve"}}), encoding="utf-8"
    )

    # Off-private out without --allow-public MUST raise.
    out_path = tmp_path / "approved.jsonl"
    with pytest.raises(PrivatePathError):
        main(
            [
                "review",
                "--drafts",
                str(drafts_path),
                "--decisions",
                str(decisions_path),
                "--out",
                str(out_path),
                "--approved-by",
                "operator",
            ]
        )

    # With --allow-public the write succeeds and approved_by is stamped.
    rc = main(
        [
            "review",
            "--drafts",
            str(drafts_path),
            "--decisions",
            str(decisions_path),
            "--out",
            str(out_path),
            "--approved-by",
            "operator",
            "--allow-public",
        ]
    )
    assert rc == 0
    approved = load_jsonl(out_path)
    assert len(approved) == 1
    assert approved[0].approved_by == "operator"
