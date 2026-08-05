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


_TORCH_THREADS_PINNED = False
_TORCH_THREADS_PIN_LOCK = threading.Lock()


def _pin_torch_threads_for_cpu_once(device: Optional[str]) -> None:
    """#299: belt-and-suspenders API-level pin alongside minnid.main()'s
    OMP_NUM_THREADS/MKL_NUM_THREADS/VECLIB_MAXIMUM_THREADS env pins.

    The env pins must be set before torch imports (they are, in main(), at
    process start) so libomp never spins up more than one worker thread —
    that's what actually prevents the fork-barrier SIGSEGV from two OpenMP
    runtimes (PyTorch's bundled libomp vs FAISS's bundled libomp; both are
    already tolerated in-process by the pre-existing KMP_DUPLICATE_LIB_OK=TRUE
    set in each getter below, which is why the collision manifests as a
    fork-barrier crash rather than the loud "OMP: Error #15" abort that flag
    suppresses) colliding. This call is a second line of defense at the API
    level for whichever singleton first triggers the torch import, and is a
    no-op unless CPU inference was actually resolved: MPS ops never touch
    OpenMP, so there is nothing to pin when running on MPS, and batch tools
    (indexer/backfill), which never enter minnid.main() and keep MPS
    auto-select, must not have their multi-threaded math throttled by a
    daemon-only crash workaround.

    Comparison is case/suffix-normalized ("CPU", "cpu:0") since
    ``_resolve_model_device`` passes the operator's raw env/config string
    through unmodified to the ST constructor's ``device=`` kwarg — a
    literal ``!= "cpu"`` here would silently skip the pin for a value the
    constructor itself accepts as CPU.

    Guarded by a lock: three getters (embedder/cross-encoder/attribution)
    can race this check-then-set from concurrent daemon RPC threads before
    the encode/predict locks in this module are ever acquired.
    """
    global _TORCH_THREADS_PINNED
    normalized = (device or "").strip().lower().split(":")[0]
    if normalized != "cpu":
        return
    with _TORCH_THREADS_PIN_LOCK:
        if _TORCH_THREADS_PINNED:
            return
        try:
            import torch

            torch.set_num_threads(1)
            _TORCH_THREADS_PINNED = True
        except Exception:
            pass  # best-effort; the env pins in main() are the primary defense


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
        _pin_torch_threads_for_cpu_once(device)
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
        _pin_torch_threads_for_cpu_once(device)
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
        _pin_torch_threads_for_cpu_once(device)
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
