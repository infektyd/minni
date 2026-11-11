import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger("minnid")

MAX_VAULT_PAGE_CHARS = 256 * 1024


@dataclass(frozen=True)
class VaultIndexContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    make_mismatch_error: Callable[[str, str, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    guard_vault_root: Callable[..., Optional[dict]]
    lazy_retrieval: Callable[[], Any]
    agent_vault: Callable[[str], tuple[Path, bool]]
    record_latency: Callable[[str, float], None]
    increment_request_count: Callable[[], None] | None = None
    logger: logging.Logger = logger


def handle_vault_index_doc(params: dict, request_id: Any, context: VaultIndexContext) -> dict:
    """Index a vault page into the semantic recall index without going through
    the learn/candidate governance pipeline.

    M-4 fix: minni_vault_write writes a page to disk but did NOT trigger the
    recall bridge, so vault_write content was NOT semantically recall-able until
    a separate VaultIndexer run. This RPC lets the TypeScript MCP handler call
    ``vault_index_doc`` immediately after writing, matching the instant-recall
    semantics of ``learn`` (which calls index_durable_document on promote).

    Params (all required unless noted):
        content    — full page content (markdown, including frontmatter if any)
        path       — relative path within the vault (e.g. "wiki/sessions/foo.md")
        agent      — agent_id owning this document
        sigil      — optional emoji sigil (default "❓")
        privacy_level — optional, default "safe"
        page_status   — optional, default "accepted"
        layer      — optional, default "knowledge"

    Returns {"status": "ok", "doc_id": <int>, "chunks": <int>} on success.
    FAIL-OPEN: engine errors are caught internally (never raises); returns an
    error envelope only for missing required params.
    """
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    content = params.get("content", "")
    path = params.get("path", "")
    wire_agent = str(params.get("agent", "")).strip()
    if wire_agent and wire_agent != principal.agent_id:
        return context.make_mismatch_error(wire_agent, principal.agent_id, request_id)
    agent = principal.agent_id

    if not content:
        return context.make_error(-32602, "content is required", request_id)
    if not path:
        return context.make_error(-32602, "path is required", request_id)

    raw_rel = str(path).replace("\\", "/").lstrip("/")
    raw_parts = raw_rel.split("/")
    if not raw_rel or any(seg == ".." for seg in raw_parts):
        return context.make_error(-32602, "path must be a relative path within the vault", request_id)
    rel_parts = [seg for seg in raw_parts if seg and seg != "."]
    if not rel_parts:
        return context.make_error(-32602, "path must be a relative path within the vault", request_id)
    if rel_parts[0].lower() == "maps":
        return context.make_error(
            -32003,
            "vault_index_doc cannot write or index into reserved maps/ directory",
            request_id,
        )
    rel_path = "/".join(rel_parts)

    vault_path = params.get("vault_path")
    if not vault_path:
        vault_path, _ = context.agent_vault(principal.agent_id)
    vault_path = str(vault_path)
    full_path = Path(vault_path).resolve() / rel_path
    for idx, seg in enumerate(full_path.parts[:-1]):
        if seg == "vault" and full_path.parts[idx + 1].lower() == "maps":
            return context.make_error(
                -32003,
                "vault_index_doc cannot write or index into reserved vault/maps/ directory",
                request_id,
            )
    root_err = context.guard_vault_root(params, full_path, request_id, label="vault_index_doc")
    if root_err:
        return root_err

    # SEC-015-style size cap: vault pages capped at 256 KiB (larger than learn
    # which is 64 KiB, because vault pages include rich frontmatter + prose).
    if len(content) > MAX_VAULT_PAGE_CHARS:
        return context.make_error(
            -32602,
            f"vault_index_doc content exceeds {MAX_VAULT_PAGE_CHARS} chars",
            request_id,
        )

    sigil = str(params.get("sigil", "❓"))
    privacy_level = str(params.get("privacy_level", "safe"))
    page_status = str(params.get("page_status", "accepted"))
    layer = str(params.get("layer", "knowledge"))

    from minni.indexer import (
        UNEMBEDDED_STATUSES,
        extract_explicit_frontmatter_page_id,
        extract_explicit_frontmatter_status,
        is_vault_path_excluded,
    )

    def _retract_existing_if_any() -> bool:
        eng = context.lazy_retrieval()
        db_obj = getattr(eng, "db", None) if eng is not None else None
        if db_obj is None:
            return False
        retracted_any = False
        old_chunk_ids: list[int] = []
        with db_obj.transaction() as c:
            for p_candidate in {str(full_path), str(rel_path)}:
                rows = c.execute(
                    "SELECT doc_id FROM documents WHERE path = ?",
                    (p_candidate,),
                ).fetchall()
                for r in rows:
                    did = int(r["doc_id"] if hasattr(r, "keys") else r[0])
                    chunk_rows = c.execute(
                        "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                        (did,),
                    ).fetchall()
                    for cr in chunk_rows:
                        old_chunk_ids.append(
                            int(cr["chunk_id"] if hasattr(cr, "keys") else cr[0])
                        )
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (did,))
                    c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (did,))
                    c.execute(
                        "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
                        (did, did),
                    )
                    c.execute("DELETE FROM documents WHERE doc_id = ?", (did,))
                    retracted_any = True
        if retracted_any and eng is not None:
            from types import SimpleNamespace

            if isinstance(eng, SimpleNamespace):
                for name, args in (
                    ("_invalidate_durable_rerank", (old_chunk_ids,)),
                    ("_remove_live_faiss", (old_chunk_ids,)),
                    ("_invalidate_trie", ()),
                ):
                    fn = getattr(eng, name, None)
                    if callable(fn):
                        fn(*args)
            else:
                eng._invalidate_durable_rerank(old_chunk_ids)
                eng._remove_live_faiss(old_chunk_ids)
                eng._invalidate_trie()
        return retracted_any

    explicit_status = extract_explicit_frontmatter_status(content)
    explicit_page_id = extract_explicit_frontmatter_page_id(content)
    path_excluded, path_reason = is_vault_path_excluded(
        full_path,
        root_dir=vault_path,
        explicit_status=explicit_status,
        explicit_page_id=explicit_page_id,
        is_rpc=True,
    )
    if path_excluded:
        try:
            retracted = _retract_existing_if_any()
        except Exception as exc:
            context.logger.exception("vault_index_doc retract failed")
            return context.make_error(-32000, f"vault_index_doc error: {exc}", request_id)
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(
            {
                "status": "skipped",
                "indexed": False,
                "skipped": True,
                "retracted": retracted,
                "reason": "excluded_path",
                "detail": path_reason,
                "chunks": 0,
            },
            request_id,
        )

    effective_status = (
        explicit_status
        if explicit_status is not None
        else str(params.get("page_status") or "accepted").strip().lower()
    )
    if effective_status in UNEMBEDDED_STATUSES:
        try:
            retracted = _retract_existing_if_any()
        except Exception as exc:
            context.logger.exception("vault_index_doc retract failed")
            return context.make_error(-32000, f"vault_index_doc error: {exc}", request_id)
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(
            {
                "status": "skipped",
                "indexed": False,
                "skipped": True,
                "retracted": retracted,
                "reason": f"excluded_status:{effective_status}",
                "chunks": 0,
            },
            request_id,
        )

    try:
        engine = context.lazy_retrieval()
        # Retract any legacy relative-path row so canonical full_path does not duplicate it
        try:
            db_obj = getattr(engine, "db", None)
            if db_obj is not None and str(rel_path) != str(full_path):
                with db_obj.transaction() as c:
                    rel_rows = c.execute(
                        "SELECT doc_id FROM documents WHERE path = ?", (rel_path,)
                    ).fetchall()
                    for r in rel_rows:
                        did = int(r["doc_id"] if hasattr(r, "keys") else r[0])
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (did,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (did,))
                        c.execute("DELETE FROM documents WHERE doc_id = ?", (did,))
        except Exception:
            pass
        result = engine.index_durable_document(
            content=content,
            path=str(full_path),
            agent=agent,
            sigil=sigil,
            privacy_level=privacy_level,
            page_status=page_status,
            layer=layer,
        )
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("vault_index_doc failed")
        return context.make_error(-32000, f"vault_index_doc error: {exc}", request_id)


