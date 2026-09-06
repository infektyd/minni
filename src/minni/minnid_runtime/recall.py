import concurrent.futures
import hashlib
import json
import logging
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from minni.config import DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.timestamps import parse_epoch_or_report
from minni.principal import (
    allows_cross_agent_recall,
    can_read_document,
    make_capability_denied_error,
)

from .redaction import redact_text, redact_value


logger = logging.getLogger("minnid")

# perf/parallel-fanout (issue #388): bounded width + kill-switch for the
# corpus-leg fan-out below (per-vault legs plus the shared tail gathered by
# _combined_leg_results; scope "both" still runs personal first, then the
# combined batch, exactly as serial). Each fan-out site creates its own
# pool on demand and drains it on gather: legs run per-variant fan-outs of
# their own inside RetrievalEngine.retrieve, so a single shared pool could
# deadlock once leg tasks occupy every worker while their variant subtasks
# queue behind them (see retrieval.py). Set RECALL_LEG_PARALLEL = False for
# the legacy serial leg order (bit-identical).
# Deadline guard (correctness over speed): the leg pool engages ONLY when
# deadline_monotonic is None — the same gate as retrieval's variant pool.
# handle_search stamps a deadline on EVERY RPC, so production corpus legs
# always run the serial loop and keep origin/main's remaining-budget
# truncation (a serial leg observes time its predecessors consumed; parallel
# legs would each start with a fuller budget and truncate differently,
# changing result content). The pool path exists for deadline-free callers
# (unit tests, operator tools) and carries NO RPC latency claim.
# Cassandra YELLOW-3b: per-site cap is 4 — leg pools compound with the
# per-variant pools inside every leg (see retrieval._MAX_VARIANT_WORKERS),
# so 8-wide sites fielded 60+ threads per both-scope search. At 4/4 the
# worst case is ~30 threads (2 legs + 4 vault legs x 4 variant workers +
# tails); queueing absorbs wider vault sets with identical merge order.
_MAX_LEG_WORKERS = 4
RECALL_LEG_PARALLEL = True

# Sentinel: a leg that NEVER executed the shared engine (e.g. a personal
# vault leg that hit, so no shared fallback ran) contributes no trace. The
# envelope then reads the handler thread's thread-local slot — exactly what
# the serial code observed there (a stale id or None). A shared leg that
# RAN and failed is NOT this sentinel: retrieve_shared_soft returns None
# (the published partial) so the envelope is None, never a stale id from a
# previous request on the reused handler thread.
_SHARED_TRACE_NOT_RUN = object()

# Plugin DEFAULT_JSON_RPC_TIMEOUT_MS is 30_000. Search runs in to_thread and
# cannot be cancelled; finish inside the client kill or the worker keeps
# burning after the socket is gone. Callers may pass timeout_ms (ms).
DEFAULT_SEARCH_BUDGET_MS = 25_000
SEARCH_BUDGET_CLIENT_FRACTION = 0.9
_SEARCH_BUDGET_MS_MAX = 300_000


def _search_deadline_monotonic(params: dict) -> float:
    raw = params.get("timeout_ms")
    try:
        if raw is None or raw == "":
            budget_ms = DEFAULT_SEARCH_BUDGET_MS
        else:
            budget_ms = int(raw)
    except (TypeError, ValueError):
        budget_ms = DEFAULT_SEARCH_BUDGET_MS
    budget_ms = max(1, min(budget_ms, _SEARCH_BUDGET_MS_MAX))
    work_ms = max(1, int(budget_ms * SEARCH_BUDGET_CLIENT_FRACTION))
    now = time.monotonic()
    start = now
    # Dispatch stamps this at request accept, before asyncio.to_thread
    # queues the handler. A 10s dequeue wait on a 30s plugin kill must
    # shrink leftover, not grant a fresh 27s work budget.
    accepted = params.get("_accepted_monotonic")
    try:
        if accepted is not None and accepted != "":
            accepted_f = float(accepted)
            if accepted_f <= now:
                start = accepted_f
    except (TypeError, ValueError):
        pass
    return start + (work_ms / 1000.0)


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
    lazy_episodic: Callable[[], Any] | None = None
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


_QTY_ENGINE_KEY = "_qty_engine"
_DEADLINE_POISONED_KEY = "_deadline_poisoned"


def _ranking_deadline_poisoned(retrieval_engine: Any) -> bool:
    """True when this leg's returned ranking is FTS-only or CE-skipped.

    A skipped HyDE enrichment is not ranking poison — first-pass hybrid still
    counts as a fill.
    """
    for attr in ("last_vector_degraded", "last_rerank_degraded"):
        flag = getattr(retrieval_engine, attr, None)
        if isinstance(flag, str) and "search deadline" in flag.lower():
            return True
    return False


def _caller_visible_deadline_poisoned(results: list) -> bool:
    """True when the merged ranking is non-empty and every dict row is poisoned.

    An empty personal vault miss is not a healthy fill. Learnings qty follows
    the caller-visible set, not a sticky per-leg ranking flag.
    """
    if not results:
        return False
    dict_rows = [r for r in results if isinstance(r, dict)]
    return bool(dict_rows) and all(r.get(_DEADLINE_POISONED_KEY) for r in dict_rows)


def _bump_merged_document_access(results: list) -> None:
    """Qty-delta unique returned docs once per search cycle, per engine db."""
    seen: set[tuple[int, Any]] = set()
    now = time.time()
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get(_DEADLINE_POISONED_KEY):
            continue
        engine = row.get(_QTY_ENGINE_KEY)
        doc_id = row.get("doc_id")
        db = getattr(engine, "db", None)
        if db is None or doc_id is None:
            continue
        key = (id(engine), doc_id)
        if key in seen:
            continue
        seen.add(key)
        with db.cursor() as c:
            c.execute(
                """UPDATE documents
                   SET access_count = access_count + 1, last_accessed = ?
                   WHERE doc_id = ?""",
                (now, doc_id),
            )


def _strip_private_search_keys(results: list) -> None:
    for row in results:
        if not isinstance(row, dict):
            continue
        row.pop(_QTY_ENGINE_KEY, None)
        row.pop(_DEADLINE_POISONED_KEY, None)


