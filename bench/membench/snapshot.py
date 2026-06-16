"""Snapshot freezer — produces a frozen, content-hashed corpus snapshot (§5.1).

Slice s2(a). Given a SOURCE corpus directory of ``*.md`` files, the freezer
produces a FROZEN snapshot directory plus a ``manifest.json`` recording, per
file, its relative path + sha256, and an overall corpus ``content_hash`` (SHA-256
over the sorted canonical manifest). The freezer is:

- **Deterministic** — files are enumerated and hashed in SORTED doc-id order;
  the embedded ``content_hash`` is computed over the canonical sorted manifest
  ONLY (no timestamps, no locale-dependent ordering, no mtimes). Freezing the
  same source twice yields the same ``content_hash``.
- **Hash-compatible with the s1 loader** — ``content_hash`` is computed by the
  exact same :func:`membench.corpus.compute_content_hash` the s1
  ``DirectoryFrozenCorpus`` loader re-derives, so a frozen snapshot loads
  through :func:`membench.corpus.load_corpus` with a matching hash-gate.
- **Private by default** — the writer REFUSES to write outside the designated
  private/gitignored area unless ``allow_public=True`` (see :mod:`membench.paths`).

The freezer does NOT scrub. Scrubbing is a separate pass (:mod:`membench.scrub`)
applied to the snapshot text; the cryptographic scrub-gate binds the two.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .corpus import _iter_corpus_files, compute_content_hash
from .paths import assert_private_path

MANIFEST_FILENAME = "manifest.json"
# The frozen *.md files live in this subdir of the snapshot so the content-hash
# walk never picks up manifest.json (which sits at the snapshot root). This is
# what lets the s1 load_corpus(corpus_subdir(...)) hash-gate match exactly.
CORPUS_SUBDIR = "corpus"
# Schema version of the emitted manifest — bumped if the on-disk shape changes.
MANIFEST_SCHEMA_VERSION = 1


def corpus_subdir(snapshot_dir: str | os.PathLike[str]) -> Path:
    """The directory under a frozen snapshot holding the content-hashed files.

    Pass THIS to :func:`membench.corpus.load_corpus` (with the manifest's
    ``content_hash``) — never the snapshot root, which also holds
    ``manifest.json`` and would perturb the hash.
    """
    return Path(snapshot_dir) / CORPUS_SUBDIR


@dataclass(frozen=True)
class SnapshotManifest:
    """The in-memory view of a frozen snapshot's ``manifest.json`` (§5.1)."""

    content_hash: str
    files: list[dict[str, str]]  # [{"path": <doc_id>, "sha256": <hex>}, ...]
    scrubbed: bool = False
    scrub_manifest_hash: str = ""
    # SALTED SHA-256 hashes of the real-name keys the scrub policy aliased — a
    # presence/"names were considered" signal the verifier can use WITHOUT
    # leaking the plaintext operator name into the manifest (which may land on a
    # public path when allow_public=True). The plaintext names needed to re-scan
    # for residual name PII are persisted ONLY in the private, gitignored
    # scrub_spans/_private sidecar — never here (item 3, §5.1).
    name_alias_key_hashes: tuple[str, ...] = ()
    # Whether the scrub policy EXPLICITLY opted out of name redaction (i.e. the
    # operator passed a policy with an empty name_aliases on purpose) vs. the
    # field simply being absent (e.g. a partial scrub that never considered
    # names). Distinguishes a verified "no names to redact" from an unverified
    # gap so verify_scrubbed cannot silently pass name-only PII (§5.1).
    name_scrub_opted_out: bool = False
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def to_json(self) -> str:
        """Serialize deterministically (sorted keys, fixed separators).

        NOTE: ``files`` is already sorted by ``path`` at build time and the
        ``content_hash`` is NOT recomputed from this JSON (it is the canonical
        manifest hash from :func:`membench.corpus.compute_content_hash`), so the
        JSON is provenance + a loadable index, not the hash authority.
        """
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "content_hash": self.content_hash,
                "scrubbed": self.scrubbed,
                "scrub_manifest_hash": self.scrub_manifest_hash,
                "name_alias_key_hashes": list(self.name_alias_key_hashes),
                "name_scrub_opted_out": self.name_scrub_opted_out,
                "files": self.files,
            },
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )


def build_manifest(corpus_dir: str | os.PathLike[str]) -> SnapshotManifest:
    """Build a deterministic manifest over a corpus directory (no write).

    Enumerates files via the s1 realpath-contained, SORTED walk and records each
    one's doc-id + sha256. The overall ``content_hash`` is the s1
    canonical-manifest hash, so it matches the loader's hash-gate exactly.
    """
    corpus_dir = Path(corpus_dir)
    files: list[dict[str, str]] = []
    for doc_id, abs_path in _iter_corpus_files(corpus_dir):
        files.append(
            {
                "path": doc_id,
                "sha256": hashlib.sha256(abs_path.read_bytes()).hexdigest(),
            }
        )
    files.sort(key=lambda e: e["path"])  # canonical, locale-independent ordering
    content_hash = compute_content_hash(corpus_dir)
    return SnapshotManifest(content_hash=content_hash, files=files)


