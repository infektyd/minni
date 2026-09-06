"""P1.3 prepare/commit coordinator core (addendum §4.3, spec §§2.1–2.2).

Substantive core for ``commit_learning_with_graph()``: model/shortlist
prepare runs OUTSIDE the write lock; one ``BEGIN IMMEDIATE`` transaction
writes learning + canonical document + join + FTS/chunks + typed edges, with
in-transaction revalidation of candidate identity, privacy, and content
hashes. Any failure before commit rolls back to zero durable writes
(fail-loud new promotion). Standing repair preserves durable truth and
defers edges instead of failing.

Scope boundaries (explicit non-goals):
- No entrypoint/activation/config/schema changes: production
  (``resolve_candidate``, ``handle_learn``, AFM) does NOT invoke this yet.
  Exact next integration is listed in ``NEXT_INTEGRATION`` below.
- No FAISS access inside the coordinator and never before commit: prepare
  takes injected ``search_chunks``/``classify``/``embed`` callables, and the
  result returns new chunk ids + vectors for the CALLER's post-commit
  refresh (Phase C).
- No auto-supersession (P2.1), no contradiction surfacing beyond the
  ``contradiction_log`` persist row (P2.2), no traversal (P1.4).
- ``graph_enabled`` defaults True (local runtime default) but enables
  nothing outside this call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from minni.durable_projection import (
    ACTIVE_LEARNING_SQL,
    durable_doc_path,
    durable_metadata,
    projection_row_closed,
)
from minni.graph_candidates import (
    COSINE_FLOOR,
    MAX_CHUNK_HITS,
    MAX_CLASSIFIER_PAIRS,
    MAX_SHORTLIST_DOCS,
    CandidateDescriptor,
    prepare_candidate_shortlist,
)
from minni.graph_commit import ensure_canonical_learning_node
from minni.principal import EffectivePrincipal, can_read_document

logger = logging.getLogger("sovereign.graph_coordinator")

# Spec §2.4 persist thresholds (calibration starting points, not measurements).
PERSIST_CONFIDENCE: Dict[str, float] = {
    "updates": 0.88,
    "contradicts": 0.88,
    "extends": 0.82,
    "relates": 0.78,
}
TYPED_LINK_TYPES = frozenset(PERSIST_CONFIDENCE)
INFERENCE_METHOD = "local_classifier"

NEXT_INTEGRATION = (
    "Unify callsites WITHOUT changing their behavior until the quality gate: "
    "governance.store path (minnid_runtime/governance.py:274) for "
    "resolve_candidate accept, handle_learn force path, and AFM consolidation "
    "each replace their store_learning + index_durable fragments with "
    "commit_learning_with_graph, passing the daemon RetrievalEngine-backed "
    "search/classify closures. Phase C (post-commit FAISS refresh) belongs in "
    "those callsites, never in this module."
)


@dataclass(frozen=True)
class CommittedEdge:
    source_doc_id: int
    target_doc_id: int
    link_type: str
    confidence: float


@dataclass(frozen=True)
class CommitResult:
    status: str  # "ok" | "error"
    learning_id: Optional[int] = None
    doc_id: Optional[int] = None
    edges: Tuple[CommittedEdge, ...] = ()
    no_candidates: bool = False
    edges_deferred: Optional[str] = None  # "disabled" on the baseline path
    new_chunk_ids: Tuple[int, ...] = ()
    new_chunk_vectors: Tuple[bytes, ...] = ()
    contradiction_logged: bool = False
    error_code: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RepairResult:
    status: str  # "complete" | "incomplete_lexical_only"
    # "incomplete_schema_missing" | "failed"
    learning_id: Optional[int] = None
    doc_id: Optional[int] = None
    edges: Tuple[CommittedEdge, ...] = ()
    edges_deferred: Optional[str] = None  # "degraded" | "disabled" | None
    new_chunk_ids: Tuple[int, ...] = ()
    new_chunk_vectors: Tuple[bytes, ...] = ()
    error_code: Optional[str] = None
    error: Optional[str] = None


class _Abort(Exception):
    """Internal rollback signal: zero durable writes, error envelope out."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _graph_ready_status(db: Any) -> str:
    """Read-only readiness probe: 'ready' or 'schema_missing'/drift."""
    from minni.graph_readiness import check_graph_readiness

    try:
        return check_graph_readiness(db._get_conn()).status
    except Exception as exc:
        logger.warning("graph coordinator: readiness probe failed (%s)", exc)
        return "schema_missing"


def _embed_all(
    embed_text: Callable[[str], bytes], texts: Sequence[str]
) -> Optional[List[bytes]]:
    """Embed every text. None on ANY failure — no partial vector sets."""
    try:
        vectors = [embed_text(text) for text in texts]
    except Exception as exc:
        logger.warning("graph coordinator: embedding failed (%s)", exc)
        return None
    for vector in vectors:
        if not isinstance(vector, (bytes, bytearray)) or not vector:
            return None
    return [bytes(v) for v in vectors]


