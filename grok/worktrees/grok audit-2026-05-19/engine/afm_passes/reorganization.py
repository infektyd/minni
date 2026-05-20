"""Vault reorganization pass for the opt-in AFM loop.

The pass is deliberately proposal-only. It reads vault pages, wikilinks, and
SQLite memory_links, then emits draft pages that a human/agent can endorse.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from afm_passes._graph_utils import VaultPage, accepted_pages, load_vault_pages

PROMPT_VERSION = "reorganization.v1"
OVERLOADED_EDGE_THRESHOLD = 4
REDUNDANT_TEXT_THRESHOLD = 0.72


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "reorganization.md"
    return prompt_path.read_text(encoding="utf-8")


def _draft_id(kind: str, title: str, sources: List[str], trace_id: str) -> str:
    digest = hashlib.sha1("|".join(["reorg", kind, title, trace_id, *sources]).encode("utf-8")).hexdigest()[:10]
    return f"afm-reorg-{kind}-{digest}"


def _normalize_link(link: str) -> str:
    return link.strip().removesuffix(".md")


def _vault_edges(pages: Iterable[VaultPage]) -> Dict[str, Set[str]]:
    page_list = list(pages)
    by_slug = {page.slug: page.rel_path for page in page_list}
    by_title = {page.title: page.rel_path for page in page_list}
    edges: Dict[str, Set[str]] = {page.rel_path: set() for page in page_list}
    for page in page_list:
        for link in page.wikilinks:
            target = by_slug.get(_normalize_link(link)) or by_title.get(_normalize_link(link))
            if target:
                edges.setdefault(page.rel_path, set()).add(target)
                edges.setdefault(target, set()).add(page.rel_path)
    return edges


def _db_edges(db) -> Dict[str, Set[str]]:
    try:
        with db.cursor() as c:
            c.execute("SELECT doc_id, path FROM documents")
            paths = {int(row["doc_id"]): row["path"] for row in c.fetchall()}
            c.execute("SELECT source_doc_id, target_doc_id FROM memory_links")
            edges: Dict[str, Set[str]] = {}
            for row in c.fetchall():
                source = paths.get(int(row["source_doc_id"]))
                target = paths.get(int(row["target_doc_id"]))
                if not source or not target:
                    continue
                edges.setdefault(source, set()).add(target)
                edges.setdefault(target, set()).add(source)
            return edges
    except Exception:
        return {}


def _merge_edges(*graphs: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    merged: Dict[str, Set[str]] = {}
    for graph in graphs:
        for source, targets in graph.items():
            merged.setdefault(source, set()).update(targets)
    return merged


def _index_mentions(vault_path: str, pages: Iterable[VaultPage]) -> Set[str]:
    vault = Path(vault_path).expanduser()
    index_text = ""
    for rel in ("index.md", "wiki/index.md"):
        path = vault / rel
        if path.exists():
            try:
                index_text += "\n" + path.read_text(encoding="utf-8")
            except Exception:
                continue
    mentioned: Set[str] = set()
    for page in pages:
        stem_ref = f"[[{page.slug}]]"
        title_ref = f"[[{page.title}]]"
        if page.rel_path in index_text or stem_ref in index_text or title_ref in index_text:
            mentioned.add(page.rel_path)
    return mentioned


def _select_incremental_pages(pages: List[VaultPage], edges: Dict[str, Set[str]], horizon_days: int) -> Set[str]:
    if horizon_days <= 0:
        return {page.rel_path for page in pages}
    cutoff = time.time() - horizon_days * 86400
    selected = {page.rel_path for page in pages if page.updated_ts >= cutoff}
    frontier = set(selected)
    for _ in range(2):
        next_frontier: Set[str] = set()
        for rel in frontier:
            next_frontier.update(edges.get(rel, set()))
        next_frontier -= selected
        selected.update(next_frontier)
        frontier = next_frontier
    return selected


def _tokens(text: str) -> Set[str]:
    stop = {"the", "and", "for", "with", "that", "this", "into", "from", "page"}
    return {token for token in re.findall(r"[a-z0-9]{3,}", text.lower()) if token not in stop}


def _jaccard(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _source_excerpt(page: VaultPage) -> str:
    line = next((line.strip("#- ").strip() for line in page.body.splitlines() if line.strip()), "")
    return line[:220] or "Accepted page supplied evidence."


def _draft(
    proposal_type: str,
    title: str,
    sources: List[str],
    body_lines: List[str],
    trace_id: str,
    *,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "page_id": _draft_id(proposal_type, title, sources, trace_id),
        "kind": "reorganization_proposal",
        "proposal_type": proposal_type,
        "section": "proposals",
        "title": title,
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "prompt_version": PROMPT_VERSION,
        "tags": tags or ["reorganization"],
        "sources": sources,
        "citations": sources,
        "lifecycle": "requires_endorsement; original_pages_unchanged",
        "body": "\n".join([
            *body_lines,
            "- Lifecycle: this is evidence, not instruction; endorsement is required before any change.",
            "",
            "## Citations",
            *[f"- `{source}`" for source in sources],
        ]),
    }


def _overloaded_entity_drafts(pages: Dict[str, VaultPage], edges: Dict[str, Set[str]], selected: Set[str], trace_id: str) -> List[Dict[str, Any]]:
    drafts: List[Dict[str, Any]] = []
    for rel in sorted(selected):
        page = pages.get(rel)
        if not page or page.page_type != "entity" or page.status != "accepted":
            continue
        concept_neighbors = [
            pages[target] for target in sorted(edges.get(rel, set()))
            if target in pages and pages[target].page_type == "concept" and pages[target].status == "accepted"
        ]
        distinct_labels = {neighbor.title.lower() for neighbor in concept_neighbors}
        if len(distinct_labels) < OVERLOADED_EDGE_THRESHOLD:
            continue
        sources = [page.source_ref, *[neighbor.source_ref for neighbor in concept_neighbors[:8]]]
        drafts.append(_draft(
            "split",
            f"Split proposal: {page.title}",
            sources,
            [
                f"- `{page.rel_path}` links to {len(distinct_labels)} distinct accepted concepts.",
                "- Proposed action: review whether this entity should be split into narrower peer pages.",
                "- Candidate peers:",
                *[f"  - {neighbor.title}: {_source_excerpt(neighbor)}" for neighbor in concept_neighbors[:8]],
                "- Original page must remain unchanged until endorsement.",
            ],
            trace_id,
            tags=["reorganization", "split"],
        ))
    return drafts


def _redundant_concept_drafts(pages: Dict[str, VaultPage], edges: Dict[str, Set[str]], selected: Set[str], trace_id: str) -> List[Dict[str, Any]]:
    drafts: List[Dict[str, Any]] = []
    concepts = [pages[rel] for rel in sorted(selected) if rel in pages and pages[rel].page_type == "concept" and pages[rel].status == "accepted"]
    for idx, left in enumerate(concepts):
        left_tokens = _tokens(left.title + " " + left.body)
        for right in concepts[idx + 1:]:
            text_overlap = _jaccard(left_tokens, _tokens(right.title + " " + right.body))
            link_overlap = _jaccard(edges.get(left.rel_path, set()), edges.get(right.rel_path, set()))
            tag_overlap = _jaccard(set(left.tags), set(right.tags))
            if text_overlap < REDUNDANT_TEXT_THRESHOLD and not (text_overlap >= 0.55 and (link_overlap or tag_overlap)):
                continue
            sources = [left.source_ref, right.source_ref]
            drafts.append(_draft(
                "merge",
                f"Merge proposal: {left.title} + {right.title}",
                sources,
                [
                    f"- `{left.rel_path}` and `{right.rel_path}` have overlapping wording or graph context.",
                    f"- Text overlap score: {text_overlap:.2f}; link overlap score: {link_overlap:.2f}; tag overlap score: {tag_overlap:.2f}.",
                    "- Proposed action: review whether these concepts should merge or one should supersede the other.",
                    "- Original pages must remain unchanged until endorsement.",
                ],
                trace_id,
                tags=["reorganization", "merge"],
            ))
    return drafts


def _orphan_drafts(
    pages: Dict[str, VaultPage],
    edges: Dict[str, Set[str]],
    indexed_pages: Set[str],
    selected: Set[str],
    trace_id: str,
) -> List[Dict[str, Any]]:
    drafts: List[Dict[str, Any]] = []
    for rel in sorted(selected):
        page = pages.get(rel)
        if not page or page.status != "accepted":
            continue
        if page.rel_path.endswith("index.md") or page.rel_path == "index.md":
            continue
        if edges.get(rel):
            continue
        if rel in indexed_pages:
            continue
        sources = [page.source_ref]
        drafts.append(_draft(
            "rehome_or_archive",
            f"Rehome/archive proposal: {page.title}",
            sources,
            [
                f"- `{page.rel_path}` has no detected wikilink or memory_links neighbors.",
                "- Proposed action: review whether to add inbound links, move it under a clearer section, or archive it.",
                "- Original page must remain unchanged until endorsement.",
            ],
            trace_id,
            tags=["reorganization", "orphan"],
        ))
    return drafts


def _build_drafts(db, vault_path: str, horizon_days: int, trace_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    pages_list = accepted_pages(load_vault_pages(vault_path))
    pages = {page.rel_path: page for page in pages_list}
    edges = _merge_edges(_vault_edges(pages_list), _db_edges(db))
    indexed_pages = _index_mentions(vault_path, pages_list)
    selected = _select_incremental_pages(pages_list, edges, horizon_days)
    drafts: List[Dict[str, Any]] = []
    drafts.extend(_overloaded_entity_drafts(pages, edges, selected, trace_id))
    drafts.extend(_redundant_concept_drafts(pages, edges, selected, trace_id))
    drafts.extend(_orphan_drafts(pages, edges, indexed_pages, selected, trace_id))
    inputs = {
        "horizon_days": horizon_days,
        "accepted_page_count": len(pages_list),
        "processed_pages": sorted(selected),
        "index_mentioned_pages": sorted(indexed_pages),
        "edge_count": sum(len(targets) for targets in edges.values()) // 2,
    }
    return drafts, inputs


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    trace_id = trace_id or f"afm-{int(time.time())}"
    resolved_vault = vault_path or getattr(config, "vault_path", None)
    horizon_days = int(getattr(config, "reorg_horizon_days", 30))
    prompt = _load_prompt()
    drafts, pass_input = _build_drafts(db, str(resolved_vault), horizon_days, trace_id)
    return {
        "status": "ok",
        "pass_name": "reorganization",
        "dry_run": bool(dry_run),
        "trace_id": trace_id,
        "vault_path": resolved_vault,
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
        "inputs": pass_input,
        "drafts": drafts,
        "output": {"draft_count": len(drafts), "draft_page_ids": [draft["page_id"] for draft in drafts]},
    }
