"""P1.3 prepare/commit coordinator core (addendum §4.3, spec §§2.1–2.2).

Split API so expensive Phase A never holds the write lock:

- ``prepare_learning_with_graph``: embed/shortlist/classify, return a frozen
  payload. Model callbacks refuse to run while ``connection.in_transaction``.
- ``commit_prepared_learning(cursor, payload)``: Phase B on the CALLER's
  open SQLite transaction. Does not commit, rollback, or refresh FAISS.
  Returned chunk vectors are uncommitted until the outer txn commits
  (status ``staged``, never ``ok``).
- ``commit_learning_with_graph``: compatible wrapper (prepare + local
  ``BEGIN IMMEDIATE``). Returns ``ok`` only after that wrapper txn commits.

Fail-loud new promotion: prepare errors yield no payload; Phase B refusal
raises ``GraphCommitAborted`` so the caller rolls back to zero durable
writes. Standing repair preserves durable truth and defers edges instead
of failing.

Scope boundaries (explicit non-goals):
- No entrypoint/activation/config/schema changes: production
  (``resolve_candidate``, ``handle_learn``, AFM) does NOT invoke this yet.
  Exact next integration is listed in ``NEXT_INTEGRATION`` below.
- No FAISS access inside this module: Phase C belongs in the callsite
  AFTER the outer txn commits, using returned chunk ids + vectors.
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
from dataclasses import dataclass, replace as _dc_replace
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
    "Callsites own the outer write transaction; this module does not wire "
    "them. governance.store (resolve_candidate accept), handle_learn force, "
    "and AFM consolidation each: (1) prepare_learning_with_graph OUTSIDE the "
    "lock with RetrievalEngine-backed search/classify/embed closures; "
    "(2) BEGIN IMMEDIATE; terminalize the candidate_packets row and any "
    "review-fence updates on that same cursor; commit_prepared_learning("
    "cursor, payload, db=, principal=); (3) COMMIT or ROLLBACK "
    "themselves — Phase B refusal is GraphCommitAborted; (4) Phase C FAISS "
    "refresh ONLY after COMMIT, using returned chunk ids/vectors. Never pass "
    "an index callback into this module. Muse owns governance/AFM "
    "canonical-missing separately."
)


@dataclass(frozen=True)
class CommittedEdge:
    source_doc_id: int
    target_doc_id: int
    link_type: str
    confidence: float


@dataclass(frozen=True)
class CommitResult:
    status: str  # "ok" | "staged" | "error"
    # "staged" = Phase B wrote on the caller's cursor; durable only after
    # the outer transaction commits. Wrapper maps staged -> ok after THAT.
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


@dataclass(frozen=True)
class LearningFields:
    """Optional learnings-row columns. None means omit (wrapper default).

    Any field that is set is written at commit or the commit aborts — never
    silently dropped. ``supersedes`` updates another row in the same cursor.
    """

    source_query: Optional[str] = None
    source_doc_ids: Any = None
    evidence_doc_ids: Any = None
    confidence: Optional[float] = None
    assertion: Optional[str] = None
    applies_when: Optional[str] = None
    contradicts_id: Optional[int] = None
    supersedes: Optional[int] = None
    content_hash: Optional[str] = None
    status: Optional[str] = None


@dataclass(frozen=True)
class PreparedLearningCommit:
    """Detached Phase A snapshot; the digest rejects changed snapshot fields."""

    digest: str
    store_id: str
    principal: EffectivePrincipal
    workspace: str
    content: str
    content_sha256: str
    category: str
    vault_path: str
    embedding_model: str
    graph_enabled: bool
    learning_vector: Optional[bytes]
    chunks: Tuple[Tuple[str, bytes], ...]
    path: str
    sigil: str
    layer: str
    shortlist_no_pairs: bool
    validated: Tuple[Tuple[CandidateDescriptor, str, str, float], ...]
    classifier_info: Tuple[Tuple[str, str], ...]
    meta_snapshots: Tuple[Tuple[int, str], ...]
    learning_fields: LearningFields
    prepared_at: float


@dataclass(frozen=True)
class PrepareResult:
    status: str  # "ok" | "error"
    payload: Optional[PreparedLearningCommit] = None
    error_code: Optional[str] = None
    error: Optional[str] = None


class GraphCommitAborted(Exception):
    """Phase B refused: caller must roll back the open write transaction."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _Abort(GraphCommitAborted):
    """Internal alias so existing raise sites stay short."""


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


