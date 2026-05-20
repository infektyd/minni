"""Vault graph helpers for AFM compile passes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Dict, Iterable, List, Optional, Set


WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class VaultPage:
    rel_path: str
    path: Path
    title: str
    page_type: str
    status: str
    tags: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    wikilinks: List[str] = field(default_factory=list)
    body: str = ""
    updated_ts: float = 0.0

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def source_ref(self) -> str:
        return f"vault:{self.rel_path}"


def _parse_list(value: str) -> List[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip().strip("\"'") for item in value.split(",") if item.strip()]


def _parse_time(value: str, fallback: float) -> float:
    if not value:
        return fallback
    value = value.strip().replace("+00:00", "Z")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return fallback


def _parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw[key.strip().lower()] = value.strip().strip("\"'")
    return raw, text[match.end():]


def load_vault_pages(vault_path: str) -> List[VaultPage]:
    vault = Path(vault_path).expanduser()
    wiki = vault / "wiki"
    if not wiki.exists():
        return []

    pages: List[VaultPage] = []
    for path in sorted(wiki.glob("**/*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, body = _parse_frontmatter(text)
        try:
            rel_path = str(path.relative_to(vault))
        except ValueError:
            rel_path = str(path)
        stat_mtime = path.stat().st_mtime
        pages.append(
            VaultPage(
                rel_path=rel_path,
                path=path,
                title=frontmatter.get("title") or path.stem.replace("-", " ").title(),
                page_type=(frontmatter.get("type") or "unknown").lower(),
                status=(frontmatter.get("status") or "candidate").lower(),
                tags=[tag.lower() for tag in _parse_list(frontmatter.get("tags", ""))],
                sources=_parse_list(frontmatter.get("sources", "")),
                wikilinks=WIKILINK_RE.findall(body),
                body=body.strip(),
                updated_ts=_parse_time(frontmatter.get("updated") or frontmatter.get("created") or "", stat_mtime),
            )
        )
    return pages


def accepted_pages(pages: Iterable[VaultPage]) -> List[VaultPage]:
    return [page for page in pages if page.status == "accepted" and page.page_type != "synthesis"]


def pages_by_tag(pages: Iterable[VaultPage]) -> Dict[str, List[VaultPage]]:
    clusters: Dict[str, List[VaultPage]] = {}
    for page in pages:
        for tag in page.tags:
            clusters.setdefault(tag, []).append(page)
    return clusters


def synthesis_pages_by_tag(pages: Iterable[VaultPage]) -> Dict[str, List[VaultPage]]:
    clusters: Dict[str, List[VaultPage]] = {}
    for page in pages:
        if page.page_type != "synthesis":
            continue
        for tag in page.tags:
            clusters.setdefault(tag, []).append(page)
    return clusters


def wikilink_neighborhoods(pages: Iterable[VaultPage], min_size: int = 3) -> List[List[VaultPage]]:
    page_list = list(pages)
    by_slug = {page.slug: page for page in page_list}
    by_title = {page.title: page for page in page_list}
    graph: Dict[str, Set[str]] = {page.rel_path: set() for page in page_list}
    for page in page_list:
        for target in page.wikilinks:
            target_page = by_slug.get(target) or by_title.get(target)
            if not target_page:
                continue
            graph[page.rel_path].add(target_page.rel_path)
            graph[target_page.rel_path].add(page.rel_path)

    seen: Set[str] = set()
    neighborhoods: List[List[VaultPage]] = []
    by_rel = {page.rel_path: page for page in page_list}
    for rel in sorted(graph):
        if rel in seen:
            continue
        stack = [rel]
        component: List[VaultPage] = []
        seen.add(rel)
        while stack:
            current = stack.pop()
            component.append(by_rel[current])
            for neighbor in graph[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        if len(component) >= min_size:
            neighborhoods.append(sorted(component, key=lambda page: page.rel_path))
    return neighborhoods


def newest_timestamp(pages: Iterable[VaultPage]) -> float:
    return max((page.updated_ts for page in pages), default=0.0)