def _validate_rpc_repo_map_root(
    repo_root: Path,
    raw_repo_path: Any,
    *,
    minni_home: Path,
    canonical_home: Path,
    is_operator: bool,
    effective_vault_roots: list[str],
) -> Optional[str]:
    """Return an error reason string when *repo_root* is unsafe for RPC ``repo_map``."""
    if raw_repo_path is not None and str(raw_repo_path).strip():
        raw_expanded = Path(str(raw_repo_path).strip()).expanduser()
        if any(part.startswith(".") and part not in (".", "..") for part in raw_expanded.parts):
            return f"Refusing hidden dot-directory repository path: {raw_repo_path}"

    if any(part.startswith(".") and part not in (".", "..") for part in repo_root.parts):
        return f"Refusing hidden dot-directory repository target: {repo_root}"

    forbidden_exact: set[Path] = {Path("/")}
    for sys_p in ("~", "/etc", "/var", "/private", "/tmp", "/Users", "/home", "/root", "/System", "/Library"):
        ep = Path(sys_p).expanduser()
        forbidden_exact.add(ep)
        try:
            forbidden_exact.add(ep.resolve())
        except OSError:
            pass
    if repo_root in forbidden_exact:
        return f"Refusing sensitive or system root for repo_map: {repo_root}"

    try:
        etc_real = Path("/etc").resolve()
        if repo_root.is_relative_to(etc_real) or repo_root.is_relative_to(Path("/etc")):
            return f"Refusing system configuration directory for repo_map: {repo_root}"
    except OSError:
        pass

    for mh in (minni_home, canonical_home):
        try:
            mh_real = mh.expanduser().resolve()
            if repo_root == mh_real or repo_root.is_relative_to(mh_real):
                return f"Refusing Minni home/vault directory for repo_map: {repo_root}"
        except OSError:
            pass

    if not is_operator:
        for vr in effective_vault_roots:
            try:
                vr_real = Path(vr).expanduser().resolve()
                if repo_root == vr_real or repo_root.is_relative_to(vr_real):
                    return f"Refusing vault directory for repo_map: {repo_root}"
            except OSError:
                continue

    git_marker = repo_root / ".git"
    try:
        if not git_marker.exists() or git_marker.is_symlink():
            return f"Repository target must be a git repository (.git missing): {repo_root}"
    except OSError:
        return f"Repository target must be a git repository (.git unreadable): {repo_root}"

    return None


