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

Inference is NOT thread-safe on these shared instances: callers must hold
get_embedder_lock() / get_cross_encoder_lock() / get_attribution_lock()
around .encode() / .predict(). See issue #284 (MPS allocator leak under
concurrent unlocked encode on Apple Silicon).
"""

import functools
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from minni.config import DEFAULT_CONFIG

logger = logging.getLogger("sovereign.models")

# One lock per singleton so unrelated models can still run concurrently;
# only same-model encode/predict calls serialize (#284).
_EMBEDDER_LOCK = threading.Lock()
_CROSS_ENCODER_LOCK = threading.Lock()
_ATTRIBUTION_LOCK = threading.Lock()

# Packaging-only first-run visibility (PACKAGING_PLAN.md §3, approved hook):
# sentence-transformers downloads model weights silently on first use, which
# reads as a multi-minute hang on a fresh install. Announce the one-time
# download before it starts. No load behavior changes.
_APPROX_SIZES = {
    "embedding": "~90 MB",
    "reranker": "~90 MB",
    "attribution": "~140 MB",
}


def get_embedder_lock() -> threading.Lock:
    """Lock guarding encode() on the process-wide embedder singleton."""
    return _EMBEDDER_LOCK


def get_cross_encoder_lock() -> threading.Lock:
    """Lock guarding predict() on the process-wide reranker singleton."""
    return _CROSS_ENCODER_LOCK


def get_attribution_lock() -> threading.Lock:
    """Lock guarding predict() on the process-wide attribution singleton."""
    return _ATTRIBUTION_LOCK


def _resolve_model_device() -> Optional[str]:
    """Device for ST constructors, or None to omit device= (library auto-select).

    Reads the live env first so minnid's ``setdefault("MINNI_MODEL_DEVICE",
    "cpu")`` in main() wins even though DEFAULT_CONFIG was snapshotted at
    import. Empty/unset → None → no ``device=`` kwarg (indexer/backfill keep
    MPS auto-select). Explicit ``mps``/``cuda``/``cpu`` always honored.
    """
    raw = (os.environ.get("MINNI_MODEL_DEVICE") or "").strip()
    if raw:
        return raw
    cfg = getattr(DEFAULT_CONFIG, "model_device", None)
    if cfg is None:
        return None
    cfg_s = str(cfg).strip()
    return cfg_s or None


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
        device = _resolve_model_device()
        kwargs = {}
        if device is not None:
            kwargs["device"] = device
        model = SentenceTransformer(DEFAULT_CONFIG.embedding_model, **kwargs)
        logger.info(
            "Embedding model loaded (singleton): %s device=%s",
            DEFAULT_CONFIG.embedding_model,
            device if device is not None else "auto",
        )
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
        device = _resolve_model_device()
        kwargs = {}
        if device is not None:
            kwargs["device"] = device
        model = CrossEncoder(DEFAULT_CONFIG.reranker_model, **kwargs)
        logger.info(
            "Cross-encoder loaded (singleton): %s device=%s",
            DEFAULT_CONFIG.reranker_model,
            device if device is not None else "auto",
        )
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
        device = _resolve_model_device()
        kwargs = {}
        if device is not None:
            kwargs["device"] = device
        model = CrossEncoder(DEFAULT_CONFIG.attribution_model, **kwargs)
        logger.info(
            "Attribution cross-encoder loaded (singleton): %s device=%s",
            DEFAULT_CONFIG.attribution_model,
            device if device is not None else "auto",
        )
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — attribution cross-encoder unavailable"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load attribution cross-encoder %s: %s", DEFAULT_CONFIG.attribution_model, e)
        return None
