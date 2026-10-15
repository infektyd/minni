"""
minni/backends — VectorBackend adapters.

Available backends:
  faiss_disk  : FAISS with disk persistence (default, wraps FAISSIndex + faiss_persist)
  faiss_mem   : FAISS pure in-memory (no persistence)

Import via:
    from minni.backends.faiss_disk import FaissDiskBackend
    from minni.backends.faiss_mem import FaissMemBackend
"""

from minni.backends.faiss_disk import FaissDiskBackend
from minni.backends.faiss_mem import FaissMemBackend

__all__ = [
    "FaissDiskBackend",
    "FaissMemBackend",
]

