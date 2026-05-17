"""Synthesis pass for the opt-in AFM loop."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from afm_passes._graph_utils import (
    accepted_pages,
    load_vault_pages,
    newest_timestamp,
    pages_by_tag,
    synthesis_pages_by_tag,
    wikilink_neighborhoods,
)

PROMPT_VERSION = "synthesis.v1"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "synthesis"


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "synthesis.md"
    return prompt_path.read_text(encoding="utf-8")


def _draft_id(title: str, sources: List[str], trace_id: str) -> str:
    digest = hashlib.sha1("|".join(["synthesis", title, trace_id, *sources]).encode("utf-8")).hexdigest()[:10]
    return f"afm-synthesis-{digest}"


def _synthesis_is_due(tag: str, sources: list, existing_by_tag: dict, threshold_days: int) -> bool:
    existing = existing_by_tag.get(tag, [])
    if not existing:
        return True
    newest_source = newest_timestamp(sources)
    newest_synthesis = newest_timestamp(existing)
    return newest_source - newest_synthesis >= threshold_days * 86400


def _source_excerpt(body: str) -> str:
    line = next((line.strip("#- ").strip() for line in body.splitlines() if line.strip()), "")
    return line[:220] or "Accepted page supplied evidence."


def _draft_for_cluster(label: str, pages: list, trace_id: str, *, source_kind: str = "tag") -> Dict[str, Any]:
    title = f"Synthesis: {label}"
    sources = [f"{source_kind}:{label}", *[page.source_ref for page in pages]]
    citations = [page.source_ref for page in pages]
    body_lines = [
        f"- Bridges {len(pages)} accepted source pages in `{label}`.",
        "- Shared evidence:",
    ]
    for page in pages[:8]:
        body_lines.append(f"  - {page.title}: {_source_excerpt(page.body)} [{page.source_ref}]")
    body_lines.extend([
        "- Review focus: confirm whether the bridge accurately represents the cited pages before endorsement.",
        "",
        "## Citations",
        *[f"- `{citation}`" for citation in citations],
    ])
    return {
        "page_id": _draft_id(title, sources, trace_id),
        "kind": "synthesis",
        "section": "syntheses",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "prompt_version": PROMPT_VERSION,
        "tags": [label],
        "sources": sources,
        "citations": citations,
        "body": "\n".join(body_lines),
    }


def _build_drafts(vault_path: str, threshold_days: int, trace_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    pages = load_vault_pages(vault_path)
    accepted = accepted_pages(pages)
    existing_syntheses = synthesis_pages_by_tag(pages)
    drafts: List[Dict[str, Any]] = []
    inputs: Dict[str, Any] = {
        "accepted_page_count": len(accepted),
        "threshold_days": threshold_days,
        "tag_clusters": {},
        "wikilink_neighborhood_count": 0,
    }

    for tag, cluster in pages_by_tag(accepted).items():
        inputs["tag_clusters"][tag] = len(cluster)
        if len(cluster) < 3:
            continue
        if not _synthesis_is_due(tag, cluster, existing_syntheses, threshold_days):
            continue
        drafts.append(_draft_for_cluster(tag, cluster[:12], trace_id, source_kind="tag"))

    existing_labels = {draft["title"].lower() for draft in drafts}
    neighborhoods = wikilink_neighborhoods(accepted, min_size=3)
    inputs["wikilink_neighborhood_count"] = len(neighborhoods)
    for idx, cluster in enumerate(neighborhoods, start=1):
        label = f"wikilink-neighborhood-{idx}"
        title_key = f"synthesis: {label}"
        if title_key in existing_labels:
            continue
        drafts.append(_draft_for_cluster(label, cluster[:12], trace_id, source_kind="wikilink"))

    return drafts, inputs


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    schedule = getattr(config, "afm_loop_schedule", {}) or {}
    pass_cfg = (schedule.get("passes") or {}).get("synthesis", {})
    threshold_days = int(pass_cfg.get("stale_after_days", 30))
    trace_id = trace_id or f"afm-{int(time.time())}"
    resolved_vault = vault_path or getattr(config, "vault_path", None)
    prompt = _load_prompt()
    drafts, pass_input = _build_drafts(str(resolved_vault), threshold_days, trace_id)
    return {
        "status": "ok",
        "pass_name": "synthesis",
        "dry_run": bool(dry_run),
        "trace_id": trace_id,
        "vault_path": resolved_vault,
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
        "inputs": pass_input,
        "drafts": drafts,
        "output": {"draft_count": len(drafts), "draft_page_ids": [draft["page_id"] for draft in drafts]},
    }
