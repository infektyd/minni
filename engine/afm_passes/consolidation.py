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
- Conservative: only `privacy: safe`, non-instruction, non-duplicate,
  quality-passing candidates are proposed for auto-promotion. Everything spicier
  is routed to review (status → `needs_review`) and surfaced as a draft.
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
import logging
import re
import time
from typing import Any, Dict, List, Optional

from safety import is_instruction_like

logger = logging.getLogger("sovereign.afm.consolidation")

_SAFE_PRIVACY = {"", "safe", "public", "low"}
_MIN_CONTENT_LEN = 12
_DEFAULT_MAX_PER_RUN = 50


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace — the canonical form for dedup."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def content_hash(content: str) -> str:
    """Stable hash of normalized content. Shared with the daemon promote path
    so promoted learnings carry the same hash the dedup index looks up."""
    return hashlib.sha1(_normalize(content).encode("utf-8")).hexdigest()


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
                    (content_hash(r["content"]), r["learning_id"]),
                )


def _hash_in_learnings(db, h: str) -> bool:
    with db.cursor() as c:
        c.execute("SELECT 1 FROM learnings WHERE content_hash=? LIMIT 1", (h,))
        return c.fetchone() is not None


def _proposed_candidates(db, limit: int) -> List[Dict[str, Any]]:
    # Exclude candidates already flagged for review (status stays 'proposed' —
    # the schema CHECK forbids new statuses — but an 'afm_review' marker keeps
    # them from being re-examined/re-drafted, so the drain loop makes progress).
    with db.cursor() as c:
        c.execute(
            """
            SELECT candidate_id, principal, workspace_id, layer, privacy_level,
                   content, instruction_like, proposed_at
            FROM candidate_packets cp
            WHERE cp.status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM consolidation_actions ca
                  WHERE ca.action_type = 'afm_review'
                    AND ca.claim = CAST(cp.candidate_id AS TEXT)
              )
            ORDER BY cp.proposed_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in c.fetchall()]


def _total_proposed(db) -> int:
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets WHERE status = 'proposed'")
        return int(c.fetchone()["n"])


def _review_draft(candidate: Dict[str, Any], reason: str, trace_id: str) -> Dict[str, Any]:
    cid = candidate["candidate_id"]
    content = (candidate.get("content") or "")[:400]
    return {
        "page_id": f"consolidation-review-{cid}-{trace_id}",
        "kind": "concept",
        "section": "concepts",
        "title": f"Candidate #{cid} needs review ({reason})",
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "sources": [f"candidate_packets:{cid}"],
        "body": (
            f"- Candidate #{cid} held for operator review.\n"
            f"- Reason: {reason}.\n"
            f"- Principal: {candidate.get('principal')}; "
            f"privacy: {candidate.get('privacy_level')}; "
            f"instruction_like: {bool(candidate.get('instruction_like'))}.\n"
            f"- Content: {content}"
        ),
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
        from afm_provider import resolve_afm_mode
        from model_provider import default_provider_chain
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if resolve_afm_mode() not in {"native", "auto"}:
        return None
    # Prefer a candidate the deterministic gate already chose to promote, so the
    # advisory is comparable to a real decision; else fall back to the first one.
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
    privacy = str(chosen.get("privacy_level") or "safe").strip().lower()
    if privacy not in {"safe", ""}:
        return None
    if int(chosen.get("instruction_like") or 0) == 1:
        return None
    content = (chosen.get("content") or "").strip()
    if not content:
        return None
    try:
        result = default_provider_chain().native_op(
            "triage", {"candidate": content[:6000]}, timeout=4.0
        )
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if not result.ok:
        data = result.data if isinstance(result.data, dict) else {}
        error_kind = str(data.get("error_kind") or "").strip() or "unknown"
        logger.info(
            "afm native op triage unavailable (error_kind=%s): status=%s error=%s trace=%s",
            error_kind, result.status, result.error, trace_id,
        )
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
    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("consolidation", {})
    max_per_run = int(pass_cfg.get("max_per_run", _DEFAULT_MAX_PER_RUN))
    trace_id = trace_id or f"afm-{int(time.time())}"

    _ensure_dedup_index(db)

    total = _total_proposed(db)
    candidates = _proposed_candidates(db, max_per_run)

    seen_this_run: set = set()
    promote_candidate_ids: List[int] = []
    dedup_candidate_ids: List[int] = []
    review_candidate_ids: List[int] = []
    drafts: List[Dict[str, Any]] = []

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
        if h in seen_this_run or _hash_in_learnings(db, h):
            dedup_candidate_ids.append(cand["candidate_id"])
            continue

        if int(cand["instruction_like"] or 0) == 1 or is_instruction_like(content):
            _review(cand, "instruction_like")
            continue

        privacy = (cand.get("privacy_level") or "").strip().lower()
        if privacy not in _SAFE_PRIVACY:
            _review(cand, f"privacy={privacy or 'unset'}")
            continue

        blockers = _quality_blockers(content)
        if blockers:
            _review(cand, "low quality: " + "; ".join(blockers))
            continue

        promote_candidate_ids.append(cand["candidate_id"])
        seen_this_run.add(h)

    summary = {
        "total_proposed": total,
        "examined": len(candidates),
        "deferred": max(0, total - len(candidates)),
        "to_promote": len(promote_candidate_ids),
        "to_dedup": len(dedup_candidate_ids),
        "to_review": len(review_candidate_ids),
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
        "drafts": drafts,
        "triage_advisory": triage_advisory,
        "prompt_version": "consolidation-v1-deterministic",
    }
