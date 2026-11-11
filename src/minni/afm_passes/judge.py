"""Model-assisted candidate evaluation pass with deterministic shields and backpressure.

Implements the authoritative judgement bridge for Minni candidate consolidation:
1. Deterministic Pre-Model Shield: Fast secret scan, injection detection, and length floors.
2. Cross-Principal Verdict Cache: Hash-keyed evaluation caching to collapse identical swarm findings.
3. Model Evaluation: Native Apple Foundation Models with structured JSON guidance.
4. Circuit Breaker & Fail-Soft Inconclusive Fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import threading
import time
from typing import Any, Dict, List, Literal, Optional

from minni.afm_chunking import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    MIN_CHUNK_TOKENS,
    estimate_native_payload_tokens,
    resolve_afm_input_budget_tokens,
    split_text,
)
from minni.afm_provider import invoke_native_afm, resolve_afm_mode
from minni.safety import is_instruction_like

logger = logging.getLogger("minni.afm.judge")

DecisionType = Literal["accept", "reject", "redact", "inconclusive"]

# Regex patterns for high-confidence secrets and credentials
_SECRET_PATTERNS = [
    re.compile(
        r"\b(password|passwd|secret|credential|token|api[_-]?key|private[_-]?key|access[_-]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN\s+([A-Z0-9_-]+\s+)?PRIVATE\s+KEY-----", re.IGNORECASE),
]


def _normalize_for_hash(text: str) -> str:
    """Normalize text whitespace and case for cross-principal deduplication."""
    return " ".join(text.strip().lower().split())


def _content_hash(text: str) -> str:
    return hashlib.sha1(_normalize_for_hash(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Verdict:
    decision: DecisionType
    reason: str
    model: str
    confidence: float = 1.0
    cached: bool = False


class VerdictCache:
    """Bounded in-memory cache of model verdicts keyed by normalized content hash."""

    def __init__(self, max_size: int = 500):
        self._max_size = max_size
        self._cache: Dict[str, Verdict] = {}
        self._lock = threading.Lock()

    def get(self, chash: str) -> Optional[Verdict]:
        with self._lock:
            v = self._cache.get(chash)
            if v is not None:
                return Verdict(
                    decision=v.decision,
                    reason=v.reason,
                    model=v.model,
                    confidence=v.confidence,
                    cached=True,
                )
            return None

    def put(self, chash: str, verdict: Verdict) -> None:
        with self._lock:
            if len(self._cache) >= self._max_size:
                # Evict oldest 20%
                to_evict = list(self._cache.keys())[: max(1, self._max_size // 5)]
                for k in to_evict:
                    self._cache.pop(k, None)
            self._cache[chash] = verdict

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


class CircuitBreaker:
    """Circuit breaker preventing model thrash on repeated timeouts or crashes."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 300.0):
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._last_failure = 0.0
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure = time.time()

    def is_open(self) -> bool:
        with self._lock:
            if self._failures < self._threshold:
                return False
            if time.time() - self._last_failure > self._cooldown:
                # Half-open reset
                self._failures = 0
                return False
            return True


_GLOBAL_CACHE = VerdictCache(max_size=1000)
_GLOBAL_BREAKER = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)


def scan_for_secrets(text: str) -> Optional[str]:
    """Scan candidate text for credential and key patterns."""
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            matched_snippet = m.group(0)[:30]
            return f"secret pattern matched: {matched_snippet}..."
    return None


def evaluate_deterministic_shield(text: str) -> Optional[Verdict]:
    """Apply fast, zero-model filters to reject or redact low-quality/dangerous inputs."""
    # 1. Secret scan
    secret_leak = scan_for_secrets(text)
    if secret_leak:
        return Verdict(
            decision="redact",
            reason=secret_leak,
            model="deterministic-shield",
            confidence=1.0,
        )

    # 2. Instruction-like / prompt injection scan
    if is_instruction_like(text):
        return Verdict(
            decision="reject",
            reason="instruction_like pattern rejected from durable memory",
            model="deterministic-shield",
            confidence=1.0,
        )

    # 3. Minimum alphanumeric content check
    alnums = [c for c in text if c.isalnum()]
    if len(alnums) < 12:
        return Verdict(
            decision="reject",
            reason="trivial or too short (< 12 alphanumeric characters)",
            model="deterministic-shield",
            confidence=1.0,
        )

    # 4. Alphabetic ratio floor
    alpha_count = sum(1 for c in text if c.isalpha())
    if len(alnums) > 0 and (alpha_count / len(alnums)) < 0.25:
        return Verdict(
            decision="reject",
            reason="insufficient alphabetic signal (< 25% alphabetic characters)",
            model="deterministic-shield",
            confidence=1.0,
        )

    return None