def _gather_leg_results(callables: list, deadline_monotonic) -> list:
    """Run leg callables, in parallel when enabled, preserving order.

    perf/parallel-fanout (#388): pool.map preserves submission order, so
    merges and diagnostic sinks observe exactly the serial leg order.
    Single-leg calls skip the pool (zero overhead, same code path as the
    kill-switch-off serial loop).

    Deadline guard (correctness over speed): the pool engages ONLY when
    deadline_monotonic is None — the same gate as retrieval's variant
    pool. handle_search stamps a deadline on EVERY RPC, so production
    corpus legs always run the serial loop and keep origin/main's
    remaining-budget truncation. No RPC latency is claimed for this pool.

    Raise semantics (defensive-only: production legs soft-fail by
    construction — per-vault try/except plus soft shared tails — so a
    raising pooled leg is unreachable via handle_search). pool.map
    submits EVERY leg eagerly at entry, unlike the serial loop, which
    never starts legs past a raise — never claim a serial abort for the
    pool. On a raise, list(map) yields in submission order, so the
    gather waits for the slowest STARTED sibling before the first error
    surfaces, and the pool join on context exit waits for every started
    worker; a worker that picks up a queued leg runs it to completion
    (pool.map can only cancel legs that never started). The
    submission-order-first error propagates — the serial raise's
    identity at the envelope. Completed siblings' sink ops are dropped
    with the batch (no partial replay; both paths land in the outer
    except as −32000, which carries no per-leg diagnostics), while
    below-envelope residue stands (trace-ring entries a completed
    sibling wrote on its worker). No access_count writes happen on this
    path: legs retrieve with update_access=False and the merged-set qty
    bump runs only after a successful gather. Explicit fail-fast
    cancellation was rejected: as_completed could surface a LATER leg's
    error first, breaking the serial-raise identity for zero gain (leg
    bodies are not cancellable work). Pinned by test_leg_gather_* in
    tests/test_parallel_fanout_red.py.
    """
    use_leg_pool = (
        RECALL_LEG_PARALLEL
        and len(callables) > 1
        and deadline_monotonic is None
    )
    if use_leg_pool:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_LEG_WORKERS, len(callables)),
            thread_name_prefix="minni-leg",
        ) as _leg_pool:
            return list(_leg_pool.map(lambda fn: fn(), callables))
    return [fn() for fn in callables]


def merge_document_results(result_sets: list, limit: int, *, prefer_personal: bool = False) -> list:
    has_healthy = any(
        isinstance(row, dict) and not row.get(_DEADLINE_POISONED_KEY)
        for rows in result_sets
        for row in rows
    )
    merged = []
    for rows in result_sets:
        if has_healthy:
            # Drop poisoned later-scope FTS before sort/slice. Unmatched
            # deadline RRF>0 otherwise evicts a completed hybrid (negative CE
            # logit) from [:limit] — combined too, not only prefer_personal.
            merged.extend(
                row
                for row in rows
                if not (isinstance(row, dict) and row.get(_DEADLINE_POISONED_KEY))
            )
        else:
            merged.extend(rows)
    if prefer_personal:
        deduped = {}
        for row in merged:
            key = result_identity(row)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = row
                continue
            row_poisoned = bool(row.get(_DEADLINE_POISONED_KEY))
            existing_poisoned = bool(existing.get(_DEADLINE_POISONED_KEY))
            # A later deadline retrieve can score FTS-only RRF>0 against a
            # completed hybrid with a negative CE logit. Do not replace the
            # healthy twin; prefer a later healthy row over an earlier
            # poisoned one.
            if row_poisoned and not existing_poisoned:
                continue
            if existing_poisoned and not row_poisoned:
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
    """Resolve the backend parameter for a search request.

    R4(c): a *named* backend (the documented `backend: "faiss-disk"` form)
    is normalized to a single-element list so it takes the validated
    ``_resolve_backends`` path. Passed through bare it reached the
    "explicit backend object" branch and had ``.search`` called on a ``str``,
    which surfaced as a -32000 internal error — a caller mistake reported as
    a server fault. Unknown names raise ValueError so the handler can answer
    -32602 with the valid values named.
    """
    cfg = config or DEFAULT_CONFIG

    if backend_param is None or backend_param == "auto":
        backends = cfg.vector_backends
        if not backends or backends == ["faiss-disk"]:
            return None
        return backends
    if isinstance(backend_param, str):
        from minni.retrieval import RetrievalEngine

        known = RetrievalEngine._KNOWN_BACKENDS
        if backend_param not in known:
            raise ValueError(
                f"unknown backend {backend_param!r}; valid values: "
                f"{sorted(known)} (or \"auto\")"
            )
        return [backend_param]
    if isinstance(backend_param, (list, tuple)):
        # Review round 4 on PR #260: R4(c) validated only the bare-string
        # form. The equally documented LIST form passed through unchecked,
        # so its unknown names raised inside retrieve() and surfaced as a
        # -32000 internal error — the same caller mistake answered with two
        # different codes ("nope" -> -32602, ["nope"] -> -32000). Mirror the
        # engine's member/size checks here so both shapes hit the handler's
        # -32602 branch; retrieve() still dedups and re-validates.
        from minni.retrieval import RetrievalEngine

        known = RetrievalEngine._KNOWN_BACKENDS
        names = [str(item) for item in backend_param]
        if not names:
            raise ValueError(
                f"backend list must not be empty; valid values: "
                f"{sorted(known)} (or \"auto\")"
            )
        unknown = sorted({name for name in names if name not in known})
        if unknown:
            raise ValueError(
                f"unknown backend(s) {unknown}; valid values: "
                f"{sorted(known)} (or \"auto\")"
            )
        deduped_count = len(dict.fromkeys(names))
        if deduped_count > RetrievalEngine._MAX_BACKENDS:
            raise ValueError(
                f"too many backends ({deduped_count}); "
                f"max {RetrievalEngine._MAX_BACKENDS}"
            )
        return names
    # Anything else on the wire (number, object, bool) is a caller mistake,
    # not a server fault — same -32602 contract as an unknown name.
    raise ValueError(
        'backend must be "auto", a backend name, or a list of backend names'
    )


