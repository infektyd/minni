"""Session distillation pass for the opt-in AFM loop.

The pass is intentionally deterministic and dependency-free. AFM model calls
can be added behind this contract later; for PR-12 the important behavior is
the lifecycle: evidence in, draft proposals out, no auto-accept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from afm_chunking import (
    MIN_CHUNK_TOKENS,
    call_native_op_and_reduce,
    call_native_op_chunked,
    estimate_native_payload_tokens,
    log_native_error_kind as _log_native_error_kind,
    resolve_afm_input_budget_tokens,
    split_list_by_token_budget,
)
from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain
from safety import is_instruction_like

logger = logging.getLogger("sovereign.afm.session_distillation")

# Post-merge cap on entity_extract results (carried over from the
# pre-chunking single-call cap; the drop is logged when it bites).
_ENTITY_CAP = 20


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "session-distillation"


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "session_distillation.md"
    return prompt_path.read_text(encoding="utf-8")


def _recent_events(db, lookback_hours: int, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cutoff = time.time() - (lookback_hours * 3600)
    with db.cursor() as c:
        if agent_id:
            c.execute(
                """
                SELECT event_id, agent_id, event_type, content, task_id, thread_id, metadata, created_at
                FROM episodic_events
                WHERE created_at >= ? AND agent_id = ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (cutoff, agent_id),
            )
        else:
            c.execute(
                """
                SELECT event_id, agent_id, event_type, content, task_id, thread_id, metadata, created_at
                FROM episodic_events
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (cutoff,),
            )
        return [dict(row) for row in c.fetchall()]


def _recent_raw_docs(db, lookback_hours: int, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cutoff = time.time() - (lookback_hours * 3600)
    with db.cursor() as c:
        if agent_id:
            c.execute(
                """
                SELECT doc_id, path, agent, page_type, page_status, privacy_level, indexed_at, last_modified
                FROM documents
                WHERE (path LIKE '%/raw/%' OR path LIKE 'raw/%' OR agent = 'raw' OR agent = ?)
                  AND COALESCE(indexed_at, last_modified, 0) >= ?
                  AND COALESCE(privacy_level, 'safe') = 'safe'
                ORDER BY COALESCE(indexed_at, last_modified, 0) DESC
                LIMIT 50
                """,
                (agent_id, cutoff),
            )
        else:
            c.execute(
                """
                SELECT doc_id, path, agent, page_type, page_status, privacy_level, indexed_at, last_modified
                FROM documents
                WHERE (path LIKE '%/raw/%' OR path LIKE 'raw/%' OR agent = 'raw')
                  AND COALESCE(indexed_at, last_modified, 0) >= ?
                  AND COALESCE(privacy_level, 'safe') = 'safe'
                ORDER BY COALESCE(indexed_at, last_modified, 0) DESC
                LIMIT 50
                """,
                (cutoff,),
            )
        return [dict(row) for row in c.fetchall()]


def _event_source(event: Dict[str, Any]) -> str:
    return f"episodic_events:{event['event_id']}"


def _draft_id(kind: str, title: str, sources: List[str], trace_id: str) -> str:
    digest = hashlib.sha1("|".join([kind, title, trace_id, *sources]).encode("utf-8")).hexdigest()[:10]
    return f"afm-{kind}-{digest}"


def _extract_concepts(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    concepts: List[Dict[str, str]] = []
    seen = set()
    patterns = [
        re.compile(r"important concept:\s*([^.\n]+)", re.IGNORECASE),
        re.compile(r"concept:\s*([^.\n]+)", re.IGNORECASE),
    ]
    for event in events:
        content = event.get("content") or ""
        for pattern in patterns:
            for match in pattern.finditer(content):
                title = match.group(1).strip()
                key = title.lower()
                if title and key not in seen:
                    seen.add(key)
                    concepts.append({"title": title, "source": _event_source(event)})
    return concepts[:5]


def _extract_entities(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    entities: List[Dict[str, str]] = []
    seen = set()
    for event in events:
        agent = (event.get("agent_id") or "").strip()
        if agent and agent not in seen:
            seen.add(agent)
            entities.append({"title": agent, "source": _event_source(event)})
    return entities[:5]


def _build_drafts(events: List[Dict[str, Any]], raw_docs: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    if not events and not raw_docs:
        return []

    event_sources = [_event_source(event) for event in events]
    raw_sources = [f"documents:{doc['doc_id']}" for doc in raw_docs]
    sources = event_sources[:12] + raw_sources[:8]
    title = time.strftime("AFM Session Distillation %Y-%m-%d", time.gmtime())
    summary_lines = [
        f"- {event.get('event_type', 'event')}: {(event.get('content') or '').strip()[:220]}"
        for event in events[:8]
    ]
    if raw_docs:
        summary_lines.extend(f"- raw document: {doc['path']}" for doc in raw_docs[:5])

    session_body = "\n".join(summary_lines) if summary_lines else "- No eligible cited evidence."

    # Finding #4: this draft's body is synthesized from multiple episodic_events
    # (events don't carry a precomputed instruction_like column, so each source's
    # own content is recomputed here). If ANY source event tripped the detector,
    # the synthesized draft inherits the flag even if the summarized body itself
    # no longer trips is_instruction_like.
    own_flag = is_instruction_like(session_body)
    flagged_sources = {
        _event_source(event)
        for event in events
        if is_instruction_like(event["content"] or "")
    }
    source_flag = bool(flagged_sources)
    session_instruction_like = own_flag or source_flag
    if session_instruction_like and not own_flag:
        logger.warning(
            "session_distillation draft %r inherits instruction_like from source "
            "event(s); the synthesized summary did not itself trip the detector",
            title,
        )

    drafts: List[Dict[str, Any]] = [{
        "page_id": _draft_id("session", title, sources, trace_id),
        "kind": "session",
        "section": "sessions",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "sources": sources,
        "body": session_body,
        "instruction_like": 1 if session_instruction_like else 0,
    }]

    # Finding #4 (review round 4): concept/entity drafts are extracted from the
    # SAME events — a benign-looking extraction (e.g. "important concept: Safe
    # Topic") from an instruction-like event must inherit that event's flag, or
    # deterministic extraction becomes a laundering path around the writer's
    # own title+body recompute.
    def _extraction_flag(kind: str, title: str, source: str) -> int:
        if source in flagged_sources:
            logger.warning(
                "session_distillation %s draft %r inherits instruction_like from "
                "flagged source %s; the extracted title/body did not itself trip "
                "the detector",
                kind, title, source,
            )
            return 1
        return 0

    for concept in _extract_concepts(events):
        sources = [concept["source"]]
        drafts.append({
            "page_id": _draft_id("concept", concept["title"], sources, trace_id),
            "kind": "concept",
            "section": "concepts",
            "title": concept["title"],
            "status": "draft",
            "agent": "afm-loop",
            "trace_id": trace_id,
            "sources": sources,
            "body": f"- Candidate concept extracted from {concept['source']}.",
            "instruction_like": _extraction_flag(
                "concept", concept["title"], concept["source"]
            ),
        })

    for entity in _extract_entities(events):
        sources = [entity["source"]]
        drafts.append({
            "page_id": _draft_id("entity", entity["title"], sources, trace_id),
            "kind": "entity",
            "section": "entities",
            "title": entity["title"],
            "status": "draft",
            "agent": "afm-loop",
            "trace_id": trace_id,
            "sources": sources,
            "body": f"- Candidate entity observed in {entity['source']}.",
            "instruction_like": _extraction_flag(
                "entity", entity["title"], entity["source"]
            ),
        })

    return [draft for draft in drafts if draft.get("sources")]


def _normalize_native_draft(candidate: Any, trace_id: str, allowed_sources: set[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(candidate, dict):
        return None
    sources = [
        str(source).strip()
        for source in candidate.get("sources", [])
        if str(source).strip() in allowed_sources
    ] if isinstance(candidate.get("sources"), list) else []
    if not sources:
        return None
    title = str(candidate.get("title") or "").strip()
    body = str(candidate.get("body") or "").strip()
    if not title or not body:
        return None
    kind = str(candidate.get("kind") or "concept").strip() or "concept"
    section = str(candidate.get("section") or f"{kind}s").strip() or f"{kind}s"
    full_body = "\n".join([
        body,
        "",
        "## Citations",
        *[f"- `{source}`" for source in sources],
        "",
        "- Lifecycle: native AFM proposal only; endorsement is required before acceptance.",
    ])
    return {
        "page_id": _draft_id(kind, title, sources, trace_id),
        "kind": kind,
        "section": section,
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "prompt_version": "native.compile.v1",
        "provider": "native",
        "sources": sources,
        "citations": sources,
        "body": full_body,
        # Finding #4 (native compile path): re-check the provider's own output.
        # _native_compile_drafts additionally ORs in inheritance from flagged
        # deterministic input drafts.
        "instruction_like": 1 if is_instruction_like(full_body) else 0,
    }


def _native_compile_drafts(pass_input: Dict[str, Any], deterministic_drafts: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    allowed_sources = {
        str(source)
        for draft in deterministic_drafts
        for source in draft.get("sources", [])
        if str(source).strip()
    }
    if not allowed_sources:
        return []
    chain = default_provider_chain()
    base_payload = {"pass_name": "session_distillation", "trace_id": trace_id, "inputs": pass_input}
    base_tokens = estimate_native_payload_tokens(base_payload)
    draft_group_budget = max(resolve_afm_input_budget_tokens() - base_tokens, MIN_CHUNK_TOKENS)
    # List-shaped input (deterministic_drafts): group by token budget instead
    # of the old hard cap of 12 (which silently dropped anything beyond it).
    groups = split_list_by_token_budget(
        deterministic_drafts, draft_group_budget,
        serialize=lambda draft: json.dumps(draft, default=str),
    )

    normalized: List[Dict[str, Any]] = []
    seen_titles: set = set()
    for group in groups:
        payload = dict(base_payload, deterministic_drafts=group)
        # P2: native helper ops route through the provider chain. The op stays
        # native-only by contract (bridge mode keeps returning no drafts).
        result = chain.native_op("compile_pass_proposals", payload, timeout=4.0)
        if not result.ok:
            _log_native_error_kind("compile_pass_proposals", trace_id, result)
            continue
        candidates = result.data.get("drafts") if isinstance(result.data, dict) else None
        if not isinstance(candidates, list):
            continue
        # Finding #4 (native compile path): the provider may paraphrase a
        # poisoned event into clean prose. Inherit per GROUP: if any input
        # draft fed to THIS call was flagged instruction_like, this call's
        # outputs inherit the flag even when the paraphrased body no longer
        # trips is_instruction_like().
        inputs_flagged = any(
            draft.get("instruction_like") for draft in group
        )
        for candidate in candidates:
            draft = _normalize_native_draft(candidate, trace_id, allowed_sources)
            if draft is None:
                continue
            key = draft["title"].strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            own_flag = bool(draft.get("instruction_like"))
            if inputs_flagged and not own_flag:
                logger.warning(
                    "session_distillation native draft %r inherits instruction_like "
                    "from flagged deterministic input draft(s); the provider's "
                    "paraphrase did not itself trip the detector",
                    draft.get("title"),
                )
            draft["instruction_like"] = 1 if (own_flag or inputs_flagged) else 0
            normalized.append(draft)
    return normalized[:5]


def _combined_session_text(events: List[Dict[str, Any]], raw_docs: List[Dict[str, Any]]) -> str:
    """Flatten the session into one text blob for the guided AFM ops.
    Unbounded — oversized input is chunked by call_native_op_chunked at the
    call site (session_distill, entity_extract) instead of being silently
    truncated here."""
    lines: List[str] = []
    for event in events:
        content = (event.get("content") or "").strip()
        if content:
            lines.append(f"{event.get('event_type', 'event')}: {content}")
    for doc in raw_docs:
        path = (doc.get("path") or "").strip()
        if path:
            lines.append(f"raw document: {path}")
    return "\n".join(lines)


def _build_session_distill_reduce_payload(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Synthesize a compact 'text' input for a second session_distill call
    from N per-chunk DistilledLearningResult-shaped dicts — reduce-via-
    same-op, no new Swift surface needed."""
    lines = []
    for partial in partials:
        title = str(partial.get("title") or "").strip()
        assertion = str(partial.get("assertion") or "").strip()
        applies_when = str(partial.get("appliesWhen") or "").strip()
        if title or assertion:
            lines.append(f"{title}: {assertion} ({applies_when})".strip())
    return {"text": "\n".join(lines)}


def _native_session_distill_draft(
    text: str, sources: List[str], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Additive review-only draft from the guided `session_distill` op. Never
    replaces the existing compile_pass drafts; emitted alongside them."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    if not text.strip() or not sources:
        return None
    chain = default_provider_chain()
    result = call_native_op_and_reduce(
        chain, "session_distill", {"text": text}, text_field="text",
        build_reduce_payload=_build_session_distill_reduce_payload,
        timeout=4.0, trace_id=trace_id,
    )
    if result is None:
        return None
    data = result.data if isinstance(result.data, dict) else {}
    title = str(data.get("title") or "").strip()
    assertion = str(data.get("assertion") or "").strip()
    if not title or not assertion:
        return None
    applies_when = str(data.get("appliesWhen") or "").strip()
    category = str(data.get("category") or "").strip() or "concept"
    cited = sources[:12]
    body_lines = [
        assertion,
        "",
        f"- Applies when: {applies_when}" if applies_when else "- Applies when: (unspecified)",
        f"- Category: {category}",
        "",
        "## Citations",
        *[f"- `{source}`" for source in cited],
        "",
        "- Lifecycle: native AFM session_distill proposal only; endorsement is required before acceptance.",
    ]
    body = "\n".join(body_lines)

    # Finding #4: the native op synthesizes `assertion` from the combined session
    # `text` (multiple events/docs flattened). Inherit instruction_like from that
    # combined source text even if the model's paraphrased assertion no longer
    # trips the regex.
    own_flag = is_instruction_like(body)
    source_flag = is_instruction_like(text)
    distill_instruction_like = own_flag or source_flag
    if distill_instruction_like and not own_flag:
        logger.warning(
            "session_distillation native draft %r inherits instruction_like from "
            "combined source text; the synthesized assertion did not itself trip "
            "the detector",
            title,
        )

    return {
        "page_id": _draft_id("session-distill", title, cited, trace_id),
        "kind": "concept",
        "section": "concepts",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "prompt_version": "native.session_distill.v1",
        "provider": "native",
        "sources": cited,
        "citations": cited,
        "body": body,
        "instruction_like": 1 if distill_instruction_like else 0,
    }


def _native_entity_extract(text: str, trace_id: str) -> List[Dict[str, str]]:
    """Additive review-only entities for the wikilink graph seed. Proposal only:
    NEVER writes durable links. List-shaped: merged deterministically across
    chunks (dedupe by name.lower(), cap _ENTITY_CAP) rather than via an AFM
    reduce pass — no synthesis is needed for a flat entity list. Entities
    dropped by the cap are logged so the truncation is observable."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    if not text.strip():
        return []
    chain = default_provider_chain()
    chunk_results, _ = call_native_op_chunked(chain, "entity_extract", {"text": text}, text_field="text", timeout=4.0)
    if not any(r.ok for r in chunk_results):
        _log_native_error_kind("entity_extract", trace_id, chunk_results[0])
        return []
    entities: List[Dict[str, str]] = []
    seen: set = set()
    for result in chunk_results:
        if not result.ok:
            continue
        data = result.data if isinstance(result.data, dict) else {}
        raw = data.get("entities")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            etype = str(item.get("type") or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                entities.append({"name": name, "type": etype})
    if len(entities) > _ENTITY_CAP:
        logger.info(
            "afm native op entity_extract: cap %d dropped %d merged entities "
            "(chunk order, not relevance) trace=%s",
            _ENTITY_CAP, len(entities) - _ENTITY_CAP, trace_id,
        )
    return entities[:_ENTITY_CAP]


def _similar_existing_learning(db, query: str) -> Optional[str]:
    """Cheap, self-contained FTS lookup of the most similar existing learning.
    Returns the learning content or None. Sanitizes the FTS query so a candidate
    full of punctuation cannot break the MATCH (mirror of retrieval's guard)."""
    tokens = re.findall(r"[A-Za-z0-9]+", query or "")
    tokens = [t for t in tokens if len(t) > 2][:8]
    if not tokens:
        return None
    safe_q = " OR ".join(tokens)
    try:
        with db.cursor() as c:
            c.execute(
                """
                SELECT lf.content AS content
                FROM learnings_fts lf
                JOIN learnings l ON l.learning_id = lf.learning_id
                WHERE learnings_fts MATCH ? AND l.superseded_by IS NULL
                ORDER BY rank
                LIMIT 1
                """,
                (safe_q,),
            )
            row = c.fetchone()
    except sqlite3.Error as exc:
        logger.info("advisory similar-learning lookup skipped: %s", exc)
        return None
    if not row:
        return None
    content = row["content"]
    return str(content).strip() if content else None


def _fold_contradiction_signals(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic OR-fold over per-chunk contradiction verdicts: any chunk
    that contradicts wins, with that chunk's reason. Contradiction is a
    judgment op — re-judging the concatenated reasons with a second AFM call
    would let non-contradicting chunks dilute a real contradiction to false."""
    hit = next((p for p in partials if bool(p.get("contradicts"))), None)
    if hit is not None:
        return {"contradicts": True, "reason": str(hit.get("reason") or "").strip()}
    first_reason = next((str(p.get("reason") or "").strip() for p in partials if p.get("reason")), "")
    return {"contradicts": False, "reason": first_reason}


def _native_contradiction_signal(
    db, drafts: List[Dict[str, Any]], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Advisory-only contradiction check: compares the strongest candidate draft
    this pass against the most similar existing learning (cheap FTS lookup). No
    durable write, no decision change — returns a {contradicts,reason} signal."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    # Strongest candidate = the native session_distill / compile draft if present,
    # else the first deterministic draft. Use its body as the candidate text.
    candidate = None
    for draft in drafts:
        if draft.get("provider") == "native":
            candidate = draft
            break
    if candidate is None and drafts:
        candidate = drafts[0]
    if candidate is None:
        return None
    candidate_text = str(candidate.get("title") or "") + ". " + str(candidate.get("body") or "")
    candidate_text = candidate_text.strip()
    existing = _similar_existing_learning(db, candidate.get("title") or candidate.get("body") or "")
    if not existing:
        return None
    chain = default_provider_chain()
    result = call_native_op_and_reduce(
        chain, "contradiction", {"existing": existing, "candidate": candidate_text},
        text_field="candidate",
        fold=_fold_contradiction_signals,
        timeout=4.0, trace_id=trace_id,
    )
    if result is None:
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_page_id": candidate.get("page_id"),
        "contradicts": bool(data.get("contradicts")),
        "reason": str(data.get("reason") or "").strip(),
    }


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("session_distillation", {})
    lookback_hours = int(pass_cfg.get("lookback_hours", 24))
    trace_id = trace_id or f"afm-{int(time.time())}"
    prompt = _load_prompt()
    # PR91-2: never query GLOBALLY when unbound — a None agent_id would pull
    # EVERY agent's session events/docs into one agent's distillation. Fall back
    # to the "unknown" sentinel (matches only unattributed rows) so an unbound
    # run fails closed instead of leaking cross-agent context. The CLI
    # consolidation entrypoint propagates --agent/MINNI_AGENT_ID so real runs are
    # properly scoped.
    bound_agent = os.environ.get("MINNI_AGENT_ID") or getattr(config, "agent_id", None)
    bound_agent = str(bound_agent).strip() if bound_agent and str(bound_agent).strip() else "unknown"
    events = _recent_events(db, lookback_hours, agent_id=bound_agent)
    raw_docs = _recent_raw_docs(db, lookback_hours, agent_id=bound_agent)
    pass_input = {
        "lookback_hours": lookback_hours,
        "event_count": len(events),
        "raw_doc_count": len(raw_docs),
        "events": [
            {
                "event_id": row["event_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "content": (row.get("content") or "")[:500],
                "created_at": row["created_at"],
            }
            for row in events[:20]
        ],
        "raw_docs": raw_docs[:20],
    }
    drafts = _build_drafts(events, raw_docs, trace_id)
    native_drafts = _native_compile_drafts(pass_input, drafts, trace_id)
    drafts = [*drafts, *native_drafts]
    pass_input["native_draft_count"] = len(native_drafts)

    # Additive guided AFM ops (review-only; never replace the existing path).
    allowed_sources = [
        str(source)
        for draft in drafts
        for source in draft.get("sources", [])
        if str(source).strip()
    ]
    combined_text = _combined_session_text(events, raw_docs)
    distill_draft = _native_session_distill_draft(combined_text, allowed_sources, trace_id)
    if distill_draft is not None:
        drafts = [*drafts, distill_draft]
    pass_input["session_distill_draft_count"] = 1 if distill_draft is not None else 0

    # Entity extraction seeds the wikilink graph — proposal only, no durable links.
    entities = _native_entity_extract(combined_text, trace_id)
    pass_input["entities"] = entities

    # Advisory contradiction signal for the strongest candidate this pass.
    contradiction = _native_contradiction_signal(db, drafts, trace_id)
    if contradiction is not None:
        pass_input["contradiction"] = contradiction
    return {
        "status": "ok",
        "pass_name": "session_distillation",
        "dry_run": bool(dry_run),
        "trace_id": trace_id,
        "vault_path": vault_path or getattr(config, "vault_path", None),
        "inputs": pass_input,
        "prompt": prompt,
        "drafts": drafts,
        "output": {"draft_count": len(drafts), "draft_page_ids": [draft["page_id"] for draft in drafts]},
    }
