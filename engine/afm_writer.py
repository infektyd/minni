"""Single-writer draft queue for AFM loop output."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # RCM-011: for safe_dump of title/tags to prevent newline key injection

from safety import is_instruction_like

logger = logging.getLogger("sovereign.afm.afm_writer")

_WORK_QUEUE: "queue.Queue[tuple[dict, threading.Event, dict]]" = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_PAGE_LOCKS: dict[str, threading.Lock] = {}
_PAGE_LOCKS_LOCK = threading.Lock()
_LATENCIES: List[float] = []
_LAST_RUN_PER_PASS: dict[str, float] = {}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "afm-draft"


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_vault(vault: Path) -> None:
    for rel in ("wiki/sessions", "wiki/entities", "wiki/concepts", "inbox", "logs"):
        (vault / rel).mkdir(parents=True, exist_ok=True)
    for rel, header in (("log.md", "# Minni Log\n\n"), ("index.md", "# Minni Index\n\n")):
        path = vault / rel
        if not path.exists():
            path.write_text(header, encoding="utf-8")


def _append_audit(vault: Path, tool: str, summary: str, details: dict) -> None:
    _ensure_vault(vault)
    ts = _utc()
    line = f"## [{ts}] {tool} | {summary}\n\n```json\n{json.dumps(details, indent=2, sort_keys=True)}\n```\n\n"
    for path in (vault / "log.md", vault / "logs" / f"{ts[:10]}.md"):
        if not path.exists():
            path.write_text(f"# {ts[:10]} Minni Audit\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _page_lock(page_id: str) -> threading.Lock:
    with _PAGE_LOCKS_LOCK:
        if page_id not in _PAGE_LOCKS:
            _PAGE_LOCKS[page_id] = threading.Lock()
        return _PAGE_LOCKS[page_id]


def _quality_blockers(draft: dict) -> List[str]:
    blockers = []
    if not draft.get("sources"):
        blockers.append("missing source citations")
    if not (draft.get("title") or "").strip():
        blockers.append("missing title")
    if not (draft.get("body") or "").strip():
        blockers.append("missing body")
    return blockers


def _contradiction_candidates(draft: dict, writeback: Any = None) -> List[dict]:
    if writeback is None or not hasattr(writeback, "detect_contradictions"):
        return []
    try:
        return list(writeback.detect_contradictions(draft.get("body") or "", agent_id=None) or [])
    except Exception:
        return []


# RCM-010: ported from writeback.py _contains_forged_frontmatter (G09 / SEC-018)
def _contains_forged_frontmatter(content: str) -> bool:
    """Return True if content has a bare '---' line or a code fence containing one.
    Prevents the written .md from having its frontmatter "re-forged" by body content.
    """
    if not content or "---" not in content:
        return False
    for line in content.splitlines():
        if line.strip() == "---":
            return True
    if "```" in content:
        parts = content.split("```")
        for i in range(1, len(parts), 2):
            if "---" in parts[i]:
                for ln in parts[i].splitlines():
                    if ln.strip() == "---":
                        return True
    return False


def _frontmatter(draft: dict, created: str, expires_at: str, gate_status: str, instruction_like: bool = False) -> str:
    # RCM-011: build via dict + yaml.safe_dump (handles multiline title/tags safely, prevents injection)
    prompt_version = draft.get("prompt_version")
    tags = [str(tag) for tag in draft.get("tags", []) if str(tag).strip()]
    fm: dict = {
        "title": draft["title"],
        "type": draft.get("kind", "concept"),
        "status": "draft",
        "agent": "afm-loop",
        "privacy": "safe",
        "trace_id": draft["trace_id"],
        "page_id": draft["page_id"],
        "created": created,
        "expires_at": expires_at,
        "gate_status": gate_status,
    }
    if instruction_like:
        # Finding #4: carried so a later synthesis pass reading this page back via
        # load_vault_pages() sees the flag rather than losing it once it is staged.
        fm["instruction_like"] = True
    if prompt_version:
        fm["prompt_version"] = prompt_version
    if tags:
        fm["tags"] = tags
    sources = draft.get("sources", [])
    if sources:
        fm["sources"] = sources
    citations = draft.get("citations", [])
    if citations:
        fm["citations"] = citations

    header = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
    return "---\n" + header + "---\n\n"


def _write_one(vault: Path, draft: dict, writeback: Any = None) -> dict:
    """Write one AFM draft (or refuse for forged). Return shape:
    - normal/quality-blocked: path/wikilink/status present, written implied by file
    - forged-frontmatter: path=None, wikilink=None, status="blocked", written=False (RCM-010 security)
    """
    blockers = _quality_blockers(draft)
    forged = _contains_forged_frontmatter(draft.get("body", ""))
    if forged:
        blockers = blockers + ["forged-frontmatter (RCM-010 / SEC-018)"]
    contradictions = _contradiction_candidates(draft, writeback)
    gate_status = "blocked" if blockers or contradictions else "ready_for_review"

    # Finding #4: this is the staging point where a synthesized draft becomes a
    # durable vault page. Trust-but-verify the draft's own instruction_like (set by
    # a synthesis pass that inherited it from its source inputs), and OR in a fresh
    # recompute on the final body so the flag can never be laundered away by a
    # pass that forgot to propagate it.
    # Scan the actual text being written — title included: a poisoned title over
    # a benign body (e.g. a concept extracted from an injected heading) lands in
    # the frontmatter and the markdown heading just the same.
    draft_body = draft.get("body") or ""
    own_flag = is_instruction_like(f"{draft.get('title') or ''}\n{draft_body}")
    carried_flag = bool(draft.get("instruction_like"))
    instruction_like = own_flag or carried_flag
    if instruction_like and not own_flag:
        logger.warning(
            "afm_writer staging draft %r (page_id=%s) as instruction_like via "
            "inherited flag; the written body did not itself trip the detector",
            draft.get("title"), draft.get("page_id"),
        )

    section = draft.get("section") or f"{draft.get('kind', 'concept')}s"
    rel = Path("wiki") / section / f"{_utc()[:10].replace('-', '')}-{_slugify(draft['title'])}-{draft['page_id'][-6:]}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    created = _utc()
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 14 * 86400))
    with _page_lock(draft["page_id"]):
        # Title in markdown heading: minimal hygiene sanitize (newlines/# could mangle ATX; not a YAML key injection
        # vector — that is fully protected by safe_dump in _frontmatter per RCM-011). Out of RCM-010/011 YAML scope.
        safe_title = str(draft.get("title", "")).replace("\n", " ").replace("#", "")[:80]
        body = (
            _frontmatter(draft, created, expires_at, gate_status, instruction_like)
            + f"# {safe_title}\n\n"
            + f"{draft.get('body', '').strip()}\n\n"
            + "## Sources\n\n"
            + "\n".join(f"- `{source}`" for source in draft.get("sources", []))
            + "\n"
        )
        if draft.get("citations"):
            body += "\n## Citations\n\n" + "\n".join(f"- `{citation}`" for citation in draft.get("citations", [])) + "\n"
        if blockers or contradictions:
            body += "\n## Gate Notes\n\n"
            for blocker in blockers:
                body += f"- quality-blocked: {blocker}\n"
            for item in contradictions[:5]:
                body += f"- contradiction-candidate: `{item}`\n"
        # Only write if not forged (forged bodies are blocked but note is still produced in return)
        if not forged:
            path.write_text(body, encoding="utf-8")
    # RCM-010: forged cases return null path/wikilink + written:false (callers see refusal shape; no fabricated draft loc)
    if forged:
        return {
            "page_id": draft["page_id"],
            "path": None,
            "wikilink": None,
            "status": "blocked",
            "gate_status": gate_status,
            "blocked": True,
            "blockers": blockers,
            "contradictions": contradictions,
            "written": False,
        }
    return {
        "page_id": draft["page_id"],
        "path": str(rel),
        "wikilink": f"[[{str(rel.with_suffix('')).replace(chr(92), '/') }]]",
        "status": "draft",
        "gate_status": gate_status,
        "blocked": bool(blockers or contradictions),
        "blockers": blockers,
        "contradictions": contradictions,
        "written": True,
    }


def _expire_stale_drafts(vault: Path) -> int:
    expired = 0
    now = time.time()
    for path in (vault / "wiki").glob("**/*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if "status: draft" not in text or "agent: afm-loop" not in text:
            continue
        match = re.search(r"expires_at:\s*([0-9T:Z-]+)", text)
        if not match:
            continue
        try:
            expires = time.mktime(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if expires < now:
            path.write_text(text.replace("status: draft", "status: expired", 1), encoding="utf-8")
            expired += 1
    return expired


def _write_batch(job: dict) -> dict:
    started = time.perf_counter()
    vault = Path(job["vault_path"]).expanduser()
    _ensure_vault(vault)
    expired = _expire_stale_drafts(vault)
    drafts = job.get("drafts") or []
    writeback = job.get("writeback")
    written = [_write_one(vault, draft, writeback=writeback) for draft in drafts]
    inbox_path = vault / "inbox" / f"afm-drafts-{_utc()[:10]}.json"
    payload = {
        "trace_id": job.get("trace_id"),
        "pass_name": job.get("pass_name"),
        "created_at": _utc(),
        "drafts": written,
    }
    existing: List[dict] = []
    if inbox_path.exists():
        try:
            existing = json.loads(inbox_path.read_text(encoding="utf-8")).get("runs", [])
        except Exception:
            existing = []
    inbox_path.write_text(json.dumps({"runs": existing + [payload]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elapsed = time.perf_counter() - started
    _LATENCIES.append(elapsed)
    del _LATENCIES[:-100]
    _LAST_RUN_PER_PASS[job.get("pass_name", "unknown")] = time.time()
    result = {
        "drafts_written": written,
        "inbox_path": str(inbox_path),
        "expired_drafts": expired,
        "latency_ms": round(elapsed * 1000, 3),
    }
    _append_audit(vault, "afm_loop", f"{job.get('pass_name')} wrote {len(written)} draft(s)", {**payload, **result})
    return result


def _worker() -> None:
    while True:
        job, done, out = _WORK_QUEUE.get()
        try:
            out["result"] = _write_batch(job)
        except Exception as exc:
            out["error"] = str(exc)
        finally:
            done.set()
            _WORK_QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            thread = threading.Thread(target=_worker, name="afm-writer", daemon=True)
            thread.start()
            _WORKER_STARTED = True


def submit_drafts(job: dict, wait: bool = True, timeout: Optional[float] = 30.0) -> dict:
    _ensure_worker()
    done = threading.Event()
    out: dict = {}
    _WORK_QUEUE.put((job, done, out))
    if not wait:
        return {"status": "queued", "queue_depth": _WORK_QUEUE.qsize()}
    if not done.wait(timeout):
        return {"status": "queued", "queue_depth": _WORK_QUEUE.qsize(), "timeout": True}
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def writer_status(vault_path: Optional[str] = None) -> dict:
    p95 = 0.0
    if _LATENCIES:
        ordered = sorted(_LATENCIES)
        idx = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
        p95 = round(ordered[idx] * 1000, 3)
    pending = 0
    oldest = None
    if vault_path:
        vault = Path(vault_path).expanduser()
        for path in (vault / "wiki").glob("**/*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if "status: draft" in text and "agent: afm-loop" in text:
                pending += 1
                created = re.search(r"created:\s*([0-9T:Z-]+)", text)
                if created:
                    value = created.group(1)
                    oldest = value if oldest is None else min(oldest, value)
    return {
        "last_run_per_pass": dict(_LAST_RUN_PER_PASS),
        "drafts_pending": pending,
        "drafts_pending_oldest": oldest,
        "afm_latency_p95": p95,
        "status": "ok",
        "queue_depth": _WORK_QUEUE.qsize(),
    }


def endorse_draft(vault_path: str, page_id: str, decision: str) -> dict:
    if decision not in {"accept", "reject", "edit"}:
        raise ValueError("decision must be accept, reject, or edit")
    vault = Path(vault_path).expanduser()
    _ensure_vault(vault)
    target = None
    for path in (vault / "wiki").glob("**/*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if f"page_id: {page_id}" in text:
            target = path
            break
    if target is None:
        raise FileNotFoundError(f"draft page_id not found: {page_id}")
    status = {"accept": "accepted", "reject": "rejected", "edit": "edit_requested"}[decision]
    with _page_lock(page_id):
        text = target.read_text(encoding="utf-8")
        if "status: draft" not in text:
            raise ValueError(f"page is not an active draft: {page_id}")
        target.write_text(text.replace("status: draft", f"status: {status}", 1), encoding="utf-8")
    rel = target.relative_to(vault)
    result = {"status": status, "page_id": page_id, "path": str(rel), "decision": decision}
    _append_audit(vault, "afm_endorse", f"{decision}: {page_id}", result)
    return result
