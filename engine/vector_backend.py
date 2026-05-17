"""
Sovereign Memory — VectorBackend Protocol and Data Types.

PR-3: Defines the protocol that all vector-store adapters must satisfy,
plus the VectorItem and VectorHit data types.

All backends:
  - name     : str identifier ("faiss-disk", "faiss-mem", "qdrant", "lance")
  - dim      : embedding dimension (must match config.embedding_dim)
  - upsert() : add/update vectors by chunk_id
  - remove() : delete vectors by chunk_id
  - search() : k-nearest-neighbour query, returns VectorHit list
  - stats()  : diagnostic dict (vector_count, name, dim, ...)

FAISS backends delegate metadata filtering to SQLite (post-filter):
  1. Ask FAISS for k*multiplier results.
  2. Apply SQLite filter on chunk_ids.
  3. Return top-k after filter.

Qdrant/Lance stubs raise ImportError on construction so they are
never instantiated without the optional extra packages.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, runtime_checkable

import numpy as np

if sys.version_info >= (3, 8):
    from typing import Protocol
else:
    from typing_extensions import Protocol


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class VectorItem:
    """
    One unit of data to upsert into a vector backend.

    chunk_id   : Primary key matching chunk_embeddings.chunk_id in SQLite.
    doc_id     : Parent document ID (chunk_embeddings.doc_id).
    vector     : float32 numpy array of shape (dim,).
    metadata   : Supplemental fields used for post-filter in FAISS backends.
                 Stored in SQLite; backends may ignore if they support native filter.
    """
    chunk_id: int
    doc_id: int
    vector: np.ndarray
    metadata: Dict = field(default_factory=dict)

    # Convenience keys expected in metadata:
    #   agent        : str
    #   layer        : str  (e.g. "vault", "learnings")
    #   source       : str  (file path)
    #   created_at   : float (unix timestamp)
    #   privacy_level: str  ("safe", "local-only", "private", "blocked")
    #   status       : str  (page_status from documents table)


@dataclass
class VectorHit:
    """
    One result from a vector search.

    chunk_id : Matching chunk ID (maps to chunk_embeddings.chunk_id).
    doc_id   : Parent document ID.
    score    : Similarity score (higher = more similar; range depends on backend).
    backend  : Name of the backend that produced this hit.
    """
    chunk_id: int
    doc_id: int
    score: float
    backend: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class VectorBackend(Protocol):
    """
    Protocol that every vector-store adapter must satisfy.

    Implementors:
      - engine/backends/faiss_disk.py   (default)
      - engine/backends/faiss_mem.py
      - engine/backends/qdrant.py       (stub)
      - engine/backends/lance.py        (stub)
      - engine/backends/multi.py        (fan-out adapter)
    """

    name: str
    """Human-readable identifier, e.g. "faiss-disk"."""

    dim: int
    """Embedding dimension. Must match config.embedding_dim."""

    def upsert(self, items: List[VectorItem]) -> None:
        """
        Add or replace vectors for the given chunk IDs.

        If a chunk_id already exists, the vector and metadata are updated.
        Implementations should be idempotent.
        """
        ...

    def remove(self, chunk_ids: List[int]) -> None:
        """
        Delete vectors for the given chunk IDs.

        IDs not present are silently ignored.
        For backends that do not support deletion (e.g. HNSW), mark for
        exclusion and clean up on next rebuild.
        """
        ...

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """
        Return up to *k* nearest neighbours for *query_vec*.

        Args:
            query_vec : float32 array, shape (dim,).
            k         : Maximum results to return.
            filter    : Optional metadata filter dict. FAISS backends ignore
                        this and do post-filtering via SQLite instead.
                        Native backends (Qdrant, Lance) can use it directly.

        Returns:
            List of VectorHit sorted by score descending.
        """
        ...

    def stats(self) -> Dict:
        """
        Return a dict with at least:
          {
            "name": str,
            "dim": int,
            "vector_count": int,
          }
        Additional keys are allowed.
        """
        ...
