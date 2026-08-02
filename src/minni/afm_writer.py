"""Single-writer draft queue for AFM loop output."""

from __future__ import annotations

import calendar
import json
import logging
import os
import queue
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # RCM-011: for safe_dump of title/tags to prevent newline key injection

from minni.safety import is_instruction_like

logger = logging.getLogger("sovereign.afm.afm_writer")

# AFM-8 (#230): the queue was unbounded. A stalled writer thread (a vault on a
# hung mount, a permissions fault) grew it without limit while every submitter
# saw status "queued" and nothing anywhere counted the backlog. Bounded with a
# stated drop policy: submissions past the bound are REJECTED at the door and
# counted, because a caller told "rejected, queue full" can retry, and a caller
# told "queued" about a job that will never run cannot.
WRITER_QUEUE_MAX = 256
# Backlog threshold for the health verdict. Well under the bound: by the time
# submissions are being dropped, the status surface should have been saying
# "backlogged" for a while.
WRITER_QUEUE_BACKLOG = 32
_WORK_QUEUE: "queue.Queue[tuple[dict, threading.Event, dict]]" = queue.Queue(
    maxsize=WRITER_QUEUE_MAX
)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_PAGE_LOCKS: dict[str, threading.Lock] = {}
_PAGE_LOCKS_LOCK = threading.Lock()
_LATENCIES: List[float] = []
# Set only when a pass actually WROTE drafts (see _write_batch). A healthy pass
# with nothing to distill never lands here, which is why it cannot be the sole
# input to the loop's health verdict.
_LAST_RUN_PER_PASS: dict[str, float] = {}
# Set when a pass completed a wet run at all, drafts or not. This is the
# liveness signal; _LAST_RUN_PER_PASS is the productivity signal.
_LAST_ATTEMPT_PER_PASS: dict[str, float] = {}
# GA4-3: a pass that RAISES has, until now, left no trace on any surface. It
# skipped record_pass_attempt, so it read as "never invoked" (silent) or went
# stale — both of which a merely-idle pass also reads as. No failure counter
# existed anywhere in the codebase. These two are the distinct failure signal:
# when a pass ran and threw, both the attempt (liveness) and the failure
# (health) are recorded, so "failing every tick" is loud and unmistakable.
_FAILURES_PER_PASS: dict[str, int] = {}
_LAST_FAILURE_PER_PASS: dict[str, dict] = {}
# AFM-8: submissions the bounded queue refused. Drafts counted here were never
# written and never will be.
_WRITES_DROPPED = 0
# Review round 2 on PR #260: _WRITES_DROPPED is cumulative and nothing clears
# it, so deriving `backlogged` from `if dropped` latched the verdict for the
# process lifetime — one full-queue burst months ago and a fully drained,
# healthy writer still read backlogged. The verdict must reflect the CURRENT
# state; the cumulative count stays visible as its own metric. A drop inside
# this window still counts (it covers the gap while the queue drains back
# under the backlog threshold); older ones are history, not status.
_LAST_DROP_AT: Optional[float] = None
WRITES_DROPPED_RECENT_SECONDS = 3600.0
# Review round 3 on PR #260: a slow-but-alive writer (every job outliving the
# caller's wait) returns "write_timeout" on every wet compile, the loop backs
# off — and writer_status still read `ok`, because attempts stay fresh, no
# failure is recorded, and a one-deep queue never crosses the backlog
# threshold. Timeouts need their own counter and recency stamp so a chronic
# condition reaches the verdict. Same recency window discipline as drops.
_WRITE_TIMEOUTS = 0
_LAST_WRITE_TIMEOUT_AT: Optional[float] = None
WRITE_TIMEOUTS_RECENT_SECONDS = 3600.0
# Review round 8 on PR #260: a write that fails AFTER its submitter timed out
# had no observer at all — the waiter was gone, so no DraftWriteError raised,
# no pass failure recorded, no counter moved, and the drafts silently never
# landed. The worker counts every batch failure itself (a waiter that IS
# still present also raises DraftWriteError — two different metrics for one
# event, not the same metric twice — because the worker cannot know race-free
# whether the waiter is still there).
_WRITE_FAILURES = 0
_LAST_WRITE_FAILURE_AT: Optional[float] = None
WRITE_FAILURES_RECENT_SECONDS = 3600.0
# Review round 9 (PR #260): a write that fails AFTER submit_drafts already
# returned write_timeout has no waiter and no lifecycle rollback. The recency-
# windowed _WRITE_FAILURES counter ages into "ok" while the drafts are gone
# forever. A non-aging residue keeps the status surface out of ok until the
# process is reset (or an operator clears counters) — permanent damage must
# not wear a temporary badge.
_UNRECOVERED_WRITE_FAILURES = 0
# Review round 5 on PR #260: at most ONE queued job per pass. A write_timeout
# response means the job is STILL queued and will land when the writer drains
# — but the loop's failure backoff re-fired the pass while it waited, and each
# re-fire minted a new trace_id and new page_ids. Once the queue drained, every
# queued generation of the same batch landed as its own set of wiki files, and
# a stuck writer filled the bounded queue ~288x faster than the old schedule.
# The guard makes a re-submit for a pass with an unfinished job a cheap,
# honest refusal instead of a duplicate enqueue.
_IN_FLIGHT_PER_PASS: dict[str, threading.Event] = {}
_IN_FLIGHT_LOCK = threading.Lock()

