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
    snapshot_dir: str | os.PathLike[str] | None = None,
) -> DirectoryFrozenCorpus:
    """Load a corpus directory and FAIL-CLOSED on hash mismatch (§5.1).

    Recomputes the content-hash over the on-disk tree and REFUSES to run
    (raises :class:`CorpusHashMismatch`) if it differs from ``pinned_hash``.
    Every adapter therefore ingests provably identical bytes (fairness §7.1).

    SCRUB-GATE ENFORCEMENT (§5.1): the spec requires the loader to REJECT a
    corpus that claims ``scrubbed=True`` unless its ``scrub_manifest_hash``
    recomputes over the actual bytes — a bare boolean over raw bytes is never
    trusted. So when ``scrubbed=True``:
      - if ``snapshot_dir`` is given (the snapshot root holding ``manifest.json``
        + ``scrub_spans.jsonl``), the full cryptographic cross-check runs via
        :func:`membench.scrub.verify_scrubbed` and a forged/unscrubbed corpus is
        refused;
      - if no ``snapshot_dir`` is given, the loader still tries to locate the
        manifest at ``corpus_dir.parent`` (the canonical snapshot layout). If a
        manifest there claims ``scrubbed=True``, the cross-check is enforced. If
        no manifest can be found, ``scrubbed=True`` cannot be honored and the
        loader RAISES — the caller must pass ``scrubbed=False`` (synthetic/public
        fixtures with no secrets) or point at a verifiable snapshot.
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
    if scrubbed:
        _enforce_scrub_gate(corpus_dir, snapshot_dir)
    return DirectoryFrozenCorpus(corpus_dir, actual, scrubbed)


def _enforce_scrub_gate(
    corpus_dir: Path, snapshot_dir: str | os.PathLike[str] | None
) -> None:
    """Run the scrub cross-check before honoring ``scrubbed=True`` (§5.1).

    Lazy-imports :mod:`membench.scrub` to avoid a circular import (scrub imports
    corpus). Raises if the snapshot's scrub gate does not verify.
    """
    from pathlib import Path as _P

    # Locate the snapshot root: explicit arg wins; else assume the canonical
    # layout where corpus_dir is "<snapshot>/corpus" and the manifest is at
    # corpus_dir.parent.
    snap = _P(snapshot_dir) if snapshot_dir is not None else corpus_dir.parent
    manifest_path = snap / "manifest.json"
    if not manifest_path.is_file():
        raise CorpusPathError(
            "load_corpus(scrubbed=True) requires a verifiable snapshot manifest, "
            f"none found at {manifest_path}. Pass scrubbed=False for public "
            "synthetic fixtures (no secrets), or point snapshot_dir at a frozen, "
            "scrub-gated snapshot (§5.1)."
        )
    # The scrub gate verifies snap/corpus/, but load_corpus serves corpus_dir.
    # If those diverge, the gate would pass over a DIFFERENT (e.g. empty) tree
    # while the served corpus stays unscrubbed — stamping scrubbed=True on
    # never-verified bytes. Require they are the SAME directory (§5.1).
    from .snapshot import corpus_subdir as _corpus_subdir

    expected = _corpus_subdir(snap).resolve()
    if corpus_dir.resolve() != expected:
        raise CorpusPathError(
            "load_corpus(scrubbed=True): corpus_dir does not match the snapshot's "
            "corpus subdir — the scrub gate would verify a different tree than the "
            "one served, letting unscrubbed bytes pass.\n"
            f"  corpus_dir:        {corpus_dir.resolve()}\n"
            f"  snapshot corpus/:  {expected}\n"
            "Pass corpus_dir == corpus_subdir(snapshot_dir) (§5.1)."
        )

    # Read the manifest's scrubbed flag; only enforce the cross-check when the
    # manifest itself claims scrubbed=True (an unscrubbed snapshot loaded with
    # scrubbed=True is a caller error caught here).
    from .scrub import ScrubGateError, verify_scrubbed

    try:
        verify_scrubbed(snap)
    except ScrubGateError as exc:
        raise CorpusPathError(
            "load_corpus(scrubbed=True) refused: the snapshot's scrub gate did "
            f"not verify — {exc}"
        ) from exc
