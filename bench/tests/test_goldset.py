"""Gold-set schema + validator tests (§5.2, §5.3, s2(a)).

Covers:
- accept/reject of individual items, incl. negative-band empties;
- missing-doc-id rejection for positive bands;
- recall-ceiling WARN when |gold_doc_ids| > k;
- MIN_PER_BAND + >=150 finalized check, exercised on a SYNTHETIC 150+-item
  fixture generated IN-TEST (not hand-authored, not real data);
- the finalized check is NOT vacuously satisfiable (unapproved items, an
  under-floor band, and a short total each fail it).
"""

import pytest

from membench import config
from membench.goldset import (
    BAND_CONTRADICTION,
    BAND_MULTI_HOP,
    BAND_NEGATIVE,
    BAND_RECENCY,
    BAND_SINGLE_HOP,
    BANDS,
    FinalizedError,
    GoldItem,
    GoldSetError,
    MIN_TOTAL,
    check_finalized,
    check_item,
    dump_jsonl,
    load_jsonl,
    min_for_band,
    validate_set,
)
from membench.paths import PrivatePathError


# A small corpus doc-id universe the synthetic gold set references.
CORPUS_IDS = {f"doc-{i:03d}.md" for i in range(60)}


def _pos(id_, band, docs, approved=True):
    return GoldItem(
        id=id_,
        question=f"q for {id_}?",
        band=band,
        gold_doc_ids=list(docs),
        gold_fact="the gold fact",
        drafted_by="stub",
        approved_by="operator" if approved else None,
    )


def _neg(id_, approved=True):
    return GoldItem(
        id=id_,
        question=f"q for {id_}?",
        band=BAND_NEGATIVE,
        gold_doc_ids=[],
        gold_fact="nothing in the corpus answers this — genuinely absent",
        drafted_by="stub",
        approved_by="operator" if approved else None,
    )


# ── single-item checks ───────────────────────────────────────────────────────
def test_accept_valid_positive():
    rep = check_item(_pos("a", BAND_SINGLE_HOP, ["doc-001.md"]), CORPUS_IDS)
    assert rep.ok and not rep.warnings


def test_accept_valid_negative():
    rep = check_item(_neg("n"), CORPUS_IDS)
    assert rep.ok


def test_reject_unknown_band():
    rep = check_item(
        GoldItem(id="x", question="q?", band="bogus", gold_doc_ids=["doc-001.md"]),
        CORPUS_IDS,
    )
    assert not rep.ok and any("not one of" in e for e in rep.errors)


def test_reject_negative_with_gold_docs():
    bad = GoldItem(
        id="n",
        question="q?",
        band=BAND_NEGATIVE,
        gold_doc_ids=["doc-001.md"],  # negatives MUST be empty
        gold_fact="why",
    )
    rep = check_item(bad, CORPUS_IDS)
    assert not rep.ok and any("empty gold_doc_ids" in e for e in rep.errors)


def test_reject_negative_without_gold_fact():
    bad = GoldItem(id="n", question="q?", band=BAND_NEGATIVE, gold_doc_ids=[])
    rep = check_item(bad, CORPUS_IDS)
    assert not rep.ok and any("gold_fact" in e for e in rep.errors)


def test_reject_positive_empty_gold_docs():
    rep = check_item(_pos("a", BAND_SINGLE_HOP, []), CORPUS_IDS)
    assert not rep.ok and any("non-empty gold_doc_ids" in e for e in rep.errors)


def test_reject_positive_nonexistent_doc():
    rep = check_item(_pos("a", BAND_MULTI_HOP, ["doc-999.md"]), CORPUS_IDS)
    assert not rep.ok and any("non-existent" in e for e in rep.errors)


def test_reject_positive_empty_gold_fact():
    """A positive-band item with a blank gold_fact is rejected (item 8)."""
    bad = GoldItem(
        id="a",
        question="q?",
        band=BAND_SINGLE_HOP,
        gold_doc_ids=["doc-001.md"],
        gold_fact="",  # blank gold_fact on a positive band
    )
    rep = check_item(bad, CORPUS_IDS)
    assert not rep.ok and any("gold_fact" in e for e in rep.errors)