# A pass is stale once it has been silent for this multiple of its configured
# interval. 2x, not 1x: a tick that lands a few seconds late is not a fault, and
# a threshold that fires on ordinary jitter gets ignored, which is the failure
# mode this whole change exists to end.
STALE_INTERVAL_MULTIPLE = 2.0
# Pending drafts nobody has endorsed. The loop keeps producing regardless, so
# depth is the only thing that reveals a review queue that has stopped draining.
DRAFTS_PENDING_BACKLOG = 200
# How far ahead a freshly written draft's `expires_at` is stamped. Matches the
# `draft_ttl_days` default derive_loop_status judges backlog age against.
DRAFT_TTL_SECONDS = 14 * 86400

# Frontmatter-anchored gates for the expiry sweep. Line-anchored so only a
# page's own YAML keys can satisfy them, never body prose quoting the same text.
_FM_DRAFT_STATUS = re.compile(r"^status:\s*['\"]?draft['\"]?\s*$", re.MULTILINE)
_FM_AFM_AGENT = re.compile(r"^agent:\s*['\"]?afm-loop['\"]?\s*$", re.MULTILINE)
_FM_PAGE_ID = re.compile(r"^page_id:\s*['\"]?([^'\"\s]+)", re.MULTILINE)


def _expires_at_of(frontmatter: str) -> Optional[float]:
    """The page's own ``expires_at`` as a UTC epoch, or None if unusable.

    Tolerates the quotes yaml.safe_dump puts on the value
    (`expires_at: '2026-06-22T…Z'`); the old unquoted-only pattern matched
    nothing a writer had ever produced, which is why the live vault expired 0
    of 1,213 drafts. Parsed with calendar.timegm via _parse_iso_utc, not
    time.mktime, which reads a struct_time as LOCAL time and shifted every
    comparison by the machine's UTC offset.
    """
    match = re.search(r"^expires_at:\s*['\"]?([0-9T:.Z-]+)", frontmatter, re.MULTILINE)
    return _parse_iso_utc(match.group(1)) if match else None


def _page_id_of(frontmatter: str) -> Optional[str]:
    """The page's own ``page_id``, used to share endorse_draft's per-page lock."""
    match = _FM_PAGE_ID.search(frontmatter)
    return match.group(1) if match else None


def record_pass_attempt(pass_name: str, now: Optional[float] = None) -> None:
    """Record that ``pass_name`` completed a wet run (drafts or not)."""
    _LAST_ATTEMPT_PER_PASS[str(pass_name or "unknown")] = (
        time.time() if now is None else now
    )


def record_pass_failure(
    pass_name: str, error: str, now: Optional[float] = None
) -> None:
    """Record that ``pass_name`` was invoked and raised (GA4-3).

    Deliberately does NOT also call :func:`record_pass_attempt` — the caller
    records the attempt first, so liveness stays true even for a pass that only
    ever fails, and this adds the fault on top of it.
    """
    name = str(pass_name or "unknown")
    _FAILURES_PER_PASS[name] = _FAILURES_PER_PASS.get(name, 0) + 1
    _LAST_FAILURE_PER_PASS[name] = {
        "at": time.time() if now is None else now,
        "error": str(error)[:500],
    }


def reset_pass_counters() -> None:
    """Clear the in-memory liveness/failure counters (tests)."""
    global _WRITES_DROPPED, _LAST_DROP_AT, _WRITE_TIMEOUTS, _LAST_WRITE_TIMEOUT_AT
    _LAST_RUN_PER_PASS.clear()
    _LAST_ATTEMPT_PER_PASS.clear()
    _FAILURES_PER_PASS.clear()
    _LAST_FAILURE_PER_PASS.clear()
    _WRITES_DROPPED = 0
    _LAST_DROP_AT = None
    _WRITE_TIMEOUTS = 0
    _LAST_WRITE_TIMEOUT_AT = None
    global _WRITE_FAILURES, _LAST_WRITE_FAILURE_AT, _UNRECOVERED_WRITE_FAILURES
    _WRITE_FAILURES = 0
    _LAST_WRITE_FAILURE_AT = None
    _UNRECOVERED_WRITE_FAILURES = 0
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT_PER_PASS.clear()


