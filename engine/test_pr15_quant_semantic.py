import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def test_pr15_config_defaults_preserve_existing_behavior():
    from config import SovereignConfig

    cfg = SovereignConfig()

    assert cfg.embedding_quantization == "fp32"
    assert cfg.chunking_semantic_merge is False


def test_semantic_merge_is_opt_in_and_merges_adjacent_same_heading(monkeypatch):
    import chunker as chunker_mod
    from chunker import MarkdownChunker
    from config import SovereignConfig

    text = "# Topic\nalpha beta gamma delta.\n\nalpha beta gamma epsilon."

    base_cfg = SovereignConfig(chunk_size=5, chunk_overlap=0, min_tokens=1)
    base_chunks = MarkdownChunker(base_cfg).chunk_document(text)
    assert len(base_chunks) == 2

    class FakeEmbedder:
        def encode(self, value):
            if "epsilon" in value:
                return np.array([1.0, 0.01, 0.0, 0.0], dtype=np.float32)
            return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(chunker_mod, "get_embedder", lambda: FakeEmbedder(), raising=False)

    merge_cfg = SovereignConfig(
        chunk_size=5,
        chunk_overlap=0,
        min_tokens=1,
        max_tokens=20,
        chunking_semantic_merge=True,
    )
    merged = MarkdownChunker(merge_cfg).chunk_document(text)

    assert len(merged) == 1
    assert "delta" in merged[0].text
    assert "epsilon" in merged[0].text
    assert merged[0].heading == "Topic"


def test_semantic_merge_respects_max_tokens(monkeypatch):
    import chunker as chunker_mod
    from chunker import MarkdownChunker
    from config import SovereignConfig

    class FakeEmbedder:
        def encode(self, value):
            return np.ones(4, dtype=np.float32)

    monkeypatch.setattr(chunker_mod, "get_embedder", lambda: FakeEmbedder(), raising=False)

    cfg = SovereignConfig(
        chunk_size=5,
        chunk_overlap=0,
        min_tokens=1,
        max_tokens=7,
        chunking_semantic_merge=True,
    )
    chunks = MarkdownChunker(cfg).chunk_document(
        "# Topic\none two three four five.\n\nsix seven eight nine ten."
    )

    assert len(chunks) == 2


def test_int8_quantization_uses_scalar_quantized_faiss_when_available():
    from config import SovereignConfig
    from faiss_index import FAISSIndex

    class FakeHnsw:
        efConstruction = None
        efSearch = None

    class FakeQuantizedIndex:
        def __init__(self, dim, quantizer_type, m, metric):
            self.dim = dim
            self.quantizer_type = quantizer_type
            self.m = m
            self.metric = metric
            self.hnsw = FakeHnsw()
            self.added = None

        def add(self, vectors):
            self.added = vectors

        def search(self, query, k):
            return np.array([[1.0]], dtype=np.float32), np.array([[0]], dtype=np.int64)

    class FakeScalarQuantizer:
        QT_8bit = "qt8"

    class FakeFaiss:
        ScalarQuantizer = FakeScalarQuantizer
        METRIC_INNER_PRODUCT = "ip"
        IndexHNSWSQ = FakeQuantizedIndex

    cfg = SovereignConfig(embedding_dim=4, embedding_quantization="int8")
    idx = FAISSIndex(cfg)
    idx._faiss = FakeFaiss
    idx.build_from_vectors([42], np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))

    assert isinstance(idx._index, FakeQuantizedIndex)
    assert idx._current_type == "hnsw-sq-int8"
    assert idx.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)) == [(42, 1.0)]


def test_quantization_setting_is_part_of_faiss_manifest(tmp_path):
    from faiss_persist import load, save

    manifest = str(tmp_path / "index.manifest.json")
    ok = save(
        index=None,
        vectors=[np.ones(4, dtype=np.float32)],
        chunk_ids=[1],
        manifest_path=manifest,
        embedding_model="test",
        vector_dim=4,
        db_checksum="checksum",
        embedding_quantization="fp32",
    )
    assert ok

    assert load(
        manifest,
        expected_db_checksum="checksum",
        expected_quantization="int8",
    ) is None
    assert load(
        manifest,
        expected_db_checksum="checksum",
        expected_quantization="fp32",
    ) is not None


def test_index_cli_semantic_merge_flag_is_opt_in(monkeypatch, capsys):
    import sovereign_memory

    calls = []

    def fake_index_all(config=None, vault=True, wiki=True, verbose=False):
        calls.append((config.chunking_semantic_merge, vault, wiki, verbose))
        return {"ok": True}

    monkeypatch.setattr("index_all.index_all", fake_index_all)

    sovereign_memory.cmd_index(["--semantic-merge", "--vault-only", "--verbose"])

    assert calls == [(True, True, False, True)]
    assert '"ok": true' in capsys.readouterr().out
