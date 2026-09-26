"""Identity-envelope embedding helper.

``get_embedding`` is the shared embedder entry point the installer
(``propagate.py``) uses when it writes a hosted agent's identity envelope
into the documents/chunk_embeddings tables. Identity documents are seeded by
the installer per wired agent; this module no longer carries a hardcoded
agent roster or reads identity files from other agent platforms' homes.
"""

import os
import sys



def get_embedding(text, model=None):
    """Generate embedding for identity text. Uses the shared model singleton."""
    try:
        if model is None:
            # Use the process-wide singleton to avoid reloading weights
            import sys as _sys
            import os as _os
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from minni.models import get_embedder
            model = get_embedder()
        if model is None:
            raise ImportError("embedding model unavailable")
        from minni.models import get_embedder_lock

        with get_embedder_lock():
            emb = model.encode(text, show_progress_bar=False)
        import numpy as np
        return emb.astype(np.float32).tobytes()
    except ImportError:
        print("WARNING: sentence-transformers not available, using zeros", file=sys.stderr)
        import numpy as np
        return np.zeros(384, dtype=np.float32).tobytes()
