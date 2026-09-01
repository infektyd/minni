"""Compact-summary distillation for the AFM loop timer.

The platform hooks harvest each compaction summary RAW into
``<vault>/inbox/*.json`` files with ``kind: 'compact_summary'`` (see
plugins/minni/src/compact-harvest.ts). This pass is the daemon-side consumer:
on the same consolidation tick that ingests stop-candidate learnings, it
splits each summary into sections, routes each section to an AUDIENCE, asks
AFM's guided ``session_distill`` op to distill the shared-audience ones into a
durable learning statement (deterministic section-flatten fallback when AFM is
off/unavailable), and proposes those as governance-gated ``candidate_packets``
rows. Nothing here writes a learning — the operator resolves.

Audience routing
----------------
A compaction summary mixes two very different things: transferable technical
knowledge ("launchctl error 5 right after bootout is a teardown race") and
session-personal narration ("the codebase is in a clean state", "the user asked
me to push"). Proposing ALL of it put session-personal content into the SHARED
learnings pool, where the consolidation loop auto-accepted it.

So each section is classified by title: ``_SHARED_SECTION_TITLES`` sections
become candidates, everything else is personal and produces no candidate row at
all. The one exception is the unsectioned whole-body fallback, which is
personal unless AFM actually distilled it into a crisp assertion.

Personal content is not discarded — every processed file is written as a
session note into the SOURCE vault's ``wiki/sessions/``, where the vault-watch
sweep indexes it for that agent alone.

Contract (mirrors afm_passes.inbox_ingest):

* Idempotent via ``derived_from`` ``(inbox_file, candidate_index)`` keys —
  re-runs are no-ops even after candidates resolve. Never deletes files.
* ``derived_from.source`` is ``'inbox'`` ON PURPOSE: it makes the row
  recognizable to ``inbox_archive._derived_inbox_file`` (a shared naming
  contract), though the resolve-time drain lifecycle in that module does NOT
  actually archive these files itself — it only understands the
  stop-candidate file shape. ``derived_from.channel`` distinguishes this
  pass's rows from that channel's.
* A file whose declared ``agent_id`` mismatches the vault-derived principal is
  skipped (counted) — same provenance rule as ingest.
* A file is archived immediately (never deleted) by THIS pass, once its
  candidate rows are durably inserted (or, for the zero-shared case, once its
  session note is written) — idempotency lives entirely on the candidate
  rows' ``derived_from`` keys, so the file itself is never read again
  regardless of outcome. A file whose candidates were already inserted by a
  prior run (the file-level idempotency short-circuit below) but never got
  archived — e.g. one processed before this archive-on-insert behavior
  shipped — is swept the same way on its next scan.
  Without this, files that DO yield shared candidates would sit in the inbox
  forever: their idempotency key already prevents reprocessing, so they are
  rescanned-and-skipped on every tick and inflate pending-inbox counts (see
  the compact-inbox-archive-gap fix) with no lifecycle event ever draining
  them, since consolidation auto-accepts these candidates without going
  through the resolve-time drain-on-resolution path that only understands the
  stop-candidate file shape.
* Candidate content passes a local path/secret scrub BEFORE insert: summaries
  quote session content verbatim, and the deterministic fallback would
  otherwise carry raw machine-local paths into the proposal queue.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from minni.afm_provider import resolve_afm_mode
from minni.model_provider import default_provider_chain
from minni.safety import is_instruction_like
from minni.afm_passes.inbox_archive import archive_inbox_file
from minni.afm_passes.inbox_ingest import (
    CONTENT_CAP,
    _canonical_principal,
    _coerce_candidate_index,
    _content_sha1,
    _existing_keys,
    _existing_keys_for_on_cursor,
    _is_unique_integrity_error,
    _make_inbox_key,
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

#: Sections whose content is transferable knowledge, not session narration.
#: ONLY these reach the shared candidate queue (see module docstring).
_SHARED_SECTION_TITLES = re.compile(
    r"^(key technical concepts|errors and fixes|problem solving|key learnings|learnings|decisions)$",
    re.IGNORECASE,
)

_SECTION_HEADING = re.compile(r"^\s*\d+\.\s+([^\n:]{3,80}):?\s*$", re.MULTILINE)

#: Synthetic title of the whole-body fallback for unsectioned summaries.
UNSECTIONED_TITLE = "Session summary"

#: Vault-relative directory the personal session notes are written to.
SESSION_NOTE_DIR = ("wiki", "sessions")


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
        return [(UNSECTIONED_TITLE, body)] if body else []
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


def _distill_file(
    doc: Dict[str, Any], afm_chain
) -> Tuple[List[Tuple[str, bool]], int, Dict[str, int]]:
    """``([(candidate_text, afm_distilled)], personal_sections, dropped)`` for
    one compact_summary document. Only shared-audience sections yield
    candidates; personal ones are merely counted (their content reaches the
    agent's own vault via the session note, never the shared proposal queue).

    AFM-9 (#230): ``dropped`` counts shared sections that were discarded
    mid-distillation. They used to vanish on a bare ``continue`` — no counter,
    no log line — so there was no surface anywhere that could say how much
    distillation input was being thrown away."""
    body = str(doc.get("summary_text") or "")
    sections = _split_sections(body)
    unsectioned = len(sections) == 1 and sections[0][0] == UNSECTIONED_TITLE
    candidates: List[Tuple[str, bool]] = []
    personal = 0
    dropped: Dict[str, int] = {}
    for section_index, (title, content) in enumerate(sections):
        if _SKIP_SECTION_TITLES.match(title):
            continue
        shared = bool(_SHARED_SECTION_TITLES.match(title))
        # AFM is spent only where it can change the outcome: on shared sections,
        # and on the unsectioned body whose upgrade to shared depends on it.
        distilled = (
            _afm_distill_section(afm_chain, title, content)
            if afm_chain and (shared or unsectioned)
            else None
        )
        if not shared:
            # The whole-body fallback earns the shared pool only when AFM turned
            # it into a crisp assertion; with AFM off or missing, and for every
            # named narration section, the content stays personal.
            if not (unsectioned and distilled is not None):
                personal += 1
                continue
        afm_used = distilled is not None
        candidate = distilled if afm_used else _fallback_candidate(title, content)
        candidate = _redact(candidate).strip()
        # Post-redaction substance floor: a section that was ONLY paths/keys
        # scrubs to placeholders and teaches nothing.
        if len(re.sub(r"\[(?:local-path|redacted(?:-key)?)\]", "", candidate).strip()) < 20:
            dropped["_below_substance_floor"] = dropped.get("_below_substance_floor", 0) + 1
            continue
        candidates.append((candidate[:CONTENT_CAP], afm_used))
        if len(candidates) >= MAX_CANDIDATES_PER_FILE:
            # Sections past the cap are not distilled at all. Counting them
            # keeps the cap from reading as "the file only had this much".
            # Round 6 (PR #260): count only the candidate-ELIGIBLE tail —
            # shared, not skip-titled, the same predicate the loop applies.
            # The raw tail also held personal/skip sections that were never
            # distillation input, and counting those overstated how much
            # shared input the cap threw away — the exact number AFM-9
            # exists to answer.
            remaining = sum(
                1
                for title, _content in sections[section_index + 1:]
                if not _SKIP_SECTION_TITLES.match(title)
                and _SHARED_SECTION_TITLES.match(title)
            )
            if remaining > 0:
                dropped["_over_candidate_cap"] = remaining
            break
    return candidates, personal, dropped


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _doc_timestamp(doc: Dict[str, Any]) -> Tuple[str, str]:
    """``(YYYYMMDD, ISO8601)`` taken from the document's OWN timestamp. Wall
    clock is used only when the document carries none — the note filename must
    stay stable across re-runs, and ``now()`` would remint it every tick."""
    for key in ("createdAt", "summary_timestamp"):
        raw = doc.get(key)
        if isinstance(raw, str) and _ISO_DATE.match(raw.strip()):
            iso = raw.strip()
            return iso[:10].replace("-", ""), iso
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            epoch = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
            stamp = time.gmtime(epoch)
            return time.strftime("%Y%m%d", stamp), time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)
    now = time.gmtime()
    return time.strftime("%Y%m%d", now), time.strftime("%Y-%m-%dT%H:%M:%SZ", now)


def _slug_fragment(value: Any, fallback: str) -> str:
    """First 8 filename-safe chars of ``value`` (session ids and hashes are
    document-controlled, so nothing but ``[a-z0-9-]`` survives)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:8].strip("-") or fallback


def _session_note_text(doc: Dict[str, Any], principal: str, inbox_file: str, iso: str) -> str:
    """Frontmatter + full redacted summary body, in the wiki/sessions format."""
    platform = str(doc.get("platform") or "").strip()
    title = f"Compact session summary — {platform or principal} {iso[:10]}"
    fm = {
        "title": title,
        "type": "session",
        "status": "candidate",
        "privacy": str(doc.get("privacy_level") or "safe").strip() or "safe",
        "source": f"compact_distillation:{inbox_file}",
        "created": iso,
        "section": "sessions",
        "agent": principal,
        "category": "session-context",
        "audience": "personal",
        "minni_learning": False,
        "platform": platform,
        "session_id": str(doc.get("session_id") or ""),
        "summary_sha1": str(doc.get("summary_sha1") or ""),
        "harvested": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body = _redact(str(doc.get("summary_text") or "")).strip()
    # A bare '---' line in the summary would re-forge this note's frontmatter
    # (same hazard afm_writer._contains_forged_frontmatter guards against).
    body = re.sub(r"(?m)^\s*---\s*$", "***", body)
    header = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{header}---\n\n# {title}\n\n{body}\n"


def _write_session_note(vault: Path, doc: Dict[str, Any], inbox_file: str,
                        principal: str) -> Optional[Path]:
    """Personal leg: the full summary as a ``wiki/sessions`` note in the SOURCE
    vault, where the vault-watch sweep indexes it for this agent alone.

    Returns the path when a NEW note was written; ``None`` when one already
    existed (idempotent) or the write failed."""
    date, iso = _doc_timestamp(doc)
    sid = _slug_fragment(doc.get("session_id"), "session")
    sha = _slug_fragment(doc.get("summary_sha1"), "") or _content_sha1(
        str(doc.get("summary_text") or ""))[:8]
    path = vault.joinpath(*SESSION_NOTE_DIR) / f"{date}-compact-{sid}-{sha}.md"
    if path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_session_note_text(doc, principal, inbox_file, iso), encoding="utf-8")
    except OSError:
        logger.exception("compact distill: session note write failed for %s", path)
        return None
    return path


# #336: why a compact-summary inbox file is unusable. These mirror the four
# skip branches in `distill` below that discard input with no archive and no
# note — every one of them is deterministic, so the same file produces the
# identical skip on every future tick and none can self-heal.
# The first two are decided BEFORE the kind gate, so they apply to any *.json
# in the shared inbox regardless of which writer owns it — an unparseable file
# has no kind to dispatch on. Named for that reality rather than for this
# channel: a corrupt handoff quarantined under a "compact" reason would
# misroute an operator's triage.
COMPACT_UNREADABLE = "_inbox_unreadable"
COMPACT_MALFORMED = "_inbox_malformed"
COMPACT_AGENT_MISMATCH = "_compact_agent_mismatch"
COMPACT_EMPTY_SUMMARY = "_compact_empty_summary"


def classify_unusable_compact_file(path: Path, principal: str) -> Optional[str]:
    """Why this inbox file is unusable to the distillation pass, or None.

    ONE classifier, used by both the health count and the quarantine drain.
    Two implementations would drift, and a count that disagrees with its drain
    is a number that can never reach zero — a signal permanently overstating
    the problem it is meant to report.

    Mirrors `distill`'s skip branches exactly. A READABLE file of another
    kind, and a compact summary the pass can still consume, return None:
    draining either would destroy real memory input.

    The unreadable/non-object cases are deliberately kind-AGNOSTIC — a file
    that will not parse has no kind to dispatch on, and no consumer anywhere
    can read it (inbox_ingest, handoff and the TS reader all apply the same
    json.loads + isinstance(dict) test). They are reported under inbox-wide
    reasons so triage is not misrouted to this channel.
    """
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return COMPACT_UNREADABLE
    if not isinstance(doc, dict):
        return COMPACT_MALFORMED
    if doc.get("kind") != COMPACT_SUMMARY_KIND:
        return None  # routing: another kind's pass owns this file
    file_agent = str(doc.get("agent_id") or "").strip()
    if file_agent and _canonical_principal(file_agent) != principal:
        return COMPACT_AGENT_MISMATCH
    if not str(doc.get("summary_text") or "").strip():
        return COMPACT_EMPTY_SUMMARY
    return None


def count_unusable_compact_files(
    inboxes: Optional[List[Path]] = None,
    *,
    fallback_principal: str,
    config: Any = None,
) -> Dict[str, Any]:
    """How many inbox files the distillation pass currently cannot use.

    #307: a cumulative counter of drop EVENTS is files x ticks-since-boot,
    because a dropped file used to stay in the inbox and be re-dropped every
    tick. This is the quantity that is actually true and that clears once the
    file is drained (#336).

    ``fallback_principal`` is REQUIRED, not defaulted. For a bare
    ``vault/inbox`` the principal IS the fallback, so a caller that let it
    default while the drain passed the configured value would classify a
    different set of files: the count would flag one the pass consumes happily
    and miss the one actually drained, and it could never reach zero after a
    drain. Making it required means that divergence cannot be reintroduced
    silently — the two callers must agree or the code does not run.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)
    files = 0
    for inbox in inboxes:
        principal = _principal_for_inbox(Path(inbox), fallback_principal)
        for path in sorted(Path(inbox).glob("*.json")):
            if classify_unusable_compact_file(path, principal) is not None:
                files += 1
    return {"files": files}


def distill(db, config, inboxes: Optional[List[Path]] = None,
            fallback_principal: str = "unknown", dry_run: bool = False) -> Dict[str, Any]:
    """Distill eligible compact_summary inbox files into candidate_packets.

    Returns a summary dict. Idempotent at
    (principal, inbox_file, candidate_index) level; never deletes.
    ``dry_run=True`` reports counts without writing.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)

    mode = resolve_afm_mode()
    afm_chain = default_provider_chain() if mode in {"native", "auto"} else None

    scanned_files = 0
    skipped: Dict[str, int] = {}
    dropped_sections: Dict[str, int] = {}
    to_insert: List[Dict[str, Any]] = []
    already = 0
    afm_sections = 0
    personal_sections = 0
    notes_written = 0
    archived_zero_shared = 0
    archived_with_shared = 0
    to_archive_with_shared: List[Path] = []

    for inbox in inboxes:
        principal = _principal_for_inbox(inbox, fallback_principal)
        existing = _existing_keys(db, {principal})
        for path in sorted(inbox.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                # AFM-9 (#230): a malformed payload used to be dropped on a
                # bare `continue` — no counter, no log line, no way to know
                # from any surface how much input was being discarded.
                skipped["_unreadable"] = skipped.get("_unreadable", 0) + 1
                logger.warning(
                    "compact distill: dropping unreadable inbox file %s: %s",
                    path.name, exc,
                )
                continue
            if not isinstance(doc, dict):
                skipped["_malformed"] = skipped.get("_malformed", 0) + 1
                logger.warning(
                    "compact distill: dropping malformed inbox file %s "
                    "(payload is %s, expected object)",
                    path.name, type(doc).__name__,
                )
                continue
            if doc.get("kind") != COMPACT_SUMMARY_KIND:
                # Not a drop: other kinds legitimately share this inbox and are
                # drained by their own pass. Counted, not warned.
                skipped["_other_kind"] = skipped.get("_other_kind", 0) + 1
                continue
            scanned_files += 1
            file_agent = str(doc.get("agent_id") or "").strip()
            if file_agent and _canonical_principal(file_agent) != principal:
                skipped["_agent_mismatch"] = skipped.get("_agent_mismatch", 0) + 1
                continue
            if not str(doc.get("summary_text") or "").strip():
                skipped["_empty_summary"] = skipped.get("_empty_summary", 0) + 1
                continue
            # File-level idempotency: index 0 always exists for a processed
            # file, so its presence marks the whole file as done. This also
            # holds the AFM/fallback split stable per file — a re-run with a
            # different AFM availability must not append a second variant set.
            # Canonicalize so leftover gemini/grok-build rows match agy/xai
            # fallback principals (same app key as inbox_ingest).
            file_key0 = _make_inbox_key(principal, path.name, 0)
            if file_key0 in existing:
                already += 1
                # Legacy sweep: a file processed by a pre-fix daemon build has
                # its candidate rows sitting in the DB already but was never
                # archived (this pass's own historical bug). Its content is
                # fully captured by those rows regardless of their resolution
                # status, so it is safe — and necessary — to archive it here
                # too, or it would keep being rescanned-and-skipped forever.
                if not dry_run and archive_inbox_file(path):
                    archived_with_shared += 1
                continue
            candidates, personal, file_dropped = _distill_file(doc, afm_chain)
            afm_sections += sum(1 for _, used in candidates if used)
            personal_sections += personal
            for reason, count in file_dropped.items():
                dropped_sections[reason] = dropped_sections.get(reason, 0) + count
            if not dry_run:
                # Personal leg runs for EVERY processed file, whatever the
                # audience mix — the vault note is the only place the full
                # session context is kept.
                if _write_session_note(inbox.parent, doc, path.name, principal):
                    notes_written += 1
            if not candidates:
                skipped["_no_candidates"] = skipped.get("_no_candidates", 0) + 1
                # No candidate rows means no idempotency key, so this file would
                # be rescanned on every tick forever. Its content is now in the
                # vault note, so retire it through the archive lifecycle (moved,
                # never deleted).
                if not dry_run and archive_inbox_file(path):
                    archived_zero_shared += 1
                continue
            # This file's content now lives entirely in inserted candidate rows
            # (plus the session note above) — idempotency is keyed on those
            # rows' derived_from, not on the file, so it can be archived as
            # soon as they are durably inserted. Deferred until after the
            # insert transaction below succeeds (never archive-before-insert).
            if not dry_run:
                to_archive_with_shared.append(path)
            raw_privacy = doc.get("privacy_level", "safe")
            privacy = str(raw_privacy).strip() if raw_privacy and str(raw_privacy).strip() else "safe"
            workspace = doc.get("workspace_id") or "default"
            for idx, (content, afm_used) in enumerate(candidates):
                existing.add(_make_inbox_key(principal, path.name, idx))
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
            # Issue #239: re-load only to_insert keys under BEGIN IMMEDIATE
            # (not full-table scan). Mirror inbox_ingest: narrow in-txn check
            # + UNIQUE swallow. Principal-scoped keys allow multi-vault peers.
            # _make_inbox_key canonicalizes agy/xai so leftover gemini/
            # grok-build rows are the same fill, not a new UNIQUE key.
            wanted = {
                _make_inbox_key(r["principal"], r["inbox_file"], r["candidate_index"])
                for r in to_insert
            }
            txn_existing = _existing_keys_for_on_cursor(c, wanted)

            for r in to_insert:
                key = _make_inbox_key(
                    r["principal"], r["inbox_file"], r["candidate_index"]
                )
                if key in txn_existing:
                    already += 1
                    continue
                derived_from = json.dumps({
                    # 'inbox' + inbox_file is the inbox_archive lifecycle key —
                    # keep it EXACTLY this shape (see module docstring).
                    "source": "inbox",
                    "channel": "compact_distillation",
                    "inbox_file": r["inbox_file"],
                    "candidate_index": r["candidate_index"],
                    "kind": COMPACT_SUMMARY_KIND,
                    "audience": "shared",
                    "summary_id": r["summary_id"],
                    "platform": r["platform"],
                    "afm_distilled": r["afm_distilled"],
                    "content_sha1": _content_sha1(r["content"]),
                })
                try:
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
                except sqlite3.IntegrityError as exc:
                    # Unique-index collision (post-#239 repair) or rare race:
                    # treat as already_present rather than aborting the batch.
                    # Re-raise CHECK/NOT NULL/FK integrity failures.
                    if not _is_unique_integrity_error(exc):
                        raise
                    already += 1
                    continue
                txn_existing.add(key)
                inserted += 1

    # Archive only after the insert transaction above has committed — the
    # rows are the durable record now, so a crash between insert and archive
    # just leaves the file to be (harmlessly, idempotently) re-skipped next
    # tick, never lost.
    for path in to_archive_with_shared:
        if archive_inbox_file(path):
            archived_with_shared += 1

    return {
        "inboxes": [str(p) for p in inboxes],
        "files_scanned": scanned_files,
        "files_already_done": already,
        "would_insert": len(to_insert),
        "inserted": inserted,
        "afm_mode": mode,
        "afm_sections": afm_sections,
        "shared_candidates": len(to_insert),
        "personal_sections": personal_sections,
        "vault_notes_written": notes_written,
        "archived_zero_shared": archived_zero_shared,
        "archived_with_shared": archived_with_shared,
        "skipped": skipped,
        "dropped_sections": dropped_sections,
        "dry_run": dry_run,
    }
