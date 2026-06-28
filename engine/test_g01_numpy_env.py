"""
G01 — Reproducible numerical environment tests.
Ensures NumPy and FAISS expose the constructors the engine needs. These catch
partial installs (e.g. namespace packages without full wheels on py3.14).
"""
import numpy as np

def test_numpy_has_version_and_core_constructors():
    """NumPy must expose __version__ and the array factories used by retrieval/faiss."""
    assert hasattr(np, "__version__"), "numpy.__version__ missing — partial/broken install"
    assert isinstance(np.__version__, str) and len(np.__version__) > 0
    # Core ops used across engine (retrieval, faiss_persist, tests, sovrd)
    a = np.array([1, 2, 3])
    assert a.shape == (3,)
    assert np.ones(3).shape == (3,)
    assert np.zeros((2, 2)).shape == (2, 2)
    # random used in some tests / embedding sim
    assert hasattr(np.random, "randn")

def test_numpy_numeric_stability_basic():
    """Basic numeric ops to catch completely broken wheels."""
    x = np.array([0.1, 0.2, 0.3])
    assert np.allclose(x + 1.0, [1.1, 1.2, 1.3])


def test_faiss_has_core_index_constructors():
    """FAISS must expose the index constructors used by retrieval/cache paths."""
    import faiss

    assert hasattr(faiss, "IndexFlatIP"), "faiss.IndexFlatIP missing — partial/broken install"
    index = faiss.IndexFlatIP(3)
    vectors = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    index.add(vectors)
    distances, indices = index.search(vectors, 1)
    assert indices.shape == (1, 1)
    assert distances.shape == (1, 1)