def _bound_store_id(db: Any, store_id: str) -> None:
    """Bind the supplied store_id to the ACTUAL database identity.

    No schema change and no caller-supplied alias map: the canonical
    identity is the realpath of the db file, and ``store_id`` must BE that
    path. An alias dict cannot make a foreign store claim "verified" — there
    is no verified registry yet, so only the canonical path is accepted.
    Anything else aborts: prepare-time evidence from one store must never
    commit into another, even when doc ids collide. Callsites pass
    ``realpath(db.config.db_path)``; a future registered binding replaces
    this function, not its callers' trust assumptions.
    """
    if not isinstance(store_id, str) or not store_id.strip():
        raise _Abort("store_binding_invalid", "store_id must be non-empty")
    try:
        actual = os.path.realpath(
            os.path.abspath(str(db.config.db_path)))
    except Exception as exc:
        raise _Abort("store_binding_mismatch",
                     f"database identity unreadable: {exc}")
    if os.path.realpath(os.path.abspath(store_id)) != actual:
        raise _Abort(
            "store_binding_mismatch",
            f"store_id {store_id!r} does not identify this database",
        )


def _classify_validated(
    source: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    classify: Callable[[Dict[str, Any], List[Dict[str, Any]]], Any],
    descriptors: Dict[str, CandidateDescriptor],
) -> Tuple[
    List[Tuple[CandidateDescriptor, str, str, float]], Dict[str, Any]
]:
    """Run the classifier and independently verify with the SHARED whole
    batch validator — never trust a duck-typed ``ok``.

    The coordinator snapshots inputs (shared snapshotter: JSON-safe, finite),
    renders the prompt itself (shared renderer: actual line counts), and
    requires the batch's provenance hashes to match those exact rendered
    inputs. Complete unique pair accounting: classified ∪ unclassified must
    equal the sent set exactly. The shared validator then enforces the exact
    six-key schema, label/direction compatibility, confidence range
    INCLUDING none, non-empty evidence indices for non-none labels, and
    non-empty rationale, with evidence refs range-checked against the actual
    render. Unclassified (deferred) pairs get no fabricated label; none
    labels persist nothing.
    """
    from minni.edge_classifier import (
        compute_canonical_hash,
        snapshot_and_validate_descriptor,
    )
    from minni.edge_inference import (
        _pair_id_of,
        render_edge_inference_prompt,
        validate_edge_inference_response,
    )

    source_snapshot, source_error = snapshot_and_validate_descriptor(
        source, "source")
    if source_error is not None:
        raise _Abort("edge_inference_failed", f"bad source: {source_error}")
    candidates_snapshot, candidates_error = snapshot_and_validate_descriptor(
        list(candidates), "candidates")
    if candidates_error is not None:
        raise _Abort("edge_inference_failed",
                     f"bad candidates: {candidates_error}")
    try:
        render = render_edge_inference_prompt(
            source=source_snapshot, candidates=candidates_snapshot)
    except Exception as exc:
        raise _Abort("edge_inference_failed", f"render failed: {exc}")
    if render.budget_exceeded:
        raise _Abort("edge_inference_failed",
                     "rendered batch exceeds token budget")
    sent_ids = [_pair_id_of(c, i) for i, c in enumerate(candidates_snapshot)]
    if len(set(sent_ids)) != len(sent_ids):
        raise _Abort("edge_inference_failed", "duplicate pair ids sent")
    rendered_ids = list(render.pair_ids)
    try:
        batch = classify(json.loads(json.dumps(source_snapshot)),
                         json.loads(json.dumps(candidates_snapshot)))
    except Exception as exc:
        raise _Abort("edge_inference_failed", f"classifier raised: {exc}")
    if batch is None:
        raise _Abort("edge_inference_failed", "classifier returned None")

    def _field(name: str) -> Any:
        return getattr(batch, name, None)

    prompt_hash = hashlib.sha256(
        render.prompt_text.encode("utf-8")).hexdigest()
    if _field("prompt_hash") != prompt_hash:
        raise _Abort("edge_inference_failed",
                     "batch provenance not bound to rendered prompt")
    if _field("source_hash") != compute_canonical_hash(source_snapshot):
        raise _Abort("edge_inference_failed",
                     "batch provenance not bound to source")
    if _field("candidates_hash") != compute_canonical_hash(candidates_snapshot):
        raise _Abort("edge_inference_failed",
                     "batch provenance not bound to candidates")
    rendered_subset = [
        c for i, c in enumerate(candidates_snapshot)
        if _pair_id_of(c, i) in set(rendered_ids)
    ]
    if (_field("batch_candidates_hash")
            != compute_canonical_hash(rendered_subset)):
        raise _Abort("edge_inference_failed",
                     "batch provenance not bound to rendered subset")
    classified = list(_field("classified_pair_ids") or [])
    unclassified = list(_field("unclassified_pair_ids") or [])
    if (set(classified) | set(unclassified) != set(sent_ids)
            or set(classified) & set(unclassified)
            or len(classified) != len(set(classified))
            or len(unclassified) != len(set(unclassified))):
        raise _Abort("edge_inference_failed",
                     "incomplete pair accounting in batch")
    if set(classified) != set(rendered_ids):
        raise _Abort("edge_inference_failed",
                     "classified pairs do not match rendered batch")

    raw_edges = _field("edges")
    items = []
    if raw_edges:
        if not isinstance(raw_edges, (list, tuple)):
            raise _Abort("edge_inference_failed",
                         "classifier edges not a sequence")
        for edge in raw_edges:
            to_dict = getattr(edge, "to_dict", None)
            if callable(to_dict):
                try:
                    items.append(to_dict())
                    continue
                except Exception as exc:
                    raise _Abort("edge_inference_failed",
                                 f"edge snapshot failed: {exc}")
            items.append({
                "pair_id": getattr(edge, "pair_id", None),
                "label": getattr(edge, "label", None),
                "direction": getattr(edge, "direction", None),
                "confidence": getattr(edge, "confidence", None),
                "supporting_evidence_indices": getattr(
                    edge, "supporting_evidence_indices", None),
                "rationale": getattr(edge, "rationale", None),
            })
    valid, validated_edges, validation_error = validate_edge_inference_response(
        raw_response=items,
        expected_pair_ids=rendered_ids,
        line_counts_per_pair=dict(render.line_counts_per_pair),
    )
    if not valid or validated_edges is None:
        raise _Abort("edge_inference_failed",
                     f"batch validation failed: {validation_error}")
    output_hash = compute_canonical_hash([edge.to_dict() for edge in validated_edges])
    if _field("output_hash") != output_hash:
        raise _Abort("edge_inference_failed", "batch output hash does not match validated edges")
    model_id, prompt_version = _field("model_id"), _field("prompt_version")
    if not isinstance(model_id, str) or not model_id or not isinstance(prompt_version, str) or not prompt_version:
        raise _Abort("edge_inference_failed", "batch model/prompt provenance missing")
    evidence_hash = hashlib.sha256(
        f"{compute_canonical_hash(source_snapshot)}:{compute_canonical_hash(rendered_subset)}:"
        f"{prompt_hash}:{output_hash}:{prompt_version}:{model_id}".encode("utf-8")
    ).hexdigest()
    if _field("evidence_hash") != evidence_hash:
        raise _Abort("edge_inference_failed", "batch evidence hash does not match validated provenance")
    validated: List[Tuple[CandidateDescriptor, str, str, float]] = []
    for edge in validated_edges:
        if edge.label == "none":
            continue  # none labels persist nothing, never fabricated
        if float(edge.confidence) < PERSIST_CONFIDENCE[edge.label]:
            continue
        validated.append(
            (descriptors[edge.pair_id], edge.label, edge.direction,
             float(edge.confidence)))
    info = {
        "model_id": str(_field("model_id") or "unknown"),
        "prompt_version": str(
            _field("prompt_version") or "edge_inference_v1"),
        "evidence_hash": str(_field("evidence_hash") or ""),
        "output_hash": str(_field("output_hash") or ""),
    }
    return validated, info


