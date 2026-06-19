"""Labeling / review CLI + pluggable Drafter interface (§5.2).

Slice s2(a). Three commands:

- **draft**   — derive candidate gold labels from a corpus via a pluggable
                :class:`Drafter`. Ships a deterministic offline
                :class:`StubDrafter` (derives a single-hop question from each
                doc's first heading). The real LLM-backed drafter is wired in
                s2(b); the interface is defined here but NO network is touched.
- **review**  — load drafts, apply per-item approve/edit/reject decisions, and
                write approved items with ``approved_by`` stamped.
- **validate**— run the schema validator and print per-band counts + the
                finalized check.

Raw drafts and approved labels are PRIVATE writers: they route through the
:func:`membench.paths.assert_private_path` guard (unless ``allow_public`` is
set), so operator label bytes never reach a public-git location (§5.1).

The "session 1 wrote it / session 3 queried it" framing is corpus-resident; the
drafter only reads scrubbed corpus bytes (never a live vault).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from .contract import FrozenCorpus
from .corpus import load_corpus
from .goldset import (
    BAND_SINGLE_HOP,
    MAX_GOLD_FILE_BYTES,
    GoldItem,
    check_finalized,
    check_item,
    dump_jsonl,
    load_jsonl,
    validate_set,
)
from .goldset import FinalizedError, GoldSetError
from .paths import assert_private_path

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


@runtime_checkable
class Drafter(Protocol):
    """Drafts candidate gold items from a corpus (§5.2).

    Implementors MUST be deterministic for tests and MUST NOT touch the network
    in the offline path. The real LLM-backed drafter (s2(b)) implements this same
    interface; the harness only ever talks to this protocol.
    """

    name: str

    def draft(self, corpus: FrozenCorpus) -> list[GoldItem]: ...


class StubDrafter:
    """Deterministic, offline drafter — no network (§5.2).

    For each corpus doc it derives a single-hop candidate from the doc's first
    Markdown heading: ``"What does <heading> describe?"`` with that doc as the
    sole gold doc. Drafts are stamped ``drafted_by="stub_drafter"`` and left
    UNAPPROVED (``approved_by=None``) — they are candidates, never gold, until
    the review step approves them (§5.2 mandatory ordering).
    """

    name = "stub_drafter"

    def draft(self, corpus: FrozenCorpus) -> list[GoldItem]:
        items: list[GoldItem] = []
        for doc_id in corpus.doc_ids():
            text = corpus.read(doc_id).decode("utf-8", errors="replace")
            m = _HEADING.search(text)
            heading = m.group(1).strip() if m else Path(doc_id).stem
            items.append(
                GoldItem(
                    id=f"stub-{doc_id}",
                    question=f"What does {heading} describe?",
                    band=BAND_SINGLE_HOP,
                    gold_doc_ids=[doc_id],
                    gold_fact=f"The answer is found in {doc_id} (heading: {heading}).",
                    drafted_by=self.name,
                    approved_by=None,
                    notes="auto-drafted single-hop candidate; requires review",
                )
            )
        return items


# ── Review decisions ─────────────────────────────────────────────────────────
def apply_review(
    drafts: list[GoldItem],
    decisions: dict[str, dict],
    *,
    approved_by: str,
    corpus_doc_ids: set[str] | None = None,
) -> list[GoldItem]:
    """Apply approve/edit/reject decisions to drafts (§5.2).

    ``decisions`` maps draft id -> {"action": "approve"|"reject"|"edit", ...edits}.
    Returned list contains ONLY approved (and approved-after-edit) items, each
    with ``approved_by`` stamped. A draft with no decision is treated as
    implicitly rejected (never auto-promoted — §5.2: drafts are never promoted to
    gold without explicit operator approval).

    Every approved/edited item is run through :func:`check_item` (with
    ``corpus_doc_ids`` when available) BEFORE it is accepted — an edit that sets a
    bogus band or a non-existent gold doc id is rejected here, so invalid items
    can never silently enter the approved set (task item 4). ``decisions`` MUST
    be a ``{id: {…}}`` mapping with dict values (validated by the caller).
    """
    out: list[GoldItem] = []
    for d in drafts:
        decision = decisions.get(d.id)
        if not decision:
            continue  # no decision -> not approved (no silent auto-promotion)
        if not isinstance(decision, dict):
            raise GoldSetError(
                f"decision for {d.id!r} must be an object, got {type(decision).__name__}"
            )
        action = decision.get("action", "reject")
        if action == "reject":
            continue
        if action not in ("approve", "edit"):
            raise ValueError(f"unknown review action {action!r} for {d.id}")
        # Route the edited item through GoldItem.from_dict so the SAME field-length
        # caps (MAX_FIELD_LEN / MAX_DOC_ID_LEN) and gold_doc_id type checks enforced
        # at load time are enforced at write time — an attacker-controlled decisions
        # dict must not be able to forge an oversized field by going around the
        # validating constructor.
        edited = GoldItem.from_dict(
            {
                "id": decision.get("id", d.id),
                "question": decision.get("question", d.question),
                "band": decision.get("band", d.band),
                "gold_doc_ids": list(decision.get("gold_doc_ids", d.gold_doc_ids)),
                "gold_fact": decision.get("gold_fact", d.gold_fact),
                "drafted_by": d.drafted_by,
                "approved_by": approved_by,  # stamp on approval
                "notes": decision.get("notes", d.notes),
            }
        )
        rep = check_item(edited, corpus_doc_ids)
        if not rep.ok:
            raise GoldSetError(
                f"reviewed item {edited.id!r} fails validation and cannot be "
                f"approved: {rep.errors}"
            )
        out.append(edited)
    return out


# ── CLI commands ─────────────────────────────────────────────────────────────
def cmd_draft(args: argparse.Namespace) -> int:
    # Operator data MUST pass the scrub gate before drafting (§5.1). load_corpus
    # with scrubbed=True enforces verify_scrubbed over the snapshot manifest;
    # public synthetic fixtures pass --no-scrubbed.
    corpus = load_corpus(
        args.corpus_dir,
        pinned_hash=args.hash,
        scrubbed=args.scrubbed,
        snapshot_dir=args.snapshot_dir,
    )
    drafter = StubDrafter()
    items = drafter.draft(corpus)
    out = assert_private_path(Path(args.out), allow_public=args.allow_public)
    dump_jsonl(items, out, allow_public=args.allow_public)
    print(f"drafted {len(items)} candidate(s) via {drafter.name} -> {out}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    items = load_jsonl(args.gold)
    corpus_ids = None
    if args.corpus_dir:
        corpus = load_corpus(
            args.corpus_dir,
            pinned_hash=args.hash,
            scrubbed=args.scrubbed,
            snapshot_dir=args.snapshot_dir,
        )
        corpus_ids = set(corpus.doc_ids())
    report = validate_set(items, corpus_ids)
    print(f"total items: {report.total}")
    print("per-band counts:")
    for band, n in report.per_band_counts.items():
        print(f"  {band:16s} {n}")
    for w in report.warnings():
        print(f"WARN  {w}")
    for e in report.errors():
        print(f"ERROR {e}")
    if args.finalized:
        try:
            check_finalized(items, corpus_ids)
            print("FINALIZED: ok (per-band minimums + >=150 total + all approved)")
        except FinalizedError as exc:
            print(str(exc))
            return 1
    return 0 if report.ok else 1


def cmd_review(args: argparse.Namespace) -> int:
    """Apply human approve/edit/reject decisions to drafts (§5.2).

    Loads drafts (JSONL) + a decisions JSON ({draft_id: {action, ...edits}}),
    applies them via :func:`apply_review` (stamping ``approved_by``), and writes
    the approved items through the PRIVATE-PATH guard — approved label bytes
    never escape to a public-git location unless ``--allow-public`` is set.
    """
    import json as _json

    drafts = load_jsonl(args.drafts)
    # Cap the decisions file BEFORE read_text (analogous to load_jsonl's guard): the
    # path is operator/attacker-controlled and a multi-GB file would OOM before any
    # dict validation runs.
    _dec_path = Path(args.decisions)
    _dec_size = _dec_path.stat().st_size
    if _dec_size > MAX_GOLD_FILE_BYTES:
        raise GoldSetError(
            f"decisions file is {_dec_size} bytes, over the "
            f"{MAX_GOLD_FILE_BYTES}-byte cap (refusing to load)"
        )
    decisions = _json.loads(_dec_path.read_text(encoding="utf-8"))
    # Structural guard (task item 5): a top-level array or non-dict per-item
    # value would otherwise blow up deep inside apply_review with a confusing
    # traceback. Reject early with a clear, path-free message.
    if not isinstance(decisions, dict):
        raise GoldSetError(
            "decisions file must be a JSON object {draft_id: {action, ...}}, got "
            f"{type(decisions).__name__}"
        )
    for key, val in decisions.items():
        if not isinstance(val, dict):
            raise GoldSetError(
                f"decision for {key!r} must be an object {{action, ...}}, got "
                f"{type(val).__name__}"
            )
    # When a corpus is available, validate edited gold_doc_ids against it.
    corpus_ids = None
    if getattr(args, "corpus_dir", None):
        corpus = load_corpus(
            args.corpus_dir,
            pinned_hash=args.hash,
            scrubbed=args.scrubbed,
            snapshot_dir=args.snapshot_dir,
        )
        corpus_ids = set(corpus.doc_ids())
    approved = apply_review(
        drafts, decisions, approved_by=args.approved_by, corpus_doc_ids=corpus_ids
    )
    out = assert_private_path(Path(args.out), allow_public=args.allow_public)
    dump_jsonl(approved, out, allow_public=args.allow_public)
    print(f"approved {len(approved)} item(s) by {args.approved_by} -> {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="membench-label", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="draft candidate labels from a corpus")
    d.add_argument("--corpus-dir", required=True)
    d.add_argument("--hash", required=True, help="pinned corpus content-hash")
    d.add_argument("--out", required=True, help="output JSONL (private by default)")
    d.add_argument("--allow-public", action="store_true")
    d.add_argument(
        "--snapshot-dir",
        default=None,
        help="snapshot root holding manifest.json (for the scrub-gate check)",
    )
    d.add_argument(
        "--no-scrubbed",
        dest="scrubbed",
        action="store_false",
        help="corpus is a public synthetic fixture with no secrets (skip scrub gate)",
    )
    d.set_defaults(func=cmd_draft, scrubbed=True)

    r = sub.add_parser("review", help="apply approve/edit/reject decisions to drafts")
    r.add_argument("--drafts", required=True, help="input drafts JSONL")
    r.add_argument(
        "--decisions",
        required=True,
        help="JSON map: {draft_id: {action: approve|edit|reject, ...edits}}",
    )
    r.add_argument("--out", required=True, help="output JSONL (private by default)")
    r.add_argument("--approved-by", required=True, help="reviewer identity to stamp")
    r.add_argument("--allow-public", action="store_true")
    r.add_argument(
        "--corpus-dir",
        default=None,
        help="corpus to validate edited gold_doc_ids against (optional)",
    )
    r.add_argument("--hash", default=None, help="pinned corpus content-hash")
    r.add_argument("--snapshot-dir", default=None)
    r.add_argument(
        "--no-scrubbed",
        dest="scrubbed",
        action="store_false",
        help="corpus is a public synthetic fixture with no secrets (skip scrub gate)",
    )
    r.set_defaults(func=cmd_review, scrubbed=True)

    v = sub.add_parser("validate", help="validate a gold set + finalized check")
    v.add_argument("--gold", required=True, help="gold-set JSONL")
    v.add_argument("--corpus-dir", default=None)
    v.add_argument("--hash", default=None)
    v.add_argument("--snapshot-dir", default=None)
    v.add_argument(
        "--no-scrubbed",
        dest="scrubbed",
        action="store_false",
        help="corpus is a public synthetic fixture with no secrets (skip scrub gate)",
    )
    v.add_argument("--finalized", action="store_true")
    v.set_defaults(func=cmd_validate, scrubbed=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
