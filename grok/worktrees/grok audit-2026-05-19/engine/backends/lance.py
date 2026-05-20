"""
engine/backends/lance.py — LanceDB vector-store stub.

This module is protocol-conformant but non-functional.
To activate, install the lance extra:

    pip install "sovereign-memory[lance]"
    # i.e.: pip install lancedb

Construction raises ImportError if lancedb is not installed.
When lancedb is present (future activation), fill in the
__init__, upsert, remove, and search methods.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorItem, VectorHit


class LanceBackend:
    """
    VectorBackend stub for LanceDB.

    Non-functional until lancedb is installed.
    Raises ImportError with install instructions on construction.
    """

    name: str = "lance"

    def __init__(self, config=None, table: str = "sovereign_memory", **kwargs):
        """
        Args:
            config  : SovereignConfig (uses DEFAULT_CONFIG if None).
            table   : LanceDB table name.
            **kwargs: Passed through to lancedb.connect.

        Raises:
            ImportError : If lancedb is not installed.
        """
        try:
            import lancedb  # noqa: F401
        except ImportError:
            raise ImportError(
                "Lance backend requires lancedb. "
                "Install it with: pip install 'sovereign-memory[lance]' "
                "or: pip install lancedb"
            )

        from config import DEFAULT_CONFIG
        self.config = config or DEFAULT_CONFIG
        self.dim: int = self.config.embedding_dim
        self.table = table
        # self._db = lancedb.connect(**kwargs)  # activate when ready

    # ------------------------------------------------------------------
    # VectorBackend protocol
    # ------------------------------------------------------------------

    def upsert(self, items: List[VectorItem]) -> None:
        """Upsert vectors into the LanceDB table."""
        raise NotImplementedError("LanceBackend.upsert not yet implemented")

    def remove(self, chunk_ids: List[int]) -> None:
        """Delete vectors from the LanceDB table."""
        raise NotImplementedError("LanceBackend.remove not yet implemented")

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """Search the LanceDB table."""
        raise NotImplementedError("LanceBackend.search not yet implemented")

    def stats(self) -> Dict:
        """Return diagnostic stats dict."""
        return {
            "name": self.name,
            "dim": self.dim,
            "vector_count": 0,
            "status": "stub-not-implemented",
        }
