"""Class F reproduction and verification tests: classifier false positives on durable procedural learnings."""

from __future__ import annotations

from minni.minnid_runtime.governance import auto_accept_blockers, non_durable_promotion_blocker
from minni.writeback import CATEGORIES


def test_class_f_procedural_rules_mentioning_slice_or_gate_not_misclassified_as_sprint_chatter():
    # 1. Procedural rule mentioning "slice <word> is green/done" must not match _SPRINT_CHATTER_RE
    rule_slice = "Never merge when slice verification is green only on a subset of targets."
    assert (
        non_durable_promotion_blocker(
            content=rule_slice,
            category="procedure",
            source="claude-code",
            origin="agent",
        )
        is None
    )

    # 2. Exact operator learning from minni-issues-2026-10-31.md Class F:
    # "Re-running a red gate is only legitimate with a recorded four-point justification"
    # Even when passed with category="constraint", "rule", "policy", or default "general",
    # it must be recognized as a durable normative rule and not blocked as non_durable:noise/ordinary.
    exact_rule = "Re-running a red gate is only legitimate with a recorded four-point justification"
    for cat in ("procedure", "constraint", "rule", "policy", "general"):
        blocker = non_durable_promotion_blocker(
            content=exact_rule,
            category=cat if cat in CATEGORIES else "general",
            source="claude-code",
            origin="agent",
        )
        assert blocker is None, f"expected None blocker for category={cat!r}, got {blocker!r}"
        blockers = auto_accept_blockers(
            exact_rule,
            False,
            "safe",
            category=cat if cat in CATEGORIES else "general",
            source="claude-code",
            origin="agent",
        )
        assert blockers == [], f"expected no auto_accept_blockers for category={cat!r}, got {blockers!r}"

    # 3. Genuine sprint status chatter MUST still be blocked as non_durable:noise
    assert (
        non_durable_promotion_blocker(
            content="Phase 2 is complete: 42/42 tests passed, 0 failed.",
            category="decision",
        )
        == "non_durable:noise"
    )
    assert (
        non_durable_promotion_blocker(
            content="Slice s3 is green and complete.",
            category="procedure",
        )
        == "non_durable:noise"
    )

    # 4. Ephemeral time/release assertions ("must be", "today", "5pm", "before release")
    # must NOT be promoted via broad/weak invariant cues on agent-origin ordinary items.
    for ephemeral in (
        "Gate must be green before 5pm today",
        "the build must be green before release",
        "Deploying to staging is only allowed after 9pm tonight",
    ):
        blocker = non_durable_promotion_blocker(
            content=ephemeral,
            category="general",
            source="claude-code",
            origin="agent",
        )
        assert blocker is not None and blocker.startswith("non_durable:"), (
            f"expected ephemeral status sentence {ephemeral!r} to be blocked, got {blocker!r}"
        )

    # 5. Durable rules with hyphenated "now-*", clock times alongside strong cues,
    # or English "am"/"pm" words must NOT be blocked by _EPHEMERAL_TIME_OR_STATUS_RE.
    for durable_rule in (
        "never push now-unverified commits to main",
        "run the 2am backup job; never bypass the gate",
        "operator rule: never loosen the gate (am I right?)",
        "invariant: ids are unique, check pm mode",
    ):
        blocker = non_durable_promotion_blocker(
            content=durable_rule,
            category="general",
            source="claude-code",
            origin="agent",
        )
        assert blocker is None, (
            f"expected durable rule {durable_rule!r} to be promotable (None), got {blocker!r}"
        )
