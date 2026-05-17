"""Procedure extraction pass for the opt-in AFM loop."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from afm_passes._graph_utils import accepted_pages, load_vault_pages

PROMPT_VERSION = "procedure_extraction.v1"
PATTERN_RE = re.compile(r"\bagent\s+(.{3,160}?\bthen\b.{3,160}?\bthen\b.{3,220}?)(?:[.\n]|$)", re.IGNORECASE)


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "procedure_extraction.md"
    return prompt_path.read_text(encoding="utf-8")


def _draft_id(title: str, sources: List[str], trace_id: str) -> str:
    digest = hashlib.sha1("|".join(["procedure", title, trace_id, *sources]).encode("utf-8")).hexdigest()[:10]
    return f"afm-procedure-{digest}"


def _normalize_pattern(text: str) -> str:
    text = re.sub(r"\s+", " ", text.lower()).strip(" .")
    text = re.sub(r"\bin run \d+,?\s*", "", text)
    return text


def _title_for_pattern(pattern: str) -> str:
    first_step = pattern.split(" then ", 1)[0].strip()
    return f"Procedure: {first_step[:64].title()}"


def _recent_events(db, lookback_days: int) -> List[Dict[str, Any]]:
    cutoff = time.time() - (lookback_days * 86400)
    with db.cursor() as c:
        c.execute(
            """
            SELECT event_id, agent_id, event_type, content, task_id, thread_id, metadata, created_at
            FROM episodic_events
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT 500
            """,
            (cutoff,),
        )
        return [dict(row) for row in c.fetchall()]


def _session_page_events(vault_path: Optional[str]) -> List[Dict[str, Any]]:
    if not vault_path:
        return []
    events: List[Dict[str, Any]] = []
    for page in accepted_pages(load_vault_pages(vault_path)):
        if page.page_type != "session":
            continue
        events.append({
            "event_id": page.rel_path,
            "agent_id": "vault",
            "event_type": "session_page",
            "content": page.body,
            "created_at": page.updated_ts,
            "source_ref": page.source_ref,
        })
    return events


def _pattern_sources(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        content = event.get("content") or ""
        for match in PATTERN_RE.finditer(content):
            pattern = _normalize_pattern(match.group(1))
            if pattern:
                clusters.setdefault(pattern, []).append(event)
    return clusters


def _source_ref(event: Dict[str, Any]) -> str:
    if event.get("source_ref"):
        return str(event["source_ref"])
    return f"episodic_events:{event['event_id']}"


def _draft_for_pattern(pattern: str, events: List[Dict[str, Any]], trace_id: str) -> Dict[str, Any]:
    title = _title_for_pattern(pattern)
    sources = [_source_ref(event) for event in events]
    steps = [step.strip().rstrip(".") for step in pattern.split(" then ") if step.strip()]
    body_lines = [
        f"- Repeated evidence count: {len(events)}.",
        "- Draft procedure steps:",
        *[f"  {idx}. {step}" for idx, step in enumerate(steps, start=1)],
        "- Review focus: confirm this is a reusable procedure before endorsement.",
        "",
        "## Citations",
        *[f"- `{source}`" for source in sources],
    ]
    return {
        "page_id": _draft_id(title, sources, trace_id),
        "kind": "procedure",
        "section": "procedures",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "prompt_version": PROMPT_VERSION,
        "tags": ["procedure-extraction"],
        "sources": sources,
        "citations": sources,
        "body": "\n".join(body_lines),
    }


def _build_drafts(events: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    drafts: List[Dict[str, Any]] = []
    for pattern, cluster in sorted(_pattern_sources(events).items()):
        unique_sources = {_source_ref(event): event for event in cluster}
        if len(unique_sources) < 3:
            continue
        drafts.append(_draft_for_pattern(pattern, list(unique_sources.values())[:12], trace_id))
    return drafts


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("procedure_extraction", {})
    lookback_days = int(pass_cfg.get("lookback_days", 90))
    trace_id = trace_id or f"afm-{int(time.time())}"
    resolved_vault = vault_path or getattr(config, "vault_path", None)
    prompt = _load_prompt()
    db_events = _recent_events(db, lookback_days)
    session_events = _session_page_events(str(resolved_vault) if resolved_vault else None)
    all_events = db_events + session_events
    drafts = _build_drafts(all_events, trace_id)
    return {
        "status": "ok",
        "pass_name": "procedure_extraction",
        "dry_run": bool(dry_run),
        "trace_id": trace_id,
        "vault_path": resolved_vault,
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
        "inputs": {
            "lookback_days": lookback_days,
            "episodic_event_count": len(db_events),
            "session_page_count": len(session_events),
            "pattern_count": len(_pattern_sources(all_events)),
        },
        "drafts": drafts,
        "output": {"draft_count": len(drafts), "draft_page_ids": [draft["page_id"] for draft in drafts]},
    }
