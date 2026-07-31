"""Compact-summary distillation for the AFM loop timer.

The platform hooks harvest each compaction summary RAW into
``<vault>/inbox/*.json`` files with ``kind: 'compact_summary'`` (see
plugins/minni/src/compact-harvest.ts). This pass is the daemon-side consumer:
on the same consolidation tick that ingests stop-candidate learnings, it
splits each summary into sections, asks AFM's guided ``session_distill`` op to
distill each section into a durable learning statement (deterministic
section-flatten fallback when AFM is off/unavailable), and proposes the
results as governance-gated ``candidate_packets`` rows. Nothing here writes a
learning — the operator resolves.

Contract (mirrors afm_passes.inbox_ingest):

* Idempotent via ``derived_from`` ``(inbox_file, candidate_index)`` keys —
  re-runs are no-ops even after candidates resolve. Never deletes files.
* ``derived_from.source`` is ``'inbox'`` ON PURPOSE: that is what
  ``inbox_archive`` keys on, so a compact_summary file whose candidates all
  reach a terminal state is archived by the existing lifecycle pass with no
  new code. ``derived_from.channel`` distinguishes this pass's rows.
* A file whose declared ``agent_id`` mismatches the vault-derived principal is
  skipped (counted) — same provenance rule as ingest.
* Candidate content passes a local path/secret scrub BEFORE insert: summaries
  quote session content verbatim, and the deterministic fallback would
  otherwise carry raw machine-local paths into the proposal queue.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from minni.afm_provider import resolve_afm_mode
from minni.model_provider import default_provider_chain
from minni.safety import is_instruction_like
from minni.afm_passes.inbox_ingest import (
    CONTENT_CAP,
    _content_sha1,
    _existing_keys,
    _principal_for_inbox,
    discover_inboxes,
)

logger = logging.getLogger("sovereign.afm.compact_distillation")

#: File-format tag written by the hook-side harvest.
COMPACT_SUMMARY_KIND = "compact_summary"

#: Upper bound on candidates distilled from one summary file.
MAX_CANDIDATES_PER_FILE = 16

#: Per-section text budget handed to the native session_distill op.
SECTION_AFM_MAX_CHARS = 6000

#: Deterministic-fallback candidate cap (mirrors the Stop drafter's 500).
FALLBACK_CANDIDATE_MAX_CHARS = 500

#: Sections that narrate conversational mechanics, not durable learnings.
_SKIP_SECTION_TITLES = re.compile(
    r"^(all user messages|current work|optional next step|pending tasks)$",
    re.IGNORECASE,
)

_SECTION_HEADING = re.compile(r"^\s*\d+\.\s+([^\n:]{3,80}):?\s*$", re.MULTILINE)


def _redact(text: str) -> str:
    """Local-path / secret scrub for candidate content (subset of the
    afm_provider._safe_status_error patterns, without its 240-char truncation)."""
    text = re.sub(
        r"\b(x-api-key|api[-_]?key|apikey|access[-_]?token|secret[-_]?key)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;)]+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted-key]", text)
    text = re.sub(r"/(?:Users|Volumes|private|var|tmp|Library)/[^\s\"')]+", "[local-path]", text)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s\"')]+", "[local-path]", text)
    return text


def _split_sections(body: str) -> List[Tuple[str, str]]:
    """Numbered-section split ('1. Primary Request and Intent:' …); whole-body
    fallback for summaries without the numbered shape."""
    matches = list(_SECTION_HEADING.finditer(body))
    if len(matches) < 2:
        body = body.strip()
        return [("Session summary", body)] if body else []
    sections: List[Tuple[str, str]] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content:
            sections.append((match.group(1).strip(), content))
    return sections


def _afm_distill_section(chain, title: str, content: str) -> Optional[str]:
    """One guided session_distill call; None on any miss (caller falls back)."""
    try:
        payload_text = f"{title}:\n{content}"[:SECTION_AFM_MAX_CHARS]
        result = chain.native_op("session_distill", {"text": payload_text}, timeout=4.0)
    except Exception:
        logger.exception("compact distill: session_distill raised for %r", title)
        return None
    if not getattr(result, "ok", False):
        return None
    data = result.data if isinstance(result.data, dict) else {}
    distilled_title = str(data.get("title") or "").strip()
    assertion = str(data.get("assertion") or "").strip()
    if not distilled_title or not assertion:
        return None
    applies_when = str(data.get("appliesWhen") or "").strip()
    text = f"{distilled_title}: {assertion}"
    if applies_when:
        text = f"{text} (applies when: {applies_when})"
    return text


def _fallback_candidate(title: str, content: str) -> str:
    flattened = re.sub(r"\s+", " ", f"Compaction summary — {title}: {content}").strip()
    return flattened[:FALLBACK_CANDIDATE_MAX_CHARS]


def _distill_file(doc: Dict[str, Any], afm_chain) -> List[Tuple[str, bool]]:
    """[(candidate_text, afm_distilled)] for one compact_summary document."""
    body = str(doc.get("summary_text") or "")
    candidates: List[Tuple[str, bool]] = []
    for title, content in _split_sections(body):
        if _SKIP_SECTION_TITLES.match(title):
            continue
        distilled = _afm_distill_section(afm_chain, title, content) if afm_chain else None
        afm_used = distilled is not None
        candidate = distilled if afm_used else _fallback_candidate(title, content)
        candidate = _redact(candidate).strip()
        # Post-redaction substance floor: a section that was ONLY paths/keys
        # scrubs to placeholders and teaches nothing.
        if len(re.sub(r"\[(?:local-path|redacted(?:-key)?)\]", "", candidate).strip()) < 20:
            continue
        candidates.append((candidate[:CONTENT_CAP], afm_used))
        if len(candidates) >= MAX_CANDIDATES_PER_FILE:
            break
    return candidates


def distill(db, config, inboxes: Optional[List[Path]] = None,
            fallback_principal: str = "unknown", dry_run: bool = False) -> Dict[str, Any]:
    """Distill eligible compact_summary inbox files into candidate_packets.

    Returns a summary dict. Idempotent at (inbox_file, candidate_index) level;
    never deletes. ``dry_run=True`` reports counts without writing.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)

    mode = resolve_afm_mode()
    afm_chain = default_provider_chain() if mode in {"native", "auto"} else None

    scanned_files = 0
    skipped: Dict[str, int] = {}
    to_insert: List[Dict[str, Any]] = []
    already = 0
    afm_sections = 0

    for inbox in inboxes:
        principal = _principal_for_inbox(inbox, fallback_principal)
        existing = _existing_keys(db, {principal})
        for path in sorted(inbox.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(doc, dict) or doc.get("kind") != COMPACT_SUMMARY_KIND:
                continue
            scanned_files += 1
            file_agent = str(doc.get("agent_id") or "").strip()
            if file_agent and file_agent != principal:
                skipped["_agent_mismatch"] = skipped.get("_agent_mismatch", 0) + 1
                continue
            if not str(doc.get("summary_text") or "").strip():
                skipped["_empty_summary"] = skipped.get("_empty_summary", 0) + 1
                continue
            # File-level idempotency: index 0 always exists for a processed
            # file, so its presence marks the whole file as done. This also
            # holds the AFM/fallback split stable per file — a re-run with a
            # different AFM availability must not append a second variant set.
            if (path.name, 0) in existing:
                already += 1
                continue
            candidates = _distill_file(doc, afm_chain)
            afm_sections += sum(1 for _, used in candidates if used)
            if not candidates:
                skipped["_no_candidates"] = skipped.get("_no_candidates", 0) + 1
                continue
            raw_privacy = doc.get("privacy_level", "safe")
            privacy = str(raw_privacy).strip() if raw_privacy and str(raw_privacy).strip() else "safe"
            workspace = doc.get("workspace_id") or "default"
            for idx, (content, afm_used) in enumerate(candidates):
                existing.add((path.name, idx))
                to_insert.append({
                    "principal": principal,
                    "workspace_id": workspace,
                    "privacy_level": privacy,
                    "content": content,
                    "inbox_file": path.name,
                    "candidate_index": idx,
                    "summary_id": str(doc.get("summary_id") or ""),
                    "platform": str(doc.get("platform") or ""),
                    "afm_distilled": afm_used,
                })

    inserted = 0
    if not dry_run and to_insert:
        now = time.time()
        with db.transaction() as c:
            for r in to_insert:
                derived_from = json.dumps({
                    # 'inbox' + inbox_file is the inbox_archive lifecycle key —
                    # keep it EXACTLY this shape (see module docstring).
                    "source": "inbox",
                    "channel": "compact_distillation",
                    "inbox_file": r["inbox_file"],
                    "candidate_index": r["candidate_index"],
                    "kind": COMPACT_SUMMARY_KIND,
                    "summary_id": r["summary_id"],
                    "platform": r["platform"],
                    "afm_distilled": r["afm_distilled"],
                    "content_sha1": _content_sha1(r["content"]),
                })
                c.execute(
                    """
                    INSERT INTO candidate_packets
                    (principal, workspace_id, layer, privacy_level, content,
                     evidence_refs, derived_from, instruction_like, status, proposed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        r["principal"],
                        r["workspace_id"],
                        None,
                        r["privacy_level"],
                        r["content"],
                        json.dumps([]),
                        derived_from,
                        1 if is_instruction_like(r["content"]) else 0,
                        now,
                    ),
                )
                inserted += 1

    return {
        "inboxes": [str(p) for p in inboxes],
        "files_scanned": scanned_files,
        "files_already_done": already,
        "would_insert": len(to_insert),
        "inserted": inserted,
        "afm_mode": mode,
        "afm_sections": afm_sections,
        "skipped": skipped,
        "dry_run": dry_run,
    }