def test_reject_positive_duplicate_gold_doc_ids():
    """gold_doc_ids with a repeated id within one item is rejected (item 10)."""
    bad = _pos("a", BAND_MULTI_HOP, ["doc-001.md", "doc-001.md"])
    rep = check_item(bad, CORPUS_IDS)
    assert not rep.ok and any("duplicate" in e for e in rep.errors)


def test_validate_set_detects_duplicate_item_ids():
    """Two items sharing an id make the set report not-ok (item 9)."""
    items = [
        _pos("dup", BAND_SINGLE_HOP, ["doc-001.md"]),
        _pos("dup", BAND_SINGLE_HOP, ["doc-002.md"]),
    ]
    report = validate_set(items, CORPUS_IDS)
    assert not report.ok
    assert report.duplicate_ids == ["dup"]


def test_validate_set_does_not_inflate_band_count_on_duplicate_id():
    """per_band_counts must count each id ONCE — a duplicate-ID item must not pad
    an under-floor band into apparent compliance (item 2)."""
    items = [
        _pos("dup", BAND_SINGLE_HOP, ["doc-001.md"]),
        _pos("dup", BAND_SINGLE_HOP, ["doc-002.md"]),  # same id -> not counted
        _pos("other", BAND_MULTI_HOP, ["doc-003.md"]),
    ]
    report = validate_set(items, CORPUS_IDS)
    assert report.duplicate_ids == ["dup"]
    # Despite TWO single_hop items in the list, the duplicate id counts once.
    assert report.per_band_counts[BAND_SINGLE_HOP] == 1
    assert report.per_band_counts[BAND_MULTI_HOP] == 1


def test_from_dict_missing_required_field_raises():
    """from_dict over a JSON object missing a required field raises (item 8)."""
    with pytest.raises(GoldSetError) as exc:
        GoldItem.from_dict({"question": "q?", "band": BAND_SINGLE_HOP})  # no id
    assert "missing required" in str(exc.value)


def test_check_item_warns_when_corpus_not_provided():
    """corpus_doc_ids=None on a positive item: ok=True but a non-silent warning
    flags the skipped existence check (item 9 — the skip must never be silent)."""
    rep = check_item(_pos("a", BAND_SINGLE_HOP, ["doc-001.md"]), corpus_doc_ids=None)
    assert rep.ok is True
    assert any("corpus not provided" in w for w in rep.warnings)


def test_warn_recall_ceiling():
    docs = [f"doc-{i:03d}.md" for i in range(config.K + 1)]
    rep = check_item(_pos("a", BAND_MULTI_HOP, docs), CORPUS_IDS)
    assert rep.ok  # not an error, only a caution
    assert any("recall@k ceiling" in w for w in rep.warnings)


def test_unknown_field_rejected():
    with pytest.raises(GoldSetError):
        GoldItem.from_dict(
            {"id": "a", "question": "q?", "band": BAND_SINGLE_HOP, "bogus": 1}
        )


# ── synthetic 150+ finalized fixture (generated in-test) ─────────────────────
def _synthetic_finalized_set() -> list[GoldItem]:
    """Build a >=150-item gold set meeting every MIN_PER_BAND, all approved.

    Generated programmatically — NEVER hand-authored 150 files, NEVER real data.
    Each band gets a margin above its floor; total comfortably exceeds 150.
    """
    items: list[GoldItem] = []
    n = 0

    def doc(i):
        return f"doc-{i % 60:03d}.md"

    # Per-band counts: floors + headroom -> total > 150.
    plan = {
        BAND_SINGLE_HOP: min_for_band(BAND_SINGLE_HOP) + 15,  # 40
        BAND_MULTI_HOP: min_for_band(BAND_MULTI_HOP) + 15,  # 40
        BAND_CONTRADICTION: min_for_band(BAND_CONTRADICTION) + 10,  # 30
        BAND_RECENCY: min_for_band(BAND_RECENCY) + 10,  # 30
        BAND_NEGATIVE: min_for_band(BAND_NEGATIVE) + 10,  # 30
    }
    for band, count in plan.items():
        for _ in range(count):
            n += 1
            if band == BAND_NEGATIVE:
                items.append(_neg(f"g-{n:04d}"))
            elif band == BAND_MULTI_HOP:
                items.append(_pos(f"g-{n:04d}", band, [doc(n), doc(n + 1)]))
            else:
                items.append(_pos(f"g-{n:04d}", band, [doc(n)]))
    return items


