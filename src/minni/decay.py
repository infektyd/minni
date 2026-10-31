"""
Minni V3.1 — Memory Decay.

Exponential decay with access-based reinforcement, no hard cutoffs.

recall-F4 (audit cluster C1): correction-class notes get a grace window and a
decay floor — access-recency reinforcement otherwise up-ranks stale-but-reread
beliefs over the fresh, unread corrections that supersede them.
"""

import json
import os
import re
import time
import math
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from minni.config import MinniConfig, DEFAULT_CONFIG, correction_class_page_types
from minni.db import MinniDB
from minni.durable_projection import (
    evict_document_to_cold_archive,
    evict_learning_to_cold_archive,
)
from minni.timestamps import parse_epoch_or_report

logger = logging.getLogger("sovereign.decay")

DEMOTE_MIN_SURFACES = 15
DEMOTE_MIN_SESSIONS = 5
EVICT_MIN_SURFACES = 25
EVICT_MIN_SESSIONS = 8
DEMOTE_MULTIPLIER = 0.5

_DURABLE_FILENAME_LID_RE = re.compile(r"^(\d+)-")


@dataclass
class UseLedgerStats:
    surfaces: int = 0
    uses: int = 0
    sessions: set[str] = field(default_factory=set)

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def should_evict(self) -> bool:
        return (
            self.uses == 0
            and self.surfaces >= EVICT_MIN_SURFACES
            and self.session_count >= EVICT_MIN_SESSIONS
        )

    @property
    def should_demote(self) -> bool:
        return (
            self.uses == 0
            and self.surfaces >= DEMOTE_MIN_SURFACES
            and self.session_count >= DEMOTE_MIN_SESSIONS
            and not self.should_evict
        )


