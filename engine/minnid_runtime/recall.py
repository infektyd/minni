import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from config import DEFAULT_CONFIG
from db import SovereignDB
from principal import (
    allows_cross_agent_recall,
    can_read_document,
    make_capability_denied_error,
)

from .redaction import redact_value


logger = logging.getLogger("minnid")


@dataclass(frozen=True)
class RecallContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    lazy_retrieval: Callable[[], Any]
    agent_vault_retrieval: Callable[[str], Any]
    all_vault_retrievals: Callable[[], list]
    trace_ring: Callable[[], Any]
    record_latency: Callable[[str, float], None]
    increment_request_count: Callable[[], None] | None = None
    sovereign_db: Callable[..., Any] = SovereignDB
    default_config: Any = field(default_factory=lambda: DEFAULT_CONFIG)
    can_read_document: Callable[[Any, str, Any], bool] = can_read_document
    allows_cross_agent_recall: Callable[[Any], bool] = allows_cross_agent_recall
    make_capability_denied_error: Callable[..., dict] = make_capability_denied_error
    redact_value: Callable[[Any], tuple[Any, bool]] = redact_value
    logger: logging.Logger = logger


def tag_document_results(results: list, *, src: str) -> list:
    for row in results:
        row["src"] = src
        # Previous vault-scope experiments exposed ownership/index paths inline.
        # Keep recall tiny; full provenance is available through drill.
        row.pop("source_agent", None)
        row.pop("source_index_db_path", None)
        provenance = row.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("source_agent", None)
            provenance.pop("source_index_db_path", None)
    return results


def result_identity(row: dict) -> tuple:
    return (
        str(row.get("source") or row.get("path") or ""),
        row.get("doc_id"),
        row.get("chunk_id"),
    )


def merge_document_results(result_sets: list, limit: int, *, prefer_personal: bool = False) -> list:
    merged = []
    for rows in result_sets:
        merged.extend(rows)
    if prefer_personal:
        deduped = {}
        for row in merged:
            key = result_identity(row)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = row
                continue
            row_score = float(row.get("score") or 0.0)
            existing_score = float(existing.get("score") or 0.0)
            row_priority = 1 if row.get("src") == "p" else 0
            existing_priority = 1 if existing.get("src") == "p" else 0
            if (row_score, row_priority) > (existing_score, existing_priority):
                deduped[key] = row
        merged = list(deduped.values())
    return sorted(
        merged,
        key=lambda row: (
            float(row.get("score") or 0.0),
            1 if prefer_personal and row.get("src") == "p" else 0,
        ),
        reverse=True,
    )[:limit]


def resolve_document_scope(params: dict) -> str:
    raw_scope = params.get("scope")
    if raw_scope is not None:
        scope = str(raw_scope)
        if scope not in {"personal", "combined", "both"}:
            raise ValueError("scope must be personal, combined, or both")
        return scope
    if bool(params.get("cross_agent", False)):
        return "combined"
    return "both"


def resolve_backend(backend_param, config=None):
    """Resolve the backend parameter for a search request."""
    cfg = config or DEFAULT_CONFIG

    if backend_param is None or backend_param == "auto":
        backends = cfg.vector_backends
        if not backends or backends == ["faiss-disk"]:
            return None
        return backends
    return backend_param


def backend_badge(backends: Any) -> str:
    if backends is None:
        names = ["faiss-disk"]
    elif isinstance(backends, str):
        names = [backends]
    elif isinstance(backends, (list, tuple)):
        names = [str(item) for item in backends if item]
    else:
        names = [str(backends)]
    return "+".join(names)


