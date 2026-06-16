"""Frozen-corpus loader with content-hash refusal + path-traversal guard (§5).

A :class:`FrozenCorpus` is the scrubbed, content-hashed set of bytes every
adapter ingests. Slice s1 implements:

- A content-hash over the canonical SORTED per-file manifest (§5.1). The loader
  REFUSES (raises) if the recomputed hash != the pinned hash passed to it.
- The path-traversal guard at BOTH ``doc_ids()`` construction time
  (realpath-containment; a symlink escaping ``corpus_dir`` is filtered out) AND
  at ``read(doc_id)`` (membership + realpath containment before opening).

NOTE: the full scrub-tool / ``scrub_manifest_hash`` cross-check (§5.1) and the
single log-safe wrapper (§5.1 / §9.12) are later slices (s2+). s1 carries the
``scrubbed`` flag through and computes ``content_hash``; the cryptographic
scrub-manifest binding is stubbed as future work and NOT relied on for safety
here (the synthetic fixture contains no secrets).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class CorpusHashMismatch(RuntimeError):
    """Raised when the recomputed content-hash != the pinned hash (§5.1)."""


class CorpusPathError(ValueError):
    """Raised by the path-traversal guard (§5.1)."""


def _iter_corpus_files(corpus_dir: Path) -> list[tuple[str, Path]]:
    """Enumerate corpus files as (rel_doc_id, abs_path), realpath-contained.

    Applies realpath-containment to EVERY discovered path (§3.1/§5.1): any path
    whose realpath escapes ``realpath(corpus_dir)`` is filtered out, so a
    symlink planted inside the corpus that points outside it never enters the
    doc-id set. Returns a SORTED list (canonical, deterministic ordering).
    """
    root_real = os.path.realpath(corpus_dir)
    root_prefix = root_real + os.sep
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(corpus_dir, followlinks=False):
        # Prune subdirectories whose realpath escapes the root.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if os.path.realpath(os.path.join(dirpath, d)).startswith(root_prefix)
        )
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            real = os.path.realpath(abs_path)
            # realpath-containment: drop anything escaping the corpus root.
            if real != root_real and not real.startswith(root_prefix):
                continue
            rel = os.path.relpath(abs_path, corpus_dir)
            # Normalize to forward-slash doc-ids for stable, portable manifests.
            doc_id = rel.replace(os.sep, "/")
            out.append((doc_id, Path(abs_path)))
    out.sort(key=lambda t: t[0])
    return out


def compute_content_hash(corpus_dir: str | os.PathLike[str]) -> str:
    """SHA-256 over the canonical sorted per-file manifest (§5.1).

    The manifest line for each file is ``"<doc_id>\\n<sha256(bytes)>\\n"``,
    files sorted by doc-id. Tampering any byte of any file changes the per-file
    hash and therefore the content-hash, so the loader's refusal fires.
    """
    corpus_dir = Path(corpus_dir)
    hasher = hashlib.sha256()
    for doc_id, abs_path in _iter_corpus_files(corpus_dir):
        file_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        hasher.update(doc_id.encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(file_hash.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


class DirectoryFrozenCorpus:
    """A FrozenCorpus backed by an on-disk directory (§5.1).

    Constructed only by :func:`load_corpus`, which enforces the content-hash
    refusal. The path-traversal guard is enforced here at both ``doc_ids()``
    (build time) and ``read()`` (call time).
    """

    def __init__(self, corpus_dir: Path, content_hash: str, scrubbed: bool):
        self._corpus_dir = corpus_dir
        self._corpus_real = os.path.realpath(corpus_dir)
        self.content_hash = content_hash
        self.scrubbed = scrubbed
        # Build the doc-id set ONCE, with realpath-containment already applied.
        self._files: dict[str, Path] = {
            doc_id: abs_path for doc_id, abs_path in _iter_corpus_files(corpus_dir)
        }
        self._doc_ids: list[str] = sorted(self._files)

    def doc_ids(self) -> list[str]:
        return list(self._doc_ids)

    def read(self, doc_id: str) -> bytes:
        """Return scrubbed bytes for one doc, path-traversal-guarded (§5.1).

        (1) membership: ``doc_id`` must be in the pre-built doc-id set;
        (2) realpath-containment: the resolved path must stay under
        ``realpath(corpus_dir)``. Either failure RAISES before any file opens.
        """
        if doc_id not in self._files:
            raise CorpusPathError(
                f"doc_id not a member of corpus.doc_ids(): {doc_id!r}"
            )
        candidate = os.path.realpath(os.path.join(self._corpus_dir, doc_id))
        if candidate != self._corpus_real and not candidate.startswith(
            self._corpus_real + os.sep
        ):
            raise CorpusPathError(
                f"doc_id escapes corpus_dir (path traversal): {doc_id!r}"
            )
        return self._files[doc_id].read_bytes()


def load_corpus(
    corpus_dir: str | os.PathLike[str],
    *,
    pinned_hash: str,
    scrubbed: bool = True,
) -> DirectoryFrozenCorpus:
    """Load a corpus directory and FAIL-CLOSED on hash mismatch (§5.1).

    Recomputes the content-hash over the on-disk tree and REFUSES to run
    (raises :class:`CorpusHashMismatch`) if it differs from ``pinned_hash``.
    Every adapter therefore ingests provably identical bytes (fairness §7.1).
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise CorpusPathError(f"corpus_dir is not a directory: {corpus_dir}")
    actual = compute_content_hash(corpus_dir)
    if actual != pinned_hash:
        raise CorpusHashMismatch(
            "corpus content-hash mismatch — refusing to run.\n"
            f"  pinned:   {pinned_hash}\n"
            f"  computed: {actual}\n"
            f"  corpus:   {corpus_dir}"
        )
    return DirectoryFrozenCorpus(corpus_dir, actual, scrubbed)
