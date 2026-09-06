"""P1 graph candidate shortlist preparation (spec §2.2, privacy addendum).

Pure prepare-only function: turns a supplied FAISS hit snapshot plus
caller-supplied metadata/content accessors into ranked canonical learning
descriptors for the edge classifier. No DB access, no model invocation, no
daemon wiring, no activation — the caller owns storage and inference.

Privacy ordering (addendum §3.1, §5): metadata is authorized BEFORE any
candidate text is loaded. ``get_content`` is invoked only for documents that
passed ``can_read_document`` and only for the classifier pairs — never for
excluded, deferred, or unreadable documents.

Single-store scope is explicit on hits and accessor envelopes. Accessors are
trusted storage adapters bound to the supplied opaque store identity, not
security attestation: they must return a consistent snapshot and truthful
metadata/content hashes. This function rejects conflicting identities and
verifies fetched full text against its metadata hash. Deferred hashes remain
accessor claims until content is fetched. Publication must revalidate them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from minni.principal import EffectivePrincipal, can_read_document

logger = logging.getLogger("sovereign.graph_candidates")

# Spec §2.2 normative bounds.
MAX_CHUNK_HITS = 48
MAX_SHORTLIST_DOCS = 12
COSINE_FLOOR = 0.42
MAX_CLASSIFIER_PAIRS = 8

# Phase 1 filter (addendum drift vector 6): learning-to-learning only.
REQUIRED_MEMORY_KIND = "learning"
REQUIRED_PAGE_TYPE = "learning"

# Closed/restricted rows are never candidates. Mirrors the content-derived
# eligibility set used by projection repair; the STORED row is authoritative.
_CLOSED_STATUSES = frozenset({"draft", "expired", "rejected", "superseded"})


@dataclass(frozen=True)
class CandidateDescriptor:
    """One shortlisted canonical learning, renderer-compatible.

    Carries pair_id/title/excerpt/status for the edge prompt renderer plus
    doc/chunk/cosine identity for later revalidation. ``excerpt`` is set only
    on classifier pairs; deferred descriptors retain identity without text.
    """

    pair_id: str
    store_id: str
    content_sha256: str
    metadata_sha256: str
    evidence_sha256: str
    doc_id: int
    chunk_id: int
    cosine: float
    title: str
    status: str
    page_type: str
    excerpt: Optional[str]
    excerpt_tokens: int
    excerpt_measured: bool


@dataclass(frozen=True)
class CandidateShortlist:
    """Bounded shortlist result. All sequences are rank-ordered tuples."""

    pairs: Tuple[CandidateDescriptor, ...]
    deferred: Tuple[CandidateDescriptor, ...]
    examined_chunks: int
    examined_docs: int
    excluded_before_cap: int
    below_floor: int
    malformed_hits: int
    hits_truncated: bool


def _is_doc_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_score(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score):
        return None
    return score


def _pair_id(doc_id: int) -> str:
    return f"candidate-doc-{doc_id}"


def _metadata_authorized(
    principal: EffectivePrincipal, workspace: str, metadata: Any
) -> bool:
    """Fail-closed metadata gate. Non-dict or unreadable metadata denies."""
    if not isinstance(metadata, dict):
        return False
    try:
        return bool(can_read_document(principal, workspace, metadata))
    except Exception:
        logger.warning("graph candidates: authorization check failed", exc_info=True)
        return False


def _metadata_eligible(metadata: Dict[str, Any]) -> bool:
    """Phase 1 + lifecycle filter on the STORED row (never content defaults)."""
    if metadata.get("memory_kind") != REQUIRED_MEMORY_KIND:
        return False
    if str(metadata.get("page_type") or "").lower() != REQUIRED_PAGE_TYPE:
        return False
    if metadata.get("privacy_level") == "blocked":
        return False
    if str(metadata.get("page_status") or "") in _CLOSED_STATUSES:
        return False
    return True


def prepare_candidate_shortlist(
    *,
    hits: Sequence[Dict[str, Any]],
    store_id: str,
    principal: EffectivePrincipal,
    get_metadata: Callable[[str, int], Optional[Dict[str, Any]]],
    get_content: Callable[[str, int], Optional[Dict[str, Any]]],
    source_doc_id: Optional[int] = None,
    workspace: str = "default",
    max_chunk_hits: int = MAX_CHUNK_HITS,
    max_docs: int = MAX_SHORTLIST_DOCS,
    cosine_floor: float = COSINE_FLOOR,
    max_pairs: int = MAX_CLASSIFIER_PAIRS,
) -> CandidateShortlist:
    """Build the bounded classifier shortlist from a FAISS hit snapshot.

    Args:
        hits: Ranked chunk-hit dicts with int ``chunk_id``, int ``doc_id``,
            finite numeric ``cosine`` (or ``score``), and matching ``store_id``.
        store_id: Nonempty opaque identity of the one storage snapshot. The
            caller must bind this to actual storage, not a display name.
        principal: Caller-stamped EffectivePrincipal. Never an agent string.
        get_metadata: ``(store_id, doc_id) -> stored metadata dict | None``.
            Must include matching store_id/doc_id and content_sha256 of the
            complete text in the same snapshot. Called before content loading.
        get_content: ``(store_id, doc_id) -> {store_id, doc_id, content} | None``.
            Called only for authorized pair candidates. Fetched text is hashed
            and checked against metadata before it can enter a descriptor.
        source_doc_id: Committing document; its hits are excluded as self.
        workspace: Workspace scope for read authorization.
        max_chunk_hits/max_docs/cosine_floor/max_pairs: Bounds, clamped to
            the normative ceilings (never raised above them).

    Raises:
        ValueError: Malformed envelope (bad hits/principal/accessors/scope)
            or cross-corpus aliasing (one chunk_id under two doc_ids).

    Returns:
        CandidateShortlist with ≤max_pairs excerpt-bearing pairs plus the
        remaining identity-only shortlist. Denied/absent documents are
        dropped silently (indistinguishable), with aggregate counts only.
    """
    if not isinstance(store_id, str) or not store_id.strip():
        raise ValueError("store_id must be a non-empty opaque storage identity")
    if not isinstance(principal, EffectivePrincipal):
        raise ValueError("principal must be a caller-stamped EffectivePrincipal")
    if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes, dict)):
        raise ValueError("hits must be a sequence of chunk-hit dicts")
    if not callable(get_metadata) or not callable(get_content):
        raise ValueError("get_metadata and get_content must be callable")
    if not isinstance(workspace, str) or not workspace:
        raise ValueError("workspace must be a non-empty string")
    if source_doc_id is not None and not _is_doc_id(source_doc_id):
        raise ValueError("source_doc_id must be an int doc id")
    max_chunk_hits = max(0, min(int(max_chunk_hits), MAX_CHUNK_HITS))
    max_docs = max(0, min(int(max_docs), MAX_SHORTLIST_DOCS))
    max_pairs = max(0, min(int(max_pairs), MAX_CLASSIFIER_PAIRS))
    cosine_floor = float(cosine_floor)
    if not math.isfinite(cosine_floor):
        raise ValueError("cosine_floor must be finite")

    cosine_floor = max(COSINE_FLOOR, cosine_floor)

    # 1) Bound the snapshot; validate rows; dedup chunks to docs by max
    #    cosine. A chunk_id under two doc_ids is cross-corpus aliasing.
    truncated = len(hits) > max_chunk_hits
    best: Dict[int, Tuple[float, int]] = {}
    chunk_owner: Dict[int, int] = {}
    malformed = 0
    examined_chunks = min(len(hits), max_chunk_hits)
    for index in range(examined_chunks):
        entry = hits[index]
        if not isinstance(entry, dict):
            malformed += 1
            continue
        doc_id = entry.get("doc_id")
        chunk_id = entry.get("chunk_id")
        score = _finite_score(
            entry.get("cosine", entry.get("score", None))
        )
        if not _is_doc_id(doc_id) or not _is_doc_id(chunk_id) or score is None:
            malformed += 1
            continue
        _require_identity(entry, store_id, doc_id, "hit")
        owner = chunk_owner.setdefault(chunk_id, doc_id)
        if owner != doc_id:
            raise ValueError(
                f"chunk_id {chunk_id} maps to two doc_ids "
                f"({owner}, {doc_id}): cross-corpus aliasing"
            )
        prev = best.get(doc_id)
        if prev is None or score > prev[0] or (
            score == prev[0] and chunk_id < prev[1]
        ):
            best[doc_id] = (score, chunk_id)
    examined_chunks = min(len(hits), max_chunk_hits)

    # 2) Deterministic rank: cosine desc, doc_id asc. Floor BEFORE the cap so
    #    excluded rows never consume shortlist slots.
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    examined_docs = len(ranked)
    below_floor = sum(1 for _, (score, _) in ranked if score < cosine_floor)
    shortlisted: List[Tuple[int, float, int, Dict[str, Any]]] = []
    excluded = 0
    for doc_id, (score, chunk_id) in ranked:
        if len(shortlisted) >= max_docs:
            break
        if score < cosine_floor:
            continue
        if source_doc_id is not None and doc_id == source_doc_id:
            excluded += 1
            continue
        try:
            metadata = get_metadata(store_id, doc_id)
        except Exception:
            logger.warning(
                "graph candidates: metadata accessor failed for doc %s",
                doc_id, exc_info=True,
            )
            excluded += 1
            continue
        if isinstance(metadata, dict):
            _require_identity(metadata, store_id, doc_id, "metadata")
            # Snapshot before any later accessor can mutate its dictionary.
            metadata = json.loads(json.dumps(metadata, allow_nan=False))
        if not _metadata_authorized(principal, workspace, metadata):
            excluded += 1
            continue
        assert isinstance(metadata, dict)
        if not _metadata_eligible(metadata):
            excluded += 1
            continue
        _require_digest(metadata.get("content_sha256"))
        shortlisted.append((doc_id, score, chunk_id, metadata))
        if len(shortlisted) >= max_docs:
            break
    # Docs past the cap are not authorized and never counted either way:
    # the cap binds the shortlist, not the exclusion tally. Metadata retained
    # from the filter pass — never re-read, never loaded before authorization.
    pairs: List[CandidateDescriptor] = []
    deferred: List[CandidateDescriptor] = []
    for doc_id, score, chunk_id, metadata in shortlisted:
        title = str(metadata.get("title") or metadata.get("path") or doc_id)
        status = str(metadata.get("page_status") or "accepted")
        page_type = str(metadata.get("page_type") or REQUIRED_PAGE_TYPE)
        if len(pairs) < max_pairs:
            try:
                envelope = get_content(store_id, doc_id)
            except Exception:
                logger.warning(
                    "graph candidates: content accessor failed for doc %s",
                    doc_id, exc_info=True,
                )
                envelope = None
            content = None
            if envelope is not None:
                _require_identity(envelope, store_id, doc_id, "content")
                content = envelope.get("content")
                if not isinstance(content, str):
                    raise ValueError("content envelope must contain full text")
                if _text_hash(content) != metadata["content_sha256"]:
                    raise ValueError("content hash does not match metadata snapshot")
            if not isinstance(content, str) or not content.strip():
                deferred.append(CandidateDescriptor(
                    **_provenance(store_id, doc_id, chunk_id, score, metadata, None),
                    pair_id=_pair_id(doc_id), doc_id=doc_id, chunk_id=chunk_id,
                    cosine=score, title=title, status=status, page_type=page_type,
                    excerpt=None, excerpt_tokens=0, excerpt_measured=True,
                ))
                continue
            excerpt, tokens, measured = _truncate_excerpt(content)
            pairs.append(CandidateDescriptor(
                **_provenance(store_id, doc_id, chunk_id, score, metadata, excerpt),
                pair_id=_pair_id(doc_id), doc_id=doc_id, chunk_id=chunk_id,
                cosine=score, title=title, status=status, page_type=page_type,
                excerpt=excerpt, excerpt_tokens=tokens, excerpt_measured=measured,
            ))
        else:
            deferred.append(CandidateDescriptor(
                **_provenance(store_id, doc_id, chunk_id, score, metadata, None),
                pair_id=_pair_id(doc_id), doc_id=doc_id, chunk_id=chunk_id,
                cosine=score, title=title, status=status, page_type=page_type,
                excerpt=None, excerpt_tokens=0, excerpt_measured=True,
            ))
    return CandidateShortlist(
        pairs=tuple(pairs),
        deferred=tuple(deferred),
        examined_chunks=examined_chunks,
        examined_docs=examined_docs,
        excluded_before_cap=excluded,
        below_floor=below_floor,
        malformed_hits=malformed,
        hits_truncated=truncated,
    )


def _truncate_excerpt(content: str) -> Tuple[str, int, bool]:
    """Cap candidate text to the classifier per-excerpt budget, honestly."""
    from minni.edge_inference import MAX_EXCERPT_TOKENS, truncate_to_tokens

    excerpt, tokens, measured = truncate_to_tokens(
        content.strip(), MAX_EXCERPT_TOKENS
    )
    return excerpt, tokens, measured


def _require_identity(envelope, store_id, doc_id, label):
    if (not isinstance(envelope, dict) or envelope.get("store_id") != store_id
            or not _is_doc_id(envelope.get("doc_id")) or envelope["doc_id"] != doc_id):
        raise ValueError(f"{label} identity does not match requested store/document")


def _require_digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("metadata requires full-content SHA256")


def _text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provenance(store_id, doc_id, chunk_id, score, metadata, excerpt):
    metadata_hash = _text_hash(json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False))
    evidence = {"version": 1, "store_id": store_id, "doc_id": doc_id,
                "chunk_id": chunk_id, "cosine": score,
                "metadata_sha256": metadata_hash,
                "content_sha256": metadata["content_sha256"], "excerpt": excerpt}
    return {"store_id": store_id, "content_sha256": metadata["content_sha256"],
            "metadata_sha256": metadata_hash,
            "evidence_sha256": _text_hash(json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False))}
