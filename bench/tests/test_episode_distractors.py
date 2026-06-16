"""Unit tests for the distractor-injection invariants (review fix 6).

``augment_episode_dict`` carries two load-bearing invariants that
``episodes.check_episode`` also enforces at load time:

  (1) the establishing (fact) session is NEVER placed last after injection, and
  (2) the question session always remains last.

The insertion index ``insert_at = min(max(fact_pos + 1, 1), len(sessions) - 1)``
is subtle (the ``fact is second-to-last`` boundary in particular), so these
invariants are asserted directly here — a misread of the slice logic would
otherwise only surface as a confusing ``check_episode`` failure at fixture load.
"""

import pytest

from membench.fixtures.episode_distractors import (
    DISTRACTORS_PER_EPISODE,
    augment_episode_dict,
)


def _minimal_episode(n_pre_fact: int = 0):
    """Episode dict: optional leading sessions, then fact session, then question.

    ``n_pre_fact`` sessions precede the fact session so we can place the fact at
    different positions (including second-to-last) and check the slice logic.
    """
    sessions = [
        {"session_id": f"s{i}", "content": f"pre {i}"} for i in range(n_pre_fact)
    ]
    sessions += [
        {"session_id": "fact", "content": "the establishing fact session"},
        {"session_id": "q", "content": "the final question session"},
    ]
    return {
        "id": "ep-test",
        "band": "single-hop",
        "sessions": sessions,
        "fact_session_id": "fact",
        "question": "what?",
        "gold_fact": "establishing fact",
    }


def test_augment_keeps_fact_not_last_and_question_last_2_session():
    # Minimal 2-session episode (fact, question): the fact is SECOND-TO-LAST, the
    # tricky boundary where insert_at must equal len-1 so distractors land BEFORE
    # the last (question) session.
    ep = _minimal_episode(n_pre_fact=0)
    out = augment_episode_dict(ep, distractors=3)
    sids = [s["session_id"] for s in out["sessions"]]
    # (a) fact session is NOT last.
    assert sids[-1] != "fact"
    assert "fact" in sids
    # (b) the original last (question) session is STILL last.
    assert sids[-1] == "q"
    # (c) all dN-noise sessions appear in the MIDDLE (between fact and question).
    noise = [s for s in sids if s.endswith("-noise")]
    assert noise == [f"d{i + 1}-noise" for i in range(3)]
    fact_idx = sids.index("fact")
    q_idx = sids.index("q")
    for n in noise:
        assert fact_idx < sids.index(n) < q_idx


def test_augment_fact_in_original_slot_with_leading_sessions():
    # With sessions before the fact, the fact must remain at its original slot
    # (not last), distractors inject immediately after it, and the question stays
    # last. Proves insert_at = fact_pos + 1 when the fact is not second-to-last.
    ep = _minimal_episode(n_pre_fact=2)  # s0, s1, fact, q
    out = augment_episode_dict(ep, distractors=2)
    sids = [s["session_id"] for s in out["sessions"]]
    assert sids[0] == "s0" and sids[1] == "s1"
    assert sids[2] == "fact"  # fact stays in its original (third) slot
    assert sids[3] == "d1-noise" and sids[4] == "d2-noise"
    assert sids[-1] == "q"
    assert sids[-1] != "fact"


def test_augment_default_count_pushes_many_noise_sessions():
    ep = _minimal_episode(n_pre_fact=0)
    out = augment_episode_dict(ep)  # default DISTRACTORS_PER_EPISODE
    noise = [s for s in out["sessions"] if s["session_id"].endswith("-noise")]
    assert len(noise) == DISTRACTORS_PER_EPISODE
    assert out["sessions"][-1]["session_id"] == "q"  # question still last
    assert out["sessions"][-1]["session_id"] != "fact"


def test_augment_rejects_under_two_sessions():
    ep = {"id": "x", "sessions": [{"session_id": "only", "content": "c"}],
          "fact_session_id": "only"}
    with pytest.raises(ValueError, match="< 2 sessions"):
        augment_episode_dict(ep)


def test_augment_rejects_missing_fact_session():
    ep = _minimal_episode(n_pre_fact=0)
    ep["fact_session_id"] = "nonexistent"
    with pytest.raises(ValueError, match="not found"):
        augment_episode_dict(ep)