def _parse_iso_utc(value: str) -> Optional[float]:
    """Parse a trailing-Z timestamp as UTC (calendar.timegm, not time.mktime)."""
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _extract_frontmatter(text: str) -> str:
    """The leading `---`-fenced YAML block, or the whole text if none is found.

    Falling back to the whole text keeps this permissive for malformed pages
    (they still get scanned) rather than silently treating them as having no
    ``created`` field; the parse-then-validate step in the caller is what
    actually rejects unusable values.

    PERMISSIVE — read-only probes only. Anything that gates a rewrite or a
    lifecycle decision must use _extract_frontmatter_block: the whole-text
    fallback means an UNFENCED note whose body documents the AFM format
    (quoting `status: draft`, `agent: afm-loop`, a past `expires_at`) reads
    as a real draft, and expiry would destructively rewrite its prose.
    """
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text if end == -1 else text[:end]


def _extract_frontmatter_block(text: str) -> Optional[str]:
    """The leading `---`-fenced YAML block, or None if there is no CLOSED one.

    The strict twin of _extract_frontmatter, for every path that decides or
    rewrites lifecycle state. No fence, or an unclosed fence, means the page
    has no frontmatter — refuse, never scan the body. Real AFM drafts are
    always written fenced by _write_one, so strictness costs nothing there.
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return None if end == -1 else text[:end]


def _atomic_write_text(path: Path, data: str) -> None:
    """Replace ``path`` with ``data`` atomically (same-directory temp + rename).

    Path.write_text opens 'w' — truncate, then write — so a crash, a full
    disk, or a concurrent reader mid-write leaves a torn page. The first live
    expiry sweep rewrites ~900 pages in one pass; each must be all-or-nothing.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def derive_loop_status(
    state: dict,
    schedule: Optional[dict] = None,
    now: Optional[float] = None,
) -> tuple[str, List[str]]:
    """Derive an honest ``status`` from the loop's observable state.

    Returns ``(status, reasons)``. Every condition found lands in ``reasons``;
    ``status`` is the worst of them, ordered

        failing > stale > backlogged > unknown > ok

    ``failing`` outranks ``stale``: a pass raising on every tick is a known
    fault, whereas ``stale`` is an inference from silence.

    ``unknown`` is deliberately not ``ok``. Where the loop cannot be inspected
    -- a pass with no recorded run at all, drafts whose age cannot be read --
    that is reported as such rather than passed over. The caller layers
    ``disabled`` and ``degraded`` on top (see minnid_runtime.health).
    """
    now = time.time() if now is None else now
    if schedule is None:
        try:
            from minni.config import DEFAULT_CONFIG

            schedule = getattr(DEFAULT_CONFIG, "afm_loop_schedule", {}) or {}
        except Exception:
            schedule = {}
    passes_cfg = (schedule or {}).get("passes") or {}
    ttl_days = int((schedule or {}).get("draft_ttl_days", 14))

    last_run = state.get("last_run_per_pass") or {}
    last_attempt = state.get("last_attempt_per_pass") or {}

    reasons: List[str] = []
    stale: List[str] = []
    silent: List[str] = []
    for name, cfg in passes_cfg.items():
        interval = float((cfg or {}).get("interval_seconds", 24 * 60 * 60))
        seen = max(
            float(last_run.get(name, 0.0) or 0.0),
            float(last_attempt.get(name, 0.0) or 0.0),
        )
        if seen <= 0.0:
            silent.append(name)
            continue
        age = now - seen
        if age > interval * STALE_INTERVAL_MULTIPLE:
            stale.append(f"{name} (last ran {round(age / 3600.0, 1)}h ago, interval {round(interval / 3600.0, 1)}h)")
    if stale:
        reasons.append("passes past their interval: " + ", ".join(sorted(stale)))
    if silent:
        reasons.append(
            "no run on record this process for: " + ", ".join(sorted(silent))
            + " (in-memory counters reset on daemon restart; treat as unverified, not healthy)"
        )

    pending = int(state.get("drafts_pending", 0) or 0)
    if pending >= DRAFTS_PENDING_BACKLOG:
        reasons.append(f"{pending} draft(s) pending endorsement (backlog threshold {DRAFTS_PENDING_BACKLOG})")
    oldest = state.get("drafts_pending_oldest")
    oldest_ts = _parse_iso_utc(oldest) if isinstance(oldest, str) else None
    if oldest_ts is not None:
        age_days = (now - oldest_ts) / 86400.0
        if age_days > ttl_days:
            reasons.append(
                f"oldest pending draft is {round(age_days, 1)}d old, past the {ttl_days}d TTL "
                "(expiry is not running)"
            )
    undated = int(state.get("drafts_pending_undated", 0) or 0)
    if pending and oldest is None:
        reasons.append(f"{pending} draft(s) pending but none carries a readable created date; age unknown")
    elif undated:
        reasons.append(f"{undated} pending draft(s) carry no readable created date; oldest may be understated")
    unreadable = int(state.get("drafts_unreadable", 0) or 0)
    if unreadable:
        reasons.append(f"{unreadable} vault page(s) could not be read; draft count is a lower bound")

    # GA2-1: queue_depth has been recorded in writer_status since the queue
    # existed and was never read by anything. An orphaned metric is worse than
    # no metric — it reads as coverage that is not there. Either consult it or
    # delete it; a writer thread that has stopped draining is exactly the kind
    # of fault this status is for, so consult it.
    queue_depth = int(state.get("queue_depth", 0) or 0)
    queue_backlogged = queue_depth >= WRITER_QUEUE_BACKLOG
    if queue_backlogged:
        reasons.append(
            f"{queue_depth} write job(s) queued (backlog threshold "
            f"{WRITER_QUEUE_BACKLOG}, hard bound {WRITER_QUEUE_MAX}); "
            "the writer thread is not draining"
        )
    # Review round 2 on PR #260: the cumulative drop count used to drive the
    # verdict directly, so one long-past burst latched `backlogged` until
    # daemon restart. Only a RECENT drop is a live condition; the lifetime
    # count stays reported as writes_dropped in the state. Unknown recency
    # (a state with no last_drop_at) keeps the alarm — better a stale reason
    # than a suppressed loss.
    dropped = int(state.get("writes_dropped", 0) or 0)
    drop_at = state.get("last_drop_at")
    dropped_recently = bool(dropped) and (
        drop_at is None or now - float(drop_at) <= WRITES_DROPPED_RECENT_SECONDS
    )
    if dropped_recently:
        # Round 4: name the recency, don't present the lifetime total as the
        # size of the current incident — "900 REJECTED" over a single fresh
        # drop reads as a live flood.
        drop_seen = (
            "recency unknown"
            if drop_at is None
            else f"most recent {int(now - float(drop_at))}s ago"
        )
        reasons.append(
            f"write job(s) REJECTED because the queue was full ({drop_seen}; "
            f"{dropped} over the process lifetime) — those drafts were never written"
        )
    # Review round 3 on PR #260: a chronic write_timeout (writer alive but
    # outliving every caller's wait) left every other signal green — attempts
    # fresh, no failures, queue one deep. Recent timeouts are the writer not
    # draining within the window callers can observe; same recency discipline
    # as drops, with the lifetime count staying as write_timeouts data.
    timeouts = int(state.get("write_timeouts", 0) or 0)
    timeout_at = state.get("last_write_timeout_at")
    timed_out_recently = bool(timeouts) and (
        timeout_at is None or now - float(timeout_at) <= WRITE_TIMEOUTS_RECENT_SECONDS
    )
    if timed_out_recently:
        timeout_seen = (
            "recency unknown"
            if timeout_at is None
            else f"most recent {int(now - float(timeout_at))}s ago"
        )
        reasons.append(
            f"write job(s) timed out waiting on the writer ({timeout_seen}; "
            f"{timeouts} over the process lifetime) — outcomes unobserved; "
            "the writer is not draining within the wait window"
        )
    # Round 8 (PR #260): a batch that failed AFTER its waiter gave up had no
    # observer — same recency discipline as timeouts, same honesty: those
    # drafts never landed and never will.
    wfails = int(state.get("write_failures", 0) or 0)
    wfail_at = state.get("last_write_failure_at")
    write_failed_recently = bool(wfails) and (
        wfail_at is None or now - float(wfail_at) <= WRITE_FAILURES_RECENT_SECONDS
    )
    if write_failed_recently:
        wfail_seen = (
            "recency unknown"
            if wfail_at is None
            else f"most recent {int(now - float(wfail_at))}s ago"
        )
        reasons.append(
            f"write job(s) FAILED in the writer ({wfail_seen}; {wfails} over "
            "the process lifetime) — those drafts were never written"
        )
    # Round 9: non-aging residue of write failures. Recency-windowed counts
    # alone let permanent draft loss age into "ok"; this does not.
    unrecovered = int(state.get("unrecovered_write_failures", 0) or 0)
    if unrecovered:
        reasons.append(
            f"{unrecovered} unrecovered write failure(s) — drafts never "
            "landed; status stays out of ok until counters are reset"
        )
    # Round 6 (PR #260): a job in flight NOW is current truth — no recency
    # window. Neither of the other writer signals covers a hung mid-job write:
    # queue_depth is 0 once the worker dequeues, and the timeout stamp ages
    # out while later ticks return write_in_flight without refreshing it, so
    # after an hour the surface read `ok` while every submit was still refused.
    jobs_in_flight = int(state.get("jobs_in_flight", 0) or 0)
    if jobs_in_flight:
        stalled_passes = ", ".join(str(p) for p in (state.get("in_flight_passes") or []))
        reasons.append(
            f"{jobs_in_flight} write job(s) still in flight"
            + (f" ({stalled_passes})" if stalled_passes else "")
            + " — new submits for those passes are refused until they land"
        )

    # GA4-3: a pass failing on every tick used to reach this function looking
    # exactly like a healthy one, because only the fact that it ran was
    # recorded. Failures are their own reason and their own status.
    # Review round 2 on PR #260: the counter is cumulative and nothing clears
    # it, so `if count` latched `failing` for the process lifetime — one
    # transient fault and every clean tick after it still read failing, the
    # exact learned-to-ignore alarm this status exists to kill. A pass is
    # failing only while its LAST outcome was a fault: the recorders stamp the
    # attempt first, then the failure, so fail_at >= attempt_at means the last
    # attempt faulted, and a later successful attempt moves attempt_at past
    # it. A nonzero count with no failure timestamp keeps the alarm (unknown
    # recency is not recovery). The cumulative count stays visible as
    # failures_per_pass in the state.
    failures = state.get("failures_per_pass") or {}
    last_failures = state.get("last_failure_per_pass") or {}
    failing = []
    for name, count in sorted(failures.items()):
        if not count:
            continue
        fail_at = (last_failures.get(name) or {}).get("at")
        attempt_at = float(last_attempt.get(name, 0.0) or 0.0)
        if fail_at is None or float(fail_at) >= attempt_at:
            failing.append(f"{name} ({count}x)")
    if failing:
        reasons.append("passes raising: " + ", ".join(failing))

    if failing:
        return "failing", reasons
    if stale:
        return "stale", reasons
    if (
        pending >= DRAFTS_PENDING_BACKLOG
        or queue_backlogged
        or dropped_recently
        or timed_out_recently
        or write_failed_recently
        or unrecovered
        or jobs_in_flight
        or (oldest_ts is not None and (now - oldest_ts) / 86400.0 > ttl_days)
    ):
        return "backlogged", reasons
    if reasons:
        return "unknown", reasons
    return "ok", reasons


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