_CONTENT_ERROR_KINDS = frozenset({"context_overflow", "guardrail", "refusal"})
_INLINE_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")
_WRAPPER_LEAK_RE = re.compile(
    r"(?im)(?:"
    r"^\s*Created At:\s*\d{4}-\d{2}-\d{2}T"
    r"|^\s*Completed At:\s*\d{4}-\d{2}-\d{2}T"
    r"|^\s*The following is a <SYSTEM_MESSAGE>"
    r"|^\s*</?(?:SYSTEM_MESSAGE|MINNI_ENVELOPE|background-task|task-notification)\b"
    r"|<(SYSTEM_MESSAGE|MINNI_ENVELOPE|background-task|task-notification)\b[^>]*>[\s\S]*?</\1>"
    r"|^\s*\[Message\]\s+timestamp="
    r"|^\s*Compaction summary\s*[—–-]"
    r")"
)
_TRANSIENT_CHATTER_RE = re.compile(
    r"(?i)^\s*(?:ok(?:ay)?|done|fixed?|lgtm|wip|ack|thanks|hello|test(?:ing)?)\W*$"
    r"|\b(?:temporary\s+intermediate\s+message|saying\s+hello\s+to\s+the\s+team)\b"
)


def _content_level_error_kind(res: Any) -> Optional[str]:
    """Return content-level AFM status/error_kind that must NOT trip CircuitBreaker."""
    status = str(getattr(res, "status", "") or "").strip().lower()
    if status in _CONTENT_ERROR_KINDS:
        return status
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        kind = str(data.get("error_kind") or "").strip().lower()
        if kind in _CONTENT_ERROR_KINDS:
            return kind
    return None


def evaluate_deterministic_quality_fallback(text: str) -> Verdict:
    """Deterministic quality/verdict evaluator for content-level AFM trips and Jev fallback.

    Valid technical notes (e.g., security, concurrency, or process-kill scars that trip
    Apple Foundation Models safety guardrails, or long notes exceeding chunk limits) get
    judged cleanly instead of stranding in inconclusive forever.
    """
    shield = evaluate_deterministic_shield(text)
    if shield is not None:
        return shield

    stripped = (text or "").strip()
    if _WRAPPER_LEAK_RE.search(_INLINE_CODE_SPAN_RE.sub(" ", stripped)):
        return Verdict(
            decision="reject",
            reason="wrapper or system envelope leak rejected",
            model="deterministic-quality-fallback",
            confidence=0.95,
        )
    if _TRANSIENT_CHATTER_RE.search(stripped):
        return Verdict(
            decision="reject",
            reason="transient conversational chatter rejected",
            model="deterministic-quality-fallback",
            confidence=0.95,
        )

    from minni.afm_passes.consolidation import (
        _quality_blockers,
        is_low_signal_boot_learning,
    )
    from minni.durability import durability_of
    from minni.minnid_runtime.governance import _SPRINT_CHATTER_RE

    if is_low_signal_boot_learning(stripped) or _SPRINT_CHATTER_RE.search(stripped):
        return Verdict(
            decision="reject",
            reason="low-signal telemetry or sprint chatter rejected",
            model="deterministic-quality-fallback",
            confidence=0.95,
        )

    blockers = _quality_blockers(stripped)
    if blockers:
        return Verdict(
            decision="reject",
            reason="quality blocker: " + "; ".join(blockers),
            model="deterministic-quality-fallback",
            confidence=0.95,
        )

    if durability_of({"content": stripped, "category": "general"}) == "noise":
        return Verdict(
            decision="reject",
            reason="non-durable noise rejected",
            model="deterministic-quality-fallback",
            confidence=0.9,
        )

    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_./:-]*", stripped)
    meaningful = [t for t in tokens if any(ch.isalpha() for ch in t) and len(t) >= 2]
    if len(meaningful) < 4 or len(re.sub(r"\s+", "", stripped)) < 15:
        return Verdict(
            decision="reject",
            reason="insufficient meaningful content (< 4 tokens or < 15 chars)",
            model="deterministic-quality-fallback",
            confidence=0.9,
        )

    return Verdict(
        decision="accept",
        reason="deterministic quality fallback accepted technical note",
        model="deterministic-quality-fallback",
        confidence=0.85,
    )


