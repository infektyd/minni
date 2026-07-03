"""
engine/backends — VectorBackend adapters.

Available backends:
  faiss_disk  : FAISS with disk persistence (default, wraps FAISSIndex + faiss_persist)
  faiss_mem   : FAISS pure in-memory (no persistence)
  qdrant      : Stub — install sovereign-memory[qdrant] to activate
  lance       : Stub — install sovereign-memory[lance] to activate
  multi       : Fan-out adapter that wraps N backends and merges via RRF

Import via:
    from backends.faiss_disk import FaissDiskBackend
    from backends.faiss_mem import FaissMemBackend
    from backends.multi import MultiBackend
"""

from minni.backends.faiss_disk import FaissDiskBackend
from minni.backends.faiss_mem import FaissMemBackend
from minni.backends.multi import MultiBackend

__all__ = [
    "FaissDiskBackend",
    "FaissMemBackend",
    "MultiBackend",
]
