"""Calibrated LLM judge + the load-bearing calibration gate (§3.3, §9.6).

The Judge scores an (answer vs gold) pair as correct / incorrect, and per-episode
task-success. Two implementations:

* :class:`StubJudge` — DETERMINISTIC, OFFLINE. Scores an answer "correct" iff the
  gold-fact substring is present in the answer. Used in every test; no randomness.
* :class:`LLMJudge` — the REAL Anthropic-model judge, pinned to
  ``config.JUDGE_MODEL`` and gated by ``MAX_API_CALLS`` + an env key; NEVER called
  in tests (mirrors :class:`membench.agent.LLMAgent`).

THE CALIBRATION GATE (load-bearing, §3.3 / §9.6). Before ANY judge numbers may be
published, the judge must clear, on a HUMAN-CHECKED subset of paired judgments
(human label vs judge label):

  (a) raw agreement >= 0.80, AND
  (b) Cohen's kappa  >= 0.60   (Landis-Koch "substantial"), AND
  (c) the subset has >= JUDGE_MIN_SUBSET_N (=40) paired judgments.

A subset with < 40 pairs is a HARD ERROR — not a warning. kappa is mandatory
because raw agreement is chance-inflated on skewed labels: a judge that always
says "success" on an 85%-success subset scores ~0.85 raw agreement yet kappa ~ 0,
and the kappa gate (correctly) REJECTS it.

Also enforces the no-self-judge gate: judge_model.model_family must differ from
agent_model.model_family (delegated to config.assert_config_valid, §3.3).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import config

# Bound untrusted judge-gate fixtures the same way episodes/goldset are bounded.
MAX_JUDGE_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_JUDGE_PAIRS = 100_000


# ── SHARED cumulative API-call budget (§7.15, review fix 4) ──────────────────
# Same rationale as the agent: MAX_API_CALLS is a CUMULATIVE cap across ALL LLM
# roles (agent + judge + llm_wiki curation), not a per-role one. The judge now
# reserves against the ONE shared counter in membench.api_budget so agent + judge
# + curation calls share a single ceiling. These wrappers preserve the names the
# tests use and delegate to the shared budget.
from . import api_budget


def _reserve_api_call(max_api_calls: int) -> None:
    """Reserve one call on the SHARED cumulative budget (delegates, fix 4)."""
    api_budget.reserve(max_api_calls, role="judge")


def _reset_api_calls() -> None:
    """Reset the SHARED cumulative call counter (test-only helper)."""
    api_budget.reset()

# Calibration thresholds (§3.3 / §9.6). Single source of truth so the gate and
# any metric re-validation (e.g. runner_layer2.results_to_dict) agree exactly.
_CALIBRATION_MIN_AGREEMENT = 0.80
_CALIBRATION_MIN_KAPPA = 0.60


def load_paired_judgments(
    path: str | os.PathLike[str],
) -> tuple[list[int], list[int]]:
    """Load a judge-gate fixture (JSONL of {human, judge} 0/1 pairs).

    Returns ``(human_labels, judge_labels)``. These fixtures are FABRICATED
    (synthetic, not real judgments) and live under
    ``membench/fixtures/judge_gate/`` — NOT under ``_private/`` (§9.6).

    Bounds untrusted input (file size + pair count) and validates that every row
    is a ``{human, judge}`` object whose values are BINARY 0/1. Non-binary labels
    are a HARD ERROR: they would let a forged fixture inflate raw agreement / kappa
    and BYPASS the load-bearing calibration gate (§3.3 / §9.6).
    """
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_JUDGE_FIXTURE_BYTES:
        raise JudgeGateError(
            f"judge-gate fixture {path.name!r} is {size} bytes, exceeds the "
            f"{MAX_JUDGE_FIXTURE_BYTES}-byte cap (refusing to load)"
        )
    human: list[int] = []
    judge: list[int] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if len(human) >= MAX_JUDGE_PAIRS:
            raise JudgeGateError(
                f"judge-gate fixture exceeds the {MAX_JUDGE_PAIRS}-pair cap"
            )
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JudgeGateError(f"judge-gate fixture has malformed JSON: {exc}")
        if not isinstance(d, dict) or "human" not in d or "judge" not in d:
            raise JudgeGateError(
                "each judge-gate row must be an object with the keys "
                f"'human' and 'judge' (got "
                f"{sorted(d) if isinstance(d, dict) else type(d).__name__})"
            )
        h, j = d["human"], d["judge"]
        # Strict integer-only labels. ``int()`` is deliberately NOT used: it
        # would silently truncate JSON floats (int(0.9)==0) and coerce JSON
        # strings (int("1")==1), corrupting continuous-score or string fixtures
        # into 0/1 with no error and computing kappa over the corrupted data.
        # ``isinstance(True, int)`` is True, so JSON true/false (valid binary
        # labels) still pass; JSON floats like 1.0 correctly fail.
        if not (isinstance(h, int) and isinstance(j, int)):
            raise JudgeGateError(
                "judge-gate labels must be integer 0 or 1, not float/string "
                "— silent coercion would corrupt calibration data (§9.6)"
            )
        if h not in (0, 1) or j not in (0, 1):
            raise JudgeGateError(
                "judge-gate paired judgments must be BINARY 0/1 — non-binary "
                "labels would forge agreement/kappa and bypass the gate (§9.6)"
            )
        human.append(h)
        judge.append(j)
    return human, judge


def judge_gate_fixture_path(name: str) -> Path:
    """Path to a named judge-gate fixture under membench/fixtures/judge_gate/."""
    return Path(__file__).resolve().parent / "fixtures" / "judge_gate" / name


# ---------------------------------------------------------------------------
# Judge scoring (offline StubJudge + gated LLMJudge)
# ---------------------------------------------------------------------------
class Judge(Protocol):
    """Scores an answer against the gold fact: correct (1) / incorrect (0)."""

    name: str
    model_family: str

    def score(self, answer: str, gold_fact: str) -> int:
        ...


class StubJudge:
    """Deterministic offline judge for tests (§9.6).

    Scores correct (1) iff the ``gold_fact`` substring appears in ``answer``.
    Pinned to ``config.JUDGE_MODEL`` family so the no-self-judge gate is testable;
    makes no network call. Fully deterministic -> non-flaky tests.
    """

    name = "stub_judge"
    model_family = config.JUDGE_MODEL.model_family

    def score(self, answer: str, gold_fact: str) -> int:
        return 1 if (gold_fact and gold_fact in answer) else 0


class ConstantStubJudge:
    """A degenerate judge that ALWAYS returns the same label (test fixture, §9.6).

    Used to prove the kappa gate rejects a constant-answer judge that rides on a
    skewed label distribution (high raw agreement, kappa ~ 0). Deterministic.
    """

    name = "constant_stub_judge"
    model_family = config.JUDGE_MODEL.model_family

    def __init__(self, constant_label: int) -> None:
        self.constant_label = int(constant_label)

    def score(self, answer: str, gold_fact: str) -> int:
        return self.constant_label


class LLMJudge:
    """The REAL Anthropic-model judge (gated; NEVER called in tests) (§3.3).

    Pinned to ``config.JUDGE_MODEL``; enforces ``config.MAX_API_CALLS`` and
    resolves its API key from the environment by NAME at call time. Never
    constructed by a test; ``score()`` raises without a key so it cannot make a
    silent network call.
    """

    name = "llm_judge"
    model_family = config.JUDGE_MODEL.model_family

    def __init__(self, *, max_api_calls: int | None = None) -> None:
        self.model_id = config.JUDGE_MODEL.model_id
        self.max_api_calls = (
            config.MAX_API_CALLS if max_api_calls is None else max_api_calls
        )

    def _resolve_key(self) -> str:
        env_name = config.CREDENTIAL_ENV_VARS["judge_api_key"]
        key = os.environ.get(env_name)
        if not key:
            raise RuntimeError(
                f"judge API key env var {env_name!r} is unset — the real LLM "
                "judge cannot run (this path is never exercised offline)."
            )
        return key

    def score(self, answer: str, gold_fact: str) -> int:
        # Reserve on the PROCESS-GLOBAL counter first so N judges share one budget.
        _reserve_api_call(self.max_api_calls)
        # SECURITY (review fix): do NOT bind the API key to a named local in this
        # stub. ``_resolve_key()`` is deliberately NOT called here — it is
        # meaningless without the real network call, and any error-reporting
        # framework that captures locals on an exception (Sentry, cgitb, logging
        # with exc_info) would otherwise expose the plaintext key from this frame
        # at the NotImplementedError below. The key is resolved at call time ONLY
        # in the real implementation, immediately before the Anthropic client call
        # (wired in the run slice, never reached by any test). This mirrors the
        # identical fix in LLMAgent.answer().
        raise NotImplementedError(
            "LLMJudge.score is the gated live path; not implemented in s5 "
            "(offline-only). Use StubJudge in tests."
        )


# ---------------------------------------------------------------------------
# Cohen's kappa + the calibration gate (§3.3, §9.6)
# ---------------------------------------------------------------------------
def compute_cohen_kappa(human: list[int], judge: list[int]) -> float:
    """Cohen's kappa over paired binary judgments (§3.3).

    kappa = (p_o - p_e) / (1 - p_e), where p_o is observed agreement and p_e is
    chance agreement from the marginal label frequencies. Returns 0.0 when the
    denominator (1 - p_e) is 0 — i.e. both raters used a single label
    everywhere, so there is no information beyond chance (a constant judge over a
    single-label human set agrees by chance, not skill). Pure / offline.
    """
    if len(human) != len(judge):
        raise ValueError("human and judge label lists must be the same length")
    n = len(human)
    if n == 0:
        raise ValueError("cannot compute kappa over an empty subset")

    p_o = sum(1 for h, j in zip(human, judge) if h == j) / n

    labels = set(human) | set(judge)
    p_e = 0.0
    for label in labels:
        p_h = sum(1 for h in human if h == label) / n
        p_j = sum(1 for j in judge if j == label) / n
        p_e += p_h * p_j

    denom = 1.0 - p_e
    if denom == 0.0:
        # No chance-corrected information available (degenerate marginals).
        return 0.0
    return (p_o - p_e) / denom


def raw_agreement(human: list[int], judge: list[int]) -> float:
    """Raw (observed) agreement over paired judgments."""
    if len(human) != len(judge):
        raise ValueError("human and judge label lists must be the same length")
    if not human:
        raise ValueError("cannot compute agreement over an empty subset")
    return sum(1 for h, j in zip(human, judge) if h == j) / len(human)


class JudgeGateError(RuntimeError):
    """Raised when the calibration gate is NOT cleared — numbers may not publish."""


@dataclass(frozen=True)
class CalibrationResult:
    """The calibration measurement (always reported, even on failure) (§3.3)."""

    n: int
    raw_agreement: float
    cohen_kappa: float
    passed: bool


def calibrate_judge(
    human_labels: list[int],
    judge_labels: list[int],
    *,
    min_subset_n: int = config.JUDGE_MIN_SUBSET_N,
    min_agreement: float = _CALIBRATION_MIN_AGREEMENT,
    min_kappa: float = _CALIBRATION_MIN_KAPPA,
) -> CalibrationResult:
    """Run the calibration GATE on a human-checked paired subset (§3.3, §9.6).

    Returns a :class:`CalibrationResult` (n, raw agreement, kappa, passed) for
    reporting. Both numbers are always computed and reported. ``passed`` is True
    iff ALL THREE thresholds clear:

      n >= ``min_subset_n`` (default 40) AND
      raw_agreement >= ``min_agreement`` (0.80) AND
      cohen_kappa  >= ``min_kappa`` (0.60).

    This function only MEASURES + reports; :func:`assert_judge_publishable` is the
    hard gate the runner calls before publishing.
    """
    if len(human_labels) != len(judge_labels):
        raise ValueError("human and judge label lists must be the same length")
    n = len(human_labels)
    if n == 0:
        raise JudgeGateError("calibration subset is empty (§3.3)")

    agreement = raw_agreement(human_labels, judge_labels)
    kappa = compute_cohen_kappa(human_labels, judge_labels)
    passed = (
        n >= min_subset_n
        and agreement >= min_agreement
        and kappa >= min_kappa
    )
    return CalibrationResult(
        n=n, raw_agreement=agreement, cohen_kappa=kappa, passed=passed
    )


def assert_judge_publishable(
    human_labels: list[int],
    judge_labels: list[int],
    *,
    min_subset_n: int = config.JUDGE_MIN_SUBSET_N,
    min_agreement: float = _CALIBRATION_MIN_AGREEMENT,
    min_kappa: float = _CALIBRATION_MIN_KAPPA,
) -> CalibrationResult:
    """HARD gate: raise unless the judge cleared calibration (§3.3, §9.6).

    Order of checks matters for clear errors:
      1. min-n gate FIRST (a subset < min_subset_n is a HARD ERROR, §3.3),
      2. no-self-judge gate (config.assert_config_valid — family must differ),
      3. raw-agreement gate,
      4. kappa gate.

    Returns the :class:`CalibrationResult` on success; raises
    :class:`JudgeGateError` (or ``config.ConfigError`` for the family clash) on
    any failure so the caller CANNOT publish judge numbers past a failed gate.
    """
    n = len(human_labels)
    # (1) minimum-n gate — hard error, checked before agreement/kappa so a tiny
    # subset is rejected even if it would otherwise clear the thresholds.
    if n < min_subset_n:
        raise JudgeGateError(
            f"calibration subset has {n} paired judgment(s) < "
            f"JUDGE_MIN_SUBSET_N={min_subset_n}: HARD ERROR — judge numbers may "
            "NOT be published (§3.3). kappa's CI is too wide below this n."
        )

    # (2) no-self-judge gate — judge family must differ from agent family.
    config.assert_config_valid()

    result = calibrate_judge(
        human_labels,
        judge_labels,
        min_subset_n=min_subset_n,
        min_agreement=min_agreement,
        min_kappa=min_kappa,
    )

    # (3) raw-agreement gate.
    if result.raw_agreement < min_agreement:
        raise JudgeGateError(
            f"raw agreement {result.raw_agreement:.4f} < {min_agreement} "
            f"(n={n}, kappa={result.cohen_kappa:.4f}): judge numbers may NOT be "
            "published (§3.3)."
        )
    # (4) kappa gate — catches the skewed-constant judge (high agreement, low
    # kappa) that a raw-agreement-only gate would wrongly pass.
    if result.cohen_kappa < min_kappa:
        raise JudgeGateError(
            f"Cohen's kappa {result.cohen_kappa:.4f} < {min_kappa} "
            f"(n={n}, raw_agreement={result.raw_agreement:.4f}): judge numbers "
            "may NOT be published — chance-inflated agreement on skewed labels "
            "(§3.3)."
        )
    return result