def _canonical_store_id(conn: Any) -> str:
    """On-disk identity of the opened main database (PRAGMA, not config)."""
    rows = conn.execute("PRAGMA database_list").fetchall()
    file = None
    for row in rows:
        try:
            name, path = row["name"], row["file"]
        except (TypeError, KeyError, IndexError):
            name, path = row[1], row[2]
        if str(name) == "main":
            file = path
            break
    if not file:
        raise _Abort("store_binding_mismatch",
                     "expected a real on-disk main database")
    return os.path.realpath(str(file))


def _bound_store_id(db: Any, store_id: str, conn: Any = None) -> str:
    """Bind the supplied store_id to the ACTUAL database identity.

    Canonical identity is ``PRAGMA database_list`` main ``file`` realpath
    (the opened connection), not the mutable ``config.db_path`` label.
    ``store_id`` must BE that path. Prepare-time evidence from one store
    must never commit into another, even when doc ids collide — including
    a cursor whose connection is a different file than ``db``.
    """
    if not isinstance(store_id, str) or not store_id.strip():
        raise _Abort("store_binding_invalid", "store_id must be non-empty")
    if conn is None:
        getter = getattr(db, "_get_conn", None)
        if callable(getter):
            try:
                conn = getter()
            except Exception:
                conn = None
    if conn is not None:
        actual = _canonical_store_id(conn)
    else:
        try:
            actual = os.path.realpath(
                os.path.abspath(str(db.config.db_path)))
        except Exception as exc:
            raise _Abort("store_binding_mismatch",
                         f"database identity unreadable: {exc}")
    supplied = os.path.realpath(os.path.abspath(store_id))
    if supplied != actual:
        raise _Abort(
            "store_binding_mismatch",
            f"store_id {store_id!r} does not identify this database",
        )
    return actual


def _snapshot_principal(principal: EffectivePrincipal) -> EffectivePrincipal:
    """Copy identity so later mutation of the caller's lists cannot relabel."""
    return _dc_replace(
        principal,
        capabilities=list(principal.capabilities),
        allowed_vault_roots=list(principal.allowed_vault_roots),
    )


def _require_same_authority(
    prepared: PreparedLearningCommit, principal: EffectivePrincipal
) -> None:
    if principal.agent_id != prepared.principal.agent_id:
        raise _Abort("principal_mismatch",
                     "commit principal does not match prepare")
    if principal.workspace_id != prepared.principal.workspace_id:
        raise _Abort("principal_mismatch",
                     "commit workspace does not match prepare")
    if principal.workspace_id != prepared.workspace:
        raise _Abort("principal_mismatch",
                     "commit workspace does not match prepared workspace")


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


def _json_or_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    raise _Abort("learning_field_invalid", f"cannot encode {type(value).__name__}")