def handle_search(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Search Minni via hybrid retrieval.

    Accepts optional ``depth`` parameter for progressive disclosure:
      headline  — wikilink, title, score, confidence, age_days (~30 tokens/result)
      snippet   — + text (≤280 chars) (~120 tokens/result) [DEFAULT]  (M-2 fix)
      chunk     — + full chunk text, heading context, provenance (~500 tokens)
      document  — + full source document (whole_document=1 rows only)
    Omitting depth returns "snippet". Previous default was "headline" (no text)
    which was a documentation/implementation mismatch — fixed.

    Accepts optional ``budget_tokens`` for MMR-diverse token-budgeted packing.
    When provided, selects a diverse subset fitting within the token budget.
    ``depth="auto"`` with ``budget_tokens`` uses "snippet" as the base tier.

    Accepts optional ``backend`` parameter:
      "auto" (default)   — use config.vector_backends priority cascade
      "faiss-disk"       — force specific backend
      ["faiss-disk", X]  — fan-out via multi-backend
    """
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    query = params.get("query", "")
    if not query:
        return context.make_error(-32602, "query is required", request_id)

    # G11: EffectivePrincipal is the single server-stamped source.
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    if bool(params.get("cross_agent", False)) and principal is not None:
        if not context.allows_cross_agent_recall(principal):
            return context.make_capability_denied_error(
                "cross_agent",
                "search",
                request_id,
                principal_id=principal.agent_id,
            )
    agent_id = principal.agent_id if principal is not None else None
    learnings_cross_agent = bool(params.get("cross_agent", False)) or principal is None
    try:
        document_scope = resolve_document_scope(params)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)
    limit = min(int(params.get("limit", 5)), 20)
    depth = str(params.get("depth", "snippet"))
    budget_tokens_param = params.get("budget_tokens")
    backend_param = params.get("backend", "auto")
    layers = params.get("layers")
    sort = str(params.get("sort", "semantic"))
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    expand = params.get("expand", True)
    summarize_neighborhood = bool(params.get("summarize_neighborhood", False))

    if depth == "auto":
        depth = "snippet"

    resolved_backend = resolve_backend(backend_param, context.default_config)

    try:
        engine = context.lazy_retrieval()

        def retrieve_from(
            retrieval_engine,
            *,
            src: str,
            principal_for_documents,
        ) -> list:
            rows = retrieval_engine.retrieve(
                query=query,
                agent_id=agent_id,
                limit=limit,
                depth=depth,
                backend=resolved_backend,
                layers=layers,
                sort=sort,
                start_date=start_date,
                end_date=end_date,
                expand=expand,
                summarize_neighborhood=summarize_neighborhood,
                cross_agent=learnings_cross_agent,
                principal=principal_for_documents,
                workspace=(
                    principal_for_documents.workspace_id
                    if principal_for_documents is not None
                    else "default"
                ),
            )
            return tag_document_results(rows, src=src)

        def retrieve_shared() -> list:
            return retrieve_from(
                engine,
                src="c",
                principal_for_documents=principal,
            )

        def retrieve_personal() -> list:
            vault_retrieval = context.agent_vault_retrieval(agent_id) if agent_id else None
            if vault_retrieval is not None:
                vault_engine, _source_agent, _source_db_path = vault_retrieval
                try:
                    return retrieve_from(
                        vault_engine,
                        src="p",
                        principal_for_documents=principal,
                    )
                except Exception as exc:
                    context.logger.warning(
                        "search: personal vault index failed for %s (%s); falling back to shared",
                        agent_id,
                        exc,
                    )
            return retrieve_shared()

        def retrieve_combined() -> list:
            result_sets = []
            for vault_engine, _source_agent, _source_db_path in context.all_vault_retrievals():
                result_sets.append(
                    retrieve_from(
                        vault_engine,
                        src="c",
                        principal_for_documents=principal,
                    )
                )
            result_sets.append(retrieve_shared())
            return merge_document_results(result_sets, limit)

        if principal is None:
            results = retrieve_shared()
        elif document_scope == "personal":
            results = retrieve_personal()
        elif document_scope == "combined":
            results = retrieve_combined()
        else:
            result_sets = [retrieve_personal(), retrieve_combined()]
            results = merge_document_results(result_sets, limit, prefer_personal=True)

        if budget_tokens_param is not None:
            try:
                budget = int(budget_tokens_param)
                from tokens import pack_results

                results = pack_results(results, budget_tokens=budget, depth=depth)
            except Exception as pack_exc:
                context.logger.warning("pack_results failed: %s - returning unbudgeted results", pack_exc)

        learnings: list = []
        try:
            learnings = engine.search_learnings(
                query,
                agent_id=agent_id,
                cross_agent=learnings_cross_agent,
                limit=limit,
                source="minnid.search",
            )
        except Exception as exc:
            context.logger.warning("search: learnings surfacing/tracking failed: %s", exc)

        return context.make_response({
            "query": query,
            "agent_id": agent_id,
            "depth": depth,
            "count": len(results),
            "backend": backend_badge(resolved_backend),
            "trace_id": getattr(engine, "last_trace_id", None),
            "query_variants": (
                results[0].get("query_variants", [query])
                if results else [query]
            ),
            "results": results,
            "learnings": learnings,
        }, request_id)
    except Exception as exc:
        context.logger.exception("search failed")
        return context.make_error(-32000, f"Search error: {exc}", request_id)
    finally:
        context.record_latency("search", time.perf_counter() - started_at)


def handle_feedback(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Store useful/not-useful feedback for a prior search result."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    query = params.get("query", "")
    result_id = params.get("result_id")
    if not query:
        return context.make_error(-32602, "query is required", request_id)
    if result_id is None:
        return context.make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return context.make_error(-32602, "result_id must be an integer", request_id)

    useful = bool(params.get("useful", False))
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id

    try:
        result = context.lazy_retrieval().record_feedback(
            query=query,
            result_id=result_id,
            useful=useful,
            agent_id=agent_id,
        )
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("feedback failed")
        return context.make_error(-32000, f"Feedback error: {exc}", request_id)


def handle_trace(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Return a process-local trace entry by id."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    trace_id = params.get("trace_id")
    if not trace_id:
        return context.make_error(-32602, "trace_id is required", request_id)

    try:
        trace = context.trace_ring().get(str(trace_id))
        if trace is None:
            return context.make_response({
                "trace_id": trace_id,
                "trace": None,
                "status": "not_found",
                "ephemeral": True,
            }, request_id)
        redacted_trace, _ = context.redact_value(trace)
        return context.make_response({
            "trace_id": trace_id,
            "trace": redacted_trace,
            "status": "ok",
            "ephemeral": True,
        }, request_id)
    except Exception as exc:
        context.logger.warning("trace lookup failed: %s", exc)
        degraded = {
            "trace_id": trace_id,
            "degraded": True,
            "reason": str(exc),
        }
        redacted_degraded, _ = context.redact_value(degraded)
        return context.make_response({
            "trace_id": trace_id,
            "trace": redacted_degraded,
            "status": "degraded",
            "ephemeral": True,
        }, request_id)


def handle_expand(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Re-fetch a specific result at a deeper depth tier."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    result_id = params.get("result_id")
    if result_id is None:
        return context.make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return context.make_error(-32602, "result_id must be an integer", request_id)

    depth = str(params.get("depth", "chunk"))

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    try:
        engine = context.lazy_retrieval()
        result = engine.expand_result(
            result_id=result_id,
            depth=depth,
            principal=principal,
            workspace=principal.workspace_id,
        )
        if result is None:
            return context.make_error(-32000, f"No result found for result_id={result_id}", request_id)
        return context.make_response({
            "result_id": result_id,
            "depth": depth,
            "result": result,
        }, request_id)
    except Exception as exc:
        context.logger.exception("expand failed")
        return context.make_error(-32000, f"Expand error: {exc}", request_id)


def indexed_at_for_result(retrieval_engine, result: dict) -> Optional[float]:
    doc_id = result.get("doc_id")
    if doc_id is None:
        return None
    try:
        with retrieval_engine.db.cursor() as c:
            c.execute("SELECT indexed_at FROM documents WHERE doc_id = ?", (int(doc_id),))
            row = c.fetchone()
        if row is None:
            return None
        value = row["indexed_at"]
        return float(value) if value is not None else None
    except Exception:
        return None


def score_components(reference: dict, result: dict) -> dict:
    ref_prov = reference.get("provenance") if isinstance(reference.get("provenance"), dict) else {}
    result_prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}

    def pick(*names):
        for name in names:
            if name in reference and reference.get(name) is not None:
                return reference.get(name)
            if name in ref_prov and ref_prov.get(name) is not None:
                return ref_prov.get(name)
            if name in result and result.get(name) is not None:
                return result.get(name)
            if name in result_prov and result_prov.get(name) is not None:
                return result_prov.get(name)
        return None

    return {
        "score": pick("score"),
        "fts_rank": pick("fts_rank"),
        "semantic_rank": pick("semantic_rank", "sem_rank"),
        "rrf_score": pick("rrf_score"),
        "cross_encoder_score": pick("cross_encoder_score", "rerank_score"),
        "decay_factor": pick("decay_factor", "decay_score"),
        "backend": pick("backend"),
    }


def full_provenance(
    *,
    retrieval_engine,
    source_agent: str,
    source_vault: str,
    index_db_path: str,
    reference: dict,
    result: dict,
) -> dict:
    return {
        "owning_agent_id": source_agent,
        "document_agent": result.get("agent"),
        "source_vault": source_vault,
        "index_db_path": index_db_path,
        "indexed_at": indexed_at_for_result(retrieval_engine, result),
        "score_components": score_components(reference, result),
    }


def reference_candidates(
    reference: dict,
    principal,
    agent_id: Optional[str],
    shared_engine,
    context: RecallContext,
) -> list:
    marker = reference.get("src")
    candidates = []

    def add(candidate):
        if candidate is None:
            return
        retrieval_engine, source_agent, index_db_path, principal_for_documents = candidate
        key = str(index_db_path)
        if any(str(existing[3]) == key for existing in candidates):
            return
        source_vault = str(Path(getattr(retrieval_engine.config, "vault_path", "")).expanduser().resolve())
        candidates.append(
            (
                retrieval_engine,
                source_agent,
                source_vault,
                index_db_path,
                principal_for_documents,
            )
        )

    shared_candidate = (
        shared_engine,
        "shared",
        context.default_config.db_path,
        principal,
    )

    if principal is None:
        add(shared_candidate)
        return candidates

    personal = context.agent_vault_retrieval(agent_id) if agent_id else None
    if marker == "p":
        if personal is not None:
            vault_engine, source_agent, index_db_path = personal
            add((vault_engine, source_agent, index_db_path, principal))
        add(shared_candidate)
        return candidates

    if marker == "c":
        for vault_engine, source_agent, index_db_path in context.all_vault_retrievals():
            add((vault_engine, source_agent, index_db_path, principal))
        add(shared_candidate)
        return candidates

    if personal is not None:
        vault_engine, source_agent, index_db_path = personal
        add((vault_engine, source_agent, index_db_path, principal))
    for vault_engine, source_agent, index_db_path in context.all_vault_retrievals():
        add((vault_engine, source_agent, index_db_path, principal))
    add(shared_candidate)
    return candidates


def reference_matches(result: dict, reference: dict) -> bool:
    ref_source = reference.get("source") or reference.get("path")
    if ref_source:
        try:
            return Path(str(result.get("source") or "")).resolve() == Path(str(ref_source)).resolve()
        except Exception:
            return str(result.get("source") or "") == str(ref_source)
    ref_wikilink = reference.get("wikilink")
    if ref_wikilink:
        return str(result.get("wikilink") or "") == str(ref_wikilink)
    return True


def reference_ids_for_engine(reference: dict, retrieval_engine) -> list[int]:
    raw_id = reference.get("chunk_id") or reference.get("doc_id") or reference.get("result_id")
    if raw_id is not None:
        try:
            return [int(raw_id)]
        except (TypeError, ValueError):
            return []

    ref_source = reference.get("source") or reference.get("path")
    ref_wikilink = reference.get("wikilink")
    normalized_wikilink = str(ref_wikilink).strip() if ref_wikilink else ""
    if normalized_wikilink and not normalized_wikilink.startswith("[["):
        normalized_wikilink = f"[[{normalized_wikilink.removesuffix('.md')}]]"

    ids = []
    try:
        from retrieval import _path_to_wikilink  # type: ignore

        with retrieval_engine.db.cursor() as c:
            c.execute("SELECT doc_id, path FROM documents")
            rows = c.fetchall()
        for row in rows:
            path_value = str(row["path"])
            if ref_source:
                try:
                    if Path(path_value).resolve() == Path(str(ref_source)).resolve():
                        ids.append(int(row["doc_id"]))
                        continue
                except Exception:
                    if path_value == str(ref_source):
                        ids.append(int(row["doc_id"]))
                        continue
            if normalized_wikilink and _path_to_wikilink(path_value) == normalized_wikilink:
                ids.append(int(row["doc_id"]))
    except Exception:
        return ids
    return ids


def expand_reference(
    reference: dict,
    *,
    depth: str,
    principal,
    agent_id: Optional[str],
    shared_engine,
    context: RecallContext,
) -> Optional[dict]:
    for retrieval_engine, source_agent, source_vault, index_db_path, principal_for_documents in reference_candidates(
        reference,
        principal,
        agent_id,
        shared_engine,
        context,
    ):
        for result_id in reference_ids_for_engine(reference, retrieval_engine):
            result = retrieval_engine.expand_result(
                result_id=result_id,
                depth=depth,
                principal=principal_for_documents,
                workspace=(
                    principal_for_documents.workspace_id
                    if principal_for_documents is not None
                    else "default"
                ),
            )
            if result is None or not reference_matches(result, reference):
                continue
            marker = "p" if reference.get("src") == "p" else "c"
            full = full_provenance(
                retrieval_engine=retrieval_engine,
                source_agent=source_agent,
                source_vault=source_vault,
                index_db_path=str(Path(index_db_path).expanduser().resolve()),
                reference=reference,
                result=result,
            )
            result["src"] = marker
            result["full_provenance"] = full
            provenance = result.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            provenance.update(full)
            result["provenance"] = provenance
            return result
    return None


def handle_sm_drill(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Batch drill prior headline results to snippet/chunk/document depth."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    raw_ids = params.get("chunk_ids", params.get("result_ids"))
    raw_references = params.get("references", params.get("refs"))
    if raw_ids is None and raw_references is None:
        return context.make_error(-32602, "chunk_ids, result_ids, references, or refs is required", request_id)
    if raw_ids is not None and not isinstance(raw_ids, list):
        return context.make_error(-32602, "chunk_ids/result_ids must be a list", request_id)
    if raw_references is not None and not isinstance(raw_references, list):
        return context.make_error(-32602, "references/refs must be a list", request_id)
    raw_ids = raw_ids or []
    raw_references = raw_references or []
    if len(raw_ids) + len(raw_references) > 20:
        return context.make_error(-32602, "sm_drill accepts at most 20 ids/references", request_id)

    depth = str(params.get("depth", "snippet"))
    if depth not in {"snippet", "chunk", "document"}:
        return context.make_error(-32602, "depth must be snippet, chunk, or document", request_id)

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    try:
        ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return context.make_error(-32602, "all ids must be integers", request_id)

    references = []
    for ref in raw_references:
        if isinstance(ref, dict):
            references.append(ref)
        elif isinstance(ref, str):
            stripped = ref.strip()
            references.append({"wikilink": stripped} if stripped.startswith("[[") else {"source": stripped})
        else:
            return context.make_error(-32602, "references must be objects or strings", request_id)

    try:
        principal, err = context.handler_principal(params, request_id)
        if err:
            return err
        agent_id = principal.agent_id if principal is not None else None

        engine = context.lazy_retrieval()
        results = []
        missing = []
        for result_id in ids:
            result = engine.expand_result(
                result_id=result_id,
                depth=depth,
                principal=principal,
                workspace=(
                    principal.workspace_id if principal is not None else "default"
                ),
            )
            if result is None:
                missing.append(result_id)
            else:
                results.append(result)
        for index, reference in enumerate(references):
            result = expand_reference(
                reference,
                depth=depth,
                principal=principal,
                agent_id=agent_id,
                shared_engine=engine,
                context=context,
            )
            if result is None:
                missing.append(reference.get("doc_id") or reference.get("chunk_id") or reference.get("source") or index)
            else:
                results.append(result)
        return context.make_response({
            "depth": depth,
            "count": len(results),
            "missing": missing,
            "results": results,
        }, request_id)
    except Exception as exc:
        context.logger.exception("sm_drill failed")
        return context.make_error(-32000, f"Drill error: {exc}", request_id)


def anchor_for_result(result: dict) -> str:
    doc_id = result.get("doc_id")
    chunk_id = result.get("chunk_id")
    if doc_id is not None and chunk_id is not None:
        return f"sm://doc/{doc_id}/chunk/{chunk_id}"
    if doc_id is not None:
        return f"sm://doc/{doc_id}"
    source = str(result.get("source") or result.get("filename") or "unknown")
    return f"sm://source/{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


def handle_sm_export_pack(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Export deterministic, cache-prefix-stable context pack."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    workspace_id = params.get("workspace_id")

    query = str(params.get("query", "")).strip()
    if not query:
        return context.make_error(-32602, "query is required", request_id)
    budget_tokens = int(params.get("budget_tokens", 4096))
    cache_key = str(params.get("cache_key", "default"))
    limit = min(int(params.get("limit", 12)), 50)

    try:
        engine = context.lazy_retrieval()
        results = engine.retrieve(
            query=query,
            agent_id=agent_id,
            limit=limit,
            depth="snippet",
            update_access=False,
            principal=principal,
            workspace=principal.workspace_id if principal is not None else (workspace_id or "default"),
        )
        prefix_anchors = []
        for result in sorted(
            results,
            key=lambda r: (
                str(r.get("source", "")),
                int(r.get("doc_id") or 0),
                int(r.get("chunk_id") or 0),
            ),
        ):
            prefix_anchors.append({
                "anchor": anchor_for_result(result),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "source": result.get("source", ""),
                "wikilink": result.get("wikilink"),
            })

        suffix_snippets = []
        used_tokens = 0
        for result in results:
            tokens = int(result.get("token_count") or max(1, len(str(result.get("text", ""))) // 4))
            if suffix_snippets and used_tokens + tokens > budget_tokens:
                break
            suffix_snippets.append({
                "anchor": anchor_for_result(result),
                "text": result.get("text", ""),
                "score": result.get("score"),
                "token_count": tokens,
            })
            used_tokens += tokens

        pack = {
            "cache_key": cache_key,
            "query": query,
            "budget_tokens": budget_tokens,
            "prefix": {
                "identity": {
                    "agent_id": agent_id,
                    "workspace_id": workspace_id,
                    "format": "sovereign-context-pack-v1",
                },
                "anchors": prefix_anchors,
            },
            "suffix": {
                "query": query,
                "snippets": suffix_snippets,
                "token_count": used_tokens,
            },
        }
        canonical = json.dumps(pack, sort_keys=True, separators=(",", ":"))
        manifest_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        pack["manifest_hash"] = manifest_hash
        return context.make_response(pack, request_id)
    except Exception as exc:
        context.logger.exception("sm_export_pack failed")
        return context.make_error(-32000, f"Export pack error: {exc}", request_id)


def handle_read(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Read agent startup context (identity + knowledge + learnings)."""
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    limit = min(int(params.get("limit", 5)), 20)

    db = None
    try:
        db = context.sovereign_db()
        lines = []

        with db.cursor() as c:
            c.execute("""
                SELECT d.path, ce.chunk_text
                FROM documents d
                JOIN chunk_embeddings ce
                  ON ce.doc_id = d.doc_id AND ce.chunk_index = 0
                WHERE d.agent = ?
                  AND d.whole_document = 1
                ORDER BY d.path
            """, (f"identity:{agent_id}",))
            rows = c.fetchall()
            if rows:
                lines.append(f"## Agent Identity: {agent_id.title()}")
                lines.append("Loaded whole (not chunked). This is Layer 1.")
                for row in rows:
                    fname = os.path.basename(row["path"]).replace(".md", "").upper()
                    lines.append(f"\n### {fname}")
                    lines.append(row["chunk_text"] or "")

        with db.cursor() as c:
            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil,
                       d.access_count, d.decay_score
                FROM documents d
                WHERE (d.agent = ? OR d.agent = 'unknown'
                       OR d.agent LIKE 'wiki:%')
                  AND d.whole_document = 0
                ORDER BY d.decay_score * d.access_count DESC,
                         d.last_accessed DESC NULLS LAST
                LIMIT ?
            """, (agent_id, limit))
            rows = c.fetchall()
            if rows:
                lines.append(f"## Prior Context ({agent_id})")
                for row in rows:
                    meta = {
                        "path": row["path"],
                        "agent": row["agent"],
                        "page_type": "wiki" if "wiki" in str(row["agent"] or "") else "knowledge",
                        "privacy_level": "safe",
                    }
                    if not context.can_read_document(principal, principal.workspace_id, meta):
                        continue
                    fname = os.path.basename(row["path"])
                    line = (
                        f"  - **{fname}** ({row['sigil']}) "
                        f"[{row['agent']}] "
                        f"accessed {row['access_count']}x, "
                        f"decay={row['decay_score']:.2f}"
                    )
                    lines.append(line)

        with db.cursor() as c:
            c.execute("""
                SELECT learning_id, category, content, confidence, created_at
                FROM learnings
                WHERE agent_id = ? AND superseded_by IS NULL
                ORDER BY created_at DESC
                LIMIT 10
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Learnings ({agent_id})")
                for row in rows:
                    lines.append(
                        f"  - [{row['category']}] {row['content'][:150]} "
                        f"(conf={row['confidence']:.1f})"
                    )
                    try:
                        c.execute(
                            """INSERT OR IGNORE INTO learning_reads
                               (learning_id, agent_id, read_at, source)
                               VALUES (?, ?, ?, ?)""",
                            (row["learning_id"], agent_id, time.time(), "minnid.read"),
                        )
                    except Exception as exc:
                        context.logger.warning(
                            "read: learning_reads insert failed for learning #%s: %s",
                            row["learning_id"],
                            exc,
                        )

        with db.cursor() as c:
            c.execute("""
                SELECT event_type, content, created_at
                FROM episodic_events
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT 5
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Recent Activity ({agent_id})")
                for row in rows:
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(row["created_at"])
                    )
                    lines.append(
                        f"  - [{row['event_type']}] "
                        f"{row['content'][:120]} ({ts})"
                    )

        return context.make_response({
            "agent_id": agent_id,
            "context": "\n".join(lines) if lines else f"No context for '{agent_id}'.",
        }, request_id)
    except Exception as exc:
        context.logger.exception("read failed")
        return context.make_error(-32000, f"Read error: {exc}", request_id)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass
        context.record_latency("read", time.perf_counter() - started_at)
