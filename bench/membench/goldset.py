"""Gold-set schema + validator (§5.2, §5.3).

Slice s2(a). A typed schema for a labeled gold item plus a validator that
enforces the spec's labeling invariants. Gold sets live on disk as JSONL (one
JSON object per line).

Schema (one item):
    { id, question, band, gold_doc_ids: [str], gold_fact: str,
      drafted_by: str, approved_by: str|null, notes: str }

Bands (the five, §5.3) — the canonical on-disk band values are:
    single_hop, multi_hop, contradiction, recency, negative

These map to the ``config.MIN_PER_BAND`` keys (which carry the spec's
human-readable hyphenated names) via :data:`BAND_TO_CONFIG_KEY`, so the
per-band minimums and the >=150 finalized check read from the single pinned
source in ``config.py`` rather than a second hardcoded table.

Validator rules:
- ``band`` ∈ the five canonical bands.
- ``band == "negative"`` -> ``gold_doc_ids`` MUST be empty AND ``gold_fact``
  must describe why nothing should match (non-empty).
- positive bands -> ``gold_doc_ids`` non-empty and EVERY id ∈ ``corpus.doc_ids()``.
- recall-ceiling caution: WARN if ``len(gold_doc_ids) > config.K`` (§6.1).
- finalized: each band meets ``MIN_PER_BAND`` AND total >= ``MIN_TOTAL`` (150).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

# ── Canonical bands (§5.3) ───────────────────────────────────────────────────
BAND_SINGLE_HOP = "single_hop"
BAND_MULTI_HOP = "multi_hop"
BAND_CONTRADICTION = "contradiction"
BAND_RECENCY = "recency"
BAND_NEGATIVE = "negative"

BANDS: tuple[str, ...] = (
    BAND_SINGLE_HOP,
    BAND_MULTI_HOP,
    BAND_CONTRADICTION,
    BAND_RECENCY,
    BAND_NEGATIVE,
)
POSITIVE_BANDS: frozenset[str] = frozenset(BANDS) - {BAND_NEGATIVE}

# Map canonical band -> the config.MIN_PER_BAND key (spec's hyphenated names).
BAND_TO_CONFIG_KEY: dict[str, str] = {
    BAND_SINGLE_HOP: "single-hop",
    BAND_MULTI_HOP: "multi-hop",
    BAND_CONTRADICTION: "contradiction",
    BAND_RECENCY: "recency-sensitive",
    BAND_NEGATIVE: "negatives",
}

# Total gold-set floor when finalized (§5.3: "Target >= 150").
MIN_TOTAL = 150

# ── Adversarial-input limits (items 5 & 6) ──────────────────────────────────
# A gold JSONL path can be operator/agent-supplied (the --gold CLI flag). An
# attacker who controls it could otherwise exhaust memory with a huge file or a
# 10 MB single field, or forge log lines via an oversized id. These caps bound
# every untrusted byte BEFORE it is held in RAM or echoed into a log message.
# Per-field maxima (bytes/chars): generous enough for any real label, tight
# enough to make a crafted field harmless.
MAX_FIELD_LEN: dict[str, int] = {
    "id": 256,
    "question": 1024,
    "gold_fact": 4096,
    "notes": 2048,
    "drafted_by": 256,
    "approved_by": 256,
}
# A single gold doc-id is a corpus-relative path; cap it like an id.
MAX_DOC_ID_LEN = 512
# A finalized set targets ~150 items; cap parsing well above that so a real set
# loads but an adversarial one cannot allocate unbounded GoldItems.
MAX_GOLD_ITEMS = 100_000
# Whole-file byte cap (a 150-item set is tens of KB; a few MB is ample headroom).
MAX_GOLD_FILE_BYTES = 8 * 1024 * 1024
# Truncate any value embedded in an error/log message to this many chars.
MAX_LOG_VALUE_LEN = 120


def min_for_band(band: str) -> int:
    """The per-band minimum from the single pinned source (``config``)."""
    return config.MIN_PER_BAND[BAND_TO_CONFIG_KEY[band]]


class GoldSetError(ValueError):
    """Raised when a gold item or set fails validation (§5.2/§5.3)."""


def _safe_id(item_id: str) -> str:
    """Render an item id safe for single-line error/log output (item 5).

    Item ids are loaded verbatim from operator/agent JSONL; an id containing a
    newline or control char would otherwise inject synthetic log lines (a fake
    "[fake-id] forged error" prefix) into errors()/warnings() output. ``repr``
    escapes newlines (``\\n``), carriage returns and other control bytes and
    quotes the value, so the rendered id can never break out onto a second line
    or forge a log prefix. The repr is ALSO truncated (item 6): a 10 MB id would
    otherwise produce a 10 MB log line and DoS any log consumer.
    """
    rendered = repr(item_id)
    if len(rendered) > MAX_LOG_VALUE_LEN:
        rendered = rendered[:MAX_LOG_VALUE_LEN] + "...(truncated)"
    return rendered


@dataclass
class GoldItem:
    """One labeled gold triple (§5.2).

    ``approved_by`` is ``None`` for a drafted-but-unapproved item; the review
    step stamps it. ``drafted_by`` records the drafter (agent or human).
    """

    id: str
    question: str
    band: str
    gold_doc_ids: list[str] = field(default_factory=list)
    gold_fact: str = ""
    drafted_by: str = ""
    approved_by: str | None = None
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "GoldItem":
        # Reject unknown keys so a typo'd field is caught, not silently dropped.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(d) - known
        if unknown:
            raise GoldSetError(f"gold item has unknown field(s): {sorted(unknown)}")
        missing = {"id", "question", "band"} - set(d)
        if missing:
            raise GoldSetError(f"gold item missing required field(s): {sorted(missing)}")
        # Cap every string field BEFORE constructing the item (items 5 & 6): an
        # adversarial JSONL must not be able to hold a multi-MB field in RAM or
        # forge a giant log line through an oversized id. Length is checked in
        # characters; a crafted field is rejected, not silently truncated.
        for fname, cap in MAX_FIELD_LEN.items():
            val = d.get(fname)
            if isinstance(val, str) and len(val) > cap:
                raise GoldSetError(
                    f"gold item field {fname!r} exceeds max length "
                    f"{cap} (got {len(val)})"
                )
        doc_ids = list(d.get("gold_doc_ids", []))
        for did in doc_ids:
            # A non-string element (int, None, dict) must be rejected up front:
            # a dict later crashes set(gold_doc_ids) with an unhashable TypeError,
            # and an int/None silently never matches a corpus id while still being
            # truthy — corrupting recall/precision denominators or flipping a
            # negative item into a phantom positive. Mirror the string-cap guard.
            if not isinstance(did, str):
                raise GoldSetError(
                    f"gold_doc_id must be a str, got {type(did).__name__}"
                )
            if len(did) > MAX_DOC_ID_LEN:
                raise GoldSetError(
                    f"gold_doc_id exceeds max length {MAX_DOC_ID_LEN} "
                    f"(got {len(did)})"
                )
        return cls(
            id=d["id"],
            question=d["question"],
            band=d["band"],
            gold_doc_ids=doc_ids,
            gold_fact=d.get("gold_fact", ""),
            drafted_by=d.get("drafted_by", ""),
            approved_by=d.get("approved_by", None),
            notes=d.get("notes", ""),
        )


@dataclass
class ItemReport:
    """Per-item validation outcome: hard errors + soft warnings."""

    item_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_item(
    item: GoldItem, corpus_doc_ids: set[str] | None = None
) -> ItemReport:
    """Validate one gold item incrementally (§5.2/§5.3).

    ``corpus_doc_ids`` (the set from ``corpus.doc_ids()``) is required to verify
    positive items reference existing docs; pass ``None`` to skip the existence
    check (e.g. when a corpus is not loaded), in which case a warning is emitted
    so the skip is never silent.
    """
    rep = ItemReport(item_id=item.id)

    if not item.id:
        rep.errors.append("id is empty")
    if not item.question or not item.question.strip():
        rep.errors.append("question is empty")
    if item.band not in BANDS:
        rep.errors.append(
            f"band {item.band!r} not one of {sorted(BANDS)}"
        )
        return rep  # band-dependent checks below are meaningless on a bad band

    if item.band == BAND_NEGATIVE:
        # Negative: gold_doc_ids MUST be empty AND gold_fact must explain why.
        if item.gold_doc_ids:
            rep.errors.append(
                "negative-band item MUST have empty gold_doc_ids "
                f"(got {item.gold_doc_ids})"
            )
        if not item.gold_fact or not item.gold_fact.strip():
            rep.errors.append(
                "negative-band item MUST set gold_fact describing why nothing "
                "should match"
            )
    else:
        # Positive: non-empty gold_doc_ids, every id exists in the corpus.
        if not item.gold_doc_ids:
            rep.errors.append(
                f"positive-band ({item.band}) item MUST have non-empty gold_doc_ids"
            )
        else:
            # No duplicate gold ids (would distort recall/precision denominators).
            if len(item.gold_doc_ids) != len(set(item.gold_doc_ids)):
                rep.errors.append("gold_doc_ids contains duplicates")
            if corpus_doc_ids is None:
                rep.warnings.append(
                    "corpus not provided — gold_doc_ids existence NOT checked"
                )
            else:
                missing = [d for d in item.gold_doc_ids if d not in corpus_doc_ids]
                if missing:
                    rep.errors.append(
                        f"gold_doc_ids reference non-existent corpus docs: {missing}"
                    )
        if not item.gold_fact or not item.gold_fact.strip():
            rep.errors.append("positive-band item MUST set a non-empty gold_fact")
        # Recall-ceiling caution (§6.1): |gold| > k makes recall@k uninterpretable.
        if len(item.gold_doc_ids) > config.K:
            rep.warnings.append(
                f"len(gold_doc_ids)={len(item.gold_doc_ids)} > k={config.K}: "
                "recall@k ceiling < 1.0 for this query (§6.1)"
            )
    return rep


@dataclass
class GoldSetReport:
    """Aggregate validation result over a whole gold set."""

    item_reports: list[ItemReport] = field(default_factory=list)
    per_band_counts: dict[str, int] = field(default_factory=dict)
    duplicate_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.duplicate_ids and all(r.ok for r in self.item_reports)

    @property
    def total(self) -> int:
        # Count UNIQUE ids only. per_band_counts already excludes duplicate-id
        # items (validate_set skips them), so summing it gives the unique-item
        # total. Using len(item_reports) would count duplicate-id items too, so a
        # set of 149 unique + 1 duplicate id could spuriously clear the >=150
        # floor — the duplicate must NOT inflate the finalized total (item 1).
        return sum(self.per_band_counts.values())

    def errors(self) -> list[str]:
        out: list[str] = [
            f"duplicate item id: {_safe_id(i)}" for i in self.duplicate_ids
        ]
        for r in self.item_reports:
            out.extend(f"[{_safe_id(r.item_id)}] {e}" for e in r.errors)
        return out

    def warnings(self) -> list[str]:
        out: list[str] = []
        for r in self.item_reports:
            out.extend(f"[{_safe_id(r.item_id)}] {w}" for w in r.warnings)
        return out


def validate_set(
    items: list[GoldItem], corpus_doc_ids: set[str] | None = None
) -> GoldSetReport:
    """Validate a whole gold set: per-item checks + per-band counts + dup ids."""
    report = GoldSetReport()
    seen: set[str] = set()
    counts: dict[str, int] = {b: 0 for b in BANDS}
    for item in items:
        if item.id in seen:
            report.duplicate_ids.append(item.id)
            rep = check_item(item, corpus_doc_ids)
            report.item_reports.append(rep)
            continue  # do NOT count a duplicate id toward per-band totals —
            # otherwise an under-floor band could appear compliant by padding
            # with duplicate-ID items (which check_finalized rejects anyway, but
            # per_band_counts must not lie to a caller inspecting it directly).
        seen.add(item.id)
        rep = check_item(item, corpus_doc_ids)
        report.item_reports.append(rep)
        if item.band in counts:
            counts[item.band] += 1
    report.per_band_counts = counts
    return report


class FinalizedError(GoldSetError):
    """Raised by :func:`check_finalized` when finalize criteria are unmet."""


def check_finalized(
    items: list[GoldItem], corpus_doc_ids: set[str] | None = None
) -> GoldSetReport:
    """Assert a gold set is FINALIZE-ready (§5.3). RAISE on failure.

    Requires: (1) every item valid; (2) each band >= its MIN_PER_BAND; (3) total
    >= MIN_TOTAL (150); (4) every item APPROVED (``approved_by`` set) — an
    unapproved draft can never count toward the finalized floor (closes the
    vacuous-satisfaction hole where 150 unreviewed drafts would "finalize").

    Returns the :class:`GoldSetReport` when all pass (so the caller can print
    per-band counts); raises :class:`FinalizedError` otherwise.
    """
    report = validate_set(items, corpus_doc_ids)
    problems: list[str] = []

    if not report.ok:
        problems.append(
            f"{len(report.errors())} validation error(s) — gold set not clean"
        )

    # (4) approval gate — a finalized set is fully operator-approved.
    unapproved = [it.id for it in items if not it.approved_by]
    if unapproved:
        problems.append(
            f"{len(unapproved)} item(s) not approved (approved_by unset): "
            f"{unapproved[:5]}{'...' if len(unapproved) > 5 else ''}"
        )

    # (2) per-band minimums
    for band in BANDS:
        need = min_for_band(band)
        have = report.per_band_counts.get(band, 0)
        if have < need:
            problems.append(
                f"band {band!r}: have {have}, need >= {need} (MIN_PER_BAND)"
            )

    # (3) total floor
    if report.total < MIN_TOTAL:
        problems.append(
            f"total {report.total} < MIN_TOTAL {MIN_TOTAL} (§5.3 >=150 floor)"
        )

    if problems:
        raise FinalizedError(
            "gold set is NOT finalize-ready:\n  - " + "\n  - ".join(problems)
        )
    return report


# ── Disk I/O (JSONL) ─────────────────────────────────────────────────────────
def load_jsonl(path: str | os.PathLike[str]) -> list[GoldItem]:
    """Load a gold set from JSONL. Each non-blank line is one GoldItem.

    The path may be operator/agent-supplied (the ``--gold`` CLI flag), so the
    loader bounds untrusted input (item 5): it REJECTS a file larger than
    :data:`MAX_GOLD_FILE_BYTES` BEFORE reading it into memory and caps the number
    of parsed items at :data:`MAX_GOLD_ITEMS`. Per-field length caps are enforced
    in :meth:`GoldItem.from_dict`.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_GOLD_FILE_BYTES:
        raise GoldSetError(
            f"gold JSONL {path.name!r} is {size} bytes, exceeds the "
            f"{MAX_GOLD_FILE_BYTES}-byte cap (refusing to load)"
        )
    items: list[GoldItem] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if len(items) >= MAX_GOLD_ITEMS:
            raise GoldSetError(
                f"gold JSONL exceeds the {MAX_GOLD_ITEMS}-item cap (refusing "
                "to load more)"
            )
        items.append(GoldItem.from_dict(json.loads(raw)))
    return items


def dump_jsonl(
    items: list[GoldItem],
    path: str | os.PathLike[str],
    *,
    allow_public: bool = False,
) -> None:
    """Write a gold set to JSONL (one item per line), PRIVATE-PATH guarded.

    Label bytes may be derived from operator/vault content (especially via the
    s2(b) LLM-backed drafter), so this writer routes through
    :func:`membench.paths.assert_private_path`: it REFUSES to write outside the
    private/gitignored area unless ``allow_public=True``. This makes the guard
    library-level rather than CLI-only — any direct caller is covered (§5.1).
    """
    from .paths import assert_private_path

    path = assert_private_path(Path(path), allow_public=allow_public)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(item.to_json() + "\n" for item in items), encoding="utf-8"
    )
