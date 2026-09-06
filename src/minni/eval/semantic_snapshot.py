"""Frozen semantic retrieval over a prepared study snapshot directory.

Separate backend from the lexical ``snapshot`` baseline: same frozen
validation, same least-privilege principal, same generation pinning, same
eligibility filter-before-limit and lifecycle gates — but ranking is exact
brute-force cosine similarity over document-level vectors from the engine's
embedding interface (``minni.models.get_embedder`` /
``SentenceTransformer.encode``), never FTS.

Deliberate minimum, stated honestly:

- Document-level vectors (one vector per vault file); no chunking, no
  cross-encoder re-rank, no HyDE, no FAISS lane. ``FAISSIndex`` is
  intentionally not used: its constructor is bound to the live
  ``DEFAULT_CONFIG`` paths, and the snapshot corpora are bounded
  (``MAX_RECORDS``), so exact in-memory cosine is both sufficient and
  free of live-path entanglement.
- The embedding model is injected in tests (deterministic, no inference).
  Without an injected embedder the runner loads the real engine embedder;
  when no model is available it fails closed — a semantic backend never
  silently degrades to lexical scoring.
- Vectors are frozen at initialization from the verified snapshot; every
  search re-validates the snapshot and its generation pin, and every
  served row is hash-bound to the frozen vault bytes, mirroring the
  lexical baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .retrievers import SnapshotSearcher

SEMANTIC_BACKEND = "snapshot-semantic"

# Ranking-leg options this runner refuses when enabled. The claim is
# semantic-ONLY: no expand, no HyDE, no cross-encoder re-rank, no hybrid
# merge. Anything outside (None, False, "off") counts as enabled; numeric
# zero compares equal to False and stays allowed (explicitly no rerank).
_UNSUPPORTED_RERANK_OPTIONS = (
    "expand", "use_hyde", "hyde", "hybrid",
    "rerank", "use_reranker", "rerank_top_k", "cross_encoder",
)

# Model-identity attributes probed without invention: the first present,
# non-None value wins, otherwise the field records "unknown".
_REVISION_ATTRS = ("revision", "commit_hash", "model_revision", "revision_hash")
_ARTIFACT_ATTRS = ("model_name_or_path", "model_path", "cache_path", "artifact_path")
_ENCODING_ATTRS = (
    "max_seq_length", "truncation", "truncate", "truncate_dim",
    "default_prompt_name", "prompts", "similarity_fn_name",
)


def _default_embedder() -> Tuple[Any, str]:
    """Load the real engine embedder; name only, no database or vault touch.

    Returns ``(embedder, model_name)`` where the embedder follows the
    ``SentenceTransformer.encode`` interface. ``(None, name)`` when the
    model is unavailable; the caller fails closed on ``None``.
    """
    from minni.config import DEFAULT_CONFIG
    from minni.models import get_embedder

    return get_embedder(), str(DEFAULT_CONFIG.embedding_model)


def _first_present(obj: Any, names: Tuple[str, ...]) -> Any:
    """First present, non-None probed attribute, else None (never invented)."""
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:  # noqa: BLE001 - a hostile property must not break probing
            continue
        if value is not None:
            return value
    return None


def _safe_scalar(value: Any) -> Any:
    """Bounded JSON-safe scalar for provenance; anything exotic becomes its type."""
    if value is None or isinstance(value, (bool, int, float, str)):
        text = str(value)
        return text if len(text) <= 200 else text[:200] + "..."
    return f"<{type(value).__name__}>"


def _as_unit_matrix(embedder: Any, texts: List[str], dim: int) -> Any:
    """Encode texts through the engine embedding interface into unit rows.

    Shape, dtype, finiteness, and dimension consistency are enforced: a
    ragged or degenerate embedding output is an integrity failure, not
    something to rank through.
    """
    import numpy as np

    raw = embedder.encode(texts)
    matrix = np.asarray(raw, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] != len(texts) or matrix.shape[1] < 1:
        raise ValueError(
            "snapshot semantic embedder must return one vector per text; "
            f"got shape {matrix.shape} for {len(texts)} texts"
        )
    if matrix.shape[1] != dim:
        raise ValueError(
            f"snapshot semantic embedder changed dimension mid-corpus "
            f"({dim} -> {matrix.shape[1]}); refusing mixed-dimension ranking"
        )
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("snapshot semantic embedder returned non-finite values")
    norms = np.linalg.norm(matrix, axis=1)
    if bool(np.any(norms <= 0.0)):
        raise ValueError("snapshot semantic embedder returned a zero vector")
    return matrix / norms[:, None]


class SnapshotSemanticSearcher(SnapshotSearcher):
    """Governed semantic retrieval over a prepared study snapshot directory.

    Inherits frozen validation, the least-privilege vault-scoped principal
    (reserved operator identities rejected), and the generation pin from
    the lexical baseline unmodified. Only the ranking leg differs: cosine
    similarity over frozen document vectors instead of FTS5 MATCH. The
    inherited forbidden-ID set (expected_eligible judgments) is used for
    SCORING only, never for retrieval: filtering consults the real
    authorization gate and lifecycle statuses, so a policy-readable row is
    returned even when its judgment label says ineligible. Read-only,
    deadline-free, and confined to the snapshot directory — the live
    ``DEFAULT_CONFIG`` database and vault are never touched (the default
    embedder loader reads only the model name).
    """

    backend = SEMANTIC_BACKEND

    def __init__(
        self,
        snapshot_dir: Path,
        embedder: Any = None,
        embedder_name: Optional[str] = None,
    ) -> None:
        super().__init__(snapshot_dir)
        # An explicitly passed embedder object is injected (deterministic
        # test double); the lazily loaded engine singleton is the real model.
        self._embedder_injected = embedder is not None
        if embedder is None:
            embedder, resolved_name = _default_embedder()
            if embedder is None:
                raise ValueError(
                    "snapshot semantic searcher has no embedding model "
                    f"({resolved_name}); refusing to degrade to lexical scoring"
                )
            resolved_model = resolved_name
        else:
            # Actual object identity, never the caller-supplied label: an
            # arbitrary embedder_name must not relabel the real model, and
            # an injected double must never be marked as the real model.
            resolved_model = (
                f"{type(embedder).__module__}.{type(embedder).__qualname__}"
            )
        self._embedder = embedder
        vectors = self._freeze_vectors()
        self._doc_ids = vectors["doc_ids"]
        self._study_ids = vectors["study_ids"]
        self._texts = vectors["texts"]
        self._statuses = vectors["statuses"]
        self._matrix = vectors["matrix"]
        self.embedding_dim = int(self._matrix.shape[1])
        revision = _first_present(embedder, _REVISION_ATTRS)
        artifact = _first_present(embedder, _ARTIFACT_ATTRS)
        self.embedding_provenance: Dict[str, Any] = {
            "model": resolved_model,
            "caller_label": embedder_name,
            "revision": _safe_scalar(revision) if revision is not None else "unknown",
            "artifact": _safe_scalar(artifact) if artifact is not None else "unknown",
            "encoding": {
                name: _safe_scalar(getattr(embedder, name, "unknown"))
                for name in _ENCODING_ATTRS
            },
            "dim": self.embedding_dim,
            "vector_sha256": vectors["vector_sha256"],
            "vector_count": len(self._doc_ids),
            "injected": self._embedder_injected,
            "snapshot_id": self._pinned["snapshot_id"],
            "manifest_digest": self._pinned["manifest_digest"],
            "note": (
                "Deterministic injected vectors exercise plumbing only and "
                "establish no retrieval quality."
                if self._embedder_injected
                else "Real engine embedder over the frozen snapshot corpus only."
            ),
        }

    def _freeze_vectors(self) -> Dict[str, Any]:
        """Embed every mapped vault file once, in sorted study-ID order."""
        import numpy as np

        from .study_snapshot import (
            MAX_VAULT_FILE_BYTES,
            StudySnapshotError,
            _read_sized_bytes,
            check_materialized,
            verify_snapshot,
        )

        verified = verify_snapshot(self.snapshot_dir)
        materialized = check_materialized(self.snapshot_dir)
        mapping = verified["mapping"]
        document_ids = materialized["document_ids"]
        study_ids = sorted(mapping)
        texts: List[str] = []
        doc_ids: List[int] = []
        statuses: List[str] = []
        for study_id in study_ids:
            row = mapping[study_id]
            doc_id = document_ids.get(study_id)
            if type(doc_id) is not int or doc_id < 1:
                raise StudySnapshotError(
                    f"snapshot semantic corpus has no positive document ID for {study_id}"
                )
            artifact = row.get("artifact_path")
            raw = _read_sized_bytes(
                self.snapshot_dir / "vault" / str(artifact), self.snapshot_dir,
                f"snapshot vault file {artifact!r}", MAX_VAULT_FILE_BYTES)
            try:
                texts.append(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise StudySnapshotError(
                    f"snapshot vault file {artifact!r} must be UTF-8 text"
                ) from exc
            doc_ids.append(doc_id)
            provenance = row.get("source_provenance") or {}
            statuses.append(str(provenance.get("page_status", row.get("page_status"))
                                or "candidate"))
        if not texts:
            raise StudySnapshotError("snapshot semantic corpus holds no texts to embed")
        first = np.asarray(self._embedder.encode([texts[0]]), dtype=np.float64)
        if first.ndim != 2 or first.shape[0] != 1 or first.shape[1] < 1:
            raise StudySnapshotError(
                "snapshot semantic embedder must return one vector per text"
            )
        dim = int(first.shape[1])
        matrix = _as_unit_matrix(self._embedder, texts, dim)
        import hashlib

        digest = hashlib.sha256(
            np.ascontiguousarray(matrix, dtype=np.float64).tobytes()
        ).hexdigest()
        return {
            "doc_ids": doc_ids, "study_ids": study_ids, "texts": texts,
            "statuses": statuses, "matrix": matrix, "vector_sha256": digest,
        }

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        """Cosine-ranked frozen documents; eligibility gates run pre-limit."""
        import hashlib

        from minni.principal import can_read_document

        from .study_snapshot import (
            DEFAULT_EXCLUDED_STATUSES,
            MAX_VAULT_FILE_BYTES,
            StudySnapshotError,
            _read_sized_bytes,
            check_materialized,
            verify_snapshot,
        )

        enabled = [
            name for name in _UNSUPPORTED_RERANK_OPTIONS
            if kwargs.get(name) not in (None, False, "off")
        ]
        if enabled:
            raise ValueError(
                "snapshot semantic retrieval is semantic-only baseline: "
                f"unsupported ranking-leg options enabled: {', '.join(enabled)}"
            )
        limit = max(1, int(kwargs.get("limit", 10)))
        if not isinstance(query, str) or not query.strip():
            return []
        # Frozen state is re-validated before every search, not just at open.
        verified = verify_snapshot(self.snapshot_dir)
        materialized = check_materialized(self.snapshot_dir)
        current = self._fingerprint(
            verified["manifest"], verified["mapping"], materialized,
            tuple(self._principal.allowed_vault_roots),
        )
        if current != self._pinned:
            raise StudySnapshotError(
                "snapshot identity changed since searcher initialization "
                f"(was {self._pinned['snapshot_id']}); refusing to serve a "
                "replacement generation under a stale ID"
            )
        query_matrix = _as_unit_matrix(self._embedder, [query], self.embedding_dim)
        scores = (self._matrix @ query_matrix[0]).tolist()
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], self._doc_ids[i]))
        excluded = set(DEFAULT_EXCLUDED_STATUSES)
        results = []
        for index in order:
            doc_id = self._doc_ids[index]
            # Authorization and lifecycle filtering happens BEFORE the output
            # limit, so a blocked top row can never starve an eligible row
            # below it. The forbidden-ID set is evaluation ground truth
            # (expected_eligible judgments for SCORING), never authorization,
            # and is deliberately not consulted here: a row the policy deems
            # readable is returned even when the judgment labels it
            # ineligible, so the evaluator can observe the error.
            if (self._statuses[index] or "candidate") in excluded:
                continue
            metadata = {
                "path": None, "agent": None,
                "page_type": None, "privacy_level": None,
            }
            study_row = verified["mapping"][self._study_ids[index]]
            provenance = study_row.get("source_provenance") or {}
            metadata.update({
                "path": str(self.snapshot_dir / "vault" / study_row["artifact_path"]),
                "agent": provenance.get("agent", study_row.get("agent")),
                "page_type": provenance.get("page_type", study_row.get("page_type")),
                "privacy_level": provenance.get(
                    "privacy_level", study_row.get("privacy_level")),
            })
            if not can_read_document(self._principal, "default", metadata):
                continue
            text = self._texts[index]
            # Bind the served vector row to the frozen vault bytes: a
            # swapped vault file cannot be served under frozen provenance.
            raw = _read_sized_bytes(
                self.snapshot_dir / "vault" / study_row["artifact_path"],
                self.snapshot_dir,
                f"snapshot vault file {study_row['artifact_path']!r}",
                MAX_VAULT_FILE_BYTES)
            if hashlib.sha256(raw).hexdigest() != hashlib.sha256(
                    text.encode("utf-8")).hexdigest():
                raise StudySnapshotError(
                    "snapshot served row diverges from frozen vault bytes"
                )
            results.append({
                "doc_id": doc_id,
                "source": metadata["path"],
                "filename": Path(metadata["path"]).name,
                "text": text,
                "score": round(float(scores[index]), 4),
                "token_count": max(1, len(text) // 4),
                "agent": metadata["agent"],
                "privacy_level": metadata["privacy_level"],
                "page_status": self._statuses[index],
                "retriever": SEMANTIC_BACKEND,
                "provenance": {
                    "doc_id": doc_id,
                    "backend": SEMANTIC_BACKEND,
                    "snapshot_id": self._pinned["snapshot_id"],
                    "lexical_only": False,
                    "semantic_only": True,
                },
            })
            if len(results) >= limit:
                break
        return results
