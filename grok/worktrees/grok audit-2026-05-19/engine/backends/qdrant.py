"""
engine/backends/qdrant.py — Qdrant vector-store stub.

This module is protocol-conformant but non-functional.
To activate, install the qdrant extra:

    pip install "sovereign-memory[qdrant]"
    # i.e.: pip install qdrant-client

Construction raises ImportError if qdrant_client is not installed.
When qdrant_client is present (future activation), fill in the
__init__, upsert, remove, and search methods.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorItem, VectorHit


class QdrantBackend:
    """
    VectorBackend stub for Qdrant.

    Non-functional until qdrant_client is installed.
    Raises ImportError with install instructions on construction.
    """

    name: str = "qdrant"

    def __init__(self, config=None, collection: str = "sovereign_memory", **kwargs):
        """
        Args:
            config     : SovereignConfig (uses DEFAULT_CONFIG if None).
            collection : Qdrant collection name.
            **kwargs   : Passed through to qdrant_client.QdrantClient.

        Raises:
            ImportError : If qdrant-client is not installed.
        """
        try:
            import qdrant_client  # noqa: F401
        except ImportError:
            raise ImportError(
                "Qdrant backend requires qdrant-client. "
                "Install it with: pip install 'sovereign-memory[qdrant]' "
                "or: pip install qdrant-client"
            )

        from config import DEFAULT_CONFIG
        self.config = config or DEFAULT_CONFIG
        self.dim: int = self.config.embedding_dim
        self.collection = collection
        # self._client = qdrant_client.QdrantClient(**kwargs)  # activate when ready

    # ------------------------------------------------------------------
    # VectorBackend protocol
    # ------------------------------------------------------------------

    def upsert(self, items: List[VectorItem]) -> None:
        """Upsert vectors into the Qdrant collection."""
        raise NotImplementedError("QdrantBackend.upsert not yet implemented")

    def remove(self, chunk_ids: List[int]) -> None:
        """Delete vectors from the Qdrant collection."""
        raise NotImplementedError("QdrantBackend.remove not yet implemented")

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """Search the Qdrant collection."""
        raise NotImplementedError("QdrantBackend.search not yet implemented")

    def stats(self) -> Dict:
        """Return diagnostic stats dict."""
        return {
            "name": self.name,
            "dim": self.dim,
            "vector_count": 0,
            "status": "stub-not-implemented",
        }
