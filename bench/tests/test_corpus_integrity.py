"""Corpus loader, hash-refusal, and path-traversal tests (§5, §9.5).

Covers, for slice s1:
- the loader fails-closed (raises) on a content-hash mismatch (tamper a byte);
- doc_count == len(corpus.doc_ids()) on the fixture;
- the path-traversal guard: read('../../etc/passwd') and read('/etc/passwd')
  both raise, opening no file;
- a symlink escaping corpus_dir never enters doc_ids();
- doc_ids() ordering is sorted/canonical (deterministic).
"""

import os
import shutil
from pathlib import Path

import pytest

from membench import config
from membench.adapters.stub import StubAdapter
from membench.corpus import (
    CorpusHashMismatch,
    CorpusPathError,
    compute_content_hash,
    load_corpus,
)


def test_fixture_hash_is_pinned_correctly(fixture_dir):
    assert compute_content_hash(fixture_dir) == config.FIXTURE_CORPUS_HASH
    # The pinned hash must be a well-formed sha256: 64 lowercase hex chars
    # (finding #9) — catches a truncated/uppercased/mistyped pin.
    pinned = config.FIXTURE_CORPUS_HASH
    assert len(pinned) == 64
    assert all(c in "0123456789abcdef" for c in pinned)


def test_loader_refuses_on_hash_mismatch(tmp_path, fixture_dir):
    """Tamper one byte -> the recomputed hash differs -> loader RAISES (§5.1)."""
    work = tmp_path / "corpus"
    shutil.copytree(fixture_dir, work)
    victim = work / "01-aurora-protocol.md"
    victim.write_bytes(victim.read_bytes() + b"X")  # one tampered byte
    with pytest.raises(CorpusHashMismatch):
        load_corpus(work, pinned_hash=config.FIXTURE_CORPUS_HASH, scrubbed=False)


def test_loader_accepts_matching_hash(corpus):
    assert corpus.content_hash == config.FIXTURE_CORPUS_HASH
    # The synthetic fixture is public (no secrets) and loaded scrubbed=False.
    assert corpus.scrubbed is False


def test_doc_count_equals_doc_ids(corpus):
    adapter = StubAdapter()
    try:
        report = adapter.ingest(corpus)
        assert report.doc_count == len(corpus.doc_ids())
    finally:
        adapter.teardown()


def test_doc_ids_sorted_canonical(corpus):
    ids = corpus.doc_ids()
    assert ids == sorted(ids), "doc_ids() must be sorted/canonical (deterministic)"
    assert len(ids) == len(set(ids))


def test_fixture_unique_uuid_present_in_corpus(corpus):
    """The unique-UUID doc exists and contains the pinned UUID verbatim (§9.5).

    This is the data precondition for the real over-count cross-check (which is
    run against a REAL retrieval system in test_minni_adapter.py, not the stub:
    against the stub the UUID-in-query/UUID-in-doc match is a tautology and
    proves nothing about indexing). Here we only assert the fixture is wired so
    the real cross-check has something to surface.
    """
    assert "03-teal-ledger.md" in corpus.doc_ids()
    body = corpus.read("03-teal-ledger.md").decode("utf-8")
    assert config.FIXTURE_UNIQUE_UUID in body


def test_read_realpath_containment_branch_raises(tmp_path, fixture_dir):
    """Directly exercise read()'s realpath-containment branch (finding #8).

    Construction-time filtering removes escaping symlinks before read(), so the
    realpath-containment branch (as opposed to the membership branch) is never
    hit in normal flow. Inject an escaping doc-id straight into corpus._files
    (bypassing construction) and assert read() still refuses via that branch.
    """
    work = tmp_path / "corpus"
    shutil.copytree(fixture_dir, work)
    recomputed = compute_content_hash(work)
    loaded = load_corpus(work, pinned_hash=recomputed, scrubbed=False)

    # Plant a doc-id whose realpath resolves OUTSIDE the corpus root via a
    # symlink, and register it in _files so the membership check passes — forcing
    # evaluation of the realpath-containment branch.
    outside = tmp_path / "secret.txt"
    outside.write_text("must never be reachable")
    link = work / "escape.md"
    os.symlink(outside, link)
    loaded._files["escape.md"] = link  # bypass construction-time filtering

    assert "escape.md" in loaded._files  # membership branch would pass
    with pytest.raises(CorpusPathError):
        loaded.read("escape.md")


def test_path_traversal_relative_raises(corpus):
    with pytest.raises(CorpusPathError):
        corpus.read("../../etc/passwd")


def test_path_traversal_absolute_raises(corpus):
    with pytest.raises(CorpusPathError):
        corpus.read("/etc/passwd")


def test_read_unknown_doc_id_raises(corpus):
    with pytest.raises(CorpusPathError):
        corpus.read("does-not-exist.md")


def test_symlink_escaping_corpus_never_enters_doc_ids(tmp_path, fixture_dir):
    """A symlink inside the corpus pointing OUTSIDE it is filtered at build
    time (§3.1/§5.1) — it never appears in doc_ids() and cannot be read."""
    work = tmp_path / "corpus"
    shutil.copytree(fixture_dir, work)
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET should never be reachable")
    link = work / "escape.md"
    os.symlink(outside, link)

    # The hash changes because the tree changed; load with the recomputed hash
    # so we can exercise doc_ids() containment independent of the hash gate.
    recomputed = compute_content_hash(work)
    loaded = load_corpus(work, pinned_hash=recomputed, scrubbed=False)
    assert "escape.md" not in loaded.doc_ids(), (
        "symlink escaping corpus_dir must not enter doc_ids()"
    )
    with pytest.raises(CorpusPathError):
        loaded.read("escape.md")
