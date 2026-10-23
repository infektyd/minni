"""Parity gate for the harness-envelope TEXT rules (recall-to-use rev5, 5.2.1).

``plugins/minni/src/harness-envelope-rules.ts`` and the Python mirror
``minni.afm_passes.harness_envelopes`` share one synthetic case file,
``tests/fixtures/harness_envelope_cases.json``. The node side is
``plugins/minni/tests/harness-envelope-parity.test.mjs``.

Two directions are pinned, so a rule added on only one side fails a gate:
  1. every rule_id in this side's constants has at least one case;
  2. every rule_id a case expects exists in this side's constants.

The template rule is TypeScript only (the daemon never sees the counts file),
so it has no case here.
"""

import json
from pathlib import Path

import pytest

from minni.afm_passes import harness_envelopes as he

CASES_PATH = Path(__file__).parent / "fixtures" / "harness_envelope_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_case_file_is_versioned_and_non_trivial():
    assert CASES["version"] == 1
    assert len(CASES["strip_cases"]) >= 10
    assert len(CASES["classify_cases"]) >= 30
    ids = [c["id"] for c in CASES["strip_cases"] + CASES["classify_cases"]]
    assert len(set(ids)) == len(ids), "case ids must be unique"


@pytest.mark.parametrize("case", CASES["strip_cases"], ids=lambda c: c["id"])
def test_strip_harness_blocks(case):
    assert he.strip_harness_blocks(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["classify_cases"], ids=lambda c: c["id"])
def test_classify_prompt_envelope(case):
    got = he.classify_prompt_envelope(case["input"])
    assert {
        "kind": got["kind"],
        "subkind": got["subkind"],
        "rule_id": got.get("rule_id"),
        "body": got["body"],
        "suspicious_prefix": got.get("suspicious_prefix", False) is True,
    } == case["expected"]


def test_every_rule_id_in_the_python_constants_has_a_case():
    expected_ids = {c["expected"]["rule_id"] for c in CASES["classify_cases"]}
    missing = [rid for rid in he.rule_ids() if rid not in expected_ids]
    assert missing == [], f"rules with no case: {missing}"


def test_every_rule_id_a_case_expects_exists_in_the_python_constants():
    known = set(he.rule_ids())
    unknown = [
        c["expected"]["rule_id"]
        for c in CASES["classify_cases"]
        if c["expected"]["rule_id"] is not None and c["expected"]["rule_id"] not in known
    ]
    assert unknown == [], f"cases naming unknown rules: {unknown}"


def test_rule_ids_are_unique_and_shaped():
    import re

    ids = he.rule_ids()
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"(block|skip|wrap)\.[a-z_]+", rid) for rid in ids)
    assert len(ids) == len(he.BLOCK_TAGS) + len(he.SKIP_RULES) + len(he.WRAPPER_RULES)


def test_template_rule_is_not_mirrored():
    assert not hasattr(he, "is_scheduled_template")
    assert "scheduled.template" not in he.rule_ids()


def test_non_string_prompt_is_an_empty_human_prompt():
    for bad in (None, 42, {}, []):
        got = he.classify_prompt_envelope(bad)
        assert (got["kind"], got["subkind"], got["body"]) == ("human", "empty", "")
    assert he.strip_harness_blocks(None) == ""


def test_hostile_inputs_stay_linear():
    import time

    started = time.monotonic()
    he.classify_prompt_envelope("<system-reminder " * 100_000)
    he.classify_prompt_envelope(" " * 2_000_000 + "x")
    he.classify_prompt_envelope("<cross-session-message " * 50_000)
    he.classify_prompt_envelope("[Workflow harness " * 50_000)
    he.classify_prompt_envelope("<system-reminder>" * 50_000 + "tail")
    assert time.monotonic() - started < 10