def _judge_chunk_recursive(
    piece: str,
    *,
    timeout: float,
    chunk_tokens: int,
    overlap_tokens: int,
) -> List[tuple[str, Any]]:
    """Invoke native AFM judge on one piece, recursively splitting on context_overflow."""
    res = invoke_native_afm("judge", {"candidate": piece}, timeout=timeout)
    if (
        not getattr(res, "ok", False)
        and _content_level_error_kind(res) == "context_overflow"
        and chunk_tokens > MIN_CHUNK_TOKENS
    ):
        next_tokens = max(chunk_tokens // 2, MIN_CHUNK_TOKENS)
        eff_overlap = max(min(overlap_tokens, next_tokens // 4), 0)
        sub_pieces = split_text(piece, chunk_tokens=next_tokens, overlap_tokens=eff_overlap)
        if len(sub_pieces) > 1:
            out: List[tuple[str, Any]] = []
            for sub in sub_pieces:
                out.extend(
                    _judge_chunk_recursive(
                        sub,
                        timeout=timeout,
                        chunk_tokens=next_tokens,
                        overlap_tokens=overlap_tokens,
                    )
                )
            return out
    return [(piece, res)]


def judge_candidate(
    content: str,
    trace_id: str = "",
    timeout: float = 15.0,
    allow_jev_egress: bool = False,
    cache: Optional[VerdictCache] = None,
    breaker: Optional[CircuitBreaker] = None,
) -> Verdict:
    """Evaluate candidate text through deterministic shield, cross-principal cache, and model judge."""
    # Step 1: Deterministic Shield
    shield_verdict = evaluate_deterministic_shield(content)
    if shield_verdict is not None:
        return shield_verdict

    verdict_cache = cache or _GLOBAL_CACHE
    circuit_breaker = breaker or _GLOBAL_BREAKER

    # Step 2: Cross-Principal Verdict Cache
    chash = _content_hash(content)
    cached_verdict = verdict_cache.get(chash)
    if cached_verdict is not None:
        return cached_verdict

    # Step 3: Circuit Breaker Check
    if circuit_breaker.is_open():
        logger.warning(
            "AFM judge circuit breaker is OPEN (cooldown active); leaving candidate in review"
        )
        return Verdict(
            decision="inconclusive",
            reason="circuit_breaker_open",
            model="circuit-breaker",
        )

    # Step 4: Model Evaluation via Native AFM (with proactive & reactive chunking)
    mode = resolve_afm_mode()
    if mode in ("native", "auto"):
        try:
            start_time = time.monotonic()
            budget = resolve_afm_input_budget_tokens()
            chunk_size = max(min(DEFAULT_CHUNK_TOKENS, budget), MIN_CHUNK_TOKENS)
            eff_overlap = max(min(DEFAULT_OVERLAP_TOKENS, chunk_size // 4), 0)

            payload = {"candidate": content}
            if estimate_native_payload_tokens(payload) > budget:
                pieces = split_text(content, chunk_tokens=chunk_size, overlap_tokens=eff_overlap)
                chunk_evals: List[tuple[str, Any]] = []
                for piece in pieces:
                    chunk_evals.extend(
                        _judge_chunk_recursive(
                            piece,
                            timeout=timeout,
                            chunk_tokens=chunk_size,
                            overlap_tokens=DEFAULT_OVERLAP_TOKENS,
                        )
                    )
            else:
                res = invoke_native_afm("judge", payload, timeout=timeout)
                if (
                    not getattr(res, "ok", False)
                    and _content_level_error_kind(res) == "context_overflow"
                ):
                    next_size = max(chunk_size // 2, MIN_CHUNK_TOKENS)
                    next_overlap = max(min(DEFAULT_OVERLAP_TOKENS, next_size // 4), 0)
                    pieces = split_text(content, chunk_tokens=next_size, overlap_tokens=next_overlap)
                    if len(pieces) > 1:
                        chunk_evals = []
                        for piece in pieces:
                            chunk_evals.extend(
                                _judge_chunk_recursive(
                                    piece,
                                    timeout=timeout,
                                    chunk_tokens=next_size,
                                    overlap_tokens=DEFAULT_OVERLAP_TOKENS,
                                )
                            )
                    else:
                        chunk_evals = [(content, res)]
                else:
                    chunk_evals = [(content, res)]

            elapsed = time.monotonic() - start_time

            provider_failed = False
            native_succeeded = False
            chunk_verdicts: List[Verdict] = []

            for piece, res in chunk_evals:
                if getattr(res, "ok", False) and isinstance(getattr(res, "data", None), dict):
                    raw_decision = str(res.data.get("decision", "")).strip().lower()
                    reason = str(res.data.get("reason", "model evaluation")).strip()
                    if raw_decision in ("accept", "reject", "redact"):
                        native_succeeded = True
                        chunk_verdicts.append(
                            Verdict(
                                decision=raw_decision,  # type: ignore[arg-type]
                                reason=reason[:500],
                                model="afm-native-judge",
                                confidence=1.0,
                            )
                        )
                        continue

                content_err = _content_level_error_kind(res)
                if content_err is not None:
                    # Content-level overflow/guardrail/refusal is NOT a provider crash:
                    # never poison the global circuit breaker, and evaluate deterministically.
                    logger.info(
                        "AFM native judge returned content-level %s; applying deterministic fallback",
                        content_err,
                    )
                    chunk_verdicts.append(evaluate_deterministic_quality_fallback(piece))
                    continue

                provider_failed = True
                logger.warning(
                    "AFM native judge returned non-ok: status=%s error=%s",
                    getattr(res, "status", None),
                    getattr(res, "error", None),
                )
                break

            if provider_failed:
                circuit_breaker.record_failure()
            elif chunk_verdicts:
                if native_succeeded:
                    circuit_breaker.record_success()
                # Fold chunk verdicts conservatively: redact > reject > accept
                redact_v = next((v for v in chunk_verdicts if v.decision == "redact"), None)
                reject_v = next((v for v in chunk_verdicts if v.decision == "reject"), None)
                if redact_v is not None:
                    final_verdict = redact_v
                elif reject_v is not None:
                    final_verdict = reject_v
                else:
                    all_native = all(v.model == "afm-native-judge" for v in chunk_verdicts)
                    reasons = "; ".join(dict.fromkeys(v.reason for v in chunk_verdicts if v.reason))
                    final_verdict = Verdict(
                        decision="accept",
                        reason=(reasons or "model evaluation")[:500],
                        model="afm-native-judge" if all_native else "deterministic-quality-fallback",
                        confidence=min(v.confidence for v in chunk_verdicts),
                    )
                verdict_cache.put(chash, final_verdict)
                logger.info(
                    "AFM judge evaluated candidate in %.2fs: decision=%s model=%s reason=%s",
                    elapsed,
                    final_verdict.decision,
                    final_verdict.model,
                    final_verdict.reason,
                )
                return final_verdict
        except Exception as exc:
            circuit_breaker.record_failure()
            logger.warning("AFM native judge invocation failed: %s", exc)

    # Step 5: Deterministic quality evaluator fallback when egress/fallback is enabled
    if allow_jev_egress:
        try:
            logger.info("Evaluating candidate via deterministic quality fallback (allow_jev_egress=True)")
            fallback_verdict = evaluate_deterministic_quality_fallback(content)
            if fallback_verdict.decision in ("accept", "reject", "redact"):
                verdict_cache.put(chash, fallback_verdict)
                return fallback_verdict
        except Exception as exc:
            logger.warning("Deterministic fallback evaluation failed: %s", exc)

    # Fail soft: stay staged in proposed for operator review
    return Verdict(
        decision="inconclusive",
        reason="model_unavailable_or_timed_out",
        model="fail-soft",
    )
