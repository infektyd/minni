"""Judging and answer-scoring scaffolding for future panel evaluation work."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class RubricScore:
    """Structured non-LLM score container used by future judge adapters."""

    score: float
    max_score: float = 1.0
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def normalized(self) -> float:
        if self.max_score <= 0:
            return 0.0
        return max(0.0, min(1.0, self.score / self.max_score))


class JudgeUnavailable(RuntimeError):
    """Raised when an opt-in LLM judge is requested before it is configured."""


def score_answer_placeholder(*, answer: str, rubric: str) -> RubricScore:
    """
    Deterministic placeholder for answer judging.

    LLM-judge scoring is intentionally follow-up work. This scaffold lets eval
    records and reports carry a stable shape without silently pretending that a
    model-based judge ran.
    """
    if not str(answer).strip():
        return RubricScore(score=0.0, reasons=["answer is empty"])
    if not str(rubric).strip():
        return RubricScore(score=0.0, reasons=["rubric is empty"])
    return RubricScore(
        score=0.0,
        reasons=["llm judge not configured"],
        metadata={"judge": "placeholder"},
    )
