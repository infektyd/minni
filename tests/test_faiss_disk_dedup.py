"""FaissDiskBackend.search must keep the BEST chunk per doc (reality-bench seam 7).

The dedup loop used to drop the existing hit for a doc and re-append the
current chunk unconditionally, so a lower-scoring later chunk replaced the
best one and the doc ranked by its weakest match.
"""
import contextlib
import sqlite3

import numpy as np

from minni.backends.faiss_disk import FaissDiskBackend


class _DB:
    def __init__(self, rows):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE chunk_embeddings (chunk_id INTEGER, doc_id INTEGER)")
        self.conn.executemany("INSERT INTO chunk_embeddings VALUES (?, ?)", rows)

    @contextlib.contextmanager
    def cursor(self):
        yield self.conn.cursor()


class _Index:
    def __init__(self, raw):
        self.raw = raw
        self.count = len(raw)

    def search(self, query, top_k=5):
        return list(self.raw)


def _backend(raw, rows):
    b = object.__new__(FaissDiskBackend)
    b.name = "faiss-disk"
    b.dim = 4
    b._faiss = _Index(raw)
    b.db = _DB(rows)
    return b


def test_best_chunk_per_doc_survives_lower_scoring_sibling():
    # FAISS returns best-first: doc 10's best chunk (0.9) precedes its weak one (0.2).
    b = _backend([(1, 0.9), (3, 0.6), (2, 0.2)], [(1, 10), (2, 10), (3, 20)])
    hits = b.search(np.zeros(4, dtype=np.float32), k=5)
    by_doc = {h.doc_id: (h.chunk_id, h.score) for h in hits}
    assert by_doc[10] == (1, 0.9)
    assert by_doc[20] == (3, 0.6)
    assert [h.doc_id for h in hits] == [10, 20]
    assert len(hits) == 2


def test_higher_scoring_later_chunk_still_replaces():
    b = _backend([(2, 0.3), (1, 0.8)], [(1, 10), (2, 10)])
    hits = b.search(np.zeros(4, dtype=np.float32), k=5)
    assert [(h.chunk_id, h.score) for h in hits] == [(1, 0.8)]
