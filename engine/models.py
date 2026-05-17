"""
Sovereign Memory — Module-Level Model Singletons.

Provides cached, process-wide singletons for the embedding model and
cross-encoder re-ranker. Replacing scattered SentenceTransformer(...)
instantiations with these helpers means models are loaded exactly once
per process, cutting cold-start time when multiple engine components are
active simultaneously.

Usage:
    from models import get_embedder, get_cross_encoder

    embedder = get_embedder()          # SentenceTransformer singleton
    cross_enc = get_cross_encoder()    # CrossEncoder singleton (or None)

Both functions are safe to call from multiple threads; functools.cache
provides the lock-free singleton guarantee after the first call completes.
"""

import functools
import logging
import os

from config import DEFAULT_CONFIG

logger = logging.getLogger("sovereign.models")


@functools.cache
def get_embedder():
    """
    Return the process-wide SentenceTransformer singleton.

    Model name is taken from DEFAULT_CONFIG.embedding_model (all-MiniLM-L6-v2).
    Returns the model instance, or None if sentence-transformers is not installed.

    The returned instance is numerically identical to any SentenceTransformer
    constructed with the same model name — it IS the same object.
    """
    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(DEFAULT_CONFIG.embedding_model)
        logger.info("Embedding model loaded (singleton): %s", DEFAULT_CONFIG.embedding_model)
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — embedding model unavailable"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load embedding model %s: %s", DEFAULT_CONFIG.embedding_model, e)
        return None


@functools.cache
def get_cross_encoder():
    """
    Return the process-wide CrossEncoder singleton for re-ranking.

    Model name is taken from DEFAULT_CONFIG.reranker_model.
    Returns the CrossEncoder instance, or None if unavailable or disabled.
    """
    if not DEFAULT_CONFIG.reranker_enabled:
        return None
    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(DEFAULT_CONFIG.reranker_model)
        logger.info("Cross-encoder loaded (singleton): %s", DEFAULT_CONFIG.reranker_model)
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — cross-encoder unavailable"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load cross-encoder %s: %s", DEFAULT_CONFIG.reranker_model, e)
        return None
