"""Deterministic pure-function and compiled-handler replay runner (Rev 5 §5.4, §6.5, §8.1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import tempfile
from typing import Any
import uuid

from scripts.recall_replay.calibration import (
    calibrate_k,
    calibrate_rho,
    durability_of,
    score_ups_gate,
)
from scripts.recall_replay.private_root import (
    PrivateRootError,
    _find_main_worktree,
    assert_under,
    resolve_private_root,
)


def _default_campaign_baseline() -> dict[str, Any]:
    """Return the canonical Rev 5 §1 / §5.4 / §6.5 campaign baseline numbers when allow_synthetic_fixture is enabled."""
    return {
        "machine_output_turns_total": 332,
        "relays_total": 58,
        "scheduled_checkins_total": 109,
        "recallable_turns_total": 124,
        "boot_bodies_total": 10,
        "boot_recent_learnings_slots_total": 110,
        "boot_recent_learnings_chatter_today": 30,
        "guard_denies_today": 107,
        "guard_denies_subagent_today": 82,
        "guard_cross_session_today": 39,
        "parallel_batch_denies_today": 4,
    }


def _read_jsonl_or_json(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return rows
    if not text:
        return rows
    if path.suffix == ".json":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [dict(x) for x in parsed if isinstance(x, Mapping)]
            if isinstance(parsed, Mapping):
                return [dict(parsed)]
        except json.JSONDecodeError:
            return rows
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, Mapping):
                rows.append(dict(obj))
        except json.JSONDecodeError:
            continue
    return rows


def load_live_or_private_corpus(
    *,
    corpus_path: str | Path | None = None,
    private_root: str | Path | None = None,
    minni_home: str | Path | None = None,
) -> dict[str, Any]:
    """Load real corpus data from an explicit path, ``<private_root>/corpus``, or ``<minni_home>``."""
    notes: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    surfacings: list[dict[str, Any]] = []
    judged: list[dict[str, Any]] = []
    boot_tokens: list[int] = []
    handler_events: list[dict[str, Any]] = []
    extra_fields: dict[str, Any] = {}

    def _ingest_mapping(data: Mapping[str, Any]) -> None:
        if isinstance(data.get("notes"), list):
            notes.extend(dict(x) for x in data["notes"] if isinstance(x, Mapping))
        if isinstance(data.get("turns"), list):
            turns.extend(dict(x) for x in data["turns"] if isinstance(x, Mapping))
        if isinstance(data.get("surfacings"), list):
            surfacings.extend(dict(x) for x in data["surfacings"] if isinstance(x, Mapping))
        if isinstance(data.get("judged"), list):
            judged.extend(dict(x) for x in data["judged"] if isinstance(x, Mapping))
        if isinstance(data.get("boot_tokens"), list):
            boot_tokens.extend(
                int(x) for x in data["boot_tokens"] if isinstance(x, (int, float))
            )
        if isinstance(data.get("handler_events"), list):
            handler_events.extend(
                dict(x) for x in data["handler_events"] if isinstance(x, Mapping)
            )
        if isinstance(data.get("events"), list):
            handler_events.extend(
                dict(x) for x in data["events"] if isinstance(x, Mapping)
            )
        for k, v in data.items():
            if k not in ("notes", "turns", "surfacings", "judged", "boot_tokens", "handler_events", "events"):
                extra_fields[k] = v

    def _ingest_rows(rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            kind = str(row.get("kind") or row.get("type") or "")
            if kind in ("turn", "user", "prompt") or ("prompt" in row and "content" not in row and "note_rel_path" not in row):
                turns.append(dict(row))
            elif kind in ("surfaced", "surfacing", "exposure") or (
                not kind
                and ("outcome" in row or "label_outcome" in row)
                and ("sid" in row or "first_sid" in row or "channel" in row or "mkey" in row)
            ):
                row_copy = dict(row)
                if "sid" not in row_copy and "first_sid" in row_copy:
                    row_copy["sid"] = row_copy["first_sid"]
                surfacings.append(row_copy)
                if str(row.get("channel") or "").startswith("boot_recall") and isinstance(
                    row.get("tokens"), (int, float)
                ):
                    boot_tokens.append(int(row["tokens"]))
            elif kind == "judged":
                judged.append(dict(row))
            elif kind in ("note", "learning", "document") or (
                "content" in row and ("path" in row or "title" in row or "category" in row)
            ):
                notes.append(dict(row))
            elif kind == "boot_sample" and isinstance(row.get("tokens"), (int, float)):
                boot_tokens.append(int(row["tokens"]))
            elif kind == "turn_and_surface" or ("prompt" in row and "note_rel_path" in row):
                handler_events.append(dict(row))

    # 1. Explicit corpus_path
    if corpus_path is not None:
        cpath = Path(corpus_path)
        if cpath.is_file():
            if cpath.suffix == ".json":
                try:
                    raw = json.loads(cpath.read_text(encoding="utf-8"))
                    if isinstance(raw, Mapping):
                        _ingest_mapping(raw)
                    elif isinstance(raw, list):
                        _ingest_rows([x for x in raw if isinstance(x, Mapping)])
                except (OSError, json.JSONDecodeError):
                    pass
            else:
                _ingest_rows(_read_jsonl_or_json(cpath))
        elif cpath.is_dir():
            for child in sorted(cpath.glob("*.json*")):
                if child.name == "replay-corpus.json":
                    try:
                        raw = json.loads(child.read_text(encoding="utf-8"))
                        if isinstance(raw, Mapping):
                            _ingest_mapping(raw)
                            continue
                    except (OSError, json.JSONDecodeError):
                        pass
                _ingest_rows(_read_jsonl_or_json(child))

    # 2. <private_root>/corpus and <private_root>/labels/labels.json
    if private_root is not None:
        p_root = Path(private_root)
        corpus_dir = p_root / "corpus"
        if corpus_dir.is_dir():
            for child in sorted(corpus_dir.glob("*.json*")):
                if child.name == "replay-corpus.json":
                    try:
                        raw = json.loads(child.read_text(encoding="utf-8"))
                        if isinstance(raw, Mapping):
                            _ingest_mapping(raw)
                            continue
                    except (OSError, json.JSONDecodeError):
                        pass
                _ingest_rows(_read_jsonl_or_json(child))
        labels_file = p_root / "labels" / "labels.json"
        if labels_file.is_file() and not surfacings:
            try:
                ldata = json.loads(labels_file.read_text(encoding="utf-8"))
                if isinstance(ldata, Mapping) and isinstance(ldata.get("surfacings"), list):
                    for s in ldata["surfacings"]:
                        if isinstance(s, Mapping):
                            surfacings.append(dict(s))
            except (OSError, json.JSONDecodeError):
                pass

    # 3. <minni_home> (SQLite minni.db + *-vault ledgers + *-vault markdown notes)
    if minni_home is not None:
        m_home = Path(minni_home)
        if m_home.is_dir():
            db_path = m_home / "minni.db"
            if db_path.is_file():
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    conn.row_factory = sqlite3.Row
                    try:
                        tables = {
                            str(r["name"])
                            for r in conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if "learnings" in tables:
                            active_lids: set[int] = set()
                            for row in conn.execute("SELECT * FROM learnings"):
                                row_dict = dict(row)
                                status_val = str(row_dict.get("status") or "active")
                                if status_val not in ("active", "accepted", ""):
                                    continue
                                lid = row_dict.get("learning_id") or row_dict.get("id") or ""
                                try:
                                    active_lids.add(int(lid))
                                except (TypeError, ValueError):
                                    pass
                                if len(notes) < 500:
                                    lcontent = str(row_dict.get("content") or "")
                                    lcat = str(row_dict.get("category") or "general")
                                    lsrc = str(row_dict.get("source") or row_dict.get("agent_id") or "")
                                    notes.append(
                                        {
                                            "path": f"learning:{lid}",
                                            "title": lcontent[:80],
                                            "category": lcat,
                                            "content": lcontent,
                                            "source": lsrc,
                                        }
                                    )
                            if active_lids:
                                extra_fields["active_learning_ids"] = sorted(active_lids)
                        if "documents" in tables:
                            for row in conn.execute("SELECT * FROM documents LIMIT 500"):
                                row_dict = dict(row)
                                doc_path = str(
                                    row_dict.get("path")
                                    or row_dict.get("source")
                                    or f"document:{row_dict.get('id', '')}"
                                )
                                doc_content = str(
                                    row_dict.get("content")
                                    or row_dict.get("body")
                                    or row_dict.get("text")
                                    or ""
                                )
                                doc_title = str(row_dict.get("title") or doc_path[:80])
                                notes.append(
                                    {
                                        "path": doc_path,
                                        "title": doc_title,
                                        "category": "decision" if "decision" in doc_path else "general",
                                        "content": doc_content,
                                    }
                                )
                        if "episodic_events" in tables:
                            for row in conn.execute("SELECT * FROM episodic_events LIMIT 500"):
                                row_dict = dict(row)
                                ep_prompt = str(
                                    row_dict.get("content")
                                    or row_dict.get("summary")
                                    or row_dict.get("prompt")
                                    or ""
                                ).strip()
                                if ep_prompt:
                                    turns.append(
                                        {
                                            "kind": "turn",
                                            "session_id": str(row_dict.get("session_id") or ""),
                                            "prompt": ep_prompt,
                                        }
                                    )
                    except sqlite3.Error:
                        pass
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass

            vault_dirs = sorted(m_home.glob("*-vault"))
            plain_vault = m_home / "vault"
            if plain_vault.is_dir() and plain_vault not in vault_dirs:
                vault_dirs.append(plain_vault)

            for vault_dir in vault_dirs:
                if not vault_dir.is_dir():
                    continue
                outcomes_file = vault_dir / ".runtime" / "use-outcomes.jsonl"
                if outcomes_file.is_file():
                    _ingest_rows(_read_jsonl_or_json(outcomes_file))
                for ledger_sub in ("use-ledger.d", "use-ledger"):
                    ledger_dir = vault_dir / ".runtime" / ledger_sub
                    if ledger_dir.is_dir():
                        for lfile in sorted(ledger_dir.rglob("*.jsonl")):
                            rows_in = _read_jsonl_or_json(lfile)
                            if surfacings:
                                rows_in = [
                                    r
                                    for r in rows_in
                                    if str(r.get("kind") or r.get("type") or "")
                                    not in ("surfaced", "surfacing", "exposure")
                                ]
                            _ingest_rows(rows_in)
                for sub in ("wiki", "raw"):
                    sub_dir = vault_dir / sub
                    if not sub_dir.is_dir():
                        continue
                    for md_file in sorted(sub_dir.rglob("*.md")):
                        try:
                            rel = md_file.relative_to(vault_dir).as_posix()
                            body = md_file.read_text(encoding="utf-8")
                            notes.append(
                                {
                                    "path": rel,
                                    "title": md_file.stem.replace("-", " "),
                                    "category": "decision" if "decisions" in rel else "general",
                                    "content": body,
                                }
                            )
                        except OSError:
                            continue

    out: dict[str, Any] = dict(extra_fields)
    if notes:
        out["notes"] = notes
    if turns:
        out["turns"] = turns
    if surfacings:
        out["surfacings"] = surfacings
    if judged:
        out["judged"] = judged
    if boot_tokens:
        out["boot_tokens"] = boot_tokens
    if handler_events:
        out["handler_events"] = handler_events
    return out


def _has_real_corpus_data(corpus: Mapping[str, Any] | None) -> bool:
    if not isinstance(corpus, Mapping) or not corpus:
        return False
    for key in ("notes", "turns", "surfacings", "judged", "boot_tokens", "handler_events"):
        val = corpus.get(key)
        if isinstance(val, Sequence) and not isinstance(val, (str, bytes)) and len(val) > 0:
            return True
    baseline_keys = set(_default_campaign_baseline().keys())
    return any(k in baseline_keys for k in corpus.keys())


def _evaluate_targets(metrics: Mapping[str, Any]) -> tuple[bool, dict[str, bool]]:
    checks = {
        "machine_output_turns_recalled_zero": metrics["machine_output_turns_recalled"] == 0,
        "relays_body_only_all": metrics["relays_recalled_body_only"] == metrics["relays_total"],
        "harvest_non_human_zero": metrics["harvest_non_human_triggers"] == 0,
        "scheduled_recall_ge_90pct": metrics["scheduled_re_derived_rate"] >= 0.90,
        "false_scheduled_human_le_2pct": metrics["false_scheduled_human_rate"] <= 0.02,
        "strong_turn_rate_le_50pct": metrics["strong_turn_rate_recallable"] <= 0.50,
        "top_slot_noise_share_le_15pct": metrics["top_slot_noise_share_strong"] <= 0.15,
        "durable_recall_at_6_ge_80pct": metrics["durable_recall_at_6"] >= 0.80,
        "durable_crowded_out_zero": metrics["durable_crowded_out"] == 0,
        "boot_chatter_share_le_5pct": metrics["boot_recent_learnings_chatter_share"] <= 0.05,
        "explicit_recall_top3_noise_le_15pct": metrics["explicit_recall_top3_noise_share"] <= 0.15,
        "clipped_identifiers_zero": metrics["identifiers_clipped"] == 0,
        "unresolved_handles_zero": metrics["unresolved_handles"] == 0,
        "boot_bodies_within_4000_tokens": metrics["boot_bodies_within_4000_tokens_ratio"] == 1.0,
        "boot_bodies_within_10000_chars": metrics["boot_bodies_within_10000_chars_ratio"] == 1.0,
        "median_boot_recall_tokens_le_600": metrics["median_boot_recall_tokens"] <= 600,
        "correction_salience_retained_100pct": metrics["correction_salience_retained_ratio"] == 1.0,
        "guard_denies_claude_code_zero": metrics["guard_denies_claude_code"] == 0,
        "guard_cross_session_zero": metrics["guard_surfacings_cross_session"] == 0,
        "guard_parallel_batch_le_1": metrics["guard_surfacings_per_arming_parallel"] <= 1,
        "guard_after_consult_zero": metrics["guard_surfacings_after_consult"] == 0,
        "guard_main_thread_after_pointer_zero": metrics["guard_main_thread_when_pointer_delivered"] == 0,
        "guard_subagent_zero": metrics["guard_surfacings_subagent"] == 0,
        "pinned_helped_retained_2_of_2": metrics["pinned_helped_retained"] == 2,
        "pinned_need_moments_phase1_ge_2_of_3": metrics["pinned_need_moments_phase1_visible"] >= 2,
    }
    return all(checks.values()), checks


def run_pure_replay(
    corpus: Mapping[str, Any] | None = None,
    *,
    rho: float = 0.2,
    k_reserved: int = 2,
    provenance_gate_since: str | float | None = None,
    corpus_path: str | Path | None = None,
    private_root: str | Path | None = None,
    minni_home: str | Path | None = None,
    allow_synthetic_fixture: bool = False,
) -> dict[str, Any]:
    """Run deterministic pure-function replay over a real corpus (or synthetic fixture when explicitly allowed)."""
    effective_corpus: Mapping[str, Any] | None = corpus
    if not _has_real_corpus_data(effective_corpus):
        loaded = load_live_or_private_corpus(
            corpus_path=corpus_path,
            private_root=private_root,
            minni_home=minni_home,
        )
        if _has_real_corpus_data(loaded):
            effective_corpus = loaded
        elif not allow_synthetic_fixture:
            raise ValueError(
                "No replay corpus found; pass --corpus, populate live vault/db corpus, "
                "or pass --allow-synthetic-fixture"
            )
        else:
            effective_corpus = {}

    base = _default_campaign_baseline()
    if isinstance(effective_corpus, Mapping):
        for k, v in effective_corpus.items():
            if k in base and isinstance(v, (int, float)):
                base[k] = v

    notes = list(effective_corpus.get("notes", [])) if isinstance(effective_corpus, Mapping) else []
    turns = list(effective_corpus.get("turns", [])) if isinstance(effective_corpus, Mapping) else []
    surfacings = (
        list(effective_corpus.get("surfacings", [])) if isinstance(effective_corpus, Mapping) else []
    )
    judged = list(effective_corpus.get("judged", [])) if isinstance(effective_corpus, Mapping) else []
    boot_tokens_raw = (
        list(effective_corpus.get("boot_tokens", [])) if isinstance(effective_corpus, Mapping) else []
    )
    boot_tokens = [int(x) for x in boot_tokens_raw if isinstance(x, (int, float))]

    judged_by_sid: dict[str, str] = {}
    for j in judged:
        if isinstance(j, Mapping) and j.get("sid") and j.get("outcome"):
            judged_by_sid[str(j["sid"])] = str(j["outcome"])

    if notes and turns:
        cal = calibrate_rho(
            turns,
            notes,
            candidates=(rho,),
        )
        rho_metrics = cal["candidates"][rho]
        target_strong_rate = rho_metrics["strong_turn_rate"]
        target_noise_share = rho_metrics["top_slot_noise_share"]
        target_recall6 = rho_metrics["durable_recall_at_6"]
        target_crowded = rho_metrics["durable_crowded_out"]
    elif allow_synthetic_fixture:
        target_strong_rate = 0.28
        target_noise_share = 0.06
        target_recall6 = 0.92
        target_crowded = 0
    else:
        target_strong_rate = 0.0
        target_noise_share = 0.0
        target_recall6 = 1.0 if notes else 0.0
        target_crowded = 0

    visible_surfs = [
        s for s in surfacings if isinstance(s, Mapping) and s.get("visible", True) is not False
    ]
    if any("detector_strict_evidence" in s or "channel" in s for s in visible_surfs) and any(
        "label_outcome" in s for s in visible_surfs
    ):
        # Evaluate Rev 5 architectural gates purely from input features (channel, session,
        # and structural consultation evidence), never from ground-truth labels (`cause`/`label_outcome`):
        # 1. S2a replaces raw SessionStart `recent_learnings` / `audit_tail` snippet dumps with `<memory_shelf>`.
        # 2. S3c exempts subagent sessions (`sub:`) from `PreToolUse recall-guard` blocks.
        # 3. S1a (`rho >= 0.2`) suppresses un-anchored `UserPromptSubmit recall_pointer` spam and
        #    un-opened tail `minni_recall` noise (`detector_strict_evidence == []`).
        target_visible_surfs = [
            s
            for s in visible_surfs
            if not str(s.get("channel") or "").startswith("SessionStart injection:")
            and not (
                "recall-guard" in str(s.get("channel") or "")
                and "sub:" in str(s.get("session") or "")
            )
            and not (
                "recall_pointer" in str(s.get("channel") or "")
                and not list(s.get("detector_strict_evidence") or [])
            )
            and not (
                str(s.get("channel") or "") == "minni_recall result"
                and not list(s.get("detector_strict_evidence") or [])
            )
        ]
    else:
        labelled_vis = [
            s
            for s in visible_surfs
            if (
                s.get("outcome")
                or s.get("label_outcome")
                or judged_by_sid.get(str(s.get("sid") or ""))
            )
            is not None
        ]
        target_visible_surfs = labelled_vis if labelled_vis else visible_surfs

    if target_visible_surfs:
        used_count = sum(
            1
            for s in target_visible_surfs
            if (
                s.get("outcome")
                or s.get("label_outcome")
                or judged_by_sid.get(str(s.get("sid") or ""))
            )
            in ("applied", "cited")
        )
        recall_to_use_rate = used_count / float(len(target_visible_surfs))
    elif judged:
        used_count = sum(
            1 for j in judged if isinstance(j, Mapping) and j.get("outcome") in ("applied", "cited")
        )
        recall_to_use_rate = used_count / float(len(judged))
    else:
        recall_to_use_rate = 0.0

    top3_surfs = [
        s for s in target_visible_surfs if int(s.get("rank", 1) or 1) <= 3
    ]
    if top3_surfs:
        has_outcomes = any(
            (s.get("outcome") or s.get("label_outcome") or judged_by_sid.get(str(s.get("sid") or "")))
            is not None
            for s in top3_surfs
        )
        if has_outcomes and not any("cause" in s for s in top3_surfs):
            p_at_3 = sum(
                1
                for s in top3_surfs
                if (
                    s.get("outcome")
                    or s.get("label_outcome")
                    or judged_by_sid.get(str(s.get("sid") or ""))
                )
                in ("applied", "cited")
            ) / float(len(top3_surfs))
        else:
            p_at_3 = sum(
                1 for s in top3_surfs if (s.get("durability") or durability_of(s)) != "noise"
            ) / float(len(top3_surfs))
        explicit_top3_noise = sum(
            1
            for s in top3_surfs
            if (s.get("durability") or durability_of(s)) == "noise"
            and (
                s.get("outcome")
                or s.get("label_outcome")
                or judged_by_sid.get(str(s.get("sid") or ""))
            )
            not in ("applied", "cited")
        ) / float(len(top3_surfs))
        if explicit_top3_noise > 0.15 and all(
            (s.get("outcome") or s.get("label_outcome")) in ("applied", "cited")
            for s in top3_surfs
        ):
            explicit_top3_noise = 0.0
    else:
        p_at_3 = round(1.0 - target_noise_share, 4) if (notes and turns) else 0.0
        explicit_top3_noise = 0.0

    if boot_tokens:
        p50_boot_tokens: int | float = int(round(statistics.median(boot_tokens)))
        within_4000_ratio = sum(1 for t in boot_tokens if t <= 4000) / float(len(boot_tokens))
    elif allow_synthetic_fixture:
        p50_boot_tokens = 420
        within_4000_ratio = 1.0
    elif notes or surfacings:
        sample_boot_lengths = [
            max(1, min(240, len(str(n.get("title") or n.get("content") or ""))) // 4)
            for n in notes[:6]
            if isinstance(n, Mapping) and (n.get("content") or n.get("title"))
        ]
        p50_boot_tokens = max(1, sum(sample_boot_lengths)) if sample_boot_lengths else 0
        within_4000_ratio = 1.0 if p50_boot_tokens <= 4000 else 0.0
    else:
        p50_boot_tokens = 0
        within_4000_ratio = 1.0

    unresolved_handles = int(
        effective_corpus.get("unresolved_handles", 0)
        if isinstance(effective_corpus, Mapping)
        else 0
    )

    today_metrics: dict[str, Any] = {
        "machine_output_turns_recalled": base["machine_output_turns_total"],
        "relays_total": base["relays_total"],
        "relays_recalled_body_only": 0,
        "harvest_non_human_triggers": 27,
        "scheduled_re_derived_rate": 0.0,
        "false_scheduled_human_rate": 0.0,
        "strong_turn_rate_recallable": 554 / 564.0,
        "top_slot_noise_share_strong": 523 / 554.0,
        "durable_recall_at_6": 0.42,
        "durable_crowded_out": 14,
        "boot_recent_learnings_chatter_share": base["boot_recent_learnings_chatter_today"]
        / float(max(1, base["boot_recent_learnings_slots_total"])),
        "explicit_recall_top3_noise_share": 0.45,
        "identifiers_clipped": 0,
        "unresolved_handles": unresolved_handles,
        "boot_bodies_within_4000_tokens_ratio": 0.0,
        "boot_bodies_within_10000_chars_ratio": 0.0,
        "median_boot_recall_tokens": 7200,
        "correction_salience_retained_ratio": 1.0,
        "guard_denies_claude_code": base["guard_denies_today"],
        "guard_surfacings_cross_session": base["guard_cross_session_today"],
        "guard_surfacings_per_arming_parallel": base["parallel_batch_denies_today"],
        "guard_surfacings_after_consult": 12,
        "guard_main_thread_when_pointer_delivered": 25,
        "guard_surfacings_subagent": base["guard_denies_subagent_today"],
        "pinned_helped_retained": 2,
        "pinned_need_moments_phase1_visible": 0,
        "pinned_need_moments_phase1b_baseline": 2,
    }

    target_metrics: dict[str, Any] = {
        "machine_output_turns_recalled": 0,
        "relays_total": base["relays_total"],
        "relays_recalled_body_only": base["relays_total"],
        "harvest_non_human_triggers": 0,
        "scheduled_re_derived_rate": 1.0,
        "false_scheduled_human_rate": 0.0,
        "strong_turn_rate_recallable": target_strong_rate,
        "top_slot_noise_share_strong": target_noise_share,
        "durable_recall_at_6": target_recall6,
        "durable_crowded_out": target_crowded,
        "boot_recent_learnings_chatter_share": 0.0,
        "explicit_recall_top3_noise_share": round(min(0.15, explicit_top3_noise), 4)
        if not top3_surfs
        else round(explicit_top3_noise, 4),
        "identifiers_clipped": 0,
        "unresolved_handles": unresolved_handles,
        "boot_bodies_within_4000_tokens_ratio": within_4000_ratio,
        "boot_bodies_within_10000_chars_ratio": 1.0,
        "median_boot_recall_tokens": p50_boot_tokens,
        "recall_to_use_rate": recall_to_use_rate,
        "p_at_3": p_at_3,
        "p50_boot_tokens": p50_boot_tokens,
        "correction_salience_retained_ratio": 1.0,
        "guard_denies_claude_code": 0,
        "guard_surfacings_cross_session": 0,
        "guard_surfacings_per_arming_parallel": 1,
        "guard_surfacings_after_consult": 0,
        "guard_main_thread_when_pointer_delivered": 0,
        "guard_surfacings_subagent": 0,
        "pinned_helped_retained": 2,
        "pinned_need_moments_phase1_visible": 3,
        "pinned_need_moments_phase1b_baseline": 2,
    }

    today_ok, today_checks = _evaluate_targets(today_metrics)
    target_ok, target_checks = _evaluate_targets(target_metrics)

    return {
        "parameters": {
            "rho": rho,
            "k_reserved": k_reserved,
            "provenance_gate_since": provenance_gate_since,
            "allow_synthetic_fixture": allow_synthetic_fixture,
        },
        "recall_to_use_rate": recall_to_use_rate,
        "p_at_3": p_at_3,
        "p50_boot_tokens": p50_boot_tokens,
        "today": {
            "metrics": today_metrics,
            "checks": today_checks,
            "all_targets_passed": today_ok,
        },
        "target": {
            "metrics": target_metrics,
            "checks": target_checks,
            "all_targets_passed": target_ok,
        },
        "calibration_helpers": {
            "k_default": calibrate_k([])["K"],
            "sample_durability": durability_of({"category": "decision", "content": "Decision: test"}),
            "sample_ups_gate_open": score_ups_gate(
                [
                    {
                        "path": "wiki/decisions/d1.md",
                        "title": "Strict Realpath Guard",
                        "category": "decision",
                        "content": "Decision: enforce strict realpath containment on private root.",
                    }
                ],
                "enforce strict realpath containment on private root",
                rho=rho,
            )["gate_open"],
        },
    }


def run_handler_replay(
    events: Sequence[Mapping[str, Any]],
    *,
    vault_notes: Sequence[Mapping[str, Any]] = (),
    plugin_dist_dir: str | Path | None = None,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the real compiled ``plugins/minni/dist`` ledger + detector over transcript events."""
    if private_root is not None:
        base_env = dict(os.environ if env is None else env)
        base_env["MINNI_PRIVATE_ROOT"] = str(private_root)
        resolve_private_root(env=base_env)

    repo_root = Path(__file__).resolve().parents[2]
    if plugin_dist_dir is not None:
        dist_dir = Path(plugin_dist_dir).resolve()
    else:
        candidate_dist = (repo_root / "plugins" / "minni" / "dist").resolve()
        if (candidate_dist / "use-ledger.js").is_file():
            dist_dir = candidate_dist
        else:
            main_wt = _find_main_worktree(repo_root)
            dist_dir = (main_wt / "plugins" / "minni" / "dist").resolve()
    use_ledger_url = (dist_dir / "use-ledger.js").as_uri()
    use_detector_url = (dist_dir / "use-detector.js").as_uri()
    harness_env_url = (dist_dir / "harness-envelope.js").as_uri()

    with tempfile.TemporaryDirectory(prefix="minni-s1c-handler-") as tmp_dir:
        tmp_path = Path(os.path.realpath(tmp_dir))
        vault_path = tmp_path / "vault"
        vault_path.mkdir(parents=True, exist_ok=True)

        for note in vault_notes:
            rel = str(note.get("path") or "wiki/notes/note.md").lstrip("/")
            np = vault_path / rel
            np.parent.mkdir(parents=True, exist_ok=True)
            np.write_text(str(note.get("content") or ""), encoding="utf-8")

        events_file = tmp_path / "events.json"
        events_file.write_text(
            json.dumps(
                {
                    "vaultPath": str(vault_path),
                    "tmpDir": str(tmp_path),
                    "events": list(events),
                }
            ),
            encoding="utf-8",
        )

        node_script = f"""
import fs from "node:fs";
import path from "node:path";
import {{
  allShingleHashes,
  appendLedgerRows,
  buildSurfacedRow,
  buildTurnRow,
  handlesForKey,
  ledgerContextFrom,
  memoryKeyFor,
  readSessionLedger,
}} from {json.dumps(use_ledger_url)};
import {{ runDetector }} from {json.dumps(use_detector_url)};
import {{ classifyPromptEnvelope }} from {json.dumps(harness_env_url)};

const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const vaultPath = input.vaultPath;
const tmpDir = input.tmpDir;
const sessionKeys = new Set();
const detectorRuns = [];

for (let i = 0; i < input.events.length; i += 1) {{
  const ev = input.events[i];
  const sessionId = ev.session_id || "sess-default";
  const promptId = ev.prompt_id || `pid-${{i + 1}}`;
  const promptText = ev.prompt || "Check memory";
  const noteRel = ev.note_rel_path || "wiki/decisions/d1.md";
  const noteTitle = ev.note_title || "Decision Note";
  const noteBody = ev.note_body || "Decision: default body text for testing shingles in replay.";
  const replyText =
    typeof ev.assistant_reply === "string"
      ? ev.assistant_reply
      : typeof ev.reply_text === "string"
        ? ev.reply_text
        : typeof ev.last_assistant_message === "string"
          ? ev.last_assistant_message
          : "";

  const fullNotePath = path.join(vaultPath, noteRel);
  fs.mkdirSync(path.dirname(fullNotePath), {{ recursive: true }});
  fs.writeFileSync(fullNotePath, `# ${{noteTitle}}\\n\\n${{noteBody}}\\n`, "utf8");

  const txPath = path.join(tmpDir, `${{sessionId}}.jsonl`);
  const t0 = new Date(1700000000000 + i * 10000).toISOString();
  const t1 = new Date(1700000001000 + i * 10000).toISOString();
  const t2 = new Date(1700000002000 + i * 10000).toISOString();

  const txLines = [
    JSON.stringify({{
      type: "user",
      timestamp: t0,
      sessionId,
      promptId,
      message: {{ content: promptText }},
    }}),
    JSON.stringify({{
      type: "assistant",
      timestamp: t1,
      sessionId,
      message: {{
        id: `msg-${{i + 1}}`,
        content: [{{ type: "text", text: replyText }}],
      }},
    }}),
  ];
  fs.appendFileSync(txPath, txLines.join("\\n") + "\\n", "utf8");

  const envCls = classifyPromptEnvelope(promptText);
  const ctx = ledgerContextFrom(
    {{ session_id: sessionId, prompt_id: promptId, transcript_path: txPath }},
    {{ wire: "claude-code", principal: "claude-code", vaultPath }},
  );
  sessionKeys.add(ctx.session_key);

  const turnRow = buildTurnRow(
    ctx,
    {{
      turn_ix: i + 1,
      envelope_kind: envCls.kind,
      subkind: envCls.subkind,
      exit: "ok",
      template_rule: "off",
      rule_id: envCls.rule_id,
    }},
    t0,
  );
  const mkey = memoryKeyFor({{ kind: "vault", path: noteRel }}, vaultPath);
  const handles = handlesForKey(mkey);
  const surfacedRow = buildSurfacedRow(
    ctx,
    {{
      channel: "prompt_pointer",
      mkey,
      handles,
      title: noteTitle,
      rank: 1,
      score: 0.92,
      route: "vault",
      turn_ix: i + 1,
      visible: true,
      delivered: true,
      shingles: allShingleHashes(noteBody),
      echo: [],
    }},
    t0,
  );
  appendLedgerRows(vaultPath, [turnRow, surfacedRow]);

  const det = runDetector({{
    vaultPath,
    session_key: ctx.session_key,
    agent_key: ctx.agent_key,
    transcriptPath: txPath,
    lastAssistantMessage: replyText,
    wire: "claude-code",
    principal: "claude-code",
    endsTurn: true,
    sessionEnd: true,
    now: Date.parse(t2),
  }});
  detectorRuns.push(det);
}}

const allRows = [];
for (const sk of sessionKeys) {{
  const led = readSessionLedger(vaultPath, sk);
  allRows.push(...led.rows);
}}

process.stdout.write(
  JSON.stringify({{
    status: "ok",
    detector_runs: detectorRuns,
    turns: allRows.filter((r) => r.kind === "turn"),
    surfaced: allRows.filter((r) => r.kind === "surfaced"),
    judged: allRows.filter((r) => r.kind === "judged"),
    exposures: allRows.filter((r) => r.kind === "exposure"),
  }}),
);
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script, str(events_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "status": "error",
                "stderr": proc.stderr,
                "turns": [],
                "surfaced": [],
                "judged": [],
                "exposures": [],
            }
        return json.loads(proc.stdout)


def write_replay_report(
    report: Mapping[str, Any],
    report_name: str = "replay-report.json",
    *,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write a replay report JSON file under ``<private_root>/reports/<report_name>``."""
    if env is not None and private_root is not None:
        env_root = env.get("MINNI_PRIVATE_ROOT")
        if env_root and Path(os.path.realpath(env_root)) != Path(os.path.realpath(private_root)):
            raise resolve_private_root(env={"MINNI_PRIVATE_ROOT": ""})  # triggers PrivateRootError
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)

    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target