def _freeze_json_value(value: Any) -> Any:
    """Copy JSON-able field values so the caller's lists cannot relabel."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    encoded = _json_or_text(value)
    if encoded is None:
        return None
    loaded = json.loads(encoded)
    if isinstance(loaded, list):
        return tuple(loaded)
    return loaded


def _freeze_fields(fields: LearningFields) -> LearningFields:
    return _dc_replace(
        fields,
        source_doc_ids=_freeze_json_value(fields.source_doc_ids),
        evidence_doc_ids=_freeze_json_value(fields.evidence_doc_ids),
    )


def _assert_unlocked(db: Any, label: str) -> None:
    getter = getattr(db, "_get_conn", None)
    if not callable(getter):
        return
    conn = getter()
    if getattr(conn, "in_transaction", False):
        raise _Abort(
            "model_in_transaction",
            f"{label} must not run while a write transaction is open",
        )


def _guard_model(db: Any, label: str, fn: Callable) -> Callable:
    """Fail-loud if a model/search callback runs under the write lock."""

    def wrapped(*args, **kwargs):
        _assert_unlocked(db, label)
        return fn(*args, **kwargs)

    return wrapped


def _payload_digest(payload: PreparedLearningCommit) -> str:
    body = {
        "store_id": payload.store_id,
        "agent": payload.principal.agent_id,
        "workspace": payload.workspace,
        "caps": list(payload.principal.capabilities),
        "roots": list(payload.principal.allowed_vault_roots),
        "content_sha256": payload.content_sha256,
        "category": payload.category,
        "vault_path": payload.vault_path,
        "embedding_model": payload.embedding_model,
        "graph_enabled": payload.graph_enabled,
        "learning_vector": None if payload.learning_vector is None
        else hashlib.sha256(payload.learning_vector).hexdigest(),
        "chunks": [
            (text, hashlib.sha256(vector).hexdigest())
            for text, vector in payload.chunks
        ],
        "path": payload.path,
        "sigil": payload.sigil,
        "layer": payload.layer,
        "shortlist_no_pairs": payload.shortlist_no_pairs,
        "validated": [
            (d.pair_id, d.doc_id, d.store_id, d.content_sha256, d.metadata_sha256,
             label, direction, confidence)
            for d, label, direction, confidence in payload.validated
        ],
        "classifier_info": list(payload.classifier_info),
        "meta_snapshots": list(payload.meta_snapshots),
        "fields": {
            "source_query": payload.learning_fields.source_query,
            "source_doc_ids": _json_or_text(payload.learning_fields.source_doc_ids),
            "evidence_doc_ids": _json_or_text(payload.learning_fields.evidence_doc_ids),
            "confidence": payload.learning_fields.confidence,
            "assertion": payload.learning_fields.assertion,
            "applies_when": payload.learning_fields.applies_when,
            "contradicts_id": payload.learning_fields.contradicts_id,
            "supersedes": payload.learning_fields.supersedes,
            "content_hash": payload.learning_fields.content_hash,
            "status": payload.learning_fields.status,
        },
        "prepared_at": payload.prepared_at,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _insert_learning(
    c: Any,
    *,
    agent_id: str,
    category: str,
    content: str,
    embedding: Optional[bytes],
    now: float,
    fields: LearningFields,
) -> int:
    """Learnings INSERT: wrapper columns plus any provided optional fields."""
    from minni.writeback import CATEGORIES

    if category not in CATEGORIES:
        category = "general"
    confidence = 1.0 if fields.confidence is None else float(fields.confidence)
    if not math.isfinite(confidence):
        raise _Abort("learning_field_invalid", "confidence must be finite")
    cols = ["agent_id", "category", "content", "confidence", "embedding", "created_at"]
    vals: List[Any] = [agent_id, category, content, confidence, embedding, now]
    present = {str(r[1]) for r in c.execute("PRAGMA table_info(learnings)").fetchall()}
    optional = (
        ("source_query", fields.source_query),
        ("source_doc_ids", _json_or_text(fields.source_doc_ids)),
        ("evidence_doc_ids", _json_or_text(fields.evidence_doc_ids)),
        ("assertion", fields.assertion),
        ("applies_when", fields.applies_when),
        ("contradicts_id", fields.contradicts_id),
        ("content_hash", fields.content_hash),
        ("status", fields.status),
    )
    for name, value in optional:
        if value is None:
            continue
        if name not in present:
            raise _Abort(
                "learning_field_unavailable",
                f"learnings.{name} was provided but is not in this schema",
            )
        cols.append(name)
        vals.append(value)
    c.execute(
        f"INSERT INTO learnings ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})",
        vals,
    )
    learning_id = int(c.lastrowid)
    if fields.supersedes is not None:
        c.execute(
            "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
            (learning_id, fields.supersedes),
        )
    return learning_id


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


def prepare_learning_with_graph(
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
    learning_fields: Optional[LearningFields] = None,
) -> PrepareResult:
    """Phase A: embed/shortlist/classify OUTSIDE any write transaction.

    Returns a detached, digest-checked payload. Model callbacks must not run while
    ``db`` is in a write transaction. Fail-loud: any prepare error yields
    ``status='error'`` and no payload (new promotion stays uncommitted).
    """
    if not isinstance(principal, EffectivePrincipal):
        return PrepareResult(status="error", error_code="bad_principal",
                             error="principal must be EffectivePrincipal")
    if workspace != principal.workspace_id:
        return PrepareResult(
            status="error", error_code="workspace_mismatch",
            error="workspace does not match principal.workspace_id",
        )
    try:
        _bound_store_id(db, store_id)
        _assert_unlocked(db, "prepare")
    except _Abort as abort:
        return PrepareResult(status="error", error_code=abort.code,
                             error=str(abort))
    principal = _snapshot_principal(principal)
    agent_id = principal.agent_id
    if not isinstance(content, str) or not content.strip():
        return PrepareResult(status="error", error_code="empty_content",
                             error="content must be non-empty text")
    now = time.time()
    meta = durable_metadata(content)
    path = durable_doc_path(agent_id, "", vault_path, content)
    try:
        fields = _freeze_fields(
            learning_fields if learning_fields is not None else LearningFields()
        )
    except _Abort as abort:
        return PrepareResult(status="error", error_code=abort.code,
                             error=str(abort))
    embed_text = _guard_model(db, "embed", embed_text)
    chunk_texts = _guard_model(db, "chunk", chunk_texts)
    search_chunks = _guard_model(db, "search", search_chunks)
    classify = _guard_model(db, "classify", classify)

    def _fail(code: str, message: str) -> PrepareResult:
        logger.warning("graph coordinator: prepare aborted (%s): %s", code, message)
        return PrepareResult(status="error", error_code=code, error=message)

    try:
        _assert_unlocked(db, "chunk")
        raw_chunks = list(chunk_texts(content)) or [content.strip()]
    except _Abort as abort:
        return _fail(abort.code, str(abort))
    except Exception as exc:
        return _fail("chunk_failed", f"chunker failed: {exc}")
    try:
        _assert_unlocked(db, "embed")
    except _Abort as abort:
        return _fail(abort.code, str(abort))
    vectors = _embed_all(embed_text, [content, *raw_chunks])
    if vectors is None:
        if graph_enabled:
            return _fail("embed_failed", "embedding unavailable")
        learning_vector, chunk_vectors = None, []
    else:
        learning_vector, chunk_vectors = vectors[0], vectors[1:]
    chunks = tuple(zip(raw_chunks, chunk_vectors)) if chunk_vectors else ()

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
            _assert_unlocked(db, "search")
            hits = list(search_chunks(learning_vector or b"", MAX_CHUNK_HITS))
        except _Abort as abort:
            return _fail(abort.code, str(abort))
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
                _assert_unlocked(db, "classify")
                validated, classifier_info = _classify_validated(
                    source, candidates, classify, descriptors)
            except _Abort as abort:
                return _fail(abort.code, str(abort))

    snapshot_rows = tuple(
        (int(doc_id), json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
        for doc_id, snapshot in sorted(meta_snapshots.items())
    )
    info_rows = tuple(sorted((str(k), str(v)) for k, v in classifier_info.items()))
    draft = PreparedLearningCommit(
        digest="",
        store_id=store_id,
        principal=principal,
        workspace=workspace,
        content=content,
        content_sha256=_sha256_text(content),
        category=category,
        vault_path=vault_path,
        embedding_model=embedding_model,
        graph_enabled=bool(graph_enabled),
        learning_vector=None if learning_vector is None else bytes(learning_vector),
        chunks=tuple((str(t), bytes(v)) for t, v in chunks),
        path=path,
        sigil=str(meta["sigil"]),
        layer=str(meta["layer"]),
        shortlist_no_pairs=bool(graph_enabled and shortlist is not None
                                and not shortlist.pairs),
        validated=tuple(validated),
        classifier_info=info_rows,
        meta_snapshots=snapshot_rows,
        learning_fields=fields,
        prepared_at=now,
    )
    payload = _dc_replace(draft, digest=_payload_digest(draft))
    return PrepareResult(status="ok", payload=payload)


def _require_open_transaction(cursor: Any) -> None:
    conn = getattr(cursor, "connection", None)
    if conn is None or not getattr(conn, "in_transaction", False):
        raise _Abort(
            "write_requires_transaction",
            "commit_prepared_learning requires the caller's open write transaction",
        )


def commit_prepared_learning(
    cursor: Any,
    prepared: PreparedLearningCommit,
    *,
    db: Any,
    principal: EffectivePrincipal,
) -> CommitResult:
    """Phase B on the CALLER's cursor. Does not commit, rollback, or refresh FAISS.

    The governed caller revalidates and terminalizes its candidate and review
    fence on this same cursor. This helper never authorizes a candidate.
    Writes are durable only after the outer transaction commits. Returned
    ``new_chunk_vectors`` are for the caller's Phase C after that commit —
    this function never invokes an index callback. On refusal it raises
    ``GraphCommitAborted`` so the caller can roll back.
    """
    if not isinstance(prepared, PreparedLearningCommit):
        raise _Abort("bad_payload", "prepared must be PreparedLearningCommit")
    if not isinstance(principal, EffectivePrincipal):
        raise _Abort("bad_principal", "principal must be EffectivePrincipal")
    if _payload_digest(prepared) != prepared.digest:
        raise _Abort("payload_tampered", "prepared digest does not match contents")
    if _sha256_text(prepared.content) != prepared.content_sha256:
        raise _Abort("payload_tampered", "content does not match snapshot hash")
    _require_same_authority(prepared, principal)
    _require_open_transaction(cursor)
    conn = getattr(cursor, "connection", None)
    _bound_store_id(db, prepared.store_id)
    _bound_store_id(db, prepared.store_id, conn=conn)
    if _payload_digest(prepared) != prepared.digest:
        raise _Abort("payload_tampered", "prepared digest mutated before write")
    snapshots = {
        doc_id: json.loads(blob) for doc_id, blob in prepared.meta_snapshots
    }
    info = dict(prepared.classifier_info)
    c = cursor
    prior = c.execute(
        "SELECT page_status, privacy_level FROM documents"
        " WHERE path = ?",
        (prepared.path,),
    ).fetchone()
    if prior is not None and projection_row_closed(
        prior["page_status"], prior["privacy_level"]
    ):
        raise _Abort("canonical_restricted",
                     "canonical node is lifecycle-closed/restricted")
    learning_id = _insert_learning(
        c, agent_id=prepared.principal.agent_id, category=prepared.category,
        content=prepared.content, embedding=prepared.learning_vector,
        now=prepared.prepared_at, fields=prepared.learning_fields,
    )
    doc_id = ensure_canonical_learning_node(
        c, learning_id=learning_id, agent_id=prepared.principal.agent_id,
        content=prepared.content, vault_path=prepared.vault_path,
        created_at=prepared.prepared_at,
    )
    if doc_id is None:
        raise _Abort("canonical_failed", "canonical node commit refused")
    bound: List[Tuple[int, int, str, float]] = []
    if prepared.graph_enabled and prepared.validated:
        targets = {}
        for descriptor, _label, _direction, _conf in prepared.validated:
            targets[descriptor.doc_id] = descriptor
        for descriptor in targets.values():
            _revalidate_target(
                c, descriptor, principal=principal,
                workspace=prepared.workspace, store_id=prepared.store_id,
                metadata_snapshot=_require_snapshot(snapshots, descriptor.doc_id),
            )
        bound = _bind_edges(prepared.validated, int(doc_id))
    new_chunk_ids: Tuple[int, ...] = ()
    new_vectors: Tuple[bytes, ...] = ()
    chunks = list(prepared.chunks)
    if chunks or prepared.graph_enabled:
        _, new_chunk_ids = _fill_projection(
            c, doc_id=doc_id, path=prepared.path,
            agent_id=prepared.principal.agent_id, sigil=prepared.sigil,
            content=prepared.content, chunks=chunks,
            embedding_model=prepared.embedding_model, layer=prepared.layer,
            now=prepared.prepared_at,
        )
        if new_chunk_ids:
            new_vectors = tuple(v for _, v in chunks)
    elif not prepared.graph_enabled:
        present = c.execute(
            "SELECT 1 FROM vault_fts WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if present is None:
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil)"
                " VALUES (?, ?, ?, ?, ?)",
                (doc_id, prepared.path, prepared.content,
                 prepared.principal.agent_id, prepared.sigil),
            )
    committed: Tuple[CommittedEdge, ...] = ()
    contradiction_logged = False
    if bound:
        run = _run_id(info["model_id"], info["prompt_version"], info["evidence_hash"])
        committed, contradiction_logged = _write_edges(
            c, bound, run_id=run, model_id=info["model_id"],
            prompt_version=info["prompt_version"],
            evidence={"evidence_hash": info["evidence_hash"]},
            now=prepared.prepared_at, new_learning_id=learning_id,
        )
    return CommitResult(
        status="staged", learning_id=learning_id, doc_id=doc_id,
        edges=committed,
        no_candidates=prepared.shortlist_no_pairs,
        edges_deferred=None if prepared.graph_enabled else "disabled",
        new_chunk_ids=new_chunk_ids, new_chunk_vectors=new_vectors,
        contradiction_logged=contradiction_logged,
    )


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
    learning_fields: Optional[LearningFields] = None,
) -> CommitResult:
    """Compatible wrapper: Phase A then caller-local ``BEGIN IMMEDIATE``.

    Phase C (FAISS) is still the caller's job after this returns. The
    wrapper only returns ``ok`` after the outer transaction commits.
    """
    prepared = prepare_learning_with_graph(
        db=db, store_id=store_id, principal=principal, content=content,
        category=category, vault_path=vault_path,
        embedding_model=embedding_model, embed_text=embed_text,
        chunk_texts=chunk_texts, search_chunks=search_chunks,
        get_metadata=get_metadata, get_content=get_content,
        classify=classify, graph_enabled=graph_enabled,
        workspace=workspace, learning_fields=learning_fields,
    )
    if prepared.status != "ok" or prepared.payload is None:
        return CommitResult(
            status="error", error_code=prepared.error_code,
            error=prepared.error,
        )

    def _fail(code: str, message: str) -> CommitResult:
        logger.warning("graph coordinator: commit aborted (%s): %s", code, message)
        return CommitResult(status="error", error_code=code, error=message)

    try:
        with db.transaction() as c:
            staged = commit_prepared_learning(
                c, prepared.payload, db=db, principal=principal,
            )
        if staged.status == "staged":
            return _dc_replace(staged, status="ok")
        return staged
    except GraphCommitAborted as abort:
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
