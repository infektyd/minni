"""Session distillation pass for the opt-in AFM loop.

The pass is intentionally deterministic and dependency-free. AFM model calls
can be added behind this contract later; for PR-12 the important behavior is
the lifecycle: evidence in, draft proposals out, no auto-accept.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "session-distillation"


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "session_distillation.md"
    return prompt_path.read_text(encoding="utf-8")


def _recent_events(db, lookback_hours: int) -> List[Dict[str, Any]]:
    cutoff = time.time() - (lookback_hours * 3600)
    with db.cursor() as c:
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


def _recent_raw_docs(db, lookback_hours: int) -> List[Dict[str, Any]]:
    cutoff = time.time() - (lookback_hours * 3600)
    with db.cursor() as c:
        c.execute(
            """
            SELECT doc_id, path, agent, page_type, page_status, indexed_at, last_modified
            FROM documents
            WHERE (path LIKE '%/raw/%' OR path LIKE 'raw/%' OR agent = 'raw')
              AND COALESCE(indexed_at, last_modified, 0) >= ?
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

    drafts: List[Dict[str, Any]] = [{
        "page_id": _draft_id("session", title, sources, trace_id),
        "kind": "session",
        "section": "sessions",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "sources": sources,
        "body": "\n".join(summary_lines) if summary_lines else "- No eligible cited evidence.",
    }]

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
        "body": "\n".join([
            body,
            "",
            "## Citations",
            *[f"- `{source}`" for source in sources],
            "",
            "- Lifecycle: native AFM proposal only; endorsement is required before acceptance.",
        ]),
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
    payload = {
        "pass_name": "session_distillation",
        "trace_id": trace_id,
        "inputs": pass_input,
        "deterministic_drafts": deterministic_drafts[:12],
    }
    # P2: native helper ops route through the provider chain. The op stays
    # native-only by contract (bridge mode keeps returning no drafts).
    result = default_provider_chain().native_op("compile_pass_proposals", payload, timeout=4.0)
    if not result.ok:
        return []
    candidates = result.data.get("drafts")
    if not isinstance(candidates, list):
        return []
    normalized = [
        draft
        for draft in (_normalize_native_draft(candidate, trace_id, allowed_sources) for candidate in candidates)
        if draft is not None
    ]
    return normalized[:5]


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("session_distillation", {})
    lookback_hours = int(pass_cfg.get("lookback_hours", 24))
    trace_id = trace_id or f"afm-{int(time.time())}"
    prompt = _load_prompt()
    events = _recent_events(db, lookback_hours)
    raw_docs = _recent_raw_docs(db, lookback_hours)
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
