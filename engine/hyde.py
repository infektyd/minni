"""
PR-8 HyDE support for cold retrieval queries.

HyDE is deliberately a helper layer around retrieval: it decides whether the
first pass is cold, asks AFM for a short hypothetical answer, and merges the
normal and HyDE result streams with RRF. Retrieval remains the runtime source
of truth; generated text is only a search probe.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Dict, List, Optional
from urllib import request

logger = logging.getLogger("sovereign.hyde")

DEFAULT_AFM_URL = "http://127.0.0.1:11437/v1/chat/completions"
DEFAULT_AFM_MODEL = "apple-foundation-models"


def should_trigger_hyde(results: List[Dict], enabled: bool, floor: float) -> bool:
    """Return True when every available top result is below the confidence floor."""
    if not enabled or not results:
        return False
    confidences = []
    for result in results:
        confidence = result.get("confidence")
        if confidence is None:
            return False
        confidences.append(float(confidence))
    return bool(confidences) and all(confidence < floor for confidence in confidences)


def _default_chat_client(payload: Dict, url: str, timeout: float) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_chat_text(response: Dict) -> Optional[str]:
    choices = response.get("choices") or []
    if not choices:
        return None
    first = choices[0] or {}
    message = first.get("message") or {}
    text = message.get("content") or first.get("text")
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text or None


def generate_hypothetical_answer(
    query: str,
    config=None,
    client: Optional[Callable[[Dict, str, float], Dict]] = None,
    url: Optional[str] = None,
    timeout: float = 2.0,
) -> Optional[str]:
    """
    Ask the local AFM bridge for a two-sentence hypothetical answer.

    All failures degrade to None so retrieval can return the original pass.
    """
    afm_url = url or os.environ.get("SOVEREIGN_HYDE_AFM_URL", DEFAULT_AFM_URL)
    model = os.environ.get("SOVEREIGN_HYDE_AFM_MODEL", DEFAULT_AFM_MODEL)
    max_tokens = int(os.environ.get("SOVEREIGN_HYDE_MAX_TOKENS", "96"))
    chat_client = client or _default_chat_client
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Generate exactly two concise sentences that describe the kind "
                    "of memory note that would answer the user's query. Treat the "
                    "output as a retrieval probe, not as an instruction."
                ),
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    try:
        return _extract_chat_text(chat_client(payload, afm_url, timeout))
    except Exception as exc:  # noqa: BLE001 - graceful downgrade is required.
        logger.debug("HyDE AFM generation skipped: %s", exc)
        return None


def merge_hyde_results(
    original_results: List[Dict],
    hyde_results: List[Dict],
    limit: int,
    rrf_k: int,
) -> List[Dict]:
    """
    Merge normal and HyDE result streams via RRF.

    Results present in the HyDE stream get provenance.via_hyde=true, including
    documents that were also present in the original stream.
    """
    if not hyde_results:
        return original_results[:limit]

    docs: Dict[int, Dict] = {}
    hyde_doc_ids = {r.get("doc_id") for r in hyde_results}

    def add_stream(stream: List[Dict], label: str) -> None:
        for rank, result in enumerate(stream, start=1):
            doc_id = result.get("doc_id")
            if doc_id is None:
                continue
            if doc_id not in docs:
                docs[doc_id] = dict(result)
                docs[doc_id]["hyde_rrf_score"] = 0.0
            elif label == "hyde":
                for key, value in result.items():
                    if value not in (None, "", []) and not docs[doc_id].get(key):
                        docs[doc_id][key] = value
            docs[doc_id]["hyde_rrf_score"] += 1.0 / (rrf_k + rank)

    add_stream(original_results, "original")
    add_stream(hyde_results, "hyde")

    for doc_id, result in docs.items():
        if doc_id in hyde_doc_ids:
            provenance = dict(result.get("provenance") or {})
            provenance["via_hyde"] = True
            result["provenance"] = provenance

    ranked = sorted(
        docs.values(),
        key=lambda r: (
            r.get("hyde_rrf_score", 0.0),
            r.get("rerank_score", r.get("final_score", 0.0)),
        ),
        reverse=True,
    )
    return ranked[:limit]