def test_synthetic_set_validates_clean():
    items = _synthetic_finalized_set()
    report = validate_set(items, CORPUS_IDS)
    assert report.ok, report.errors()
    assert report.total >= MIN_TOTAL
    for band in BANDS:
        assert report.per_band_counts[band] >= min_for_band(band)


def test_finalized_passes_on_synthetic_set():
    items = _synthetic_finalized_set()
    report = check_finalized(items, CORPUS_IDS)  # must not raise
    assert report.total >= MIN_TOTAL


def test_finalized_rejects_short_total():
    items = _synthetic_finalized_set()[:100]  # under 150
    with pytest.raises(FinalizedError):
        check_finalized(items, CORPUS_IDS)


def test_finalized_rejects_under_floor_band():
    # Drop negatives below their floor while keeping total >= 150 via single-hop.
    items = [it for it in _synthetic_finalized_set() if it.band != BAND_NEGATIVE]
    items += [_neg(f"few-{i}") for i in range(min_for_band(BAND_NEGATIVE) - 1)]
    # pad single-hop so total stays >= 150
    items += [
        _pos(f"pad-{i}", BAND_SINGLE_HOP, ["doc-001.md"]) for i in range(40)
    ]
    assert len(items) >= MIN_TOTAL
    with pytest.raises(FinalizedError) as exc:
        check_finalized(items, CORPUS_IDS)
    assert "negative" in str(exc.value)


def test_jsonl_round_trip_is_lossless(tmp_path):
    """dump_jsonl -> load_jsonl preserves items incl. unicode, null approved_by,
    and empty lists (item 11). Off-private write needs allow_public=True."""
    items = [
        GoldItem(
            id="uni-1",
            question="¿Qué describe el café — naïve façade? 日本語",
            band=BAND_SINGLE_HOP,
            gold_doc_ids=["doc-001.md"],
            gold_fact="answer with ünïcode",
            drafted_by="stub",
            approved_by=None,  # null approved_by
            notes="",
        ),
        GoldItem(
            id="neg-1",
            question="nothing matches?",
            band=BAND_NEGATIVE,
            gold_doc_ids=[],  # empty list
            gold_fact="genuinely absent",
            drafted_by="stub",
            approved_by="operator",
        ),
    ]
    path = tmp_path / "gold.jsonl"
    dump_jsonl(items, path, allow_public=True)
    back = load_jsonl(path)
    assert back == items


def test_dump_jsonl_refuses_off_private(tmp_path):
    """dump_jsonl is private-path guarded at the library level (item 6)."""
    items = [_pos("a", BAND_SINGLE_HOP, ["doc-001.md"])]
    with pytest.raises(PrivatePathError):
        dump_jsonl(items, tmp_path / "leak.jsonl")  # allow_public defaults False


def test_finalized_rejects_unapproved_items():
    """Not vacuously satisfiable: 150 UNAPPROVED drafts must NOT finalize."""
    items = _synthetic_finalized_set()
    items[0].approved_by = None  # one unapproved item
    with pytest.raises(FinalizedError) as exc:
        check_finalized(items, CORPUS_IDS)
    assert "not approved" in str(exc.value)


