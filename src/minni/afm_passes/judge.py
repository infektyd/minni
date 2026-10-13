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
from typing import Any, Dict, Literal, Optional

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

    # Step 4: Model Evaluation via Native AFM
    mode = resolve_afm_mode()
    if mode in ("native", "auto"):
        try:
            start_time = time.monotonic()
            res = invoke_native_afm("judge", {"candidate": content}, timeout=timeout)
            elapsed = time.monotonic() - start_time

            if res.ok and isinstance(res.data, dict):
                raw_decision = str(res.data.get("decision", "")).strip().lower()
                reason = str(res.data.get("reason", "model evaluation")).strip()

                if raw_decision in ("accept", "reject", "redact"):
                    circuit_breaker.record_success()
                    verdict = Verdict(
                        decision=raw_decision,  # type: ignore[arg-type]
                        reason=reason[:500],
                        model="afm-native-judge",
                        confidence=1.0,
                    )
                    verdict_cache.put(chash, verdict)
                    logger.info(
                        "AFM judge evaluated candidate in %.2fs: decision=%s reason=%s",
                        elapsed,
                        verdict.decision,
                        verdict.reason,
                    )
                    return verdict

            # Non-ok or unrecognized decision from AFM helper
            circuit_breaker.record_failure()
            logger.warning(
                "AFM native judge returned non-ok: status=%s error=%s",
                res.status,
                res.error,
            )
        except Exception as exc:
            circuit_breaker.record_failure()
            logger.warning("AFM native judge invocation failed: %s", exc)

    # Step 5: Jev Fallback (if explicitly enabled)
    if allow_jev_egress:
        try:
            # Jev/Typesafe AI integration hook
            # Kept strictly opt-in per privacy governance
            logger.info("Attempting Jev fallback classification")
            # Fallback path if configured
        except Exception as exc:
            logger.warning("Jev fallback failed: %s", exc)

    # Fail soft: stay staged in proposed for operator review
    return Verdict(
        decision="inconclusive",
        reason="model_unavailable_or_timed_out",
        model="fail-soft",
    )