def _bind_edges(
    validated: Sequence[Tuple[CandidateDescriptor, str, str, float]],
    source_doc_id: int,
) -> List[Tuple[int, int, str, float]]:
    """Bind validated pairs to directed doc edges. Self-edges (re-promotion
    sharing the canonical node) are dropped, never persisted."""
    bound = []
    for descriptor, label, direction, confidence in validated:
        target = descriptor.doc_id
        if target == source_doc_id:
            continue
        if direction == "forward":
            bound.append((source_doc_id, target, label, confidence))
        elif direction == "backward":
            bound.append((target, source_doc_id, label, confidence))
        else:  # mutual: both directions, one row each
            bound.append((source_doc_id, target, label, confidence))
            bound.append((target, source_doc_id, label, confidence))
    return bound


def _write_edges(
    c: Any,
    bound: Sequence[Tuple[int, int, str, float]],
    *,
    run_id: str,
    model_id: str,
    prompt_version: str,
    evidence: Dict[str, Any],
    now: float,
    new_learning_id: int,
) -> Tuple[Tuple[CommittedEdge, ...], bool]:
    """Upsert typed edges (confidence/provenance only on conflict) plus the
    contradicts persist row. Never touches wikilink/derived_from."""
    committed: List[CommittedEdge] = []
    contradiction_logged = False
    logged_pairs = set()
    for source_doc, target_doc, link_type, confidence in bound:
        evidence_json = json.dumps(
            {"pair": [source_doc, target_doc], **evidence},
            sort_keys=True,
            separators=(",", ":"),
        )
        c.execute(
            """INSERT INTO memory_links
               (source_doc_id, target_doc_id, link_type, weight, created_at,
                confidence, inference_method, model_id, prompt_version,
                inference_run_id, evidence_json, inferred_at, edge_status)
               VALUES (?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
               ON CONFLICT(source_doc_id, target_doc_id, link_type)
               DO UPDATE SET confidence=excluded.confidence,
                   inference_method=excluded.inference_method,
                   model_id=excluded.model_id,
                   prompt_version=excluded.prompt_version,
                   inference_run_id=excluded.inference_run_id,
                   evidence_json=excluded.evidence_json,
                   inferred_at=excluded.inferred_at,
                   edge_status='active'""",
            (
                source_doc, target_doc, link_type, now, confidence,
                INFERENCE_METHOD, model_id, prompt_version, run_id,
                evidence_json, now,
            ),
        )
        committed.append(CommittedEdge(source_doc, target_doc, link_type, confidence))
        # One log row per contradicts PAIR (mutual binds two directed rows).
        if link_type == "contradicts" and frozenset(
            (source_doc, target_doc)
        ) not in logged_pairs:
            logged_pairs.add(frozenset((source_doc, target_doc)))
            c.execute(
                """INSERT INTO contradiction_log
                   (memory_a_id, memory_b_id, detected_at, detection_method,
                    source_doc_id, target_doc_id, edge_run_id, confidence,
                    resolution_status)
                   VALUES (?, NULL, ?, 'graph_classifier', ?, ?, ?, ?,
                           'unresolved')""",
                (new_learning_id, now, source_doc, target_doc, run_id, confidence),
            )
            contradiction_logged = True
    return tuple(committed), contradiction_logged


