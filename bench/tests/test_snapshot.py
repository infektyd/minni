"""Snapshot freezer tests (§5.1, s2(a)).

Covers:
- determinism: freezing the same source twice yields the same content_hash;
- manifest correctness (per-file sha256 + path, overall content_hash);
- the frozen snapshot loads through the s1 FrozenCorpus loader with a MATCHING
  hash-gate (load_corpus(corpus_subdir(...), pinned_hash=manifest.content_hash));
- the private-path write guard fires off-private and is bypassable only via
  allow_public.
"""

import hashlib

import pytest

from membench import config
from membench.corpus import load_corpus
from membench.paths import PrivatePathError, PRIVATE_ROOT
from membench.snapshot import (
    MANIFEST_FILENAME,
    build_manifest,
    corpus_subdir,
    freeze_snapshot,
    load_manifest,
)


def test_freeze_is_deterministic(tmp_path, fixture_dir):
    """Same source -> same content_hash, twice (no timestamps/locale in hash)."""
    d1 = tmp_path / "snap1"
    d2 = tmp_path / "snap2"
    m1 = freeze_snapshot(fixture_dir, d1, allow_public=True)
    m2 = freeze_snapshot(fixture_dir, d2, allow_public=True)
    assert m1.content_hash == m2.content_hash
    # The content_hash matches what the s1 hasher derives over the source too.
    assert m1.content_hash == config.FIXTURE_CORPUS_HASH


def test_manifest_correctness(tmp_path, fixture_dir):
    dest = tmp_path / "snap"
    manifest = freeze_snapshot(fixture_dir, dest, allow_public=True)
    corpus = corpus_subdir(dest)

    # Every manifest entry's sha256 matches the on-disk frozen file bytes.
    for entry in manifest.files:
        on_disk = (corpus / entry["path"]).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == entry["sha256"]

    # Files are sorted by path (canonical, deterministic).
    paths = [e["path"] for e in manifest.files]
    assert paths == sorted(paths)
    # The persisted manifest round-trips.
    reloaded = load_manifest(dest)
    assert reloaded.content_hash == manifest.content_hash
    assert reloaded.files == manifest.files


def test_manifest_excludes_itself_from_hash(tmp_path, fixture_dir):
    """manifest.json sits at the snapshot ROOT, outside corpus/, so it never
    enters the content-hash — that is what lets the s1 loader's hash match."""
    dest = tmp_path / "snap"
    freeze_snapshot(fixture_dir, dest, allow_public=True)
    files = {e["path"] for e in load_manifest(dest).files}
    assert MANIFEST_FILENAME not in files


def test_frozen_snapshot_loads_through_s1_loader(tmp_path, fixture_dir):
    """The frozen snapshot loads via the s1 FrozenCorpus loader with the
    manifest's content_hash as the pinned hash — proving hash-gate compatibility.
    """
    dest = tmp_path / "snap"
    manifest = freeze_snapshot(fixture_dir, dest, allow_public=True)
    # Freshly frozen, not yet scrubbed -> load with scrubbed=False (the scrub
    # gate is exercised end-to-end in test_scrub.py).
    corpus = load_corpus(
        corpus_subdir(dest), pinned_hash=manifest.content_hash, scrubbed=False
    )
    assert corpus.content_hash == manifest.content_hash
    assert sorted(corpus.doc_ids()) == sorted(e["path"] for e in manifest.files)


def test_freeze_refuses_non_private_without_allow_public(tmp_path, fixture_dir):
    """Default (allow_public=False) write outside _private/ MUST raise."""
    outside = tmp_path / "public_snap"  # tmp_path is not under PRIVATE_ROOT
    with pytest.raises(PrivatePathError):
        freeze_snapshot(fixture_dir, outside)  # allow_public defaults False


def test_freeze_pattern_filters_files(tmp_path):
    """A non-default pattern only freezes matching files (item 14)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("# keep\n", encoding="utf-8")
    (src / "drop.md").write_text("# drop\n", encoding="utf-8")
    dest = tmp_path / "snap"
    manifest = freeze_snapshot(src, dest, allow_public=True, pattern="*.txt")
    paths = {e["path"] for e in manifest.files}
    assert paths == {"keep.txt"}
    # The .md file was not copied into the snapshot corpus/ subdir.
    assert not (corpus_subdir(dest) / "drop.md").exists()


def test_freeze_allows_private_path(tmp_path, fixture_dir, monkeypatch):
    """A dest under PRIVATE_ROOT is accepted with allow_public=False (default)."""
    # Redirect PRIVATE_ROOT to tmp so the test does not touch the real _private/.
    fake_private = tmp_path / "_private" / "membench"
    monkeypatch.setattr("membench.paths.PRIVATE_ROOT", fake_private)
    dest = fake_private / "corpus_real" / "snap"
    manifest = freeze_snapshot(fixture_dir, dest)  # allow_public defaults False
    assert manifest.content_hash == config.FIXTURE_CORPUS_HASH