def _discover_ledger_files(roots: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if not root or not root.exists():
            continue
        candidates: list[Path] = []
        if root.name == "use-ledger" and root.is_dir():
            candidates.append(root)
        else:
            direct = root / ".runtime" / "use-ledger"
            if direct.is_dir():
                candidates.append(direct)
            # Also check sibling *-vault/.runtime/use-ledger under minni_home
            try:
                for child in root.iterdir():
                    if child.is_dir() and child.name.endswith("-vault"):
                        sub = child / ".runtime" / "use-ledger"
                        if sub.is_dir():
                            candidates.append(sub)
            except OSError:
                pass
        for ledger_dir in candidates:
            try:
                for p in sorted(ledger_dir.rglob("*.jsonl")):
                    resolved = p.resolve()
                    if resolved not in seen and p.is_file():
                        seen.add(resolved)
                        files.append(p)
            except OSError:
                continue
    return files


def _expand_item_keys(raw_item: Any) -> list[str]:
    keys: list[str] = []
    if raw_item is None:
        return keys
    if isinstance(raw_item, dict):
        for field_name in ("id", "item_id", "learning_id", "doc_id", "path"):
            val = raw_item.get(field_name)
            if val is not None:
                keys.extend(_expand_item_keys(val))
        return list(dict.fromkeys(keys))

    s = str(raw_item).strip()
    if not s:
        return keys
    keys.append(s)
    if s.startswith("learn:"):
        num = s.split(":", 1)[1]
        keys.extend([f"learning:{num}", num])
    elif s.startswith("learning:"):
        num = s.split(":", 1)[1]
        keys.extend([f"learn:{num}", num])
    elif s.startswith("doc:"):
        parts = s.split(":")
        num = parts[-1]
        keys.extend([f"doc:{num}", num])
    elif s.isdigit():
        keys.extend([f"learning:{s}", f"learn:{s}", f"doc:{s}"])
    if "/" in s or s.endswith(".md"):
        keys.append(os.path.basename(s))
    return list(dict.fromkeys(keys))


def aggregate_use_ledger(*roots: str | Path | None) -> dict[str, UseLedgerStats]:
    """Aggregate `.runtime/use-ledger/**/*.jsonl` entries across sessions."""
    path_roots: list[Path] = []
    for r in roots:
        if r:
            try:
                path_roots.append(Path(r))
            except TypeError:
                pass
    ledger_files = _discover_ledger_files(path_roots)
    if not ledger_files:
        return {}

    by_key: dict[str, UseLedgerStats] = {}

    def _touch(key: str) -> UseLedgerStats:
        stats = by_key.get(key)
        if stats is None:
            stats = UseLedgerStats()
            by_key[key] = stats
        return stats

    for file_path in ledger_files:
        default_session = file_path.stem
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue

            session_id = str(
                entry.get("session_key")
                or entry.get("session_id")
                or entry.get("sessionId")
                or entry.get("sessionKey")
                or entry.get("session")
                or default_session
            )
            kind = str(
                entry.get("kind")
                or entry.get("event")
                or entry.get("type")
                or entry.get("action")
                or ""
            ).strip().lower()

            # Real use-ledger format: {"kind": "exposure", "session_key": ..., "mkey": ..., "outcome": ..., "credits": [...]}
            if kind == "exposure" and entry.get("mkey"):
                outcome = str(entry.get("outcome") or "").strip().lower()
                credits = entry.get("credits")
                was_used = outcome in (
                    "used",
                    "cited",
                    "cited_handle",
                    "applied",
                    "opened",
                ) or (isinstance(credits, list) and len(credits) > 0)
                for k in _expand_item_keys(entry.get("mkey")):
                    st = _touch(k)
                    st.surfaces += 1
                    st.sessions.add(session_id)
                    if was_used:
                        st.uses += 1
                continue

            # Format A: {"kind": "exposure", "sessionKey": ..., "surfaced": [...], "used": [...]}
            if kind == "exposure" or isinstance(entry.get("surfaced"), list) or isinstance(entry.get("used"), list):
                for item in entry.get("surfaced") or []:
                    for k in _expand_item_keys(item):
                        st = _touch(k)
                        st.surfaces += 1
                        st.sessions.add(session_id)
                for item in entry.get("used") or []:
                    for k in _expand_item_keys(item):
                        st = _touch(k)
                        st.uses += 1
                        st.sessions.add(session_id)
                continue

            # Format B1: {"kind": "surfaced", "sessionId": ..., "items": [{"id": ...}]}
            if isinstance(entry.get("items"), list):
                for item in entry.get("items") or []:
                    for k in _expand_item_keys(item):
                        st = _touch(k)
                        st.surfaces += 1
                        st.sessions.add(session_id)
                continue

            # Format B2: {"kind": "judged", "sessionId": ..., "verdicts": [{"id": ..., "used": true}]}
            if isinstance(entry.get("verdicts"), list):
                for v in entry.get("verdicts") or []:
                    if isinstance(v, dict) and v.get("used"):
                        for k in _expand_item_keys(v):
                            st = _touch(k)
                            st.uses += 1
                            st.sessions.add(session_id)
                continue

            keys: list[str] = []
            lid = entry.get("learning_id")
            if lid is not None:
                keys.extend([f"learning:{lid}", f"learn:{lid}", str(lid)])
            did = entry.get("doc_id")
            if did is not None:
                keys.extend([f"doc:{did}", str(did)])
            item_id = entry.get("item_id") or entry.get("id")
            if item_id is not None:
                keys.extend(_expand_item_keys(item_id))
            path_val = entry.get("path")
            if path_val:
                keys.extend(_expand_item_keys(path_val))

            if not keys:
                continue

            unique_keys = list(dict.fromkeys(keys))
            for k in unique_keys:
                st = _touch(k)
                if "surfaces" in entry or "uses" in entry:
                    st.surfaces += int(entry.get("surfaces") or 0)
                    st.uses += int(entry.get("uses") or 0)
                    sess_list = entry.get("sessions")
                    if isinstance(sess_list, list):
                        for s in sess_list:
                            st.sessions.add(str(s))
                    elif isinstance(sess_list, int) and sess_list > 0:
                        for i in range(sess_list):
                            st.sessions.add(f"{default_session}:{i}")
                    else:
                        st.sessions.add(session_id)
                elif kind in ("surfaced", "surface", "shown", "recalled"):
                    st.surfaces += int(entry.get("count") or 1)
                    st.sessions.add(session_id)
                elif kind in ("used", "use", "referenced", "cited", "applied"):
                    st.uses += int(entry.get("count") or 1)
                    st.sessions.add(session_id)
                else:
                    if entry.get("surfaced"):
                        st.surfaces += (
                            int(entry["surfaced"])
                            if isinstance(entry["surfaced"], int) and not isinstance(entry["surfaced"], bool)
                            else 1
                        )
                        st.sessions.add(session_id)
                    if entry.get("used"):
                        st.uses += (
                            int(entry["used"])
                            if isinstance(entry["used"], int) and not isinstance(entry["used"], bool)
                            else 1
                        )
                        st.sessions.add(session_id)

    return by_key


def lookup_learning_ledger_stats(
    ledger_map: dict[str, UseLedgerStats],
    learning_id: int | str,
    *,
    doc_id: int | str | None = None,
    path: str | None = None,
) -> UseLedgerStats | None:
    if not ledger_map:
        return None
    candidates = [f"learning:{learning_id}", f"learn:{learning_id}", str(learning_id)]
    if doc_id is not None:
        candidates.extend([f"doc:{doc_id}", str(doc_id)])
    if path:
        candidates.extend([str(path), os.path.basename(str(path))])
    for k in candidates:
        if k in ledger_map:
            return ledger_map[k]
    return None


def lookup_document_ledger_stats(
    ledger_map: dict[str, UseLedgerStats],
    doc_id: int | str,
    *,
    path: str | None = None,
    learning_id: int | str | None = None,
) -> UseLedgerStats | None:
    if not ledger_map:
        return None
    candidates = [f"doc:{doc_id}", str(doc_id)]
    if learning_id is not None:
        candidates.extend([f"learning:{learning_id}", f"learn:{learning_id}", str(learning_id)])
    if path:
        base = os.path.basename(str(path))
        candidates.extend([str(path), base])
        m = _DURABLE_FILENAME_LID_RE.match(base)
        if m:
            lid_str = m.group(1)
            candidates.extend([f"learning:{lid_str}", f"learn:{lid_str}", lid_str])
    for k in candidates:
        if k in ledger_map:
            return ledger_map[k]
    return None


class MemoryDecay:
    """Exponential memory decay with access-based reinforcement."""

    def __init__(
        self,
        db: Optional[MinniDB] = None,
        config: MinniConfig = DEFAULT_CONFIG,
        *,
        db_path: Optional[os.PathLike | str] = None,
    ):
        if db_path is not None:
            from dataclasses import replace

            config = replace(config, db_path=str(db_path))
        if db is None:
            db = MinniDB(config)
        self.db = db
        self.config = config

    def _ledger_roots(self, minni_home: Optional[os.PathLike | str] = None) -> list[Path]:
        roots: list[Path] = []
        if minni_home is not None:
            roots.append(Path(minni_home))
        elif os.environ.get("MINNI_HOME"):
            roots.append(Path(os.environ["MINNI_HOME"]))
        vault_path = getattr(self.config, "vault_path", None)
        if vault_path:
            roots.append(Path(vault_path))
        db_path = getattr(self.db, "db_path", None) or getattr(self.config, "db_path", None)
        if db_path:
            db_parent = Path(db_path).parent
            roots.append(db_parent)
            if db_parent.name == ".index" and db_parent.parent.name.endswith("-vault"):
                roots.append(db_parent.parent.parent)
        return roots

    def run_decay(self, *, minni_home: Optional[os.PathLike | str] = None) -> Dict:
        """
        Update decay_score for all documents and apply use-ledger demotion/eviction.
        Should be called daily (cron at 04:00).
        """
        now = time.time()
        half_life_sec = self.config.decay_half_life_days * 86400
        min_score = self.config.decay_min_score
        correction_types = correction_class_page_types(self.config)
        grace_sec = float(self.config.correction_decay_grace_days) * 86400
        correction_floor = float(self.config.correction_decay_floor)
        stats = {
            "updated": 0, "reinforced": 0,
            "bad_indexed_at": 0, "bad_last_accessed": 0,
            "demoted": 0, "evicted": 0,
            "evicted_learnings": 0, "penalized_learnings": 0,
            "evicted_documents": 0, "penalized_documents": 0,
        }

        ledger_map = aggregate_use_ledger(*self._ledger_roots(minni_home))
        db_path_val = getattr(self.db, "db_path", None) or getattr(self.config, "db_path", None)
        vault_path_val = getattr(self.config, "vault_path", None)

        with self.db.transaction() as c:
            doc_cols = {
                r[1] if not hasattr(r, "keys") else r["name"]
                for r in c.execute("PRAGMA table_info(documents)").fetchall()
            }
            has_doc_pinned = "pinned" in doc_cols
            has_whole_doc = "whole_document" in doc_cols

            doc_to_learning: dict[int, int] = {}
            has_ld = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_documents'"
                ).fetchone()
                is not None
            )
            if has_ld:
                for ld_row in c.execute(
                    "SELECT doc_id, learning_id FROM learning_documents"
                ).fetchall():
                    d_val = ld_row["doc_id"] if hasattr(ld_row, "keys") else ld_row[0]
                    l_val = ld_row["learning_id"] if hasattr(ld_row, "keys") else ld_row[1]
                    if d_val is not None and l_val is not None:
                        doc_to_learning[int(d_val)] = int(l_val)

            select_cols = [
                "doc_id", "path", "agent", "indexed_at", "last_accessed",
                "access_count", "decay_score", "page_type",
            ]
            if has_whole_doc:
                select_cols.append("whole_document")
            if has_doc_pinned:
                select_cols.append("pinned")

            c.execute(f"SELECT {', '.join(select_cols)} FROM documents")
            doc_rows = list(c.fetchall())

            evicted_learning_ids: set[int] = set()
            evicted_doc_ids: set[int] = set()

            for row in doc_rows:
                doc_id = row["doc_id"]
                if doc_id in evicted_doc_ids:
                    continue
                path_str = str(row["path"] or "")
                agent_str = str(row["agent"] or "")
                whole_doc = bool(row["whole_document"]) if has_whole_doc and row["whole_document"] else False
                is_pinned = bool(row["pinned"]) if has_doc_pinned and row["pinned"] else False
                is_identity = whole_doc or agent_str.startswith("identity:")

                linked_lid = doc_to_learning.get(int(doc_id))
                ledger_st = (
                    lookup_document_ledger_stats(
                        ledger_map, doc_id, path=path_str, learning_id=linked_lid
                    )
                    if not (is_identity or is_pinned)
                    else None
                )

                if ledger_st is not None and ledger_st.should_evict:
                    if linked_lid is not None:
                        ev_res = evict_learning_to_cold_archive(
                            c,
                            None,
                            linked_lid,
                            "use_ledger_unreferenced_25_8",
                            db_path=db_path_val,
                            vault_path=vault_path_val,
                        )
                        evicted_learning_ids.add(linked_lid)
                        stats["evicted_learnings"] += 1
                        for edid in ev_res.get("doc_ids", []):
                            evicted_doc_ids.add(int(edid))
                    else:
                        evict_document_to_cold_archive(
                            c,
                            None,
                            doc_id,
                            "use_ledger_unreferenced_25_8",
                            db_path=db_path_val,
                            vault_path=vault_path_val,
                        )
                        evicted_doc_ids.add(int(doc_id))
                        stats["evicted_documents"] += 1
                    stats["evicted"] += 1
                    continue

                indexed_at = parse_epoch_or_report(
                    row["indexed_at"], field="indexed_at",
                    source="decay.run_decay", doc_id=doc_id,
                )
                last_accessed = parse_epoch_or_report(
                    row["last_accessed"], field="last_accessed",
                    source="decay.run_decay", doc_id=doc_id,
                )
                if row["indexed_at"] is not None and indexed_at is None:
                    stats["bad_indexed_at"] += 1
                if indexed_at is None:
                    indexed_at = now
                if row["last_accessed"] is not None and last_accessed is None:
                    stats["bad_last_accessed"] += 1
                access_count = row["access_count"] or 0

                reference_time = last_accessed if last_accessed else indexed_at
                age_sec = now - reference_time

                raw_decay = math.pow(0.5, age_sec / half_life_sec) if half_life_sec > 0 else 1.0

                # Access reinforcement: each access adds 0.05 to floor, capped at 0.5
                access_boost = min(access_count * 0.05, 0.5)

                new_score = max(min_score, min(1.0, raw_decay + access_boost))

                if (row["page_type"] or "").lower() in correction_types:
                    if (now - indexed_at) <= grace_sec:
                        new_score = 1.0
                    else:
                        new_score = max(new_score, correction_floor)

                if ledger_st is not None and ledger_st.should_demote:
                    new_score = round(new_score * DEMOTE_MULTIPLIER, 4)
                    stats["demoted"] += 1
                    stats["penalized_documents"] += 1

                old_score = row["decay_score"] or 1.0
                if abs(new_score - old_score) > 0.001:
                    c.execute(
                        "UPDATE documents SET decay_score = ? WHERE doc_id = ?",
                        (round(new_score, 4), doc_id),
                    )
                    stats["updated"] += 1

                    if access_boost > 0:
                        stats["reinforced"] += 1

            # Also apply use-ledger demotion/eviction to active learnings
            has_learnings = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learnings'"
                ).fetchone()
                is not None
            )
            if has_learnings and ledger_map:
                l_cols = {
                    r[1] if not hasattr(r, "keys") else r["name"]
                    for r in c.execute("PRAGMA table_info(learnings)").fetchall()
                }
                has_l_pinned = "pinned" in l_cols
                has_l_status = "status" in l_cols
                has_l_superseded = "superseded_by" in l_cols
                l_select = ["learning_id", "confidence"]
                if has_l_pinned:
                    l_select.append("pinned")
                where_clauses: list[str] = []
                if has_l_status:
                    where_clauses.append("COALESCE(status, 'active') = 'active'")
                if has_l_superseded:
                    where_clauses.append("superseded_by IS NULL")
                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
                l_rows = list(
                    c.execute(f"SELECT {', '.join(l_select)} FROM learnings {where_sql}").fetchall()
                )
                for l_row in l_rows:
                    lid = int(l_row["learning_id"])
                    if lid in evicted_learning_ids:
                        continue
                    l_pinned = bool(l_row["pinned"]) if has_l_pinned and l_row["pinned"] else False
                    if l_pinned:
                        continue
                    l_st = lookup_learning_ledger_stats(ledger_map, lid)
                    if l_st is None:
                        continue
                    if l_st.should_evict:
                        evict_learning_to_cold_archive(
                            c,
                            None,
                            lid,
                            "use_ledger_unreferenced_25_8",
                            db_path=db_path_val,
                            vault_path=vault_path_val,
                        )
                        evicted_learning_ids.add(lid)
                        stats["evicted"] += 1
                        stats["evicted_learnings"] += 1
                    elif l_st.should_demote:
                        old_conf = float(
                            l_row["confidence"] if l_row["confidence"] is not None else 1.0
                        )
                        if old_conf > 0.5:
                            new_conf = round(old_conf * DEMOTE_MULTIPLIER, 4)
                            c.execute(
                                "UPDATE learnings SET confidence = ? WHERE learning_id = ?",
                                (new_conf, lid),
                            )
                            stats["demoted"] += 1
                            stats["penalized_learnings"] += 1

        if stats["bad_indexed_at"]:
            logger.warning(
                "Decay pass decayed %d document(s) using a fallback anchor "
                "(last_accessed or now): the stored indexed_at was non-numeric. "
                "Run migration 016 to normalize them.",
                stats["bad_indexed_at"],
            )
        if stats["bad_last_accessed"]:
            logger.warning(
                "Decay pass decayed %d document(s) from indexed_at instead of "
                "last_accessed: the stored last_accessed was non-numeric. "
                "Run migration 016 to normalize them.",
                stats["bad_last_accessed"],
            )
        logger.info(
            "Decay pass complete: %d updated, %d reinforced, %d decayed via "
            "fallback anchor (bad indexed_at), %d decayed via indexed_at "
            "fallback (bad last_accessed)",
            stats["updated"], stats["reinforced"],
            stats["bad_indexed_at"], stats["bad_last_accessed"],
        )
        return stats

    def get_decay_report(self) -> Dict:
        """Get summary of current decay state."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT
                    COUNT(*) as total,
                    AVG(decay_score) as avg_score,
                    MIN(decay_score) as min_score,
                    MAX(decay_score) as max_score,
                    SUM(CASE WHEN decay_score < 0.1 THEN 1 ELSE 0 END) as fading,
                    SUM(CASE WHEN decay_score > 0.8 THEN 1 ELSE 0 END) as strong
                FROM documents
            """)
            row = c.fetchone()
            return {
                "total_docs": row["total"],
                "avg_decay_score": round(row["avg_score"] or 0, 3),
                "min_score": round(row["min_score"] or 0, 3),
                "max_score": round(row["max_score"] or 0, 3),
                "fading_count": row["fading"],
                "strong_count": row["strong"],
            }