def _require_snapshot(
    snapshots: Dict[int, Dict[str, Any]], doc_id: int
) -> Dict[str, Any]:
    snapshot = snapshots.get(doc_id)
    if snapshot is None:
        raise _Abort("stale_candidate",
                     f"target doc {doc_id} has no metadata snapshot")
    return snapshot


def _track_candidate_content(get_content, attempted):
    def read(store_id, doc_id):
        attempted.add(doc_id)
        return get_content(store_id, doc_id)
    return read


def _require_prepared_content(shortlist, attempted):
    # Shortlist deferred descriptors intentionally share one shape. A deferred
    # doc whose content was attempted failed preparation (missing/blank/error);
    # a deferred doc never read is the normal 12-to-8 pair-cap exclusion.
    if any(descriptor.doc_id in attempted for descriptor in shortlist.deferred):
        raise _Abort("candidate_preparation_failed", "shortlisted candidate content unavailable")


def _capture_target_metadata(get_metadata, snapshots):
    """Capture the exact original shortlist read, never fetch a second version.

    The retained snapshot and returned value are independent deep copies, so
    either a caller or a classifier mutating its inputs cannot rewrite evidence.
    """
    from minni.edge_classifier import snapshot_and_validate_descriptor

    def capture(store_id, doc_id):
        metadata = get_metadata(store_id, doc_id)
        if metadata is None:
            return None
        snapshot, error = snapshot_and_validate_descriptor(metadata, "target_metadata")
        if error is not None or not isinstance(snapshot, dict):
            raise ValueError("invalid target metadata snapshot")
        if snapshot.get("store_id") != store_id or snapshot.get("doc_id") != doc_id:
            raise ValueError("target metadata identity mismatch")
        snapshots[doc_id] = snapshot
        return json.loads(json.dumps(snapshot))
    return capture