def freeze_snapshot(
    source_dir: str | os.PathLike[str],
    dest_dir: str | os.PathLike[str],
    *,
    allow_public: bool = False,
    pattern: str = "*.md",
) -> SnapshotManifest:
    """Freeze ``source_dir`` into ``dest_dir`` + a ``manifest.json`` (§5.1).

    Copies every file matching ``pattern`` (default ``*.md``), then writes a
    deterministic manifest. Writing is PRIVATE-PATH GUARDED: unless
    ``allow_public=True`` the destination must resolve under the designated
    private/gitignored area, else :class:`membench.paths.PrivatePathError`.

    The copy preserves bytes but NOT mtimes that would perturb the hash — the
    ``content_hash`` is computed over file BYTES + doc-ids only, so two freezes
    of the same source produce the same hash regardless of when they ran.
    """
    source_dir = Path(source_dir)
    dest_dir = Path(dest_dir)
    if not source_dir.is_dir():
        raise NotADirectoryError(f"source_dir is not a directory: {source_dir}")

    # PRIVATE-PATH WRITE GUARD (§5.1): refuse to write outside the private area
    # unless explicitly allowed. Checked BEFORE any byte is written.
    assert_private_path(dest_dir, allow_public=allow_public)

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_corpus = dest_dir / CORPUS_SUBDIR
    out_corpus.mkdir(parents=True, exist_ok=True)

    # Copy matching files into the snapshot's corpus/ subdir, preserving relative
    # layout, in SORTED order (deterministic; keeps the copy reproducible).
    for doc_id, abs_path in _iter_corpus_files(source_dir):
        if pattern != "*" and not Path(doc_id).match(pattern):
            continue
        out_path = out_corpus / doc_id
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(abs_path, out_path)

    # The content-hash is computed over the corpus/ subdir ONLY — so the
    # manifest.json at the snapshot root never enters it, and
    # load_corpus(corpus_subdir(dest_dir)) re-derives the SAME hash.
    manifest = build_manifest(out_corpus)
    (dest_dir / MANIFEST_FILENAME).write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return manifest


def load_manifest(snapshot_dir: str | os.PathLike[str]) -> SnapshotManifest:
    """Load a previously-frozen ``manifest.json`` from a snapshot dir."""
    snapshot_dir = Path(snapshot_dir)
    raw = json.loads((snapshot_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    # Light type validation before constructing the manifest (NIT d, mirrors the
    # scrub-span fix): manifest.json is edit-controlled, so a tampered shape must
    # surface as a clean ValueError here rather than corrupting downstream
    # hash/scrub logic with a wrong-typed field. content_hash must be a str and
    # files a list (the hash gate and loader iterate them).
    if not isinstance(raw, dict):
        raise ValueError("malformed manifest.json: top level must be an object")
    if not isinstance(raw.get("content_hash"), str):
        raise ValueError("malformed manifest.json: content_hash must be a string")
    if not isinstance(raw.get("files"), list):
        raise ValueError("malformed manifest.json: files must be a list")
    return SnapshotManifest(
        content_hash=raw["content_hash"],
        files=raw["files"],
        scrubbed=raw.get("scrubbed", False),
        scrub_manifest_hash=raw.get("scrub_manifest_hash", ""),
        name_alias_key_hashes=tuple(raw.get("name_alias_key_hashes", ())),
        name_scrub_opted_out=raw.get("name_scrub_opted_out", False),
        schema_version=raw.get("schema_version", MANIFEST_SCHEMA_VERSION),
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """CLI: freeze a SOURCE corpus into a private FROZEN snapshot + manifest."""
    import argparse

    p = argparse.ArgumentParser(
        prog="membench-snapshot",
        description="Freeze a source corpus into a private, content-hashed snapshot.",
    )
    p.add_argument("--source", required=True, help="source corpus directory")
    p.add_argument(
        "--dest",
        required=True,
        help="destination snapshot dir (must be under _private/ unless --allow-public)",
    )
    p.add_argument("--allow-public", action="store_true")
    p.add_argument("--pattern", default="*.md")
    args = p.parse_args(argv)
    manifest = freeze_snapshot(
        args.source,
        args.dest,
        allow_public=args.allow_public,
        pattern=args.pattern,
    )
    print(f"froze {len(manifest.files)} file(s)")
    print(f"content_hash: {manifest.content_hash}")
    print(f"snapshot dir: {args.dest}  (corpus/ subdir + {MANIFEST_FILENAME})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
