"""Pruning pass for the opt-in AFM loop.

The pass emits inbox proposals only. It never mutates or deletes source pages.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from afm_passes._graph_utils import FRONTMATTER_RE

PROMPT_VERSION = "pruning.v1"
HYGIENE_DECAY_FLOOR = 0.05


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_prompt() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "afm_prompts" / "pruning.md"
    return prompt_path.read_text(encoding="utf-8")


def _proposal_id(kind: str, path: str, trace_id: str) -> str:
    digest = hashlib.sha1("|".join(["pruning", kind, path, trace_id]).encode("utf-8")).hexdigest()[:10]
    return f"afm-pruning-{kind}-{digest}"


def _frontmatter(path: Path) -> Dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip().lower()] = value.strip().strip("\"'")
    return data


def _parse_time(value: str) -> Optional[float]:
    if not value:
        return None
    value = value.strip().replace("+00:00", "Z")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def _db_documents(db) -> List[Dict[str, Any]]:
    with db.cursor() as c:
        c.execute(
            """
            SELECT doc_id, path, page_status, page_type, expires_at, decay_score, access_count
            FROM documents
            ORDER BY path
            """
        )
        return [dict(row) for row in c.fetchall()]


def _transition(path: str, from_status: str, to_status: str, reason: str, trace_id: str) -> Dict[str, Any]:
    return {
        "proposal_id": _proposal_id("transition", path, trace_id),
        "proposal_type": "status_transition",
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "path": path,
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
        "lifecycle": "requires_endorsement; original_page_unchanged",
    }


def _finding(path: str, reason: str, trace_id: str) -> Dict[str, Any]:
    return {
        "proposal_id": _proposal_id("hygiene", path, trace_id),
        "proposal_type": "hygiene_finding",
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "path": path,
        "severity": "warn",
        "reason": reason,
        "lifecycle": "requires_endorsement; original_page_unchanged",
    }


def _build_proposals(db, vault_path: str, trace_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    vault = Path(vault_path).expanduser()
    now = time.time()
    proposals: List[Dict[str, Any]] = []
    rows = _db_documents(db)
    accepted_count = 0
    for row in rows:
        path = str(row["path"])
        status = row.get("page_status") or "candidate"
        if status != "accepted":
            continue
        accepted_count += 1
        page_path = vault / path
        fm = _frontmatter(page_path)
        expires_at = row.get("expires_at")
        if not expires_at:
            expires_at = _parse_time(fm.get("expires_at", ""))
        if expires_at and float(expires_at) < now:
            proposals.append(_transition(path, "accepted", "expired", "expires_at is in the past", trace_id))
        if fm.get("superseded_by"):
            proposals.append(_transition(path, "accepted", "candidate", "frontmatter references superseded evidence", trace_id))
        decay = float(row.get("decay_score") if row.get("decay_score") is not None else 1.0)
        access_count = int(row.get("access_count") or 0)
        if decay < HYGIENE_DECAY_FLOOR and access_count == 0:
            proposals.append(_finding(path, f"accepted page has decay_score={decay:.3f} and access_count=0", trace_id))
        if page_path.exists():
            required = {"title", "status", "privacy", "type"}
            missing = sorted(required - set(fm))
            if missing:
                proposals.append(_finding(path, f"accepted page missing frontmatter keys: {', '.join(missing)}", trace_id))
    return proposals, {
        "document_count": len(rows),
        "accepted_document_count": accepted_count,
    }


def _append_audit(vault: Path, trace_id: str, proposal_count: int, inbox_rel: Optional[str]) -> None:
    ts = _utc()
    for rel, header in (("log.md", "# Sovereign Memory Log\n\n"), (f"logs/{ts[:10]}.md", f"# {ts[:10]} Sovereign Memory Audit\n\n")):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(header, encoding="utf-8")
        details = {"trace_id": trace_id, "proposal_count": proposal_count, "inbox_path": inbox_rel}
        line = f"## [{ts}] afm_loop | pruning proposed {proposal_count} transition(s)\n\n```json\n{json.dumps(details, indent=2, sort_keys=True)}\n```\n\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _write_inbox(vault_path: str, trace_id: str, proposals: List[Dict[str, Any]]) -> Dict[str, Any]:
    vault = Path(vault_path).expanduser()
    inbox = vault / "inbox" / f"afm-pruning-{_utc()[:10]}.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "trace_id": trace_id,
        "pass_name": "pruning",
        "created_at": _utc(),
        "proposals": proposals,
    }
    existing: List[dict] = []
    if inbox.exists():
        try:
            existing = json.loads(inbox.read_text(encoding="utf-8")).get("runs", [])
        except Exception:
            existing = []
    inbox.write_text(json.dumps({"runs": existing + [payload]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rel = str(inbox.relative_to(vault))
    _append_audit(vault, trace_id, len(proposals), rel)
    return {"path": rel, "proposal_count": len(proposals)}


def run(db, config, vault_path: Optional[str] = None, dry_run: bool = True, trace_id: Optional[str] = None) -> Dict[str, Any]:
    trace_id = trace_id or f"afm-{int(time.time())}"
    resolved_vault = vault_path or getattr(config, "vault_path", None)
    prompt = _load_prompt()
    proposals, pass_input = _build_proposals(db, str(resolved_vault), trace_id)
    result: Dict[str, Any] = {
        "status": "ok",
        "pass_name": "pruning",
        "dry_run": bool(dry_run),
        "trace_id": trace_id,
        "vault_path": resolved_vault,
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
        "inputs": pass_input,
        "proposals": proposals,
        "drafts": [],
        "output": {"proposal_count": len(proposals), "proposal_ids": [proposal["proposal_id"] for proposal in proposals]},
    }
    if not dry_run and proposals:
        result["inbox_written"] = _write_inbox(str(resolved_vault), trace_id, proposals)
    return result