def test_total_counts_unique_ids_only_at_floor(tmp_path):
    """149 UNIQUE ids + 1 DUPLICATE id == 150 list rows, but only 149 unique.

    GoldSetReport.total must count UNIQUE ids, so the >=150 finalized floor FAILS
    for the RIGHT reason (a total shortfall), not merely the duplicate-id error
    (item 1). Without the fix, len(item_reports)==150 would clear the floor.
    """
    # Build exactly 149 UNIQUE single-hop items (each its own id), all approved,
    # meeting the single-hop floor with room to spare, plus enough of the other
    # bands so ONLY the total floor (not a per-band floor) is at issue. To keep
    # the test focused, satisfy every band floor first, then pad single-hop to
    # exactly 149 unique items, then append ONE duplicate id (the 150th row).
    items: list[GoldItem] = []
    n = 0

    def _add_pos(band, docs):
        nonlocal n
        n += 1
        items.append(_pos(f"u-{n:04d}", band, docs))

    for _ in range(min_for_band(BAND_MULTI_HOP)):
        _add_pos(BAND_MULTI_HOP, ["doc-001.md", "doc-002.md"])
    for _ in range(min_for_band(BAND_CONTRADICTION)):
        _add_pos(BAND_CONTRADICTION, ["doc-003.md"])
    for _ in range(min_for_band(BAND_RECENCY)):
        _add_pos(BAND_RECENCY, ["doc-004.md"])
    for _ in range(min_for_band(BAND_NEGATIVE)):
        n += 1
        items.append(_neg(f"u-{n:04d}"))
    # Pad single-hop until we have exactly 149 UNIQUE-id items total.
    while len(items) < 149:
        _add_pos(BAND_SINGLE_HOP, ["doc-005.md"])
    assert len(items) == 149
    assert len({it.id for it in items}) == 149

    # Append the 150th ROW as a DUPLICATE id (reuses the first item's id).
    dup = _pos(items[0].id, BAND_SINGLE_HOP, ["doc-006.md"])
    items.append(dup)
    assert len(items) == 150  # 150 rows...
    report = validate_set(items, CORPUS_IDS)
    assert report.total == 149  # ...but only 149 UNIQUE ids counted
    assert report.duplicate_ids == [items[0].id]

    with pytest.raises(FinalizedError) as exc:
        check_finalized(items, CORPUS_IDS)
    msg = str(exc.value)
    # The failure MUST cite the total shortfall, not only the duplicate-id error.
    assert f"total 149 < MIN_TOTAL {MIN_TOTAL}" in msg


def test_reject_empty_id():
    """An empty id is a hard error (item 7)."""
    rep = check_item(GoldItem(id="", question="q?", band=BAND_SINGLE_HOP), CORPUS_IDS)
    assert not rep.ok and any("id is empty" in e for e in rep.errors)


def test_reject_whitespace_only_question():
    """A whitespace-only question is a hard error (item 7)."""
    rep = check_item(
        GoldItem(
            id="a",
            question="   ",
            band=BAND_SINGLE_HOP,
            gold_doc_ids=["doc-001.md"],
            gold_fact="fact",
        ),
        CORPUS_IDS,
    )
    assert not rep.ok and any("question is empty" in e for e in rep.errors)


def test_error_rendering_sanitizes_newline_in_id():
    """An item id containing a newline cannot inject a fake log line (item 5).

    A malicious/typo'd id like 'evil\\n[fake] forged' must render on a SINGLE
    line in errors() — repr() escapes the newline so no synthetic log prefix can
    be forged."""
    evil_id = "evil\n[forged] injected error"
    bad = GoldItem(id=evil_id, question="", band=BAND_SINGLE_HOP)  # empty question
    report = validate_set([bad], CORPUS_IDS)
    errs = report.errors()
    # Every rendered error is a single physical line (no embedded newline).
    for e in errs:
        assert "\n" not in e, f"error line contains a raw newline: {e!r}"
    # The literal injected text is not present as a standalone line prefix.
    assert not any(e.startswith("[forged] injected error") for e in errs)
    # The id is still represented (escaped) so the error is not anonymized.
    assert any("evil" in e for e in errs)