def _revalidate_target(
    c: Any,
    descriptor: CandidateDescriptor,
    *,
    principal: EffectivePrincipal,
    workspace: str,
    store_id: str,
    metadata_snapshot: Dict[str, Any],
) -> None:
    """In-transaction revalidation: store binding, identity, metadata, and
    content hash — not only privacy.

    Raises _Abort (full rollback) when the descriptor's store does not match
    this commit, the target moved, closed, became unreadable, lost its FTS
    content, its content hash diverged from the prepare-time evidence, or
    its stored status/type drifted from the descriptor. Missing join-table
    mappers or zero active learnings on a joined node are equally stale:
    edges bind live memory.
    """
    if descriptor.store_id != store_id:
        raise _Abort("stale_candidate",
                     f"target doc {descriptor.doc_id} bound to another store")
    row = c.execute(
        "SELECT doc_id, path, agent, privacy_level, page_status, page_type,"
        " memory_kind FROM documents WHERE doc_id = ?",
        (descriptor.doc_id,),
    ).fetchone()
    if row is None:
        raise _Abort("stale_candidate", f"target doc {descriptor.doc_id} gone")
    metadata = {
        "agent": row["agent"],
        "privacy_level": row["privacy_level"],
        "page_status": row["page_status"],
        "page_type": row["page_type"],
        "memory_kind": row["memory_kind"],
        "path": row["path"],
    }
    try:
        authorized = bool(can_read_document(principal, workspace, metadata))
    except Exception:
        authorized = False
    if not authorized:
        raise _Abort(
            "stale_candidate", f"target doc {descriptor.doc_id} unreadable"
        )
    if (
        metadata["memory_kind"] != "learning"
        or str(metadata["page_type"] or "").lower() != "learning"
        or metadata["privacy_level"] == "blocked"
        or str(metadata["page_status"] or "")
        in ("draft", "expired", "rejected", "superseded")
    ):
        raise _Abort(
            "stale_candidate", f"target doc {descriptor.doc_id} closed"
        )
    from minni.edge_classifier import compute_canonical_hash
    if compute_canonical_hash(metadata_snapshot) != descriptor.metadata_sha256:
        raise _Abort("stale_candidate", "target metadata does not match shortlisted digest")
    # The full prepare-time snapshot must still match the authoritative row:
    # owner, path, privacy, status, type, and kind — not only the descriptor
    # status/type and not only readability. Any drift aborts the whole batch.
    # Title has no documents column: it is display-only untrusted evidence
    # (renderer-escaped), and its integrity follows from path (titles derive
    # from paths) plus the content hash (excerpts are content prefixes). A
    # title column or accessor title provenance would be needed for a strict
    # title comparison — stated here, not silently assumed.
    for field in ("agent", "privacy_level", "page_status", "page_type",
                  "memory_kind", "path"):
        fresh = metadata[field]
        expected = metadata_snapshot.get(field)
        if str(fresh or "") != str(expected or ""):
            raise _Abort(
                "stale_candidate",
                f"target doc {descriptor.doc_id} field {field} changed"
                " since prepare",
            )
    if (
        str(metadata["page_status"] or "accepted") != descriptor.status
        or str(metadata["page_type"] or "learning") != descriptor.page_type
    ):
        raise _Abort(
            "stale_candidate",
            f"target doc {descriptor.doc_id} metadata changed since prepare",
        )
    fts = c.execute(
        "SELECT content FROM vault_fts WHERE doc_id = ?", (descriptor.doc_id,)
    ).fetchone()
    if fts is None or fts["content"] is None:
        raise _Abort(
            "stale_candidate", f"target doc {descriptor.doc_id} has no FTS content"
        )
    if _sha256_text(fts["content"]) != descriptor.content_sha256:
        raise _Abort(
            "stale_candidate",
            f"target doc {descriptor.doc_id} content changed since prepare",
        )
    tables = {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "learning_documents" in tables:
        live = c.execute(
            f"""SELECT 1 FROM learning_documents jd
               JOIN learnings l ON l.learning_id = jd.learning_id
               WHERE jd.doc_id = ? AND {ACTIVE_LEARNING_SQL} LIMIT 1""",
            (descriptor.doc_id,),
        ).fetchone()
        if live is None:
            raise _Abort(
                "stale_candidate",
                f"target doc {descriptor.doc_id} backs no active learning",
            )


def _insert_learning(
    c: Any,
    *,
    agent_id: str,
    category: str,
    content: str,
    embedding: Optional[bytes],
    now: float,
) -> int:
    """Production learning-row shape (mirrors writeback.store_learning)."""
    from minni.writeback import CATEGORIES

    if category not in CATEGORIES:
        category = "general"
    c.execute(
        """INSERT INTO learnings
           (agent_id, category, content, confidence, embedding, created_at)
           VALUES (?, ?, ?, 1.0, ?, ?)""",
        (agent_id, category, content, embedding, now),
    )
    return int(c.lastrowid)


def _fill_projection(
    c: Any,
    *,
    doc_id: int,
    path: str,
    agent_id: str,
    sigil: str,
    content: str,
    chunks: Sequence[Tuple[str, bytes]],
    embedding_model: str,
    layer: str,
    now: float,
) -> Tuple[bool, Tuple[int, ...]]:
    """Fill a node's FTS row and chunk rows INDEPENDENTLY.

    FTS existence is NOT projection completeness: a lexical-only node (FTS
    present, vectors missing) still gets its chunk rows filled when vectors
    are available. Returns (fts_present_after, new_chunk_ids). Already
    complete nodes are untouched (re-promotion shares the node).
    """
    fts_present = c.execute(
        "SELECT 1 FROM vault_fts WHERE doc_id = ?", (doc_id,)
    ).fetchone() is not None
    if not fts_present:
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc_id, path, content, agent_id, sigil),
        )
        fts_present = True
    chunk_ids: List[int] = []
    if chunks:
        have_chunks = c.execute(
            "SELECT 1 FROM chunk_embeddings WHERE doc_id = ? LIMIT 1",
            (doc_id,),
        ).fetchone() is not None
        if not have_chunks:
            for index, (text, vector) in enumerate(chunks):
                c.execute(
                    """INSERT INTO chunk_embeddings
                       (doc_id, chunk_index, chunk_text, embedding,
                        heading_context, model_name, computed_at, layer)
                       VALUES (?, ?, ?, ?, '', ?, ?, ?)""",
                    (doc_id, index, text, vector, embedding_model, now,
                     layer),
                )
                chunk_ids.append(int(c.lastrowid))
    return fts_present, tuple(chunk_ids)


def _run_id(model_id: str, prompt_version: str, evidence_hash: str) -> str:
    return hashlib.sha256(
        f"{model_id}\x00{prompt_version}\x00{evidence_hash}".encode("utf-8")
    ).hexdigest()[:16]


