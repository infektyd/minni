"""Parity gate for the shared durability predicate (recall-to-use rev5 §5.2.3, §5.3).

``plugins/minni/src/durability.ts`` and ``src/minni/durability.py`` share one
synthetic case file, ``tests/fixtures/durability_cases.json``. The Node side is
``plugins/minni/tests/durability-parity.test.mjs``.

Two directions are pinned:
  1. every rule_id in this side's constants has at least one case;
  2. every rule_id a case expects exists in this side's constants.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from minni import durability as dur

CASES_PATH = Path(__file__).parent / "fixtures" / "durability_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_case_file_is_versioned_and_non_trivial() -> None:
    assert CASES["version"] == 1
    assert len(CASES["cases"]) >= 30
    ids = [c["id"] for c in CASES["cases"]]
    assert len(set(ids)) == len(ids), "case ids must be unique"


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_classify_durability_and_durability_of(case: dict) -> None:
    opts = case.get("options") or {}
    kwargs = {}
    if "provenance_gate_since" in opts:
        kwargs["provenance_gate_since"] = opts["provenance_gate_since"]
    if "content_test_enabled" in opts:
        kwargs["content_test_enabled"] = opts["content_test_enabled"]

    got = dur.classify_durability(case["input"], **kwargs)
    assert got == case["expected"]
    assert dur.durability_of(case["input"], **kwargs) == case["expected"]["kind"]


def test_pinned_constants_in_the_case_file_match_the_python_constants() -> None:
    pinned = CASES["constants"]
    assert dur.DURABILITY_RULES_VERSION == pinned["version"]
    assert dict(dur.CATEGORY_SYNONYMS) == pinned["categorySynonyms"]
    assert list(dur.DURABLE_CATEGORIES) == pinned["durableCategories"]
    assert list(dur.CONTENT_TEST_PREFIXES) == pinned["contentTestPrefixes"]
    assert dur.CONTENT_SECTION_PATTERN == pinned["contentSectionPattern"]
    assert dur.durability_rule_ids() == pinned["ruleIds"]


def test_every_rule_id_in_the_python_constants_has_a_case() -> None:
    expected_ids = {c["expected"]["rule_id"] for c in CASES["cases"]}
    missing = [rid for rid in dur.durability_rule_ids() if rid not in expected_ids]
    assert missing == [], f"rules with no case: {missing}"


def test_every_rule_id_a_case_expects_exists_in_the_python_constants() -> None:
    known = set(dur.durability_rule_ids())
    unknown = [
        c["expected"]["rule_id"]
        for c in CASES["cases"]
        if c["expected"]["rule_id"] not in known
    ]
    assert unknown == [], f"cases naming unknown rules: {unknown}"


def test_rule_ids_are_unique_and_shaped() -> None:
    ids = dur.durability_rule_ids()
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"(noise|durable|ordinary)\.[a-z_]+", rid) for rid in ids)


def test_non_mapping_input_defaults_to_ordinary() -> None:
    for bad in (None, 42, "string", []):
        assert dur.classify_durability(bad) == {  # type: ignore[arg-type]
            "kind": "ordinary",
            "rule_id": "ordinary.default",
        }
        assert dur.durability_of(bad) == "ordinary"  # type: ignore[arg-type]
