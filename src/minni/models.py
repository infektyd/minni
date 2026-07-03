"""
Minni — Module-Level Model Singletons.

Provides cached, process-wide singletons for the embedding model and
cross-encoder re-ranker and NLI attribution scorer. Replacing scattered SentenceTransformer(...)
instantiations with these helpers means models are loaded exactly once
per process, cutting cold-start time when multiple engine components are
active simultaneously.

Usage:
    from models import get_embedder, get_cross_encoder, get_attribution_cross_encoder

    embedder = get_embedder()          # SentenceTransformer singleton
    cross_enc = get_cross_encoder()    # CrossEncoder singleton (or None)
    nli_enc = get_attribution_cross_encoder()  # NLI CrossEncoder singleton (or None)

Both functions are safe to call from multiple threads; functools.cache
provides the lock-free singleton guarantee after the first call completes.
"""

import functools
import logging
import os
import sys
from pathlib import Path

from minni.config import DEFAULT_CONFIG

logger = logging.getLogger("sovereign.models")

# Packaging-only first-run visibility (PACKAGING_PLAN.md §3, approved hook):
# sentence-transformers downloads model weights silently on first use, which
# reads as a multi-minute hang on a fresh install. Announce the one-time
# download before it starts. No load behavior changes.
_APPROX_SIZES = {
    "embedding": "~90 MB",
    "reranker": "~90 MB",
    "attribution": "~140 MB",
}


def _announce_download_once(model_name: str, role: str) -> None:
    """Print a one-time notice if `model_name` is not in the local HF cache."""
    try:
        if "HF_HUB_CACHE" in os.environ:
            cache = Path(os.environ["HF_HUB_CACHE"])
        else:
            cache = Path(os.environ.get(
                "HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        snapshots = cache / ("models--" + model_name.replace("/", "--")) / "snapshots"
        if snapshots.is_dir() and any(snapshots.iterdir()):
            return
        message = (
            f"First run: downloading {role} model {model_name} "
            f"({_APPROX_SIZES.get(role, 'tens of MB')}, one time, cached in "
            f"{cache}). This can take a few minutes."
        )
        logger.info(message)
        print(f"[minni] {message}", file=sys.stderr, flush=True)
    except OSError:
        pass  # visibility must never block a load


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
        _announce_download_once(DEFAULT_CONFIG.embedding_model, "embedding")
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
        _announce_download_once(DEFAULT_CONFIG.reranker_model, "reranker")
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


@functools.cache
def get_attribution_cross_encoder():
    """
    Return the process-wide CrossEncoder singleton for NLI attribution scoring.

    Model name is taken from DEFAULT_CONFIG.attribution_model.
    Returns the CrossEncoder instance, or None if unavailable or disabled.
    """
    if not DEFAULT_CONFIG.attribution_enabled:
        return None
    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        from sentence_transformers import CrossEncoder
        _announce_download_once(DEFAULT_CONFIG.attribution_model, "attribution")
        model = CrossEncoder(DEFAULT_CONFIG.attribution_model)
        logger.info("Attribution cross-encoder loaded (singleton): %s", DEFAULT_CONFIG.attribution_model)
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — attribution cross-encoder unavailable"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load attribution cross-encoder %s: %s", DEFAULT_CONFIG.attribution_model, e)
        return None
