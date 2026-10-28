"""Consolidation pass for the opt-in AFM loop.

The pass that actually MOVES candidates along: it triages proposed
`candidate_packets` and decides which are safe to promote into durable
`learnings`, which are duplicates to drop, and which need a human's eyes.

Design notes:
- Deterministic v1 (no model call). An AFM scoring hook can be added behind this
  contract later, exactly like the other passes started deterministic.
- The pass NEVER performs the durable write itself. It returns decisions; the
  daemon applies promote/dedup/review via `_apply_consolidation_result` so the
  privileged write + embedding stay in one audited place (`minnid.py`).
- Conservative: `privacy: safe` auto-promotes. Learn-only `privacy: review`
  (the stage_candidate clamp) is AFM-examinable — quality / IL / dedup
  still drain — but quality-pass review stays parked for
  ``resolve_candidate``. Unset/NULL privacy is still parked (I1/I2).
  Instruction-like / duplicate / quality failures still route to review +
  fence. An active fence hides the row from the next drain so the loop
  makes progress. A one-shot unpark lifts fences only on quality-pass,
  non-IL ``privacy=review`` rows.
- Swarm-scale:
    * Dedup is an INDEXED hash lookup (`content_hash`), not an O(N) in-memory
      scan of all learnings. Duplicate volume costs ~O(log N), so a swarm
      hammering the queue with copies is cheap to absorb.
    * Every decision moves the candidate OUT of `proposed` (accepted / rejected /
      needs_review), so the daemon's per-tick drain loop strictly makes progress
      and can clear bursts instead of trickling.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from minni.safety import is_instruction_like

logger = logging.getLogger("sovereign.afm.consolidation")

# NOTE: "" (unset/NULL privacy) is deliberately excluded. Current writers stamp
# explicit privacy, but legacy/other paths can still produce NULL. Unknown
# privacy is NOT the same as "known safe" and must never auto-promote to durable
# learnings without a review pass (I1/I2 security fix).
_SAFE_PRIVACY = {"safe", "public", "low"}
_STRICT_PRIVATE = {"private", "local-only", "secret", "blocked"}
# Learn-only stage_candidate clamps non-operator rows to privacy=review.
# That label means "AFM must filter", not "park forever behind afm_review"
# and not "auto-promote into learnings/writeback as safe". Unset/NULL
# stays out of this set (I1/I2). Auto-promote still requires _SAFE_PRIVACY.
_EXAMINABLE_PRIVACY = _SAFE_PRIVACY | {"review"}
_MIN_CONTENT_LEN = 12
_DEFAULT_MAX_PER_RUN = 50


def is_judge_eligible(privacy_level: Optional[str], requested_privacy: Optional[str]) -> bool:
    """Return True if candidate is eligible for AFM Model Judge evaluation.

    Candidate must have privacy_level == 'review' (clamped non-operator author)
    AND requested_privacy in _SAFE_PRIVACY (author explicitly intended to share).
    Explicit private/local-only, explicit review, and legacy NULL rows are NOT judge-eligible.
    """
    p = (privacy_level or "").strip().lower()
    r = (requested_privacy or "").strip().lower()
    return p == "review" and r in _SAFE_PRIVACY


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace — the canonical form for dedup."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def content_hash(content: str) -> str:
    """Stable hash of normalized content. Shared with the daemon promote path
    so promoted learnings carry the same hash the dedup index looks up."""
    return hashlib.sha1(_normalize(content).encode("utf-8")).hexdigest()


_RAW_PROMPT_ECHO_RE = re.compile(
    r"^\s*(?:executed\s+prompt|user\s+prompt|raw\s+prompt|system\s+prompt|prompt\s+transcript)\s*:",
    re.IGNORECASE,
)
_RAW_TEST_OUTPUT_RE = re.compile(
    r"(?:^\s*=+\s*\d+\s+(?:passed|failed|skipped|error|errors|deselected)\b.*=+\s*$|^\s*(?:ran\s+\d+\s+tests?\s+in\s+[\d.]+s|test\s+suites?:\s+\d+\s+passed(?:\b.*)?|\d+\s+passed(?:,\s+\d+\s+\w+)*\s+in\s+[\d.]+s)\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def is_low_signal_boot_learning(content: str) -> bool:
    """Return True if content is blank, a raw prompt echo, raw test runner output,
    or an audit telemetry echo unsuitable for the boot context envelope."""
    text = (content or "").strip() if isinstance(content, str) else ""
    if not text:
        return True
    if _RAW_PROMPT_ECHO_RE.search(content) or _RAW_TEST_OUTPUT_RE.search(content):
        return True
    from minni.afm_passes.inbox_ingest import is_audit_echo

    return is_audit_echo(content)


def _quality_blockers(content: str) -> List[str]:
    """Structural quality gate. Returns reasons to WITHHOLD auto-promotion
    (the candidate is routed to review, never silently dropped).

    Scope: catches filler / gibberish / oversized blobs. It intentionally does
    NOT reject plausible short facts — those are indistinguishable from real
    lessons by structure alone and are handled by de-seeding (the purge), not here.
    """
    reasons: List[str] = []
    text = (content or "").strip()
    if len(text) > 8000:
        reasons.append("oversized blob")
    if _RAW_PROMPT_ECHO_RE.search(text):
        reasons.append("raw prompt echo")
    if _RAW_TEST_OUTPUT_RE.search(text):
        reasons.append("raw test output telemetry")
    alnum_chars = {ch.lower() for ch in text if ch.isalnum()}
    if len(text) > 10 and len(alnum_chars) <= 2:
        reasons.append("low character diversity (filler)")
    words = re.findall(r"[A-Za-z0-9']+", text)
    if len({w.lower() for w in words}) < 2:
        reasons.append("too few distinct words")
    letters = sum(ch.isalpha() for ch in text)
    if len(text) > 20 and letters / max(1, len(text)) < 0.4:
        reasons.append("low alphabetic ratio")
    return reasons


def _ensure_dedup_index(db) -> None:
    """Idempotently ensure the `learnings.content_hash` column + index exist and
    backfill any NULLs. Cheap after first run (only newly-inserted rows are NULL).
    Normalization needs regex, so the hash is computed in Python, not SQL."""
    with db.cursor() as c:
        c.execute("PRAGMA table_info(learnings)")
        cols = [r["name"] for r in c.fetchall()]
        if "content_hash" not in cols:
            c.execute("ALTER TABLE learnings ADD COLUMN content_hash TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_learnings_content_hash "
            "ON learnings(content_hash)"
        )
    with db.cursor() as c:
        c.execute("SELECT learning_id, content FROM learnings WHERE content_hash IS NULL")
        nulls = [dict(r) for r in c.fetchall()]
    if nulls:
        with db.transaction() as c:
            for r in nulls:
                c.execute(
                    "UPDATE learnings SET content_hash=? WHERE learning_id=?",
                    (content_hash(r["content"] or ""), r["learning_id"]),
                )


def _learning_row_to_dict(r: Any) -> Dict[str, Any]:
    if isinstance(r, dict) or hasattr(r, "keys"):
        return dict(r)
    return {
        "learning_id": r[0],
        "category": r[1],
        "content": r[2],
        "status": r[3],
    }


def find_active_learning_by_hash(
    cursor, h: str, agent_id: str
) -> Optional[Dict[str, Any]]:
    """Return the newest active un-superseded learning matching (agent_id, h), or None."""
    pragma = cursor.execute("PRAGMA table_info(learnings)").fetchall()
    cols = {
        r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
        for r in pragma
    }
    status_col = "status" if "status" in cols else "'active' AS status"
    status_where = (
        "AND COALESCE(status, 'active') = 'active'" if "status" in cols else ""
    )
    sup_where = "AND superseded_by IS NULL" if "superseded_by" in cols else ""

    if "content_hash" in cols:
        cursor.execute(
            f"""
            SELECT learning_id, category, content, {status_col}
            FROM learnings
            WHERE content_hash = ? AND agent_id = ?
              {status_where}
              {sup_where}
            ORDER BY created_at DESC, learning_id DESC
            LIMIT 1
            """,
            (h, agent_id),
        )
        row = cursor.fetchone()
        if row is not None:
            return _learning_row_to_dict(row)
        cursor.execute(
            f"""
            SELECT learning_id, category, content, {status_col}
            FROM learnings
            WHERE content_hash IS NULL AND agent_id = ?
              {status_where}
              {sup_where}
            ORDER BY created_at DESC, learning_id DESC
            """,
            (agent_id,),
        )
        for r in cursor.fetchall():
            rd = _learning_row_to_dict(r)
            if content_hash(rd["content"] or "") == h:
                return rd
    else:
        cursor.execute(
            f"""
            SELECT learning_id, category, content, {status_col}
            FROM learnings
            WHERE agent_id = ?
              {status_where}
              {sup_where}
            ORDER BY created_at DESC, learning_id DESC
            """,
            (agent_id,),
        )
        for r in cursor.fetchall():
            rd = _learning_row_to_dict(r)
            if content_hash(rd["content"] or "") == h:
                return rd
    return None


def has_active_learning_hash(cursor, h: str, agent_id: str) -> bool:
    """Duplicate predicate shared by AFM and operator-enabled auto-accept.

    A terminal row or another owner's memory cannot replace this owner's
    learning. Accept an existing cursor so auto-accept keeps its check inside
    the write transaction rather than opening a cursor that commits it early.
    """
    return find_active_learning_by_hash(cursor, h, agent_id) is not None


def dedup_active_learnings(
    db,
    agent_id: Optional[str] = None,
    *,
    evict_to_archive: Optional[bool] = None,
) -> Dict[str, int]:
    """Evict duplicate active learnings (keeping the newest learning_id per
    (agent_id, content_hash) or near-duplicate cluster) and prune low-signal boot rows."""
    from minni.durable_projection import (
        _cosine_similarity_blobs,
        evict_learning_to_cold_archive,
        five_gram_jaccard,
    )

    _ensure_dedup_index(db)
    with db.cursor() as c:
        pragma = c.execute("PRAGMA table_info(learnings)").fetchall()
        l_cols = {
            r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
            for r in pragma
        }
        has_cat = "category" in l_cols
        has_emb = "embedding" in l_cols
        cat_sel = "category, " if has_cat else "'general' AS category, "
        emb_sel = "embedding, " if has_emb else "NULL AS embedding, "
        query = (
            f"SELECT learning_id, agent_id, {cat_sel}{emb_sel}content, content_hash "
            "FROM learnings "
            "WHERE COALESCE(status, 'active') = 'active' "
            "  AND superseded_by IS NULL"
        )
        params: list[Any] = []
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY created_at DESC, learning_id DESC"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]

    winners: Dict[tuple[str, str], int] = {}
    winner_entries_by_group: Dict[tuple[str, str], List[tuple[int, str, Any]]] = {}
    dup_updates: List[tuple[int, int]] = []
    low_sig_ids: List[int] = []

    for row in rows:
        lid = int(row["learning_id"])
        text = str(row.get("content") or "")
        if is_low_signal_boot_learning(text):
            low_sig_ids.append(lid)
            continue
        owner = str(row.get("agent_id") or "")
        cat = str(row.get("category") or "general").strip().lower()
        emb = row.get("embedding")
        h = str(row.get("content_hash") or "") or content_hash(text)
        key = (owner, h)
        if key in winners:
            dup_updates.append((winners[key], lid))
            continue

        group_key = (owner, cat)
        matched_winner: Optional[int] = None
        for w_lid, w_text, w_emb in winner_entries_by_group.get(group_key, []):
            if five_gram_jaccard(text, w_text) >= 0.65:
                matched_winner = w_lid
                break
            if emb is not None and w_emb is not None and _cosine_similarity_blobs(emb, w_emb) >= 0.88:
                matched_winner = w_lid
                break
        if matched_winner is not None:
            dup_updates.append((matched_winner, lid))
            continue

        winners[key] = lid
        winner_entries_by_group.setdefault(group_key, []).append((lid, text, emb))

    cfg = getattr(db, "config", None)
    db_path = getattr(cfg, "db_path", None)
    vault_path = getattr(cfg, "vault_path", None)
    should_evict = (
        evict_to_archive
        if evict_to_archive is not None
        else bool(getattr(cfg, "writeback_enabled", True))
    )

    if dup_updates or low_sig_ids:
        with db.transaction() as c:
            if should_evict:
                for winner_id, dup_id in dup_updates:
                    evict_learning_to_cold_archive(
                        c,
                        None,
                        dup_id,
                        reason=f"dedup_active_learnings:superseded_by:{winner_id}",
                        db_path=db_path,
                        vault_path=vault_path,
                    )
                for low_id in low_sig_ids:
                    evict_learning_to_cold_archive(
                        c,
                        None,
                        low_id,
                        reason="dedup_active_learnings:low_signal",
                        db_path=db_path,
                        vault_path=vault_path,
                    )
            else:
                for winner_id, dup_id in dup_updates:
                    c.execute(
                        "UPDATE learnings SET superseded_by = ?, status = 'superseded' "
                        "WHERE learning_id = ? AND superseded_by IS NULL",
                        (winner_id, dup_id),
                    )
                for low_id in low_sig_ids:
                    c.execute(
                        "UPDATE learnings SET status = 'rejected' "
                        "WHERE learning_id = ? AND superseded_by IS NULL",
                        (low_id,),
                    )

    return {
        "superseded_duplicates": len(dup_updates),
        "pruned_low_signal": len(low_sig_ids),
        "examined": len(rows),
    }


def _hash_in_learnings(db, h: str, agent_id: str) -> bool:
    with db.cursor() as c:
        return has_active_learning_hash(c, h, agent_id)


def _proposed_candidates(db, limit: int) -> List[Dict[str, Any]]:
    # Exclude candidates already flagged for review (status stays 'proposed' —
    # the schema CHECK forbids new statuses — but an 'afm_review' marker keeps
    # them from being re-examined/re-drafted, so the drain loop makes progress).
    with db.cursor() as c:
        pragma = c.execute("PRAGMA table_info(candidate_packets)").fetchall()
        cols = {row["name"] if isinstance(row, dict) else row[1] for row in pragma}
        req_col = "requested_privacy, " if "requested_privacy" in cols else "NULL AS requested_privacy, "
        cat_col = "category, " if "category" in cols else "NULL AS category, "
        df_col = "derived_from, " if "derived_from" in cols else "NULL AS derived_from, "
        src_col = "source, " if "source" in cols else "NULL AS source, "
        orig_col = "origin, " if "origin" in cols else "NULL AS origin, "
        c.execute(
            f"""
            SELECT candidate_id, principal, workspace_id, layer, privacy_level,
                   {req_col}{cat_col}{df_col}{src_col}{orig_col}
                   content, instruction_like, proposed_at
            FROM candidate_packets cp
            WHERE cp.status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM consolidation_actions ca
                  WHERE ca.action_type IN ('afm_review', 'afm_judge_reject', 'afm_judge_redact')
                    AND ca.claim = CAST(cp.candidate_id AS TEXT)
                    AND COALESCE(ca.status, '') != 'superseded'
              )
            ORDER BY cp.proposed_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in c.fetchall()]