def _degradation_for(
    retrieval_engine: Any, src: str, *, source_agent: Optional[str] = None
) -> dict:
    """Describe what degraded on one corpus during this request.

    R4(a) (#226): the search response carried no ``vector_model`` or
    ``degraded`` field at all, so a caller had no way to learn the semantic leg
    was unavailable — a lexical-only answer was indistinguishable from a
    healthy hybrid one. Always reported (not only when something is wrong) so
    "the field is absent" can never be mistaken for "nothing degraded".

    ``source_agent`` names WHICH vault this is when the corpus is one of many
    fanned out by combined scope (review round 2). Without it every vault
    reports as a bare ``src: "c"``, so N degraded vaults render as N identical
    lines and the reader cannot tell a fleet outage from a duplicated report —
    the same ambiguity the per-corpus report exists to remove.
    """
    config = getattr(retrieval_engine, "config", None)
    # Review round 2 on PR #260: read the THREAD-LOCAL per-request verdict, not
    # the process-global vector_model_down bool — one engine serves concurrent
    # search workers, and a racing request could flip the global between this
    # request's retrieve() returning and this read, reporting a lexical-only
    # answer as healthy (or the inverse). The global stays for the health
    # surface, where encoder presence is a process-wide fact.
    vector_down = bool(getattr(retrieval_engine, "last_vector_degraded", None))
    entry: dict = {
        "src": src,
        "vector_model": getattr(config, "embedding_model", None),
        "vector_degraded": vector_down,
        "degraded": vector_down,
    }
    if source_agent:
        entry["source_agent"] = source_agent
    # F5 (review round 1): these three flags are set from a raw ``str(exc)`` in
    # retrieval.py — and the rerank/expand legs do not even truncate it. They
    # are also the PROVIDER-calling legs, so their exception text is the most
    # likely to carry a path or an echoed credential. Scrubbing only the
    # index-failure details left this the open half of the same hole, and it
    # is now rendered into agent context by the plugin. Redact at the SINK, so
    # every route onto the wire is covered by one rule.
    for flag, key in (
        ("last_rerank_degraded", "rerank_degraded"),
        ("last_query_expand_degraded", "query_expand_degraded"),
        ("last_hyde_degraded", "hyde_degraded"),
    ):
        value = getattr(retrieval_engine, flag, None)
        if value:
            # Only string details need scrubbing; a bare True must stay a bool
            # rather than become the string "True", which reads as a detail.
            entry[key] = _degrade_detail(value) if isinstance(value, str) else value
            entry["degraded"] = True
    return entry


def _degrade_detail(message: str) -> str:
    """Scrub a degrade detail before it goes on the wire.

    Every degrade detail is derived from ``str(exc)`` in a retrieval engine,
    and those exceptions routinely carry the failing index/db PATH — and, when
    the failure came out of a provider call, whatever the provider echoed back.
    The same module already scrubs the trace surface and the durable recall
    trace; the degrade details bypassed it and shipped raw, and the plugin now
    renders them into agent context. Redact first, then truncate, so a secret
    straddling the 400-char boundary cannot survive as a prefix — and so the
    two flags that reached the wire with no length bound at all are capped.

    Known limit (flagged, not fixed here): ``LOCAL_PATH_PATTERN`` in
    redaction.py matches only /Users, /Volumes and /private, so a Linux
    deployment's /home or /var paths still pass through. Widening the shared
    pattern affects every redaction caller and belongs in its own change.
    """
    redacted, _ = redact_text(message)
    return redacted[:400]


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


