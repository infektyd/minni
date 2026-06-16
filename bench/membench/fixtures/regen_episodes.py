"""Regenerate the public synthetic episode fixture WITH distractor sessions (fix 1).

Reads the base (pre-distractor) episodes, injects the deterministic distractor
pool (``episode_distractors``) so each episode's total session corpus exceeds the
token budget by a wide margin, re-validates the augmented set against the episode
leak guards, and writes the canonical JSONL back.

Run:  engine/.venv/bin/python -m membench.fixtures.regen_episodes

This is a developer/CI fixture tool — it reads only the public base fixture and
writes only the public fixture. It is deterministic: same base -> same output.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..episodes import Episode, fixture_episodes_path, validate_episodes
from . import episode_distractors as dist

# The base (tiny, pre-distractor) episodes live alongside the generated fixture
# so the augmentation is reproducible and auditable.
_BASE_PATH = (
    Path(__file__).resolve().parent / "episodes" / "synthetic_episodes_base.jsonl"
)


def _load_base() -> list[dict]:
    out: list[dict] = []
    for raw in _BASE_PATH.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            out.append(json.loads(raw))
    return out


def regenerate() -> Path:
    base = _load_base()
    gold_facts = [ep["gold_fact"] for ep in base]
    # Fail loud if any distractor would smuggle in a gold fact (leak-guard trip).
    dist.assert_distractors_fact_free(gold_facts)

    augmented = [dist.augment_episode_dict(ep) for ep in base]

    # Re-validate via the REAL loader path so the written fixture is guaranteed to
    # satisfy every episode invariant (multi-session, no leak, fact-before-question).
    episodes = [Episode.from_dict(d) for d in augmented]
    validate_episodes(episodes)

    out_path = fixture_episodes_path()
    lines = [json.dumps(d, sort_keys=True, ensure_ascii=False) for d in augmented]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


if __name__ == "__main__":
    p = regenerate()
    print(f"wrote {p}")
