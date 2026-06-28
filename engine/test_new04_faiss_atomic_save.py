"""NEW-04: faiss_persist.save() must be crash/out-of-disk safe.

Every artifact (FAISS index OR npz fallback, plus the manifest) is written to a
sibling temp file and only os.replace()'d into place once fully written, with the
manifest renamed last. A save that fails mid-write must therefore leave the prior
good index+manifest intact and must not leave dangling temp files.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pytest

import faiss_persist


def _manifest(tmp_path) -> str:
    return os.path.join(tmp_path, "store.manifest.json")


def _save_v1_npz(tmp_path, checksum="sumA"):
    """Save a v1 store via the numpy fallback path (index=None)."""
    ok = faiss_persist.save(
        index=None,
        vectors=[np.ones(3, dtype=np.float32)],
        chunk_ids=[101],
        manifest_path=_manifest(tmp_path),
        embedding_model="m",
        vector_dim=3,
        db_checksum=checksum,
    )
    assert ok is True
    return checksum


def test_successful_save_leaves_no_temp_files(tmp_path):
    _save_v1_npz(tmp_path)
    leftovers = glob.glob(os.path.join(tmp_path, "*.tmp"))
    assert leftovers == [], leftovers
    # Manifest loads back.
    loaded = faiss_persist.load(_manifest(tmp_path), expected_db_checksum="sumA")
    assert loaded is not None


def test_failed_data_write_preserves_prior_good_save(tmp_path, monkeypatch):
    checksum_v1 = _save_v1_npz(tmp_path, "sumA")

    # Simulate a crash/out-of-disk while writing the v2 data artifact.
    def _boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(faiss_persist.np, "savez_compressed", _boom)
    ok = faiss_persist.save(
        index=None,
        vectors=[np.full(3, 2.0, dtype=np.float32)],
        chunk_ids=[202],
        manifest_path=_manifest(tmp_path),
        embedding_model="m",
        vector_dim=3,
        db_checksum="sumB",
    )
    assert ok is False

    # Prior good state is intact: the manifest still reports v1's checksum, and
    # nothing committed under the new checksum.
    loaded = faiss_persist.load(_manifest(tmp_path), expected_db_checksum=checksum_v1)
    assert loaded is not None
    assert faiss_persist.load(_manifest(tmp_path), expected_db_checksum="sumB") is None
    # No dangling temp files from the failed save.
    assert glob.glob(os.path.join(tmp_path, "*.tmp")) == []


def test_faiss_index_path_is_atomic(tmp_path):
    faiss = pytest.importorskip("faiss")
    index = faiss.IndexFlatL2(3)
    index.add(np.array([[1.0, 2.0, 3.0]], dtype=np.float32))
    ok = faiss_persist.save(
        index=index,
        vectors=[np.array([1.0, 2.0, 3.0], dtype=np.float32)],
        chunk_ids=[7],
        manifest_path=_manifest(tmp_path),
        embedding_model="m",
        vector_dim=3,
        db_checksum="sumF",
    )
    assert ok is True
    assert glob.glob(os.path.join(tmp_path, "*.tmp")) == []
    loaded = faiss_persist.load(_manifest(tmp_path), expected_db_checksum="sumF")
    assert loaded is not None
    faiss_index, chunk_ids, _vectors = loaded
    assert chunk_ids == [7]