def commit_learning_with_graph(
    *,
    db: Any,
    store_id: str,
    principal: EffectivePrincipal,
    content: str,
    category: str = "general",
    vault_path: str,
    embedding_model: str,
    embed_text: Callable[[str], bytes],
    chunk_texts: Callable[[str], Sequence[str]],
    search_chunks: Callable[[bytes, int], Sequence[Dict[str, Any]]],
    get_metadata: Callable[[str, int], Optional[Dict[str, Any]]],
    get_content: Callable[[str, int], Optional[Dict[str, Any]]],
    classify: Callable[[Dict[str, Any], List[Dict[str, Any]]], Any],
    graph_enabled: bool = True,
    workspace: str = "default",
) -> CommitResult:
    """Prepare outside the write lock, then atomically commit one learning
    with its canonical node, projection, and typed edges.

    Phase A (no lock): embed + chunk the new content, FAISS snapshot,
    shortlist, batched classification. Phase B (``BEGIN IMMEDIATE``): insert
    the learning, revalidate every edge target, stamp the canonical node +
    join, fill FTS/chunks, upsert edges. Any abort before commit leaves zero
    durable writes. New chunk ids + vectors return for the caller's
    post-commit FAISS refresh — this module never touches a live index.
    """
    if not isinstance(principal, EffectivePrincipal):
        return CommitResult(status="error", error_code="bad_principal",
                            error="principal must be EffectivePrincipal")
    try:
        _bound_store_id(db, store_id)
    except _Abort as abort:
        return CommitResult(status="error", error_code=abort.code,
                            error=str(abort))
    agent_id = principal.agent_id
    if not isinstance(content, str) or not content.strip():
        return CommitResult(status="error", error_code="empty_content",
                            error="content must be non-empty text")
    now = time.time()
    meta = durable_metadata(content)
    path = durable_doc_path(agent_id, "", vault_path, content)

    def _fail(code: str, message: str) -> CommitResult:
        logger.warning("graph coordinator: commit aborted (%s): %s", code, message)
        return CommitResult(status="error", error_code=code, error=message)

    # Phase A1: vectors outside the lock. Graph-on promotion is fail-loud on
    # any model failure; the disabled baseline tolerates missing vectors.
    try:
        raw_chunks = list(chunk_texts(content)) or [content.strip()]
    except Exception as exc:
        return _fail("chunk_failed", f"chunker failed: {exc}")
    vectors = _embed_all(embed_text, [content, *raw_chunks])
    if vectors is None:
        if graph_enabled:
            return _fail("embed_failed", "embedding unavailable")
        learning_vector, chunk_vectors = None, []
    else:
        learning_vector, chunk_vectors = vectors[0], vectors[1:]
    chunks = list(zip(raw_chunks, chunk_vectors)) if chunk_vectors else []

    shortlist = None
    validated: List[Tuple[CandidateDescriptor, str, str, float]] = []
    classifier_info: Dict[str, Any] = {}
    meta_snapshots: Dict[int, Dict[str, Any]] = {}
    content_attempts: set[int] = set()
    if graph_enabled:
        if _graph_ready_status(db) != "ready":
            return _fail("edge_inference_schema_missing",
                         "graph schema not ready; promotion stays uncommitted")
        try:
            hits = list(search_chunks(learning_vector or b"", MAX_CHUNK_HITS))
        except Exception as exc:
            return _fail("search_failed", f"chunk search failed: {exc}")
        try:
            shortlist = prepare_candidate_shortlist(
                hits=hits, store_id=store_id, principal=principal,
                get_metadata=_capture_target_metadata(get_metadata, meta_snapshots),
                get_content=_track_candidate_content(get_content, content_attempts),
                workspace=workspace,
            )
            _require_prepared_content(shortlist, content_attempts)
        except _Abort as abort:
            return _fail(abort.code, str(abort))
        except ValueError as exc:
            return _fail("shortlist_rejected", str(exc))
        # Self-exclusion binds post-commit in _bind_edges: re-promotion
        # shares the canonical node, and a self-edge must never persist.
        pairs = list(shortlist.pairs)
        if pairs:
            source = {
                "store_id": store_id,
                "agent_id": agent_id,
                "title": f"learning:{agent_id}",
                "content_sha256": _sha256_text(content),
                "excerpt": content.strip()[:2000],
            }
            descriptors = {p.pair_id: p for p in pairs}
            candidates = [
                {"pair_id": p.pair_id, "doc_id": p.doc_id,
                 "store_id": p.store_id, "title": p.title,
                 "status": p.status, "page_type": p.page_type,
                 "excerpt": p.excerpt,
                 "content_sha256": p.content_sha256}
                for p in pairs
            ]
            try:
                validated, classifier_info = _classify_validated(
                    source, candidates, classify, descriptors)
            except _Abort as abort:
                return _fail(abort.code, str(abort))

    # Phase B: one BEGIN IMMEDIATE transaction. _Abort or any DB error rolls
    # back everything; the caller sees an error envelope, never partial rows.
    try:
        with db.transaction() as c:
            # A pre-existing closed/restricted node at this path must not be
            # resurrected by a new promotion sharing its content.
            prior = c.execute(
                "SELECT page_status, privacy_level FROM documents"
                " WHERE path = ?",
                (path,),
            ).fetchone()
            if prior is not None and projection_row_closed(
                prior["page_status"], prior["privacy_level"]
            ):
                raise _Abort("canonical_restricted",
                             "canonical node is lifecycle-closed/restricted")
            learning_id = _insert_learning(
                c, agent_id=agent_id, category=category, content=content,
                embedding=learning_vector, now=now,
            )
            doc_id = ensure_canonical_learning_node(
                c, learning_id=learning_id, agent_id=agent_id,
                content=content, vault_path=vault_path, created_at=now,
            )
            if doc_id is None:
                raise _Abort("canonical_failed",
                             "canonical node commit refused")
            bound: List[Tuple[int, int, str, float]] = []
            if graph_enabled and validated:
                targets = {}
                for descriptor, _label, _direction, _conf in validated:
                    targets[descriptor.doc_id] = descriptor
                for descriptor in targets.values():
                    _revalidate_target(
                        c, descriptor, principal=principal,
                        workspace=workspace, store_id=store_id,
                        metadata_snapshot=_require_snapshot(
                            meta_snapshots, descriptor.doc_id))
                bound = _bind_edges(validated, int(doc_id))
            new_chunk_ids: Tuple[int, ...] = ()
            new_vectors: Tuple[bytes, ...] = ()
            if chunks or graph_enabled:
                _, new_chunk_ids = _fill_projection(
                    c, doc_id=doc_id, path=path, agent_id=agent_id,
                    sigil=meta["sigil"], content=content, chunks=chunks,
                    embedding_model=embedding_model, layer=meta["layer"],
                    now=now,
                )
                if new_chunk_ids:
                    new_vectors = tuple(v for _, v in chunks)
            elif not graph_enabled:
                # Disabled baseline with no vectors still lands lexical FTS.
                present = c.execute(
                    "SELECT 1 FROM vault_fts WHERE doc_id = ?", (doc_id,)
                ).fetchone()
                if present is None:
                    c.execute(
                        "INSERT INTO vault_fts (doc_id, path, content, agent, sigil)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (doc_id, path, content, agent_id, meta["sigil"]),
                    )
            committed: Tuple[CommittedEdge, ...] = ()
            contradiction_logged = False
            if bound:
                run = _run_id(classifier_info["model_id"],
                              classifier_info["prompt_version"],
                              classifier_info["evidence_hash"])
                committed, contradiction_logged = _write_edges(
                    c, bound, run_id=run, model_id=classifier_info["model_id"],
                    prompt_version=classifier_info["prompt_version"],
                    evidence={"evidence_hash": classifier_info["evidence_hash"]},
                    now=now, new_learning_id=learning_id,
                )
            return CommitResult(
                status="ok", learning_id=learning_id, doc_id=doc_id,
                edges=committed,
                no_candidates=bool(graph_enabled and shortlist is not None
                                   and not shortlist.pairs),
                edges_deferred=None if graph_enabled else "disabled",
                new_chunk_ids=new_chunk_ids, new_chunk_vectors=new_vectors,
                contradiction_logged=contradiction_logged,
            )
    except _Abort as abort:
        return _fail(abort.code, str(abort))
    except Exception as exc:
        logger.exception("graph coordinator: commit transaction failed")
        return _fail("commit_failed", f"transaction rolled back: {exc}")


