"""Read-only vault/wiki hygiene checks for Sovereign Memory.

The checker writes a Markdown report plus JSON summary under ``logs/`` in the
target vault. It does not modify wiki pages, indexes, or SQLite state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
VALID_STATUSES = {"draft", "candidate", "accepted", "superseded", "rejected"}
VALID_PRIVACY = {"safe", "private", "sensitive", "blocked"}


@dataclass
class Page:
    path: Path
    rel: str
    body: str
    frontmatter: Dict[str, object]
    wikilinks: List[str]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_scalar(value: str):
    value = value.strip()
    if value in ("[]", ""):
        return [] if value == "[]" else ""
    return value.strip("\"'")


def _parse_frontmatter(text: str) -> Tuple[Dict[str, object], str, bool]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text, False
    raw = match.group(1)
    data: Dict[str, object] = {}
    current_key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(_parse_scalar(line[4:]))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            current_key = key.strip()
            data[current_key] = _parse_scalar(value)
    return data, text[match.end():], True


def _load_pages(vault: Path) -> List[Page]:
    pages = []
    for root in (vault / "wiki",):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            text = _read_text(path)
            frontmatter, body, _ = _parse_frontmatter(text)
            rel = path.relative_to(vault).as_posix()
            pages.append(Page(path, rel, body, frontmatter, WIKILINK_RE.findall(body)))
    return pages


def _targets_for_pages(pages: Iterable[Page]) -> Dict[str, Page]:
    targets = {}
    for page in pages:
        stem = page.path.with_suffix("").name
        no_ext = page.rel[:-3] if page.rel.endswith(".md") else page.rel
        targets[stem] = page
        targets[no_ext] = page
        targets[f"/{no_ext}"] = page
    return targets


def _add(findings: Dict[str, List[dict]], severity: str, check: str, path: str, message: str) -> None:
    findings[severity].append({
        "severity": severity,
        "check": check,
        "path": path,
        "message": message,
    })


def _source_values(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [value.strip()]
    return [str(value)]


def run_hygiene_report(vault: str | Path) -> dict:
    vault = Path(vault).expanduser()
    logs_dir = vault / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    findings = {"block": [], "warn": [], "info": []}
    pages = _load_pages(vault)
    targets = _targets_for_pages(pages)
    linked_pages = set()

    # 1. broken wikilinks
    for page in pages:
        for target in page.wikilinks:
            if target not in targets:
                _add(findings, "block", "broken_wikilinks", page.rel, f"Unresolved wikilink [[{target}]]")
            else:
                linked_pages.add(targets[target].rel)

    # 2. missing sources
    for page in pages:
        if page.rel.startswith("wiki/") and not _source_values(page.frontmatter.get("sources")):
            _add(findings, "warn", "missing_sources", page.rel, "Wiki page has no sources frontmatter")

    # 3. status drift
    for page in pages:
        status = str(page.frontmatter.get("status", "")).strip()
        if status == "superseded" and not page.frontmatter.get("superseded_by"):
            _add(findings, "warn", "status_drift", page.rel, "Superseded page is missing superseded_by")
        if status == "rejected" and page.rel in linked_pages:
            _add(findings, "warn", "status_drift", page.rel, "Rejected page is still linked by another page")

    # 4. orphan pages
    for page in pages:
        if page.rel not in linked_pages and page.path.name not in {"index.md"}:
            _add(findings, "info", "orphan_pages", page.rel, "Page has no inbound wikilinks")

    # 5. frontmatter violations
    for page in pages:
        text = _read_text(page.path)
        _, _, has_frontmatter = _parse_frontmatter(text)
        if not has_frontmatter:
            _add(findings, "block", "frontmatter_violations", page.rel, "Missing YAML frontmatter")
            continue
        for required in ("title", "status", "privacy", "type"):
            if not page.frontmatter.get(required):
                _add(findings, "block", "frontmatter_violations", page.rel, f"Missing {required}")
        status = str(page.frontmatter.get("status", "")).strip()
        privacy = str(page.frontmatter.get("privacy", "")).strip()
        if status and status not in VALID_STATUSES:
            _add(findings, "block", "frontmatter_violations", page.rel, f"Unknown status {status}")
        if privacy and privacy not in VALID_PRIVACY:
            _add(findings, "block", "frontmatter_violations", page.rel, f"Unknown privacy {privacy}")

    # 6. privacy mismatches
    private_terms = ("secret", "token", "password", "private key", "api key")
    for page in pages:
        privacy = str(page.frontmatter.get("privacy", "safe")).strip() or "safe"
        body_lower = page.body.lower()
        if privacy == "safe" and any(term in body_lower for term in private_terms):
            _add(findings, "block", "privacy_mismatches", page.rel, "Safe page appears to contain private material")
        if privacy == "blocked" and str(page.frontmatter.get("status", "")) == "accepted":
            _add(findings, "block", "privacy_mismatches", page.rel, "Blocked page should not be accepted")

    # 7. contradictions
    for page in pages:
        contradictions = _source_values(page.frontmatter.get("contradictions"))
        if contradictions:
            _add(findings, "warn", "contradictions", page.rel, "Page declares contradictions")
        if str(page.frontmatter.get("status", "")) == "contradiction":
            _add(findings, "warn", "contradictions", page.rel, "Page status is contradiction")

    # 8. index/log drift
    index_text = _read_text(vault / "index.md") if (vault / "index.md").exists() else ""
    log_text = _read_text(vault / "log.md") if (vault / "log.md").exists() else ""
    for page in pages:
        no_ext = page.rel[:-3] if page.rel.endswith(".md") else page.rel
        if no_ext not in index_text and f"[[{no_ext}]]" not in index_text and f"[[{page.path.stem}]]" not in index_text:
            _add(findings, "info", "index_log_drift", page.rel, "Page is not referenced from index.md")
    if pages and not log_text.strip():
        _add(findings, "info", "index_log_drift", "log.md", "Vault has pages but empty log.md")

    counts = {severity: len(items) for severity, items in findings.items()}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = logs_dir / f"hygiene-{today}.md"
    json_path = logs_dir / f"hygiene-{today}.json"

    summary = {
        "status": "ok",
        "vault": str(vault),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "checks": [
            "broken_wikilinks",
            "missing_sources",
            "status_drift",
            "orphan_pages",
            "frontmatter_violations",
            "privacy_mismatches",
            "contradictions",
            "index_log_drift",
        ],
        "findings": findings,
        "report_path": str(report_path),
        "json_path": str(json_path),
    }

    lines = [
        f"# Sovereign Memory Hygiene Report - {today}",
        "",
        f"Vault: `{vault}`",
        "",
        f"Counts: block={counts['block']} warn={counts['warn']} info={counts['info']}",
    ]
    for severity in ("block", "warn", "info"):
        lines.extend(["", f"## {severity.upper()}"])
        if findings[severity]:
            for item in findings[severity]:
                lines.append(f"- `{item['check']}` `{item['path']}` - {item['message']}")
        else:
            lines.append("- None")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
