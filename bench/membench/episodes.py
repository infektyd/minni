"""Scripted multi-session episode schema + loader (Layer 2, slice s5; §3.3).

A Layer-2 **episode** is an ordered list of SESSIONS plus a final question with a
gold answer. The defining property (§3.3): an EARLY session establishes a fact,
a LATER session needs it, and the final question can only be answered by recalling
that fact across sessions — this is what exercises the cross-session retrieval the
benchmark is built to measure.

Schema (one episode):
    {
      "id": str,                       # stable episode id
      "band": str,                     # difficulty band (goldset.BANDS)
      "tag": str,                      # free-form sub-tag for diagnostics
      "sessions": [                    # >= 2 ordered sessions
        {"session_id": str, "content": str}, ...
      ],
      "fact_session_id": str,          # the session that ESTABLISHES the fact
      "question": str,                 # the final question (a LATER turn)
      "gold_answer": str,              # full reference answer (judge reference)
      "gold_fact": str                 # the atomic fact the answer must assert
    }

The loader validates structure AND the two load-bearing invariants:

1. **Multi-session.** ``len(sessions) >= 2`` and ``fact_session_id`` names a real
   session that is NOT the last one — the fact must be established before the
   final question's turn, so the episode genuinely tests recall over sessions.

2. **No answer leak (§-threats-to-validity).** The ``gold_fact`` substring MUST
   appear in the establishing session's content but MUST NOT appear in the final
   ``question`` text itself. If the needed fact is trivially present in the
   question prompt, the episode tests prompt-reading, not memory, and is REJECTED
   — this is the exact leak the review panel hunts for.

Episodes are SYNTHETIC (invented), never real-vault data. Fixtures live under
``membench/fixtures/episodes/`` as JSONL (one episode per line) — public, so the
pipeline runs end-to-end with no operator data.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .goldset import BANDS

# Bound untrusted episode files the same way goldset bounds gold JSONL.
MAX_EPISODE_FILE_BYTES = 8 * 1024 * 1024
MAX_EPISODES = 100_000
MIN_SESSIONS = 2


class EpisodeError(ValueError):
    """Raised when an episode or episode set fails validation (§3.3)."""


@dataclass(frozen=True)
class Session:
    """One scripted session within an episode."""

    session_id: str
    content: str


@dataclass
class Episode:
    """One scripted multi-session episode (§3.3).

    ``fact_session_id`` names the session that establishes ``gold_fact``; the
    final ``question`` is a later turn that can only be answered by recalling it.
    """

    id: str
    band: str
    sessions: list[Session] = field(default_factory=list)
    fact_session_id: str = ""
    question: str = ""
    gold_answer: str = ""
    gold_fact: str = ""
    tag: str = ""

    @property
    def fact_session(self) -> Session:
        """The session that establishes ``gold_fact`` (validated to exist)."""
        for s in self.sessions:
            if s.session_id == self.fact_session_id:
                return s
        raise EpisodeError(
            f"episode {self.id!r}: fact_session_id {self.fact_session_id!r} "
            "not found among sessions"
        )

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        known = {
            "id",
            "band",
            "sessions",
            "fact_session_id",
            "question",
            "gold_answer",
            "gold_fact",
            "tag",
        }
        unknown = set(d) - known
        if unknown:
            raise EpisodeError(f"episode has unknown field(s): {sorted(unknown)}")
        required = {"id", "band", "sessions", "fact_session_id", "question",
                    "gold_answer", "gold_fact"}
        missing = required - set(d)
        if missing:
            raise EpisodeError(
                f"episode missing required field(s): {sorted(missing)}"
            )
        raw_sessions = d["sessions"]
        if not isinstance(raw_sessions, list):
            raise EpisodeError("'sessions' must be a list")
        sessions: list[Session] = []
        for i, rs in enumerate(raw_sessions):
            if not isinstance(rs, dict):
                raise EpisodeError(f"session #{i} must be an object")
            s_unknown = set(rs) - {"session_id", "content"}
            if s_unknown:
                raise EpisodeError(
                    f"session #{i} has unknown field(s): {sorted(s_unknown)}"
                )
            if "session_id" not in rs or "content" not in rs:
                raise EpisodeError(
                    f"session #{i} must have 'session_id' and 'content'"
                )
            if not isinstance(rs["session_id"], str) or not isinstance(
                rs["content"], str
            ):
                raise EpisodeError(
                    f"session #{i} 'session_id'/'content' must be strings"
                )
            sessions.append(
                Session(session_id=rs["session_id"], content=rs["content"])
            )
        return cls(
            id=d["id"],
            band=d["band"],
            sessions=sessions,
            fact_session_id=d["fact_session_id"],
            question=d["question"],
            gold_answer=d["gold_answer"],
            gold_fact=d["gold_fact"],
            tag=d.get("tag", ""),
        )


def check_episode(ep: Episode) -> None:
    """Validate one episode; raise :class:`EpisodeError` on any violation (§3.3).

    Enforces structure AND the two load-bearing invariants: genuinely
    multi-session (fact established before the final question), and NO answer
    leak (the gold fact is not trivially present in the question prompt).
    """
    if not ep.id:
        raise EpisodeError("episode id is empty")
    if ep.band not in BANDS:
        raise EpisodeError(
            f"episode {ep.id!r}: band {ep.band!r} not one of {sorted(BANDS)}"
        )
    if len(ep.sessions) < MIN_SESSIONS:
        raise EpisodeError(
            f"episode {ep.id!r}: needs >= {MIN_SESSIONS} sessions "
            f"(got {len(ep.sessions)}) — a single-session episode does not test "
            "cross-session recall (§3.3)"
        )
    sid_list = [s.session_id for s in ep.sessions]
    # Reject empty/whitespace-only session_ids: a blank id cannot be named by
    # fact_session_id and would collide under uniqueness — it is never valid.
    for s in ep.sessions:
        if not s.session_id or not s.session_id.strip():
            raise EpisodeError(
                f"episode {ep.id!r}: a session has an empty session_id"
            )
    if len(sid_list) != len(set(sid_list)):
        raise EpisodeError(f"episode {ep.id!r}: duplicate session_id(s)")
    if not ep.question or not ep.question.strip():
        raise EpisodeError(f"episode {ep.id!r}: question is empty")
    if not ep.gold_answer or not ep.gold_answer.strip():
        raise EpisodeError(f"episode {ep.id!r}: gold_answer is empty")
    if not ep.gold_fact or not ep.gold_fact.strip():
        raise EpisodeError(f"episode {ep.id!r}: gold_fact is empty")

    # fact_session_id must name a real session (raises if not via .fact_session)
    fact_session = ep.fact_session

    # The establishing session must NOT be the last session — the fact must be
    # set BEFORE the turn that asks the final question, so recall is actually
    # over earlier sessions (§3.3 multi-session character).
    if ep.sessions[-1].session_id == ep.fact_session_id:
        raise EpisodeError(
            f"episode {ep.id!r}: fact_session_id is the LAST session; the fact "
            "must be established in an EARLIER session than the final question "
            "(§3.3)"
        )

    # The gold fact must actually be present in the establishing session — a
    # correct memory system must be able to retrieve it from there.
    if ep.gold_fact not in fact_session.content:
        raise EpisodeError(
            f"episode {ep.id!r}: gold_fact substring not found in the "
            f"establishing session {ep.fact_session_id!r} content; the fact must "
            "be recoverable from the corpus, not invented at the question"
        )

    # ANSWER-LEAK GUARD (the panel's target): the gold fact must NOT be present
    # in the final question prompt itself — otherwise the episode tests
    # prompt-reading, not memory.
    if ep.gold_fact in ep.question:
        raise EpisodeError(
            f"episode {ep.id!r}: gold_fact leaks into the question prompt; the "
            "answer would be readable from the question itself rather than "
            "recalled across sessions — REJECTED (threats-to-validity leak guard)"
        )

    # CROSS-SESSION LEAK GUARD (the panel's target): the gold fact must appear
    # ONLY in the establishing session. If it also appears verbatim in any OTHER
    # session — in particular the LAST (question-turn) session, which is
    # co-ingested into the corpus — an adapter can retrieve it from that session
    # at query time WITHOUT recalling the earlier establishing session. The
    # episode would then test same-session retrieval, not the cross-session
    # memory the benchmark measures — REJECTED (threats-to-validity leak guard).
    for s in ep.sessions:
        if s.session_id != ep.fact_session_id and ep.gold_fact in s.content:
            raise EpisodeError(
                f"episode {ep.id!r}: gold_fact also appears in non-establishing "
                f"session {s.session_id!r}; it must be present ONLY in the "
                f"establishing session {ep.fact_session_id!r}, else the episode "
                "tests same-session retrieval, not cross-session memory — "
                "REJECTED (threats-to-validity leak guard)"
            )


def validate_episodes(episodes: list[Episode]) -> None:
    """Validate a whole episode set: per-episode checks + unique ids."""
    seen: set[str] = set()
    for ep in episodes:
        if ep.id in seen:
            raise EpisodeError(f"duplicate episode id: {ep.id!r}")
        seen.add(ep.id)
        check_episode(ep)


def load_episodes(path: str | os.PathLike[str]) -> list[Episode]:
    """Load + VALIDATE a JSONL episode set. Raises on any structural violation.

    Bounds untrusted input (file size + item count) like the gold-set loader,
    then validates every episode (structure + multi-session + no-leak). A failing
    file raises :class:`EpisodeError`; a clean file returns the episode list.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_EPISODE_FILE_BYTES:
        raise EpisodeError(
            f"episode JSONL {path.name!r} is {size} bytes, exceeds the "
            f"{MAX_EPISODE_FILE_BYTES}-byte cap (refusing to load)"
        )
    episodes: list[Episode] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if len(episodes) >= MAX_EPISODES:
            raise EpisodeError(
                f"episode JSONL exceeds the {MAX_EPISODES}-item cap"
            )
        episodes.append(Episode.from_dict(json.loads(raw)))
    validate_episodes(episodes)
    return episodes


def fixture_episodes_path() -> Path:
    """Path to the public synthetic fixture episode set (JSONL)."""
    return (
        Path(__file__).resolve().parent
        / "fixtures"
        / "episodes"
        / "synthetic_episodes.jsonl"
    )


def load_fixture_episodes() -> list[Episode]:
    """Load the shipped public synthetic fixture episodes (validated)."""
    return load_episodes(fixture_episodes_path())
