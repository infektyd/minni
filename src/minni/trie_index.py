"""In-memory inverted prefix/token Trie index for sub-millisecond fast-path recall."""

import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _strip_frontmatter(text: str) -> str:
    if not text:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 4)
            return text[nl + 1 :].lstrip() if nl != -1 else ""
    return text


def _tokenize_trie_text(text: str) -> List[str]:
    if not text:
        return []
    return [tok for tok in re.findall(r"[a-z0-9_]{2,}", text.lower()) if len(tok) >= 2]


class TrieIndexMixin:
    """Tier 1 Reflex: in-memory inverted prefix/token Trie (< 0.5 ms)."""

    _OR_FALLBACK_STOPWORDS = frozenset({
        "minni", "sovereign", "the", "and", "for", "with", "from", "this",
        "that", "what", "how", "why", "when", "where", "are", "was", "were",
        "is", "in", "on", "to", "of", "by", "at", "as", "or", "not",
    })

    _strip_frontmatter = staticmethod(_strip_frontmatter)
    _tokenize_trie_text = staticmethod(_tokenize_trie_text)

    def _index_doc_in_trie(
        self,
        *,
        doc_id: int,
        path: str,
        agent: str,
        sigil: str,
        decay_score: float,
        chunk_text: str,
        heading_context: str,
        page_status: str,
        privacy_level: str,
        page_type: Optional[str],
        evidence_refs: Any,
        indexed_at: Any,
        layer: str,
    ) -> None:
        import minni.retrieval as _ret

        if getattr(self, "_trie_docs", None) is None:
            return
        lock = getattr(self, "_trie_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._trie_lock = lock
        with lock:
            entry = {
                "doc_id": doc_id,
                "path": path or "",
                "agent": agent or "",
                "sigil": sigil or "❓",
                "decay_score": float(decay_score if decay_score is not None else 1.0),
                "chunk_text": self._strip_frontmatter(chunk_text or "")[:1200],
                "heading_context": heading_context or "",
                "page_status": page_status or "candidate",
                "privacy_level": privacy_level or "safe",
                "page_type": page_type,
                "evidence_refs": evidence_refs,
                "indexed_at": indexed_at,
                "layer": layer or "knowledge",
            }
            self._trie_docs[doc_id] = entry
            header_text = f"{path or ''} {heading_context or ''} {(chunk_text or '')[:600]}"
            tokens = set(self._tokenize_trie_text(header_text))
            for tok in tokens:
                self._trie_token_map.setdefault(tok, set()).add(doc_id)
                max_pref = min(len(tok), 12)
                for plen in range(3, max_pref + 1):
                    self._trie_prefix_map.setdefault(tok[:plen], set()).add(doc_id)
            if indexed_at is not None:
                try:
                    self._trie_max_doc_id = max(
                        int(getattr(self, "_trie_max_doc_id", 0) or 0), int(doc_id)
                    )
                    self._trie_checked_at = _ret.time.monotonic()
                except Exception:
                    pass

    def _selective_or_terms(self, lowered_terms: Sequence[str]) -> List[str]:
        token_map = getattr(self, "_trie_token_map", None) or {}
        return [
            t for t in lowered_terms
            if t not in self._OR_FALLBACK_STOPWORDS
            and len(token_map.get(t, ())) <= 500
        ]

    def _ensure_trie_loaded(self) -> None:
        import minni.retrieval as _ret

        if getattr(self, "db", None) is None:
            return
        now_mono = _ret.time.monotonic()
        if (
            getattr(self, "_trie_docs", None) is not None
            and (now_mono - float(getattr(self, "_trie_checked_at", 0.0) or 0.0)) < 30.0
        ):
            return
        lock = getattr(self, "_trie_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._trie_lock = lock
        with lock:
            now_mono = _ret.time.monotonic()
            if (
                getattr(self, "_trie_docs", None) is not None
                and (now_mono - float(getattr(self, "_trie_checked_at", 0.0) or 0.0)) < 30.0
            ):
                return
            try:
                with self.db.cursor() as c:
                    c.execute("SELECT COUNT(*), COALESCE(MAX(doc_id), 0) FROM documents")
                    meta = c.fetchone()
                    doc_count = int(meta[0] or 0) if meta else 0
                    max_doc_id = int(meta[1] or 0) if meta else 0
                    prev_max = int(getattr(self, "_trie_max_doc_id", -1))
                    prev_count = int(getattr(self, "_trie_doc_count", -1))
                    self._trie_checked_at = now_mono
                    if (
                        getattr(self, "_trie_docs", None) is not None
                        and max_doc_id == prev_max
                        and doc_count == prev_count
                    ):
                        return
                    incremental = (
                        getattr(self, "_trie_docs", None) is not None
                        and prev_max >= 0
                        and doc_count >= prev_count
                        and max_doc_id >= prev_max
                    )
                    if not incremental:
                        self._trie_docs = {}
                        self._trie_token_map = {}
                        self._trie_prefix_map = {}
                        prev_max = 0
                    self._trie_max_doc_id = max_doc_id
                    self._trie_doc_count = doc_count
                    if doc_count == 0:
                        return
                    if doc_count < 50:
                        c.execute(
                            """
                            SELECT d.doc_id, d.path, d.agent, d.sigil, d.decay_score,
                                   d.page_status, d.privacy_level, d.page_type,
                                   d.evidence_refs, d.indexed_at, d.layer,
                                   COALESCE(
                                       (SELECT ce.chunk_text FROM chunk_embeddings ce
                                        WHERE ce.doc_id = d.doc_id
                                        ORDER BY ce.chunk_index ASC LIMIT 1),
                                       (SELECT substr(vf.content, 1, 1200) FROM vault_fts vf
                                        WHERE vf.doc_id = d.doc_id LIMIT 1),
                                       ''
                                   ) AS chunk_text,
                                   COALESCE(
                                       (SELECT ce.heading_context FROM chunk_embeddings ce
                                        WHERE ce.doc_id = d.doc_id
                                        ORDER BY ce.chunk_index ASC LIMIT 1),
                                       ''
                                   ) AS heading_context
                            FROM documents d
                            WHERE d.doc_id > ?
                              AND COALESCE(d.privacy_level, 'safe') != 'blocked'
                            """,
                            (prev_max,),
                        )
                    else:
                        c.execute(
                            """
                            SELECT d.doc_id, d.path, d.agent, d.sigil, d.decay_score,
                                   d.page_status, d.privacy_level, d.page_type,
                                   d.evidence_refs, d.indexed_at, d.layer,
                                   COALESCE(ce.chunk_text, '') AS chunk_text,
                                   COALESCE(ce.heading_context, '') AS heading_context
                            FROM documents d
                            LEFT JOIN chunk_embeddings ce
                              ON ce.doc_id = d.doc_id AND ce.chunk_index = 0
                            WHERE d.doc_id > ?
                              AND COALESCE(d.privacy_level, 'safe') != 'blocked'
                            """,
                            (prev_max,),
                        )
                    rows = c.fetchall()
            except Exception:
                # Watermarks were advanced before the SELECT; reset them so the
                # next load (after the 30s throttle) does a full rebuild instead
                # of treating the empty/partial index as current.
                self._trie_max_doc_id = -1
                self._trie_doc_count = -1
                return
            for r in rows:
                did = r["doc_id"]
                raw_text = r["chunk_text"] or ""
                clean_text = self._strip_frontmatter(raw_text)[:1200]
                path_str = r["path"] or ""
                heading_str = r["heading_context"] or ""
                self._trie_docs[did] = {
                    "doc_id": did,
                    "path": path_str,
                    "path_lower": path_str.lower(),
                    "agent": r["agent"] or "",
                    "sigil": r["sigil"] or "❓",
                    "decay_score": float(
                        r["decay_score"] if r["decay_score"] is not None else 1.0
                    ),
                    "chunk_text": clean_text,
                    "heading_context": heading_str,
                    "page_status": r["page_status"] or "candidate",
                    "privacy_level": r["privacy_level"] or "safe",
                    "page_type": r["page_type"],
                    "evidence_refs": r["evidence_refs"],
                    "indexed_at": r["indexed_at"],
                    "layer": r["layer"] or "knowledge",
                }
                header_text = f"{path_str} {heading_str} {clean_text[:300]}"
                tokens = set(self._tokenize_trie_text(header_text))
                for tok in tokens:
                    self._trie_token_map.setdefault(tok, set()).add(did)
                    max_pref = min(len(tok), 10)
                    for plen in range(3, max_pref + 1):
                        self._trie_prefix_map.setdefault(tok[:plen], set()).add(did)

    def trie_search(
        self,
        query: str,
        limit: int = 10,
        agent_filter: Optional[Sequence[str]] = None,
        exclude_statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """Sub-millisecond Tier 1 in-memory Prefix/Token Trie lookup."""
        self._ensure_trie_loaded()
        trie_docs = getattr(self, "_trie_docs", None)
        if not trie_docs:
            return []
        q_tokens = self._tokenize_trie_text(query)
        if not q_tokens:
            return []
        excluded = set(exclude_statuses or ())
        agent_set = self._normalize_agent_filter(agent_filter) if agent_filter else None
        scores: Dict[int, float] = {}
        token_map = getattr(self, "_trie_token_map", {})
        prefix_map = getattr(self, "_trie_prefix_map", {})
        selective_tokens = (
            self._selective_or_terms(q_tokens) if len(q_tokens) > 1 else q_tokens
        )
        primary_tokens = selective_tokens if selective_tokens else q_tokens
        ubiquitous_tokens = (
            [tok for tok in q_tokens if tok not in primary_tokens]
            if selective_tokens
            else []
        )
        for tok in primary_tokens:
            for did in token_map.get(tok, ()):
                scores[did] = scores.get(did, 0.0) + 3.0
            if len(tok) >= 3:
                pref = tok[:10]
                p_matches = prefix_map.get(pref, ())
                if len(p_matches) <= 500 or not selective_tokens:
                    for did in p_matches:
                        scores[did] = scores.get(did, 0.0) + 1.0
        if not scores and ubiquitous_tokens:
            for tok in ubiquitous_tokens:
                for did in token_map.get(tok, ()):
                    scores[did] = scores.get(did, 0.0) + 3.0
        elif scores and ubiquitous_tokens:
            for tok in ubiquitous_tokens:
                t_set = token_map.get(tok, ())
                if t_set:
                    for did in list(scores.keys()):
                        if did in t_set:
                            scores[did] += 3.0
        if not scores:
            return []
        q_lower = query.strip().lower()
        scored_ids: List[Tuple[float, int]] = []
        for did, sc in scores.items():
            doc = trie_docs.get(did)
            if doc is None:
                continue
            if (doc.get("page_status") or "candidate") in excluded:
                continue
            if (doc.get("privacy_level") or "safe") == "blocked":
                continue
            if agent_set and doc.get("agent") not in agent_set:
                continue
            path_lower = doc.get("path_lower") or (doc.get("path") or "").lower()
            if q_lower and q_lower in path_lower:
                sc += 4.0
            else:
                for tok in q_tokens:
                    if tok in path_lower:
                        sc += 1.5
            scored_ids.append((sc, did))
        scored_ids.sort(key=lambda item: item[0], reverse=True)
        out: List[Dict] = []
        for sc, did in scored_ids[:limit]:
            row_copy = dict(trie_docs[did])
            row_copy.pop("path_lower", None)
            row_copy["bm25_rank"] = -sc
            out.append(row_copy)
        return out