def _extract_candidate_provenance(cand: Dict[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    cat = str(cand.get("category") or "general").strip().lower()
    source: Optional[str] = cand.get("source")
    origin: Optional[str] = cand.get("origin")
    raw_df = cand.get("derived_from")
    if isinstance(raw_df, str) and raw_df.strip():
        try:
            df_obj = json.loads(raw_df)
        except Exception:
            df_obj = None
    elif isinstance(raw_df, dict):
        df_obj = raw_df
    else:
        df_obj = None
    if isinstance(df_obj, dict):
        if not source and df_obj.get("source"):
            source = str(df_obj.get("source"))
        if not origin and df_obj.get("origin"):
            origin = str(df_obj.get("origin"))
    return (
        cat,
        str(source).strip() if source is not None and str(source).strip() else None,
        str(origin).strip() if origin is not None and str(origin).strip() else None,
    )


def expire_and_compact_candidates(
    db: Any,
    *,
    now: Optional[float] = None,
    retention_seconds: float = 7 * 86400.0,
    config: Any = None,
    archive_callback: Optional[Any] = None,
) -> Dict[str, int]:
    """Cleanup expired candidates and truncate content on resolved/rejected/expired rows older than 7 days."""
    from minni.afm_review_markers import supersede_afm_review

    ts_now = float(now if now is not None else time.time())
    cutoff = ts_now - float(retention_seconds)
    expired_cleaned = 0
    truncated_count = 0
    expired_cids: List[int] = []

    with db.transaction() as c:
        try:
            pragma = c.execute("PRAGMA table_info(candidate_packets)").fetchall()
        except Exception:
            return {"expired_cleaned": 0, "truncated": 0}
        if not pragma:
            return {"expired_cleaned": 0, "truncated": 0}
        cols = {
            r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
            for r in pragma
        }
        has_content_hash = "content_hash" in cols
        has_resolved_at = "resolved_at" in cols

        exp_rows = c.execute(
            "SELECT candidate_id FROM candidate_packets WHERE status = 'expired'"
        ).fetchall()
        for r in exp_rows:
            cid = int(r["candidate_id"] if hasattr(r, "keys") else r[0])
            expired_cids.append(cid)
            supersede_afm_review(c, cid)
            if has_resolved_at:
                c.execute(
                    "UPDATE candidate_packets SET resolved_at = COALESCE(resolved_at, ?) WHERE candidate_id = ?",
                    (ts_now, cid),
                )
            expired_cleaned += 1

        terminal_statuses = (
            "accepted",
            "rejected",
            "expired",
            "redacted",
            "do_not_store",
            "log_only",
            "merged",
            "superseded",
        )
        placeholders = ", ".join("?" * len(terminal_statuses))
        if has_resolved_at:
            old_rows = c.execute(
                f"""
                SELECT candidate_id, content
                FROM candidate_packets
                WHERE status IN ({placeholders})
                  AND COALESCE(resolved_at, proposed_at, 0) > 0
                  AND COALESCE(resolved_at, proposed_at, 0) < ?
                  AND COALESCE(content, '') != ''
                """,
                (*terminal_statuses, cutoff),
            ).fetchall()
        else:
            old_rows = c.execute(
                f"""
                SELECT candidate_id, content
                FROM candidate_packets
                WHERE status IN ({placeholders})
                  AND COALESCE(proposed_at, 0) > 0
                  AND COALESCE(proposed_at, 0) < ?
                  AND COALESCE(content, '') != ''
                """,
                (*terminal_statuses, cutoff),
            ).fetchall()

        for r in old_rows:
            cid = int(r["candidate_id"] if hasattr(r, "keys") else r[0])
            raw_text = str((r["content"] if hasattr(r, "keys") else r[1]) or "")
            if has_content_hash:
                chash = content_hash(raw_text)
                c.execute(
                    """
                    UPDATE candidate_packets
                    SET content = '',
                        content_hash = COALESCE(content_hash, ?)
                    WHERE candidate_id = ?
                    """,
                    (chash, cid),
                )
            else:
                c.execute(
                    "UPDATE candidate_packets SET content = '' WHERE candidate_id = ?",
                    (cid,),
                )
            truncated_count += 1

    if expired_cids:
        for cid in expired_cids:
            if callable(archive_callback):
                try:
                    archive_callback(db, cid)
                except Exception:
                    pass
            else:
                try:
                    from minni.minnid_runtime.afm import maybe_archive_inbox_source

                    maybe_archive_inbox_source(
                        db,
                        cid,
                        config or getattr(db, "config", None),
                        logging.getLogger("minnid"),
                    )
                except Exception:
                    pass

    return {"expired_cleaned": expired_cleaned, "truncated": truncated_count}


def _total_proposed(db) -> int:
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets WHERE status = 'proposed'")
        return int(c.fetchone()["n"])


_DRAFT_PRIVACY = {"safe", "public", "low", "review", "private", "local-only", "blocked"}


def _draft_privacy(candidate: Dict[str, Any]) -> str:
    """Wiki-page privacy for a review draft. Learn-only review / NULL / unknown
    must not compile as fleet-readable ``privacy: safe``."""
    raw = candidate.get("privacy_level")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "review"
    privacy = str(raw).strip().lower()
    if privacy in _SAFE_PRIVACY:
        return "safe"
    return privacy if privacy in _DRAFT_PRIVACY else "review"


def _review_draft(candidate: Dict[str, Any], reason: str, trace_id: str) -> Dict[str, Any]:
    cid = candidate["candidate_id"]
    privacy = _draft_privacy(candidate)
    content = candidate.get("content") or ""
    instr_like = (
        int(candidate.get("instruction_like") or 0) == 1
        or is_instruction_like(content)
    )
    # Inbox ingest stamps missing privacy as safe, so IL / injection
    # candidates still compile as privacy:safe. Never copy that body
    # into a fleet-readable wiki page.
    if privacy in _SAFE_PRIVACY and not instr_like:
        content_line = f"- Content: {content[:400]}"
    else:
        content_line = "- Content: [withheld; learn-only / non-safe privacy]"
    return {
        "page_id": f"consolidation-review-{cid}-{trace_id}",
        "kind": "concept",
        "section": "concepts",
        "title": f"Candidate #{cid} needs review ({reason})",
        "status": "draft",
        "agent": "afm-loop",
        "privacy": privacy,
        "trace_id": trace_id,
        "sources": [f"candidate_packets:{cid}"],
        "body": (
            f"- Candidate #{cid} held for operator review.\n"
            f"- Reason: {reason}.\n"
            f"- Principal: {candidate.get('principal')}; "
            f"privacy: {candidate.get('privacy_level')}; "
            f"instruction_like: {bool(candidate.get('instruction_like'))}.\n"
            f"{content_line}"
        ),
    }


def _fold_triage_decisions(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic priority-fold over per-chunk triage verdicts:
    any chunk redact -> redact, else any reject -> reject, else accept.

    Triage is a judgment op — re-judging the model's own per-chunk verdicts
    with a second triage call would make the final decision depend on whether
    a reason string happens to echo a trigger word, not on the candidate
    content, silently losing a redact signal."""
    def _decision(p: Dict[str, Any]) -> str:
        return str(p.get("decision") or "").strip().lower()

    winner = None
    for priority in ("redact", "reject"):
        winner = next((p for p in partials if _decision(p) == priority), None)
        if winner is not None:
            break
    if winner is None:
        winner = next((p for p in partials if _decision(p) == "accept"), partials[0])
    hits = sum(1 for p in partials if _decision(p) == _decision(winner))
    return {
        "decision": _decision(winner),
        "reason": f"chunked fold ({hits}/{len(partials)} chunks): {str(winner.get('reason') or '').strip()}",
        "tool_used": any(bool(p.get("tool_used")) for p in partials),
    }


def _triage_advisory(candidates: List[Dict[str, Any]],
                     promote_candidate_ids: List[int],
                     trace_id: str) -> Optional[Dict[str, Any]]:
    """ADVISORY-ONLY native triage signal for one representative candidate.

    Strictly informational: it is attached to the pass output and is NEVER read
    by the deterministic accept/reject/redact gate above (which stays
    deterministic by design — "never reject plausible short facts"). Native-only
    by contract; bridge/off modes no-op.
    """
    # Lazy imports keep the deterministic pass dependency-light and avoid paying
    # provider-chain import cost when AFM is off.
    try:
        from minni.afm_chunking import call_native_op_and_reduce
        from minni.afm_provider import resolve_afm_mode
        from minni.model_provider import default_provider_chain
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if resolve_afm_mode() not in {"native", "auto"}:
        return None
    # Prefer a candidate the deterministic gate already chose to promote, so the
    # advisory is comparable to a real decision; else fall back to the first one.
    # Review / NULL / content-IL never reach the native call — gated below —
    # so the fallback cannot exfiltrate injection text the way an unguarded
    # candidates[0] did when promote_candidate_ids was empty.
    chosen = None
    if promote_candidate_ids:
        promote_set = set(promote_candidate_ids)
        chosen = next((c for c in candidates if c.get("candidate_id") in promote_set), None)
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen is None:
        return None
    promote_set = set(promote_candidate_ids)
    if promote_candidate_ids and chosen.get("candidate_id") not in promote_set:
        return None
    # Native triage is a model-path. Learn-only review (and I1/I2 NULL/unset)
    # stay on the deterministic drain; do not fail-open as "safe". Content
    # that trips is_instruction_like is review-routed even when the column
    # is still 0 and privacy was stamped safe at ingest.
    raw_privacy = chosen.get("privacy_level")
    if raw_privacy is None or (
        isinstance(raw_privacy, str) and not raw_privacy.strip()
    ):
        return None
    privacy = str(raw_privacy).strip().lower()
    if privacy not in _SAFE_PRIVACY:
        return None
    content = (chosen.get("content") or "").strip()
    if not content:
        return None
    if int(chosen.get("instruction_like") or 0) == 1 or is_instruction_like(content):
        return None
    try:
        chain = default_provider_chain()
        result = call_native_op_and_reduce(
            chain, "triage", {"candidate": content}, text_field="candidate",
            fold=_fold_triage_decisions,
            # Advisory generation needs longer than the 4s probe-style budget;
            # on this hardware a real completion routinely exceeds 4s.
            timeout=float(os.environ.get("MINNI_AFM_TRIAGE_TIMEOUT", "30.0")),
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if result is None:
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_id": chosen.get("candidate_id"),
        "decision": str(data.get("decision") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
        "tool_used": bool(data.get("tool_used")),
        "note": "advisory only; deterministic gate decision is authoritative",
    }


def run(db, config, vault_path: Optional[str] = None,
        dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    from minni.minnid_runtime.governance import non_durable_promotion_blocker

    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("consolidation", {})
    max_per_run = int(pass_cfg.get("max_per_run", _DEFAULT_MAX_PER_RUN))
    trace_id = trace_id or f"afm-{int(time.time())}"

    _ensure_dedup_index(db)
    if not dry_run:
        expire_and_compact_candidates(db, config=config)

    total = _total_proposed(db)
    candidates = _proposed_candidates(db, max_per_run)

    seen_this_run: set[tuple[str, str]] = set()
    promote_candidate_ids: List[int] = []
    dedup_candidate_ids: List[int] = []
    review_candidate_ids: List[int] = []
    reject_candidate_ids: List[int] = []
    redact_candidate_ids: List[int] = []
    judge_verdicts: Dict[int, Any] = {}
    drafts: List[Dict[str, Any]] = []

    judge_mode = str(
        pass_cfg.get("judge_mode")
        or getattr(config, "judge_mode", None)
        or os.environ.get("MINNI_JUDGE_MODE", "enforce")
    ).strip().lower()
    max_model_calls = int(pass_cfg.get("max_model_calls_per_batch", 8))
    model_timeout = float(pass_cfg.get("model_timeout", 15.0))
    allow_jev_egress = bool(pass_cfg.get("allow_jev_egress", False))

    model_calls_made = 0
    deferred_budget = 0

    def _review(cand, reason):
        review_candidate_ids.append(cand["candidate_id"])
        drafts.append(_review_draft(cand, reason, trace_id))

    for cand in candidates:
        content = cand.get("content") or ""
        norm = _normalize(content)

        if not norm or len(norm) < _MIN_CONTENT_LEN:
            _review(cand, "too short / trivial")
            continue

        h = content_hash(content)
        # Match the owner promote_candidate_durable writes, including its
        # legacy fallback for packets without a principal.
        from minni.afm_passes.inbox_ingest import _canonical_principal

        agent_id = _canonical_principal(cand.get("principal") or "afm-loop")
        batch_key = (agent_id, h)
        if batch_key in seen_this_run or _hash_in_learnings(db, h, agent_id):
            dedup_candidate_ids.append(cand["candidate_id"])
            continue

        if int(cand["instruction_like"] or 0) == 1 or is_instruction_like(content):
            _review(cand, "instruction_like")
            continue

        privacy = (cand.get("privacy_level") or "").strip().lower()
        if privacy not in _EXAMINABLE_PRIVACY:
            _review(cand, f"privacy={privacy or 'unset'}")
            continue

        blockers = _quality_blockers(content)
        if blockers:
            _review(cand, "low quality: " + "; ".join(blockers))
            continue

        cat, source, origin = _extract_candidate_provenance(cand)
        dur_blocker = non_durable_promotion_blocker(
            content=content,
            category=cat,
            source=source,
            origin=origin,
        )
        if dur_blocker is not None:
            reject_candidate_ids.append(cand["candidate_id"])
            continue

        # If privacy is review, evaluate via authoritative model judge if eligible
        if privacy not in _SAFE_PRIVACY:
            if judge_mode == "off":
                _review(cand, f"privacy={privacy or 'unset'}")
                continue

            req_priv = cand.get("requested_privacy")
            if not is_judge_eligible(privacy, req_priv):
                # Author intent was not public/safe, or legacy unknown
                r_clean = (req_priv or "").strip().lower()
                if r_clean in _STRICT_PRIVATE:
                    _review(cand, f"requested_privacy={r_clean}")
                elif r_clean == "review":
                    _review(cand, "requested_privacy=review")
                elif not r_clean:
                    _review(cand, "legacy_unknown")
                else:
                    _review(cand, f"requested_privacy={r_clean}")
                continue

            if model_calls_made >= max_model_calls:
                # Swarm influx protection: defer excess to next batch to avoid model starvation
                deferred_budget += 1
                continue

            from minni.afm_passes.judge import judge_candidate

            verdict = judge_candidate(
                content=content,
                trace_id=trace_id,
                timeout=model_timeout,
                allow_jev_egress=allow_jev_egress,
            )
            if not verdict.cached:
                model_calls_made += 1

            judge_verdicts[cand["candidate_id"]] = verdict

            if verdict.decision == "accept":
                if judge_mode == "enforce":
                    promote_candidate_ids.append(cand["candidate_id"])
                    seen_this_run.add(batch_key)
                else:  # shadow mode: log verdict, route to review
                    _review(cand, f"afm_judge_shadow: accept ({verdict.reason})")
            elif verdict.decision == "reject":
                reject_candidate_ids.append(cand["candidate_id"])
            elif verdict.decision == "redact":
                redact_candidate_ids.append(cand["candidate_id"])
            else:  # inconclusive / timeout / circuit open
                _review(cand, f"afm_judge_inconclusive: {verdict.reason}")
            continue



        promote_candidate_ids.append(cand["candidate_id"])
        seen_this_run.add(batch_key)

    summary = {
        "total_proposed": total,
        "examined": len(candidates),
        "deferred": max(0, total - len(candidates)),
        "deferred_budget": deferred_budget,
        "to_promote": len(promote_candidate_ids),
        "to_dedup": len(dedup_candidate_ids),
        "to_review": len(review_candidate_ids),
        "to_reject": len(reject_candidate_ids),
        "to_redact": len(redact_candidate_ids),
        "model_calls": model_calls_made,
        "judge_mode": judge_mode,
        "dry_run": dry_run,
    }

    triage_advisory = _triage_advisory(candidates, promote_candidate_ids, trace_id)

    return {
        "pass_name": "consolidation",
        "trace_id": trace_id,
        "inputs": summary,
        "summary": summary,
        "promote_candidate_ids": promote_candidate_ids,
        "dedup_candidate_ids": dedup_candidate_ids,
        "review_candidate_ids": review_candidate_ids,
        "reject_candidate_ids": reject_candidate_ids,
        "redact_candidate_ids": redact_candidate_ids,
        "judge_verdicts": judge_verdicts,
        "drafts": drafts,
        "triage_advisory": triage_advisory,
        "prompt_version": "consolidation-v2-model-judge",
    }


class ConsolidationPass:
    """Object-oriented wrapper around the consolidation pass runner."""

    def __init__(self, db=None, config=None):
        self.db = db
        self.config = config

    @staticmethod
    def run(db=None, config=None, **kwargs) -> Dict[str, Any]:
        return run(db=db, config=config, **kwargs)