def handle_repo_map(params: dict, request_id: Any, context: VaultIndexContext) -> dict:
    """Synthesize and index Living Repo Map cards into SQLite + live RetrievalEngine."""
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    from minni.principal import PLATFORM_NO_ROOTS_SENTINEL, is_operator_principal

    is_op = is_operator_principal(principal)
    effective_roots = [
        str(r)
        for r in (principal.allowed_vault_roots or [])
        if r and str(r) != PLATFORM_NO_ROOTS_SENTINEL
    ]
    if not effective_roots and (not is_op or bool(principal.allowed_vault_roots)):
        return context.make_error(
            -32004,
            f"Principal {principal.agent_id!r} lacks valid allowed_vault_roots for repo_map",
            request_id,
        )

    raw_repo_path = params.get("repo_path") or params.get("path")
    raw_workspace = params.get("workspace_id") or params.get("workspace")
    refresh = bool(params.get("refresh", False))

    try:
        from minni.config import CANONICAL_SOVEREIGN_HOME, normalize_workspace_scope
        from minni.repo_map import (
            _effective_minni_home,
            build_and_index_repo_map,
            resolve_repo_root,
            workspace_slug_from_id,
        )

        repo_root = resolve_repo_root(raw_repo_path)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)

    try:
        engine = context.lazy_retrieval()
    except Exception as exc:
        context.logger.exception("repo_map retrieval init failed")
        return context.make_error(-32000, f"repo_map error: {exc}", request_id)

    resolved_home = _effective_minni_home(engine=engine)
    canonical_home = Path(CANONICAL_SOVEREIGN_HOME).expanduser()
    root_violation = _validate_rpc_repo_map_root(
        repo_root,
        raw_repo_path,
        minni_home=resolved_home,
        canonical_home=canonical_home,
        is_operator=is_op,
        effective_vault_roots=effective_roots,
    )
    if root_violation is not None:
        return context.make_error(-32003, root_violation, request_id)

    canonical_ws = normalize_workspace_scope(repo_root.name, default="")
    if not canonical_ws or canonical_ws in ("default", "global", "*", "workspace-unknown"):
        return context.make_error(
            -32602,
            f"Repository directory name {repo_root.name!r} resolves to a reserved workspace scope",
            request_id,
        )

    raw_ws_str = str(raw_workspace).strip() if raw_workspace is not None else ""
    p_ws = str(getattr(principal, "workspace_id", "") or "default").strip() or "default"

    if not is_op:
        if raw_ws_str:
            supplied_ws = normalize_workspace_scope(raw_ws_str, default="")
            if supplied_ws != canonical_ws:
                return context.make_error(
                    -32003,
                    f"workspace_mismatch: non-operator principal {principal.agent_id!r} cannot override workspace {canonical_ws!r} with {raw_ws_str!r}",
                    request_id,
                )
        target_ws = canonical_ws
        if p_ws not in ("*", "default", "global") and p_ws != canonical_ws:
            return context.make_error(
                -32004,
                f"Principal {principal.agent_id!r} is pinned to workspace {p_ws!r} and cannot map {canonical_ws!r}",
                request_id,
            )

        db_obj = getattr(engine, "db", None)
        resolved_target_root = repo_root.resolve()
        if db_obj is not None:
            with db_obj.cursor() as c:
                has_tbl = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_map_cards'"
                ).fetchone()
                if has_tbl is not None:
                    existing_rows = c.execute(
                        "SELECT DISTINCT repo_root FROM repo_map_cards WHERE workspace_id = ?",
                        (target_ws,),
                    ).fetchall()
                    for r in existing_rows:
                        existing_raw = str((r["repo_root"] if hasattr(r, "keys") else r[0]) or "").strip()
                        if not existing_raw:
                            continue
                        try:
                            existing_resolved = Path(existing_raw).expanduser().resolve()
                        except OSError:
                            existing_resolved = Path(existing_raw)
                        if existing_resolved != resolved_target_root:
                            return context.make_error(
                                -32003,
                                f"workspace_clobber_denied: workspace {target_ws!r} is already mapped to a different repository",
                                request_id,
                            )
        idx_file = resolved_home / "vault" / "maps" / workspace_slug_from_id(target_ws) / "_index.json"
        if idx_file.is_file():
            try:
                import json

                idx_data = json.loads(idx_file.read_text(encoding="utf-8"))
                idx_root = str((idx_data or {}).get("repo_root") or "").strip()
                if idx_root and Path(idx_root).expanduser().resolve() != resolved_target_root:
                    return context.make_error(
                        -32003,
                        f"workspace_clobber_denied: workspace {target_ws!r} is already mapped to a different repository",
                        request_id,
                    )
            except Exception:
                pass
    else:
        supplied_ws = normalize_workspace_scope(raw_ws_str, default="") if raw_ws_str else ""
        target_ws = (
            supplied_ws
            if supplied_ws and supplied_ws not in ("default", "global", "*")
            else canonical_ws
        )
        if p_ws not in ("*", "default", "global") and p_ws != target_ws:
            return context.make_error(
                -32004,
                f"Principal {principal.agent_id!r} is pinned to workspace {p_ws!r} and cannot map {target_ws!r}",
                request_id,
            )

    try:
        result = build_and_index_repo_map(
            repo_root,
            workspace_id=target_ws,
            refresh=refresh,
            db=getattr(engine, "db", None),
            engine=engine,
        )
        context.record_latency("repo_map", time.perf_counter() - started_at)
        return context.make_response(result, request_id)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)
    except Exception as exc:
        context.logger.exception("repo_map failed")
        return context.make_error(-32000, f"repo_map error: {exc}", request_id)