def _write_one(
    vault: Path,
    draft: dict,
    writeback: Any = None,
    now: Optional[float] = None,
) -> dict:
    """Write one AFM draft (or refuse for forged). Return shape:
    - normal/quality-blocked: path/wikilink/status present, written implied by file
    - forged-frontmatter: path=None, wikilink=None, status="blocked", written=False (RCM-010 security)

    ``now`` overrides the wall clock stamped into ``created``/``expires_at`` so
    the writer and :func:`_expire_stale_drafts` can be round-tripped over a
    real TTL boundary without patching the time module.
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

    stamp = time.time() if now is None else now
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    section = draft.get("section") or f"{draft.get('kind', 'concept')}s"
    rel = Path("wiki") / section / f"{created[:10].replace('-', '')}-{_slugify(draft['title'])}-{draft['page_id'][-6:]}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp + DRAFT_TTL_SECONDS))
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
        # Only write if not forged (forged bodies are blocked but note is still produced in return).
        # Same atomic path as expiry/endorse: vault-watch indexes concurrently, so a
        # truncate-in-place crash mid-write must not leave a torn page for the next sweep.
        if not forged:
            _atomic_write_text(path, body)
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


def _expire_stale_drafts(vault: Path, now: Optional[float] = None) -> int:
    expired = 0
    now = time.time() if now is None else now
    for path in (vault / "wiki").glob("**/*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        # EVERY decision below reads the frontmatter block only. Body prose is
        # free-form and can contain any of these lines verbatim (a page quoting
        # another page's frontmatter is the ordinary case); letting it satisfy
        # the entry gate would drag non-AFM pages into the expiry path, and the
        # rewrite below would then edit that prose instead of the real status.
        # STRICT extractor: a page with no closed fence has no frontmatter at
        # all — the permissive whole-text fallback would read an unfenced note
        # that documents the AFM format as an expirable draft.
        frontmatter = _extract_frontmatter_block(text)
        if frontmatter is None:
            continue
        if not _FM_DRAFT_STATUS.search(frontmatter):
            continue
        if not _FM_AFM_AGENT.search(frontmatter):
            continue
        expires = _expires_at_of(frontmatter)
        if expires is None:
            continue
        if expires < now:
            # Re-read and re-validate under the SAME lock endorse_draft takes.
            # Everything above ran against a buffer read outside any lock, and
            # this now runs on the vault-watch thread concurrently with RPC
            # endorsement: without this, an operator accepting a past-TTL draft
            # between the read and the write would have their endorsement
            # overwritten with `status: expired` and silently lost.
            page_id = _page_id_of(frontmatter)
            with _page_lock(page_id) if page_id else nullcontext():
                try:
                    current = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                current_fm = _extract_frontmatter_block(current)
                if current_fm is None:
                    continue
                if not _FM_DRAFT_STATUS.search(current_fm):
                    # Endorsed (or already expired) while we were deciding.
                    continue
                if not _FM_AFM_AGENT.search(current_fm):
                    continue
                # Re-check the TTL too, not just the status. A concurrent
                # writer may have re-stamped this page with a LATER expires_at
                # while still leaving it a draft (a TTL extension or manual
                # edit); expiring it on the strength of the value we read
                # before taking the lock would silently undo that.
                current_expires = _expires_at_of(current_fm)
                if current_expires is None or current_expires >= now:
                    continue
                # Rewrite inside the frontmatter slice and splice it back, so
                # the substitution can never land on body text that merely
                # looks like frontmatter.
                rewritten = _FM_DRAFT_STATUS.sub("status: expired", current_fm, count=1)
                _atomic_write_text(path, rewritten + current[len(current_fm):])
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


def _process_job(job: dict, done: threading.Event, out: dict) -> None:
    """One queued batch, start to Event. Extracted from the worker loop so the
    failure accounting is testable without driving the daemon thread."""
    try:
        out["result"] = _write_batch(job)
    except Exception as exc:
        out["error"] = str(exc)
        # Round 8: count HERE, unconditionally. A waiter that already timed
        # out (write_timeout) is gone — nobody raises DraftWriteError, nothing
        # records a pass failure, and the drafts silently never land. A waiter
        # that is still present raises too, but that is a different metric
        # for the same event, not double-counting this one.
        # Round 9: also stamp a non-aging unrecovered counter so the status
        # surface cannot return to ok while those drafts are permanently gone.
        global _WRITE_FAILURES, _LAST_WRITE_FAILURE_AT, _UNRECOVERED_WRITE_FAILURES
        with _IN_FLIGHT_LOCK:
            _WRITE_FAILURES += 1
            _LAST_WRITE_FAILURE_AT = time.time()
            _UNRECOVERED_WRITE_FAILURES += 1
        logger.error(
            "AFM writer: batch for pass %r FAILED (%d draft(s) not written): %s",
            job.get("pass_name"), len(job.get("drafts") or []), exc,
        )
    finally:
        done.set()


def _worker() -> None:
    while True:
        job, done, out = _WORK_QUEUE.get()
        try:
            _process_job(job, done, out)
        finally:
            _WORK_QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            thread = threading.Thread(target=_worker, name="afm-writer", daemon=True)
            thread.start()
            _WORKER_STARTED = True


class DraftWriteError(RuntimeError):
    """A draft batch reached the writer and failed to be written.

    AFM-8 (#230): the writer used to raise a bare ``RuntimeError``, which the
    daemon's blanket handler reported as ``afm_unavailable`` — attributing a
    durable-storage problem to the model provider. A distinct type lets that
    handler tell "the vault could not be written" from "AFM was not there".
    """


class DraftQueueFull(RuntimeError):
    """The bounded writer queue rejected a submission (AFM-8 drop policy)."""


def submit_drafts(job: dict, wait: bool = True, timeout: Optional[float] = 30.0) -> dict:
    _ensure_worker()
    pass_name = str(job.get("pass_name") or "unknown")
    done = threading.Event()
    out: dict = {}
    with _IN_FLIGHT_LOCK:
        prior = _IN_FLIGHT_PER_PASS.get(pass_name)
        if prior is not None and not prior.is_set():
            # Round 5: the previous batch for this pass is still queued and
            # WILL land. Enqueuing another job now duplicates it — these
            # drafts are refused, not queued, and the caller is told so.
            logger.warning(
                "AFM writer: pass %r already has a job in flight; REFUSED "
                "%d draft(s) — resubmit after the previous batch lands",
                pass_name, len(job.get("drafts") or []),
            )
            return {
                "status": "write_in_flight",
                "queue_depth": _WORK_QUEUE.qsize(),
                "drafts_written": [],
                "drafts_deferred": len(job.get("drafts") or []),
            }
        try:
            _WORK_QUEUE.put_nowait((job, done, out))
        except queue.Full:
            # Drop policy: reject at the door and count it, rather than block
            # the caller or silently grow. The drafts are NOT written; saying
            # so is the whole point.
            global _WRITES_DROPPED, _LAST_DROP_AT
            _WRITES_DROPPED += 1
            _LAST_DROP_AT = time.time()
            logger.error(
                "AFM writer queue full (%d jobs); REJECTED %d draft(s) from pass %r — "
                "not written",
                WRITER_QUEUE_MAX, len(job.get("drafts") or []), job.get("pass_name"),
            )
            raise DraftQueueFull(
                f"writer queue full ({WRITER_QUEUE_MAX} jobs); "
                f"{len(job.get('drafts') or [])} draft(s) rejected, not written"
            )
        _IN_FLIGHT_PER_PASS[pass_name] = done
    if not wait:
        return {"status": "queued", "queue_depth": _WORK_QUEUE.qsize()}
    if not done.wait(timeout):
        # AFM-8: the job is still in flight and WILL be written by the worker;
        # the caller just did not get to see the outcome. Saying "queued" alone
        # left the caller with no way to know its result was never observed, so
        # the drafts read as landed. Name it — and count it (round 3): a writer
        # that outlives every caller's wait is chronic, and without a counter
        # the status surface reads ok while nothing is ever observed landing.
        global _WRITE_TIMEOUTS, _LAST_WRITE_TIMEOUT_AT
        # Round 6: same lock as the drop counters — a bare RMW loses
        # increments when the loop and a wire daemon.compile time out at once.
        with _IN_FLIGHT_LOCK:
            _WRITE_TIMEOUTS += 1
            _LAST_WRITE_TIMEOUT_AT = time.time()
        logger.warning(
            "AFM writer timed out after %.1fs waiting on pass %r; %d draft(s) "
            "still in flight — outcome unobserved",
            timeout or 0.0, job.get("pass_name"), len(job.get("drafts") or []),
        )
        return {
            "status": "write_timeout",
            "queue_depth": _WORK_QUEUE.qsize(),
            "timeout": True,
            "drafts_written": [],
            "drafts_in_flight": len(job.get("drafts") or []),
        }
    if "error" in out:
        raise DraftWriteError(out["error"])
    return out["result"]


def writer_status(
    vault_path: Optional[str] = None,
    schedule: Optional[dict] = None,
    now: Optional[float] = None,
) -> dict:
    """Report the loop's observable state AND a status derived from it.

    ``status`` used to be the literal ``"ok"``. It is now computed by
    :func:`derive_loop_status` from the same numbers the caller can see, so a
    loop whose passes have all been silent for days can no longer read ``ok``
    next to an empty ``last_run_per_pass``.
    """
    p95 = 0.0
    if _LATENCIES:
        ordered = sorted(_LATENCIES)
        idx = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
        p95 = round(ordered[idx] * 1000, 3)
    pending = 0
    oldest = None
    undated = 0
    unreadable = 0
    if vault_path:
        vault = Path(vault_path).expanduser()
        for path in (vault / "wiki").glob("**/*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                # A draft we cannot read is not a draft we know to be fine.
                unreadable += 1
                continue
            # Gate on the frontmatter block only, with the SAME predicates AND
            # the same strict extractor the expiry engine uses. The whole-file
            # substring test counted a page whose body merely quotes another
            # draft's frontmatter, and the permissive extractor would count an
            # unfenced note expiry can never expire — either way the pending
            # count reports a backlog the expiry engine will never drain.
            frontmatter = _extract_frontmatter_block(text)
            if frontmatter is None:
                continue
            if (_FM_DRAFT_STATUS.search(frontmatter)
                    and _FM_AFM_AGENT.search(frontmatter)):
                pending += 1
                # Search only the frontmatter block (between the leading `---`
                # fences), not the whole file: a draft's body is free-form prose
                # and may itself contain the substring "created:", which must
                # not be read as the draft's own creation date.
                # The value is yaml.safe_dump'd, so it arrives quoted:
                # `created: '2026-06-20T00:07:39Z'`. The unquoted-only pattern
                # matched nothing, which is why drafts_pending_oldest was null
                # on a vault of 1,210 drafts and the age threshold never fired.
                created = re.search(r"^created:\s*['\"]?([0-9T:.Z-]+)", frontmatter, re.MULTILINE)
                # A match that does not parse as a UTC timestamp (e.g. a
                # millisecond-precision value) is not usable as `oldest` either:
                # stuffing an unparseable string in there would neither read as
                # dated (age/TTL logic silently no-ops on it) nor as undated
                # (the "age unknown" reason would not fire). Count it as undated
                # instead so the gap is visible rather than swallowed.
                if created and _parse_iso_utc(created.group(1)) is not None:
                    value = created.group(1)
                    oldest = value if oldest is None else min(oldest, value)
                else:
                    undated += 1
    # Round 6 (PR #260): live in-flight state, read fresh per status call.
    # While the worker is mid-job the queue is EMPTY, and the timeout stamp
    # ages out after an hour — without this a hung write vanished from the
    # surface while every new tick was still refused with write_in_flight.
    with _IN_FLIGHT_LOCK:
        in_flight_passes = sorted(
            name for name, event in _IN_FLIGHT_PER_PASS.items() if not event.is_set()
        )
    state = {
        "last_run_per_pass": dict(_LAST_RUN_PER_PASS),
        "last_attempt_per_pass": dict(_LAST_ATTEMPT_PER_PASS),
        "jobs_in_flight": len(in_flight_passes),
        "in_flight_passes": in_flight_passes,
        "drafts_pending": pending,
        "drafts_pending_oldest": oldest,
        "drafts_pending_undated": undated,
        "drafts_unreadable": unreadable,
        "afm_latency_p95": p95,
        "queue_depth": _WORK_QUEUE.qsize(),
        "queue_max": WRITER_QUEUE_MAX,
        "writes_dropped": _WRITES_DROPPED,
        "last_drop_at": _LAST_DROP_AT,
        "write_timeouts": _WRITE_TIMEOUTS,
        "last_write_timeout_at": _LAST_WRITE_TIMEOUT_AT,
        "write_failures": _WRITE_FAILURES,
        "last_write_failure_at": _LAST_WRITE_FAILURE_AT,
        "unrecovered_write_failures": _UNRECOVERED_WRITE_FAILURES,
        "failures_per_pass": dict(_FAILURES_PER_PASS),
        "last_failure_per_pass": dict(_LAST_FAILURE_PER_PASS),
    }
    status, reasons = derive_loop_status(state, schedule=schedule, now=now)
    state["status"] = status
    state["status_reasons"] = reasons
    return state


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
        # Resolve by the page's OWN frontmatter page_id, never a whole-file
        # substring: a draft whose body quotes another page's frontmatter
        # would be discovered first, endorsed in the target's place, and the
        # audit would claim the requested id. Same body-quotation class as
        # the status gate above, one field over. Strict extractor: an
        # unfenced page has no frontmatter and therefore no page_id.
        fm = _extract_frontmatter_block(text)
        if fm is not None and _page_id_of(fm) == page_id:
            target = path
            break
    if target is None:
        raise FileNotFoundError(f"draft page_id not found: {page_id}")
    status = {"accept": "accepted", "reject": "rejected", "edit": "edit_requested"}[decision]
    with _page_lock(page_id):
        text = target.read_text(encoding="utf-8")
        # Decide from the frontmatter block only, and rewrite inside it — the
        # same contract as _expire_stale_drafts, which shares this lock. The
        # whole-file test accepted a page whose FM was already expired (or
        # endorsed) as long as its body quoted "status: draft" somewhere, and
        # then rewrote that body prose while reporting success.
        frontmatter = _extract_frontmatter_block(text)
        # Re-validate the identity under the lock too: the discovery read ran
        # unlocked, and the per-page lock is keyed by the REQUESTED id — if
        # the file's own id no longer matches (or its fences vanished), this
        # write would not even be serialized against the page it is about to
        # modify.
        if frontmatter is None or _page_id_of(frontmatter) != page_id:
            raise FileNotFoundError(f"draft page_id not found: {page_id}")
        if not _FM_DRAFT_STATUS.search(frontmatter):
            raise ValueError(f"page is not an active draft: {page_id}")
        rewritten = _FM_DRAFT_STATUS.sub(f"status: {status}", frontmatter, count=1)
        _atomic_write_text(target, rewritten + text[len(frontmatter):])
    rel = target.relative_to(vault)
    result = {"status": status, "page_id": page_id, "path": str(rel), "decision": decision}
    _append_audit(vault, "afm_endorse", f"{decision}: {page_id}", result)
    return result