def _episodic_layer_requested(layers: Any) -> bool:
    """True when the episodic layer is in scope for this search.

    No ``layers`` filter means every advertised layer, episodic included — the
    same convention retrieval._filter_candidates uses (a None layer_set filters
    nothing). An explicit filter must name it.
    """
    if layers is None:
        return True
    if isinstance(layers, str):
        return layers.strip().lower() == "episodic"
    try:
        return any(str(layer).strip().lower() == "episodic" for layer in layers)
    except TypeError:
        return False


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

    Response ``trace_ids`` contains the fresh traces captured by this request's
    successful corpus retrievals, including searches returning no documents.
    ``trace_scope="retrieval_leg"`` means their timings cover those retrievals,
    not the whole RPC. Legacy ``trace_id`` is populated only for a single trace;
    per-result trace IDs retain their original attribution.
    """
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()
    deadline_monotonic = _search_deadline_monotonic(params)

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
    # grok-review round 4 (finding 3): normalize a bare-string `layers` ONCE at
    # the RPC edge. _episodic_layer_requested handles the string form, but
    # retrieval._normalize_layers iterates it character by character — an empty
    # layer set, i.e. NO document filter — so layers="episodic" ran an
    # episodic-scoped search that still returned knowledge/identity documents.
    if isinstance(layers, str):
        layers = [layers]
    sort = str(params.get("sort", "semantic"))
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    expand = params.get("expand", True)
    summarize_neighborhood = bool(params.get("summarize_neighborhood", False))
    claim = str(params.get("claim", "")).strip() or None

    if depth == "auto":
        depth = "snippet"

    try:
        resolved_backend = resolve_backend(backend_param, context.default_config)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)

    try:
        engine = context.lazy_retrieval()

        # P0-A contract (2026-07-19 blackout): an auth gate that filtered a
        # non-empty candidate set to zero must be visible in the response —
        # a scope blackout and "nothing matched" are different answers.
        auth_suppressions: list = []
        # R4/R5 (#226): every corpus this request touched, with whatever
        # degraded on it. Collected per-src for the same reason auth
        # suppressions are: "combined" recall fans out over independently
        # scored engines, and a degrade on one of them is not a degrade on all.
        degradations: list = []

        # F4: the shared engine is reached by up to TWO legs in one request —
        # the personal leg's soft fallback and retrieve_combined's shared tail
        # — so one shared corpus produced two identical reports and a caller
        # counting broken corpora read one outage as two.
        #
        # Two rejected shapes, both of which review caught as worse than the
        # bug (this is the third):
        #  - Deduping the WHOLE report by content collapsed N genuinely
        #    degraded vaults into one whenever they failed alike, and a dead
        #    embedder is process-wide, so failing alike is the common case.
        #  - Letting the FIRST shared leg claim the report and silencing the
        #    second dropped a second leg whose outcome DIFFERED — leg one
        #    healthy, leg two throwing, reported as a healthy hybrid recall.
        #    That is a health overstatement, strictly worse than an over-count.
        #
        # So: identity dedupe, scoped to the shared corpus only. Two identical
        # shared reports are one report; a second shared report that differs is
        # news and lands. Per-vault entries never enter this path.
        _shared_seen: dict[str, set] = {"degradation": set(), "auth": set()}
        # Empty-abort skip must follow whichever retrieve_from ran, not the
        # shared lazy_retrieval() engine. scope=personal with a vault returns
        # from the vault leg on a deadline miss and never touches shared.
        cycle_deadline_poisoned = False
        # scope=both reaches the own vault again in combined. All retrieve
        # arguments are request constants; only presentation src changes.
        # Keep one raw snapshot before tagging mutates rows/provenance and
        # before another engine call can overwrite thread-local diagnostics.
        # Shared fallback and failed calls retain their existing retry behavior.
        personal_snapshot = None
        retrieval_trace_ids: list[str] = []
        _snapshot_lock = threading.Lock()
        _personal_snapshot_ready = threading.Event()
        if document_scope != "both":
            _personal_snapshot_ready.set()

        def record_shared(entry: dict, *, bucket: str, sink: list) -> None:
            key = json.dumps(entry, sort_keys=True, default=str)
            if key in _shared_seen[bucket]:
                return
            _shared_seen[bucket].add(key)
            sink.append(entry)

        def _replay_leg_ops(ops: list) -> None:
            """Replay one leg's sink writes on the gathering thread, in order.

            Workers never touch auth_suppressions / degradations / _shared_seen
            / retrieval_trace_ids / cycle_deadline_poisoned themselves; they
            return ops and the gatherer replays them in deterministic leg
            order, so parallel legs report in exactly the serial order.
            """
            nonlocal cycle_deadline_poisoned
            for kind, entry, is_shared in ops:
                if kind == "auth":
                    if is_shared:
                        record_shared(entry, bucket="auth", sink=auth_suppressions)
                    else:
                        auth_suppressions.append(entry)
                elif kind == "degradation":
                    if is_shared:
                        record_shared(
                            entry, bucket="degradation", sink=degradations
                        )
                    else:
                        degradations.append(entry)
                elif kind == "trace":
                    if (
                        isinstance(entry, str)
                        and entry
                        and entry not in retrieval_trace_ids
                    ):
                        retrieval_trace_ids.append(entry)
                elif kind == "poison":
                    if entry:
                        cycle_deadline_poisoned = True

        def retrieve_from(
            retrieval_engine,
            *,
            src: str,
            principal_for_documents,
            shared: bool = False,
            source_agent: Optional[str] = None,
            personal: bool = False,
        ) -> tuple:
            """Run one corpus leg; return (rows, sink_ops, trace_id).

            sink_ops are replayed on the gathering thread in serial leg
            order (auth / degradation / trace / poison). Workers never
            mutate those sinks, so parallel legs report exactly as serial.

            trace_id is this leg's OWN call trace: retrieve() publishes its
            RetrievalCallState.trace_id to the calling thread's slot on
            return (RED-1), so sampling last_trace_id HERE — on the worker
            that ran the call — can never observe a sibling's id.

            origin/main snapshot: scope=both reuses the personal vault
            retrieve for the combined own-vault leg (one retrieve, one
            trace, src rewritten p→c). Parallel both-scope waits for that
            snapshot so combined cannot double-retrieve the own vault.
            """
            nonlocal personal_snapshot
            reused = False
            if (
                not shared
                and not personal
                and document_scope == "both"
            ):
                _personal_snapshot_ready.wait()
            with _snapshot_lock:
                snapshot = personal_snapshot
            if (
                not shared
                and snapshot is not None
                and snapshot[0] is retrieval_engine
                and snapshot[1] is principal_for_documents
            ):
                rows, poisoned, suppression, degradation = deepcopy(snapshot[2])
                reused = True
                trace_id = None
            else:
                previous_trace_id = getattr(retrieval_engine, "last_trace_id", None)
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
                    claim=claim,
                    principal=principal_for_documents,
                    workspace=(
                        principal_for_documents.workspace_id
                        if principal_for_documents is not None
                        else "default"
                    ),
                    deadline_monotonic=deadline_monotonic,
                    update_access=False,
                )
                # Capture this leg immediately, including empty results. An
                # unchanged diagnostic can belong to an earlier request when
                # retrieval returns before creating a trace. Failed calls
                # never reach this point; reused personal snapshots add no
                # new trace. Sample on THIS worker thread (RED-1).
                trace_id = getattr(retrieval_engine, "last_trace_id", None)
                if not (
                    isinstance(trace_id, str)
                    and trace_id
                    and trace_id != previous_trace_id
                ):
                    trace_id = None
                poisoned = _ranking_deadline_poisoned(retrieval_engine)
                suppression = getattr(retrieval_engine, "last_auth_suppression", None)
                degradation = _degradation_for(retrieval_engine, src)
                if personal and document_scope == "both":
                    with _snapshot_lock:
                        personal_snapshot = (
                            retrieval_engine,
                            principal_for_documents,
                            deepcopy((rows, poisoned, suppression, degradation)),
                        )
                    _personal_snapshot_ready.set()
            ops: list = []
            if suppression:
                # Review round 2: the suppression channel had the same
                # double-report on the same two-leg shared path, and the
                # plugin now RENDERS it — one blackout of one corpus was
                # about to be announced to the agent twice.
                entry = {"src": src, **suppression}
                ops.append(("auth", entry, shared))
            if reused:
                degradation = dict(degradation)
                degradation["src"] = src
                if source_agent:
                    degradation["source_agent"] = source_agent
                elif "source_agent" in degradation:
                    degradation.pop("source_agent", None)
            else:
                degradation = _degradation_for(
                    retrieval_engine, src, source_agent=source_agent
                )
            ops.append(("degradation", degradation, shared))
            if trace_id:
                ops.append(("trace", trace_id, False))
            if poisoned:
                ops.append(("poison", True, False))
            tagged = tag_document_results(rows, src=src)
            for row in tagged:
                if not isinstance(row, dict):
                    continue
                row[_QTY_ENGINE_KEY] = retrieval_engine
                if poisoned:
                    row[_DEADLINE_POISONED_KEY] = True
            return tagged, ops, trace_id if not reused else None

        def retrieve_shared() -> tuple:
            rows, ops, trace_id = retrieve_from(
                engine,
                src="c",
                principal_for_documents=principal,
                shared=True,
            )
            return rows, ops, trace_id

        def retrieve_shared_soft() -> tuple:
            """Shared leg that soft-fails when other corpora may already have hits.

            Round 18 (PR #260): retrieve_combined / both-scope already soft-failed
            per-agent vaults, but a throw from the shared engine still bubbled to
            handle_search's outer except as −32000 and dropped every partial hit.
            Mirror the agent-leg contract: log, record degradation, return [].
            Sole-shared callers (principal is None / pure shared scope) still use
            hard retrieve_shared so a total failure surfaces as an RPC error.

            Returns (rows, sink_ops, trace_id); the failure entry is a
            replayable op, and the trace is None — the shared leg ran and
            failed, so there is no fresh id. None (not the sentinel) so the
            caller does NOT fall back to the handler thread's slot: under
            parallel legs the shared retrieve ran on a pool worker, and the
            handler slot may hold a PREVIOUS request's id on these reused
            threads. _SHARED_TRACE_NOT_RUN stays reserved for legs where the
            shared engine never executed at all.
            """
            try:
                return retrieve_shared()
            except Exception as exc:
                detail = _degrade_detail(f"shared index failed: {exc}")
                context.logger.warning(
                    "search: shared index failed (%s); continuing with partial results",
                    exc,
                )
                emb_model = None
                try:
                    emb_model = getattr(engine.config, "embedding_model", None)
                except Exception:
                    emb_model = None
                if emb_model is None:
                    emb_model = getattr(
                        getattr(context, "default_config", None),
                        "embedding_model",
                        None,
                    )
                ops: list = [
                    (
                        "degradation",
                        {
                            "src": "c",
                            "vector_model": emb_model,
                            "vector_degraded": False,
                            "degraded": True,
                            "shared_index_failed": detail,
                            "reason": detail,
                        },
                        True,
                    )
                ]
                return [], ops, None

        def retrieve_personal(vault_retrieval, *, soft: bool = False) -> tuple:
            """Personal vault leg; optional soft shared fallback for multi-leg scopes.

            When the personal vault never ran (no agent vault), sole personal
            scope still uses a hard shared fallback so total shared failure
            surfaces as −32000. Once a personal-leg exception has been recorded
            into ``degradations``, shared is always soft — a hard shared throw
            would −32000 and drop the personal degrade that Round 13 added
            (dual-corpus total failure is exactly when that signal matters).
            Scope "both" also soft-fails shared so a personal boom + shared boom
            cannot erase combined hits with −32000.

            ``vault_retrieval`` is resolved by the CALLER on the gathering
            thread (lazy engine singletons stay off pool workers). Returns
            (rows, sink_ops, shared_trace) where shared_trace is the shared
            fallback's trace, or _SHARED_TRACE_NOT_RUN when the shared engine
            never ran here.
            """
            personal_failed = False
            personal_ops: list = []
            if vault_retrieval is not None:
                vault_engine, _source_agent, _source_db_path = vault_retrieval
                try:
                    rows, ops, _trace = retrieve_from(
                        vault_engine,
                        src="p",
                        principal_for_documents=principal,
                        personal=True,
                    )
                    _personal_snapshot_ready.set()
                    return rows, ops, _SHARED_TRACE_NOT_RUN
                except Exception as exc:
                    _personal_snapshot_ready.set()
                    # Round 13 (PR #260): personal leg failure was log-only while
                    # the shared fallback could still report degraded:false —
                    # personal memory never ran, response looked healthy.
                    personal_failed = True
                    detail = _degrade_detail(f"personal vault index failed: {exc}")
                    context.logger.warning(
                        "search: personal vault index failed for %s (%s); falling back to shared",
                        agent_id,
                        exc,
                    )
                    emb_model = None
                    try:
                        emb_model = getattr(
                            vault_engine.config, "embedding_model", None
                        )
                    except Exception:
                        emb_model = None
                    if emb_model is None:
                        emb_model = getattr(
                            getattr(context, "default_config", None),
                            "embedding_model",
                            None,
                        )
                    personal_ops.append(
                        (
                            "degradation",
                            {
                                "src": "p",
                                "vector_model": emb_model,
                                "vector_degraded": False,
                                "degraded": True,
                                "personal_index_failed": detail,
                                "reason": detail,
                            },
                            False,
                        )
                    )
            else:
                _personal_snapshot_ready.set()
            # Round 19: soft shared when other legs may still run (scope both).
            # Round 22: also soft when personal already failed — dual-corpus
            # total failure must keep personal_index_failed on the 200 body,
            # not discard it via outer −32000.
            if soft or personal_failed:
                rows, ops, trace_id = retrieve_shared_soft()
            else:
                rows, ops, trace_id = retrieve_shared()
            return rows, personal_ops + ops, trace_id

        def _combined_leg_results(vault_legs: list) -> tuple:
            """Fan out vault legs + shared tail; return (result_sets, ops, tail_trace).

            The shared tail is the LAST leg in submission order, so the gather
            order — and therefore the merge input and the replay order — match
            the serial loop exactly. ops across all legs are concatenated in
            leg order for the caller to replay.
            """
            # Round 16: mirror personal-leg hardening — one agent vault throw
            # must not JSON-RPC −32000 the whole combined search. Per-engine
            # try/except, degradation entry, continue with partial hits.
            def _vault_leg(vault_engine, source_agent) -> tuple:
                try:
                    rows, ops, _trace = retrieve_from(
                        vault_engine,
                        src="c",
                        principal_for_documents=principal,
                        source_agent=source_agent,
                    )
                    return rows, ops, None
                except Exception as exc:
                    detail = _degrade_detail(
                        f"combined vault index failed"
                        f"{f' ({source_agent})' if source_agent else ''}: {exc}"
                    )
                    context.logger.warning(
                        "search: combined vault index failed for %s (%s); continuing with partial results",
                        source_agent,
                        exc,
                    )
                    emb_model = None
                    try:
                        emb_model = getattr(
                            vault_engine.config, "embedding_model", None
                        )
                    except Exception:
                        emb_model = None
                    if emb_model is None:
                        emb_model = getattr(
                            getattr(context, "default_config", None),
                            "embedding_model",
                            None,
                        )
                    return (
                        [],
                        [
                            (
                                "degradation",
                                {
                                    "src": "c",
                                    "vector_model": emb_model,
                                    "vector_degraded": False,
                                    "degraded": True,
                                    "combined_index_failed": detail,
                                    "reason": detail,
                                    "source_agent": source_agent,
                                },
                                False,
                            )
                        ],
                        None,
                    )

            leg_fns = []
            for vault_engine, source_agent, _source_db_path in vault_legs:
                leg_fns.append(
                    lambda _ve=vault_engine, _sa=source_agent: _vault_leg(_ve, _sa)
                )
            # Round 18: soft-fail shared so agent-vault hits already collected
            # are not erased by a shared-index throw (−32000).
            leg_fns.append(retrieve_shared_soft)
            outcomes = _gather_leg_results(leg_fns, deadline_monotonic)
            result_sets = []
            combined_ops: list = []
            for rows, ops, _trace in outcomes[:-1]:
                result_sets.append(rows)
                combined_ops.extend(ops)
            tail_rows, tail_ops, tail_trace = outcomes[-1]
            result_sets.append(tail_rows)
            combined_ops.extend(tail_ops)
            return result_sets, combined_ops, tail_trace

        def retrieve_combined(vault_legs: list) -> tuple:
            """Combined scope: merge vault legs + shared tail.

            Returns (merged_rows, tail_trace) after replaying every leg's sink
            writes in deterministic leg order.
            """
            result_sets, combined_ops, tail_trace = _combined_leg_results(
                vault_legs
            )
            _replay_leg_ops(combined_ops)
            return merge_document_results(result_sets, limit), tail_trace

        # Lazy engine singletons resolve HERE, on the gathering thread, in the
        # same order and multiplicity as the serial code (personal scope never
        # touches all_vault_retrievals; combined never touches
        # agent_vault_retrieval; both resolves agent vault first, then all
        # vaults). Pool workers only ever use already-resolved engines.
        if principal is None:
            results, results_ops, envelope_trace = retrieve_shared()
            _replay_leg_ops(results_ops)
        elif document_scope == "personal":
            personal_vault = (
                context.agent_vault_retrieval(agent_id) if agent_id else None
            )
            results, results_ops, personal_shared_trace = retrieve_personal(
                personal_vault
            )
            _replay_leg_ops(results_ops)
            # Slot fallback ONLY for the never-ran sentinel (vault hit, no
            # shared fallback executed): a ran-and-failed shared leg reports
            # None, which selects None here — never a stale handler id.
            envelope_trace = (
                personal_shared_trace
                if personal_shared_trace is not _SHARED_TRACE_NOT_RUN
                else getattr(engine, "last_trace_id", None)
            )
        elif document_scope == "combined":
            combined_vaults = list(context.all_vault_retrievals())
            results, envelope_trace = retrieve_combined(combined_vaults)
            # As above: only the never-ran sentinel reads the handler slot;
            # a failed shared tail is None and stays None.
            if envelope_trace is _SHARED_TRACE_NOT_RUN:
                envelope_trace = getattr(engine, "last_trace_id", None)
        else:
            # scope "both": personal + combined. Combined already soft-fails
            # shared; personal must soft-fail its shared fallback too — a hard
            # shared throw here (−32000) ran before combined and dropped every
            # agent-vault hit that combined would have returned.
            both_personal_vault = (
                context.agent_vault_retrieval(agent_id) if agent_id else None
            )
            # origin/main runs personal retrieve, then all_vault_retrievals
            # (a between-legs hook that may mutate diagnostics), then the
            # combined own-vault snapshot reuse. Resolving vaults before
            # personal would snapshot the post-mutate verdict. Combined
            # vault legs still parallelize inside _combined_leg_results.
            personal_rows, personal_ops, personal_shared_trace = retrieve_personal(
                both_personal_vault, soft=True
            )
            _replay_leg_ops(personal_ops)
            both_combined_vaults = list(context.all_vault_retrievals())
            combined_sets, combined_ops, combined_tail_trace = _combined_leg_results(
                both_combined_vaults
            )
            _replay_leg_ops(combined_ops)
            combined_rows = merge_document_results(combined_sets, limit)
            result_sets = [personal_rows, combined_rows]
            results = merge_document_results(result_sets, limit, prefer_personal=True)
            # Serial last-shared-leg-wins: combined's tail ran after personal's
            # fallback, so it wins when it ran (a ran-and-failed tail is None
            # and selects None — never the handler slot); else personal's
            # fallback trace; else the handler thread's slot (vault-only
            # path, shared never executed), as serial.
            if combined_tail_trace is not _SHARED_TRACE_NOT_RUN:
                envelope_trace = combined_tail_trace
            elif personal_shared_trace is not _SHARED_TRACE_NOT_RUN:
                envelope_trace = personal_shared_trace
            else:
                envelope_trace = getattr(engine, "last_trace_id", None)

        if budget_tokens_param is not None:
            try:
                budget = int(budget_tokens_param)
                from minni.tokens import pack_results

                results = pack_results(results, budget_tokens=budget, depth=depth)
            except Exception as pack_exc:
                context.logger.warning("pack_results failed: %s - returning unbudgeted results", pack_exc)

        # GA4-1 / grok-review rounds 4-5 (finding 1): the calibration window is
        # fed HERE, over the final merged caller-visible set — and only here.
        # _format_results used to record, but production search is multi-call
        # (scope=both fans out personal+combined; expansion recurses per
        # variant), so one default limit=5 RPC could insert 10 rows — enough to
        # cross _ACTIVATION_THRESHOLD alone on a padded window. It is also
        # multi-ENGINE: vault hits used to calibrate against their own
        # forever-empty vault windows while shared hits used the shared one, so
        # a single response mixed raw blends with percentile ranks. Formatting
        # is now raw-only (db=None); this loop records every final row's raw
        # into the SHARED window and rewrites its confidence onto that one
        # basis, then pops the carrier so the transport surface is unchanged.
        # Deadline FTS-only RRF (no semantic weight, no CE logit) and
        # deadline-skipped CE (hybrid RRF, still no CE logit) are a
        # different numeric population than hybrid+CE scores. Encoder-down
        # FTS still records — that path is the historical calibration
        # feeder. Gate qty/calibration on the ranking that was actually
        # returned: a skipped HyDE enrichment is not poison, and a later
        # deadline leg must not wipe a completed hybrid fill in the same RPC.
        # Learnings qty is not per-row — skip it when the merged
        # caller-visible set is non-empty and every dict row is poisoned,
        # or when the document ranking is empty AND this cycle aborted on
        # a search deadline (FTS miss after vector/CE skip). Key empty-abort
        # off retrieve_from cycle poison, not the shared engine: a personal
        # vault miss never sets shared flags. An empty personal miss must
        # not fail-open that gate, and an empty deadline cycle must not
        # qty-delta learnings.
        skip_score_record = _caller_visible_deadline_poisoned(results) or (
            not results and cycle_deadline_poisoned
        )
        _bump_merged_document_access(results)
        try:
            from minni.rationale import explain
            from minni.retrieval import _recommended_action
            from minni.scoring import calibrated_confidence, record_score

            # grok-review round 6 (finding 1): TWO passes. Recording and
            # calibrating row-by-row let the window cross
            # _ACTIVATION_THRESHOLD mid-response — hit 1 served raw_blend,
            # hits 2..n percentile_rank, inside one payload. Record everything
            # first, then calibrate every row against the same post-record
            # window so the whole response shares one basis.
            recorded: list = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                poisoned_row = bool(r.get(_DEADLINE_POISONED_KEY))
                raw_score = r.pop("confidence_raw", None)
                if raw_score is None:
                    continue
                if skip_score_record or poisoned_row:
                    continue
                try:
                    record_score(float(raw_score), "combined", engine.db)
                    recorded.append((r, float(raw_score)))
                except Exception as exc:
                    context.logger.debug("search: score record failed: %s", exc)
            for r, raw_score in recorded:
                if r.get("confidence") is None:
                    continue
                try:
                    r["confidence"] = calibrated_confidence(raw_score, engine.db)
                except Exception as exc:
                    context.logger.debug("search: calibration failed: %s", exc)
                    continue
                # grok-review round 8 (finding 1): formatting freezes
                # recommended_action and rationale from the PRE-calibration
                # blend. Rewriting confidence alone left the envelope
                # disagreeing with itself after activation — e.g. confidence
                # 0.85 with recommended_action "follow_up" and rationale
                # "…; confidence 0.15.". Re-derive anything that consumed the
                # old value so one payload has one meaning of confidence.
                if "recommended_action" in r:
                    try:
                        r["recommended_action"] = _recommended_action(
                            r.get("review_state"),
                            r.get("instruction_like"),
                            r.get("confidence"),
                        )
                    except Exception as exc:
                        context.logger.debug(
                            "search: recommended_action refresh failed: %s", exc
                        )
                if "rationale" in r:
                    try:
                        r["rationale"] = explain(r)
                    except Exception as exc:
                        context.logger.debug(
                            "search: rationale refresh failed: %s", exc
                        )
        except Exception as exc:
            context.logger.warning("search: score recording failed: %s", exc)
        _strip_private_search_keys(results)

        learnings: list = []
        try:
            learnings = engine.search_learnings(
                query,
                agent_id=agent_id,
                cross_agent=learnings_cross_agent,
                limit=limit,
                source="minnid.search",
                update_access=not skip_score_record,
            )
        except Exception as exc:
            context.logger.warning("search: learnings surfacing/tracking failed: %s", exc)

        # Audit #225-R1: the episodic layer was advertised (the `layer` enum and
        # BOOT_RECALL_LAYERS both expose it) but search_episodic had ZERO
        # production call sites, so 2,178 captured events were unretrievable —
        # document retrieval can never reach them, because episodic events live
        # in episodic_events/episodic_fts, not in `documents`. Surfaced as their
        # own array for the same reason learnings are: they are not documents
        # and must not be merged into a result set whose consumers assume a
        # doc_id.
        episodic_hits: list = []
        if _episodic_layer_requested(layers):
            try:
                episodic_hits = engine.search_episodic(
                    query,
                    agent_id=None if learnings_cross_agent else agent_id,
                    limit=limit,
                    exclude_event_types=engine.EPISODIC_NON_MEMORY_TYPES,
                )
            except Exception as exc:
                context.logger.warning("search: episodic surfacing failed: %s", exc)

        # Durable recall trace (observability, config.recall_trace): a TTL'd
        # episodic event per search so `minni watch` can show raw-RPC recalls
        # too, not just plugin-mediated ones. Best-effort — a trace failure
        # must never fail the search itself.
        if (
            principal is not None
            and context.lazy_episodic is not None
            and getattr(context.default_config, "recall_trace", False)
        ):
            try:
                top_score = float(results[0].get("score") or 0.0) if results else 0.0
                session_id = params.get("session_id")
                # The trace is durable — scrub secrets from the persisted
                # query snippet just like the ephemeral trace path does.
                safe_query, _ = redact_text(query[:120])
                episodic = context.lazy_episodic()
                episodic.add_event(
                    agent_id=agent_id,
                    event_type="recall",
                    content=(f'recall "{safe_query}" — {len(results)} hits, '
                             f"top {top_score:.2f}"),
                    thread_id=str(session_id) if session_id else None,
                    # Observability only: never thread-bind (that mutates
                    # thread_doc_links and runs a semantic search).
                    bind_thread=False,
                    metadata={
                        "query_sha256_12": hashlib.sha256(
                            query.encode("utf-8")).hexdigest()[:12],
                        "hits": len(results),
                        "learnings": len(learnings),
                        "top_score": top_score,
                        "depth": depth,
                        "scope": document_scope,
                    },
                )
                # Keep the trace's own footprint honest to its 7d TTL —
                # nothing schedules the global episodic cleanup today.
                episodic.trim_recall_traces()
            except Exception as exc:
                context.logger.debug("search: recall trace failed: %s", exc)

        response_payload = {
            "query": query,
            "agent_id": agent_id,
            "depth": depth,
            "count": len(results),
            "backend": backend_badge(resolved_backend),
            # Traces describe individual corpus retrievals, not total RPC
            # latency (which also includes merging, learnings and episodic).
            # Keep the singular compatibility field only when unambiguous.
            # Gather-order replay of per-leg trace ops preserves origin/main
            # serial capture order under the #388 fan-out.
            "trace_id": retrieval_trace_ids[0] if len(retrieval_trace_ids) == 1 else None,
            "trace_ids": retrieval_trace_ids,
            "trace_scope": "retrieval_leg",
            "query_variants": (
                results[0].get("query_variants", [query])
                if results else [query]
            ),
            "results": results,
            "learnings": learnings,
            "episodic": episodic_hits,
            "episodic_count": len(episodic_hits),
        }
        # R3 (#226): this used to be `if not results and auth_suppressions`. A
        # vault entirely blacked out by authorization was therefore reported as
        # normal whenever ANY other leg returned hits, and the caller could not
        # tell "this vault had nothing" from "this vault was fully suppressed".
        # Per-corpus suppression is per-corpus news; report it on its own terms.
        if auth_suppressions:
            response_payload["auth_suppression"] = auth_suppressions
        # R4/R5 (#226): always present, so a caller reading the response can
        # always answer "was this a healthy hybrid search?" without inferring it.
        response_payload["degradation"] = degradations
        response_payload["degraded"] = any(d.get("degraded") for d in degradations)
        return context.make_response(response_payload, request_id)
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
        # R8: bind the read to the requesting principal — a trace_id is not an
        # authorization token, so another authenticated principal cannot read a
        # trace it did not create (owner-bound entries deny a non-owner).
        trace = context.trace_ring().get(
            str(trace_id), requester=getattr(principal, "agent_id", None)
        )
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


def _attribution_trace_items(results: list) -> list:
    items = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("attribution_score") is None:
            continue
        items.append({
            "doc_id": result.get("doc_id"),
            "chunk_id": result.get("chunk_id"),
            "attribution": result.get("attribution"),
            "score": result.get("attribution_score"),
        })
    return items


def record_attribution_trace(
    *,
    context: RecallContext,
    operation: str,
    claim: Optional[str],
    results: list,
    owner: Optional[str] = None,
) -> Optional[str]:
    claim_text = str(claim or "").strip()
    if not claim_text:
        return None
    items = _attribution_trace_items(results)
    if not items:
        return None
    try:
        # R8: bind the creating principal so a different authenticated caller
        # cannot read this attribution trace just by knowing/guessing the id.
        return context.trace_ring().add({
            "operation": operation,
            "claim": claim_text,
            "attribution_scores": items,
            "timing": {},
        }, owner=owner)
    except Exception as exc:
        context.logger.debug("%s attribution trace capture failed: %s", operation, exc)
        return None


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
    claim = str(params.get("claim", "")).strip() or None

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
            claim=claim,
        )
        if result is None:
            return context.make_error(-32000, f"No result found for result_id={result_id}", request_id)
        trace_id = record_attribution_trace(
            context=context,
            operation="expand",
            claim=claim,
            results=[result],
            owner=getattr(principal, "agent_id", None),
        )
        if trace_id is not None:
            result["trace_id"] = trace_id
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
        # Audit R0: was float() inside a bare except, so a TEXT indexed_at
        # vanished as a silent None. Parse-or-report keeps the None but makes
        # the bad row countable in the health surface.
        return parse_epoch_or_report(
            row["indexed_at"], field="indexed_at",
            source="recall.indexed_at_for_result", doc_id=doc_id,
        )
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
        "attribution": pick("attribution"),
        "attribution_score": pick("attribution_score"),
        "attribution_model": pick("attribution_model"),
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
    existing = result.get("full_provenance")
    base = existing if isinstance(existing, dict) else {}
    return {
        **base,
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


def reference_id_kind(reference: dict) -> str:
    """Identifier kind for a drill reference, mirroring reference_ids_for_engine.

    Explicit ``chunk_id`` resolves in chunk namespace only; explicit
    ``doc_id`` and source/path/wikilink lookups (which resolve to doc_ids via
    the documents table) resolve in doc namespace only. A bare legacy
    ``result_id`` keeps the ambiguous chunk-first fallback ("auto"). The
    truthiness chain matches reference_ids_for_engine so the kind always
    describes the id that function actually returned.
    """
    if reference.get("chunk_id"):
        return "chunk"
    if reference.get("doc_id"):
        return "doc"
    if reference.get("result_id"):
        return "auto"
    return "doc"


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
        from minni.retrieval import _path_to_wikilink  # type: ignore

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
    claim: Optional[str] = None,
) -> Optional[dict]:
    for retrieval_engine, source_agent, source_vault, index_db_path, principal_for_documents in reference_candidates(
        reference,
        principal,
        agent_id,
        shared_engine,
        context,
    ):
        id_kind = reference_id_kind(reference)
        for result_id in reference_ids_for_engine(reference, retrieval_engine):
            result = retrieval_engine.expand_result(
                result_id=result_id,
                depth=depth,
                id_kind=id_kind,
                principal=principal_for_documents,
                workspace=(
                    principal_for_documents.workspace_id
                    if principal_for_documents is not None
                    else "default"
                ),
                claim=claim,
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
    claim = str(params.get("claim", "")).strip() or None

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
                claim=claim,
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
                claim=claim,
            )
            if result is None:
                missing.append(reference.get("doc_id") or reference.get("chunk_id") or reference.get("source") or index)
            else:
                results.append(result)
        trace_id = record_attribution_trace(
            context=context,
            operation="sm_drill",
            claim=claim,
            results=results,
            owner=getattr(principal, "agent_id", None),
        )
        if trace_id is not None:
            for result in results:
                if isinstance(result, dict) and result.get("attribution_score") is not None:
                    result["trace_id"] = trace_id
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
    deadline_monotonic = _search_deadline_monotonic(params)

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
            deadline_monotonic=deadline_monotonic,
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


def handle_list_events(params: dict, request_id: Any, context: RecallContext) -> dict:
    """Read-only cursor listing over episodic_events.

    Plain parameterized SELECT — no INSERT/UPDATE/DELETE, no FTS, no side
    effects. Params: since_id (default 0), limit (default 50, clamp 1-200),
    agent_id (optional filter), event_type (optional filter). Result is
    ordered by event_id ASC so a caller can page with
    since_id=last_id from the previous response.
    """
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    since_id_raw = params.get("since_id", 0)
    if isinstance(since_id_raw, bool) or not isinstance(since_id_raw, int):
        return context.make_error(-32602, "since_id must be an integer", request_id)
    since_id = since_id_raw

    limit_raw = params.get("limit", 50)
    if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
        return context.make_error(-32602, "limit must be an integer", request_id)
    if limit_raw < 1:
        return context.make_error(-32602, "limit must be >= 1", request_id)
    limit = min(limit_raw, 200)

    agent_id_filter = params.get("agent_id")
    if agent_id_filter is not None and not isinstance(agent_id_filter, str):
        return context.make_error(-32602, "agent_id must be a string", request_id)

    event_type_filter = params.get("event_type")
    if event_type_filter is not None and not isinstance(event_type_filter, str):
        return context.make_error(-32602, "event_type must be a string", request_id)

    # Cross-agent visibility follows the same gate as search.cross_agent:
    # a plain read principal is scoped to its own stamped agent_id; only
    # cross_agent/govern/operator principals may omit the filter or name
    # another agent (the console's zero-config operator keeps its fleet view).
    principal_agent = getattr(principal, "agent_id", None)
    if principal is None or not allows_cross_agent_recall(principal):
        if agent_id_filter is not None and agent_id_filter != principal_agent:
            return make_capability_denied_error(
                "cross_agent",
                "list_events",
                request_id,
                principal_id=principal_agent,
            )
        agent_id_filter = principal_agent

    db = None
    try:
        db = context.sovereign_db()
        query = (
            "SELECT event_id, agent_id, event_type, content, created_at, thread_id "
            "FROM episodic_events WHERE event_id > :since_id"
        )
        bindings: dict = {"since_id": since_id, "limit": limit}
        if agent_id_filter is not None:
            query += " AND agent_id = :agent_id"
            bindings["agent_id"] = agent_id_filter
        if event_type_filter is not None:
            query += " AND event_type = :event_type"
            bindings["event_type"] = event_type_filter
        query += " ORDER BY event_id ASC LIMIT :limit"

        with db.cursor() as c:
            c.execute(query, bindings)
            rows = c.fetchall()

        events = [
            {
                "event_id": row["event_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "content": row["content"],
                "created_at": row["created_at"],
                "thread_id": row["thread_id"],
            }
            for row in rows
        ]
        last_id = events[-1]["event_id"] if events else since_id
        return context.make_response({
            "events": events,
            "last_id": last_id,
        }, request_id)
    except Exception as exc:
        context.logger.exception("list_events failed")
        return context.make_error(-32000, f"list_events error: {exc}", request_id)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass
        context.record_latency("list_events", time.perf_counter() - started_at)


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
            # Reads NON_MEMORY_EVENT_TYPES rather than hardcoding 'recall': a
            # third literal here would drift from the search filter and the
            # episodic coverage metric the moment a trace type is added.
            from minni.episodic import NON_MEMORY_EVENT_TYPES

            _traces = ",".join("?" * len(NON_MEMORY_EVENT_TYPES))
            c.execute(f"""
                SELECT event_type, content, created_at
                FROM episodic_events
                WHERE agent_id = ?
                  AND event_type NOT IN ({_traces})
                ORDER BY created_at DESC
                LIMIT 5
            """, (agent_id, *NON_MEMORY_EVENT_TYPES))
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
