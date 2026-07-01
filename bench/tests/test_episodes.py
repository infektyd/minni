"""Episode loader accept/reject tests (§3.3, slice s5)."""

import json

import pytest

from membench.episodes import (
    MAX_EPISODE_FILE_BYTES,
    POISON_INSTRUCTION_MARKER,
    Episode,
    EpisodeError,
    Session,
    check_episode,
    extract_poison_instruction,
    load_fixture_episodes,
    validate_episodes,
)
from membench.goldset import BAND_POISONED


def _good_episode(**over) -> Episode:
    base = dict(
        id="ep-x",
        band="single_hop",
        sessions=[
            Session("s1", "The seal timeout is thirty seconds in the spec."),
            Session("s2", "Some later unrelated session content."),
        ],
        fact_session_id="s1",
        question="What is the seal timeout?",
        gold_answer="The seal timeout is thirty seconds.",
        gold_fact="thirty seconds",
    )
    base.update(over)
    return Episode(**base)


def test_fixture_episodes_load_and_validate():
    episodes = load_fixture_episodes()
    # >= 6 fixture episodes required for tests (§3.3 / task scope).
    assert len(episodes) >= 6
    # Loader already validated; re-validate to be explicit.
    validate_episodes(episodes)
    for ep in episodes:
        assert len(ep.sessions) >= 2
        # The fact lives in the establishing session and NOT in the question.
        assert ep.gold_fact in ep.fact_session.content
        assert ep.gold_fact not in ep.question


def test_fixture_includes_valid_poisoned_band_episodes():
    episodes = load_fixture_episodes()
    poisoned = [ep for ep in episodes if ep.band == BAND_POISONED]
    assert 10 <= len(poisoned) <= 20
    for ep in poisoned:
        check_episode(ep)
        poison_sessions = [
            s for s in ep.sessions if POISON_INSTRUCTION_MARKER in s.content
        ]
        assert len(poison_sessions) == 1
        assert poison_sessions[0].session_id != ep.fact_session_id
        assert ep.gold_fact not in poison_sessions[0].content
        assert extract_poison_instruction(ep)


def test_accept_good_episode():
    check_episode(_good_episode())  # no raise


def test_reject_single_session():
    ep = _good_episode(
        sessions=[Session("s1", "The seal timeout is thirty seconds.")],
        fact_session_id="s1",
    )
    with pytest.raises(EpisodeError, match="2 sessions"):
        check_episode(ep)


def test_reject_fact_in_last_session():
    # fact_session is the LAST session -> not established before the question turn.
    ep = _good_episode(
        sessions=[
            Session("s1", "Unrelated earlier content."),
            Session("s2", "The seal timeout is thirty seconds."),
        ],
        fact_session_id="s2",
    )
    with pytest.raises(EpisodeError, match="LAST session"):
        check_episode(ep)


def test_reject_answer_leak_in_question():
    # The needed fact is trivially present in the question prompt itself.
    ep = _good_episode(
        question="Confirm: is the seal timeout thirty seconds?",
    )
    with pytest.raises(EpisodeError, match="leaks into the question"):
        check_episode(ep)


def test_reject_fact_not_in_establishing_session():
    ep = _good_episode(gold_fact="sixty seconds")
    with pytest.raises(EpisodeError, match="not found in the establishing"):
        check_episode(ep)


def test_reject_duplicate_session_ids_within_episode():
    # Two sessions sharing a session_id within ONE episode must be rejected.
    ep = _good_episode(
        sessions=[
            Session("s1", "The seal timeout is thirty seconds in the spec."),
            Session("s1", "A second session reusing the same session_id."),
        ],
        fact_session_id="s1",
    )
    with pytest.raises(EpisodeError, match="duplicate session_id"):
        check_episode(ep)


def test_reject_empty_session_id():
    # An empty/whitespace-only session_id is never valid (nit a): it cannot be
    # named by fact_session_id and would collide under the uniqueness check.
    ep = _good_episode(
        sessions=[
            Session("s1", "The seal timeout is thirty seconds in the spec."),
            Session("", "A later session with a blank session_id."),
        ],
        fact_session_id="s1",
    )
    with pytest.raises(EpisodeError, match="empty session_id"):
        check_episode(ep)


def test_reject_gold_fact_in_non_establishing_session():
    # The gold fact leaks into a LATER (co-ingested) session — an adapter could
    # retrieve it from there without cross-session recall. Must be rejected.
    ep = _good_episode(
        sessions=[
            Session("s1", "The seal timeout is thirty seconds in the spec."),
            Session("s2", "Reminder: the value is thirty seconds, as noted."),
        ],
        fact_session_id="s1",
    )
    with pytest.raises(EpisodeError, match="non-establishing session"):
        check_episode(ep)


def test_reject_unknown_band():
    ep = _good_episode(band="not-a-band")
    with pytest.raises(EpisodeError, match="not one of"):
        check_episode(ep)


def test_reject_missing_fact_session_id():
    ep = _good_episode(fact_session_id="does-not-exist")
    with pytest.raises(EpisodeError, match="not found among sessions"):
        check_episode(ep)


def test_reject_duplicate_ids():
    eps = [_good_episode(id="dup"), _good_episode(id="dup")]
    with pytest.raises(EpisodeError, match="duplicate episode id"):
        validate_episodes(eps)


def test_from_dict_rejects_unknown_field():
    with pytest.raises(EpisodeError, match="unknown field"):
        Episode.from_dict({"id": "x", "band": "single_hop", "bogus": 1})


def test_from_dict_rejects_missing_field():
    with pytest.raises(EpisodeError, match="missing required field"):
        Episode.from_dict({"id": "x"})


def test_loader_rejects_bad_jsonl(tmp_path):
    bad = tmp_path / "eps.jsonl"
    # A leaking episode written to disk must be rejected at load time.
    leak = {
        "id": "leak",
        "band": "single_hop",
        "sessions": [
            {"session_id": "s1", "content": "thirty seconds is the timeout"},
            {"session_id": "s2", "content": "later"},
        ],
        "fact_session_id": "s1",
        "question": "Is it thirty seconds?",
        "gold_answer": "Yes, thirty seconds.",
        "gold_fact": "thirty seconds",
    }
    bad.write_text(json.dumps(leak) + "\n", encoding="utf-8")
    from membench.episodes import load_episodes

    with pytest.raises(EpisodeError, match="leaks into the question"):
        load_episodes(bad)


def test_loader_rejects_malformed_json_line(tmp_path):
    # A structurally-broken JSONL line must surface as a JSON decode error from
    # the parser, not silently skip. This exercises the JSONL parse error path.
    bad = tmp_path / "eps.jsonl"
    bad.write_text("{unclosed json\n", encoding="utf-8")
    from membench.episodes import load_episodes

    with pytest.raises(json.JSONDecodeError):
        load_episodes(bad)


def test_loader_rejects_oversize_file(tmp_path):
    # A file exceeding MAX_EPISODE_FILE_BYTES must be refused before parsing.
    big = tmp_path / "eps.jsonl"
    big.write_bytes(b"x" * (MAX_EPISODE_FILE_BYTES + 1))
    from membench.episodes import load_episodes

    with pytest.raises(EpisodeError, match="exceeds"):
        load_episodes(big)