def repair_learning_projection(
    *,
    db: Any,
    store_id: str,
    learning_id: int,
    vault_path: str,
    embedding_model: str,
    embed_text: Callable[[str], bytes],
    chunk_texts: Callable[[str], Sequence[str]],
    search_chunks: Any = None,
    get_metadata: Any = None,
    get_content: Any = None,
    classify: Any = None,
    principal: Any = None,
    graph_enabled: bool = True,
    workspace: str = "default",
) -> RepairResult:
    """Standing repair of one committed learning: reconstruct its projection
    without ever mutating the durable learnings row.

    Returns complete / incomplete_lexical_only (embedder down: FTS only) /
    incomplete_schema_missing / failed. Classifier outage defers edges
    (``edges_deferred='degraded'``) without touching the projection outcome.
    """

    def _fail(code: str, message: str) -> RepairResult:
        logger.warning("graph coordinator: repair %s (%s)", code, message)
        return RepairResult(status="failed", learning_id=learning_id,
                            error_code=code, error=message)

    try:
        _bound_store_id(db, store_id)
    except _Abort as abort:
        return _fail(abort.code, str(abort))

    with db.cursor() as c:
        row = c.execute(
            "SELECT agent_id, content, status, superseded_by FROM learnings"
            " WHERE learning_id = ?",
            (learning_id,),
        ).fetchone()
    if row is None:
        return _fail("learning_missing", f"learning {learning_id} not found")
    agent_id, content = row["agent_id"], row["content"]
    if (
        row["superseded_by"] is not None
        or str(row["status"] or "") in ("rejected", "expired", "superseded")
    ):
        return _fail("learning_inactive", f"learning {learning_id} not active")
    if not content or not content.strip():
        return _fail("learning_empty", f"learning {learning_id} has no content")

    meta = durable_metadata(content)
    path = durable_doc_path(agent_id, "", vault_path, content)
    now = time.time()
    try:
        raw_chunks = list(chunk_texts(content)) or [content.strip()]
    except Exception as exc:
        return _fail("chunk_failed", f"chunker failed: {exc}")
    vectors = _embed_all(embed_text, raw_chunks)
    lexical_only = vectors is None
    chunks = list(zip(raw_chunks, vectors)) if vectors is not None else []

    want_edges = (
        graph_enabled and classify is not None and search_chunks is not None
        and get_metadata is not None and get_content is not None
        and isinstance(principal, EffectivePrincipal)
    )
    validated: List[Tuple[CandidateDescriptor, str, str, float]] = []
    classifier_info: Dict[str, Any] = {}
    meta_snapshots: Dict[int, Dict[str, Any]] = {}
    content_attempts: set[int] = set()
    edges_deferred: Optional[str] = None
    if want_edges and not lexical_only:
        if _graph_ready_status(db) != "ready":
            edges_deferred = "degraded"
        else:
            try:
                learning_vector = embed_text(content)
                hits = list(search_chunks(learning_vector, MAX_CHUNK_HITS))
                shortlist = prepare_candidate_shortlist(
                    hits=hits, store_id=store_id, principal=principal,
                    get_metadata=_capture_target_metadata(get_metadata, meta_snapshots),
                    get_content=_track_candidate_content(get_content, content_attempts),
                    workspace=workspace,
                )
                _require_prepared_content(shortlist, content_attempts)
                if shortlist.pairs:
                    source = {
                        "store_id": store_id, "agent_id": agent_id,
                        "title": f"learning:{agent_id}",
                        "content_sha256": _sha256_text(content),
                        "excerpt": content.strip()[:2000],
                    }
                    descriptors = {p.pair_id: p for p in shortlist.pairs}
                    try:
                        validated, classifier_info = _classify_validated(
                            source, [
                                {"pair_id": p.pair_id, "doc_id": p.doc_id,
                                 "store_id": p.store_id, "title": p.title,
                                 "status": p.status, "page_type": p.page_type,
                                 "excerpt": p.excerpt,
                                 "content_sha256": p.content_sha256}
                                for p in shortlist.pairs
                            ], classify, descriptors)
                    except _Abort:
                        validated = []
                        edges_deferred = "degraded"
            except (_Abort, ValueError):
                edges_deferred = "degraded"
            except Exception:
                logger.warning("graph coordinator: repair edge prep failed",
                               exc_info=True)
                edges_deferred = "degraded"
    elif not graph_enabled:
        edges_deferred = "disabled"

    try:
        with db.transaction() as c:
            # Revalidate the SOURCE inside the write transaction: the
            # learning may have changed status, content, or ownership
            # between the pre-embed read and this commit (e.g. an embed
            # callback superseding it). A drifted source aborts with zero
            # writes — repair never recreates projection for dead content.
            source = c.execute(
                "SELECT agent_id, content, status, superseded_by FROM learnings"
                " WHERE learning_id = ?",
                (learning_id,),
            ).fetchone()
            if source is None:
                raise _Abort("stale_source",
                             f"learning {learning_id} gone")
            if (
                source["agent_id"] != agent_id
                or source["content"] != content
                or source["superseded_by"] is not None
                or str(source["status"] or "")
                in ("rejected", "expired", "superseded")
            ):
                raise _Abort("stale_source",
                             f"learning {learning_id} changed since prepare")
            # Preserve stored source restrictions: an existing canonical doc
            # that is lifecycle-closed or restricted (blocked/rejected since
            # commit) must NOT gain fresh FTS/chunks — repair leaves it
            # alone instead of resurrecting it.
            prior = c.execute(
                "SELECT page_status, privacy_level FROM documents"
                " WHERE path = ?",
                (path,),
            ).fetchone()
            if prior is not None and projection_row_closed(
                prior["page_status"], prior["privacy_level"]
            ):
                raise _Abort("projection_restricted",
                             "canonical node is lifecycle-closed/restricted")
            doc_id = ensure_canonical_learning_node(
                c, learning_id=learning_id, agent_id=agent_id,
                content=content, vault_path=vault_path, created_at=now,
            )
            schema_missing = doc_id is None
            if schema_missing:
                existing = c.execute(
                    "SELECT doc_id FROM documents WHERE path = ?", (path,)
                ).fetchone()
                if existing is None:
                    c.execute(
                        """INSERT INTO documents
                           (path, agent, sigil, last_modified, indexed_at,
                            page_status, privacy_level, page_type, layer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (path, agent_id, meta["sigil"], now, now,
                         meta["page_status"], meta["privacy_level"],
                         meta["page_type"], meta["layer"]),
                    )
                    doc_id = int(c.lastrowid)
                else:
                    doc_id = int(existing["doc_id"])
            bound: List[Tuple[int, int, str, float]] = []
            if validated and not schema_missing:
                targets = {}
                for descriptor, _l, _d, _cf in validated:
                    targets[descriptor.doc_id] = descriptor
                for descriptor in targets.values():
                    _revalidate_target(
                        c, descriptor, principal=principal,
                        workspace=workspace, store_id=store_id,
                        metadata_snapshot=_require_snapshot(
                            meta_snapshots, descriptor.doc_id))
                bound = _bind_edges(validated, int(doc_id))
            # FTS and vectors fill independently: a lexical-only node
            # (FTS present, chunks missing) gains vectors here when the
            # embedder is healthy; a vectorless repair still lands FTS.
            fts_ok, new_chunk_ids = _fill_projection(
                c, doc_id=int(doc_id), path=path, agent_id=agent_id,
                sigil=meta["sigil"], content=content, chunks=chunks,
                embedding_model=embedding_model, layer=meta["layer"],
                now=now,
            )
            committed: Tuple[CommittedEdge, ...] = ()
            if bound:
                run = _run_id(classifier_info["model_id"],
                              classifier_info["prompt_version"],
                              classifier_info["evidence_hash"])
                committed, _ = _write_edges(
                    c, bound, run_id=run, model_id=classifier_info["model_id"],
                    prompt_version=classifier_info["prompt_version"],
                    evidence={"evidence_hash": classifier_info["evidence_hash"]},
                    now=now, new_learning_id=learning_id,
                )
            # Truthful status from actual post-fill state, not from which
            # code path ran: complete requires FTS AND vectors present.
            chunks_ok = (
                c.execute(
                    "SELECT 1 FROM chunk_embeddings WHERE doc_id = ? LIMIT 1",
                    (doc_id,),
                ).fetchone()
                is not None
            )
            if schema_missing:
                status = "incomplete_schema_missing"
            elif fts_ok and chunks_ok:
                status = "complete"
            elif fts_ok:
                status = "incomplete_lexical_only"
            else:
                raise _Abort("failed", "projection fill wrote nothing")
            return RepairResult(
                status=status, learning_id=learning_id, doc_id=int(doc_id),
                edges=committed, edges_deferred=edges_deferred,
                new_chunk_ids=new_chunk_ids,
                new_chunk_vectors=tuple(
                    v for _, v in chunks) if new_chunk_ids else (),
            )
    except _Abort as abort:
        return _fail(f"repair_{abort.code}", str(abort))
    except Exception as exc:
        logger.exception("graph coordinator: repair transaction failed")
        return _fail("repair_failed", f"transaction rolled back: {exc}")