def run_decay_all_indexes(
    config: MinniConfig = DEFAULT_CONFIG,
    *,
    minni_home=None,
) -> Dict[str, Dict]:
    """Run one decay pass over the shared index AND every per-agent vault index.

    Audit #225-R2: run_decay was reachable only from the manual CLI, which
    decays the shared DB alone — but documents live across every vault index
    too, so even a hand-run pass left most of the corpus at decay_score=1.0.
    The scheduler (minnid._decay_runner) calls this, not run_decay directly.

    Per-index isolation is the point: one unreadable or locked vault index must
    not cost every other index its decay pass, so each is caught and reported
    as its own ``error`` entry rather than raised.
    """
    from minni.index_all import discover_agent_vaults
    from minni.vault_index import build_vault_index_config

    results: Dict[str, Dict] = {}

    db = MinniDB(config)
    try:
        results["shared"] = MemoryDecay(db, config).run_decay(minni_home=minni_home)
    except Exception as exc:
        logger.exception("Decay pass failed for the shared index")
        results["shared"] = {"error": str(exc)}
    finally:
        db.close()

    for vault in discover_agent_vaults(minni_home):
        name = vault.name
        vault_db = None
        try:
            # Inside the try: building the config resolves paths on disk and can
            # fail on its own, and that failure must be isolated to this vault
            # exactly like a failed pass is.
            vault_config = build_vault_index_config(vault, base_config=config)
            vault_db = MinniDB(vault_config)
            results[name] = MemoryDecay(vault_db, vault_config).run_decay(minni_home=minni_home)
        except Exception as exc:
            logger.warning("Decay pass failed for vault %s: %s", name, exc)
            results[name] = {"error": str(exc)}
        finally:
            if vault_db is not None:
                try:
                    vault_db.close()
                except Exception:
                    pass

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = MinniDB()
    decay = MemoryDecay(db)
    stats = decay.run_decay()
    print(f"Decay stats: {stats}")
    report = decay.get_decay_report()
    print(f"Report: {report}")
    db.close()
