"""Offline labelling pass and §4.7 detector validity evaluation (Rev 5 §4.7, §8.1)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

from scripts.recall_replay.private_root import assert_under

SANITY_BAR_NOTE = "With 2 positives this is a sanity bar, not a statistical validation."


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _load_recall_state_note_stats(
    recall_state: Mapping[str, Any] | Path | str | None,
) -> dict[str, dict[str, Any]]:
    if recall_state is None:
        return {}
    data: Mapping[str, Any] | None = None
    if isinstance(recall_state, (str, Path)):
        p = Path(recall_state)
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = None
    elif isinstance(recall_state, Mapping):
        data = recall_state
    if not isinstance(data, Mapping):
        return {}
    raw_stats = data.get("noteStats") or data.get("note_stats") or data
    if not isinstance(raw_stats, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw_stats.items():
        if isinstance(v, Mapping):
            out[str(k)] = dict(v)
    return out


def build_labels_from_audit(
    audit_fixture: Mapping[str, Any],
    *,
    recall_state: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Extract labelled surfacings (visible vs invisible) and the 5 need moments from the audit fixture."""
    learnings = audit_fixture.get("learnings", [])
    if not isinstance(learnings, list):
        learnings = []

    note_stats = _load_recall_state_note_stats(
        recall_state if recall_state is not None else audit_fixture.get("recall_state")
    )

    surfacings_out: list[dict[str, Any]] = []
    need_moments: list[dict[str, Any]] = []
    per_note_boot_seen: dict[str, int] = {}

    for item in learnings:
        if not isinstance(item, Mapping):
            continue
        lid = item.get("id")
        note_key = str(lid if lid is not None else item.get("mkey") or "")
        l_class = str(item.get("class") or "")
        notes_txt = str(item.get("notes") or "")
        use_quote = item.get("use_quote") if isinstance(item.get("use_quote"), Mapping) else None
        use_strength = str(item.get("use_strength") or "")

        stat_entry = (
            note_stats.get(note_key)
            or note_stats.get(f"learning:{note_key}")
            or note_stats.get(str(item.get("mkey") or ""))
            or {}
        )
        seeded_boot_seen = int(
            stat_entry.get(
                "unannotatedBootSeen",
                stat_entry.get("unannotated_boot_seen", item.get("unannotated_boot_seen", 0)),
            )
            or 0
        )

        if l_class == "never-recalled":
            nm = item.get("need_moment") if isinstance(item.get("need_moment"), Mapping) else {}
            need_moments.append(
                {
                    "id": f"need-learning-{lid}",
                    "learning_id": lid,
                    "kind": "never_recalled",
                    "addressable_in_phase1": True,
                    "session": nm.get("session", "unknown"),
                    "ts": nm.get("ts", ""),
                }
            )

        raw_surfs = item.get("surfacings", [])
        if not isinstance(raw_surfs, list):
            continue

        # Identify which surfacing (if any) immediately preceded use_quote in a helped learning
        used_idx: int | None = None
        if l_class == "helped" and raw_surfs:
            if use_quote and isinstance(use_quote.get("session"), str):
                u_sess = use_quote["session"]
                u_ts = str(use_quote.get("ts") or "")
                candidates = [
                    i
                    for i, s in enumerate(raw_surfs)
                    if isinstance(s, Mapping)
                    and s.get("session") == u_sess
                    and (not u_ts or str(s.get("ts") or "") <= u_ts)
                ]
                used_idx = candidates[-1] if candidates else 0
            else:
                used_idx = 0

        for idx, surf in enumerate(raw_surfs):
            if not isinstance(surf, Mapping):
                continue
            channel = str(surf.get("channel") or "")
            sess = str(surf.get("session") or "")
            ts = str(surf.get("ts") or "")
            sid = str(
                surf.get("sid")
                or hashlib.sha256(f"{lid}:{idx}:{channel}:{sess}:{ts}".encode("utf-8")).hexdigest()[:16]
            )

            has_explicit_vis = isinstance(surf.get("visible"), bool)
            is_boot = channel.startswith("SessionStart") or channel.startswith("boot_")
            eff_boot_seen = seeded_boot_seen

            if has_explicit_vis:
                visible = bool(surf["visible"])
                if is_boot:
                    base_seen = per_note_boot_seen.get(note_key, max(0, seeded_boot_seen - 1))
                    per_note_boot_seen[note_key] = base_seen + 1
                    eff_boot_seen = per_note_boot_seen[note_key]
            elif is_boot:
                base_seen = per_note_boot_seen.get(note_key, max(0, seeded_boot_seen - 1))
                per_note_boot_seen[note_key] = base_seen + 1
                if surf.get("unannotated_boot_seen") is not None:
                    eff_boot_seen = int(surf["unannotated_boot_seen"])
                else:
                    eff_boot_seen = per_note_boot_seen[note_key]
                visible = "recall+" not in channel and "truncated snippet" not in channel
            else:
                visible = True

            is_dismissed = bool(surf.get("dismissed")) or (
                "dismissed" in notes_txt.lower() and (not ts or ts[:5] in notes_txt or "explicitly dismissed" in notes_txt.lower())
            )

            confidence = "medium"
            if not visible:
                label_outcome = "invisible"
                cause = "invisible"
                confidence = "high"
            elif used_idx == idx:
                label_outcome = "applied" if use_strength.lower().startswith("strong") else "cited"
                cause = "used"
                confidence = "high"
            elif is_dismissed:
                label_outcome = "dismissed"
                cause = "irrelevant"
                confidence = "high"
            elif is_boot and not has_explicit_vis:
                if eff_boot_seen >= 3:
                    label_outcome = "ignored"
                    cause = "noise_crowded"
                    confidence = "medium"
                else:
                    label_outcome = "abstain"
                    cause = "unannotated_boot"
                    confidence = "low"
            else:
                label_outcome = "ignored"
                cause = "noise_crowded" if "SessionStart" in channel else "irrelevant"
                confidence = "medium"

            surfacings_out.append(
                {
                    "sid": sid,
                    "learning_id": lid,
                    "learning_class": l_class,
                    "channel": channel,
                    "session": sess,
                    "ts": ts,
                    "visible": visible,
                    "label_outcome": label_outcome,
                    "confidence": confidence,
                    "unannotated_boot_seen": eff_boot_seen,
                    "cause": cause,
                }
            )

    scars = audit_fixture.get("scars", [])
    if isinstance(scars, list):
        for scar in scars:
            if isinstance(scar, Mapping) and scar.get("recurred") is True:
                need_moments.append(
                    {
                        "id": str(scar.get("id") or f"scar-{len(need_moments) + 1}"),
                        "kind": "recurrence",
                        "addressable_in_phase1": False,
                        "lesson": str(scar.get("lesson") or ""),
                    }
                )

    vis_count = sum(1 for s in surfacings_out if s["visible"])
    invis_count = len(surfacings_out) - vis_count
    p1_needs = sum(1 for n in need_moments if n["addressable_in_phase1"])
    p1b_needs = len(need_moments) - p1_needs

    return {
        "surfacings": surfacings_out,
        "need_moments": need_moments,
        "summary": {
            "total_surfacings": len(surfacings_out),
            "visible_surfacings": vis_count,
            "invisible_surfacings": invis_count,
            "need_moments_total": len(need_moments),
            "need_moments_addressable_phase1": p1_needs,
            "need_moments_phase1b_recurrences": p1b_needs,
        },
        "sanity_bar_note": SANITY_BAR_NOTE,
    }


def write_labels(
    labels_data: Mapping[str, Any],
    *,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timestamp_tag: str | None = None,
) -> dict[str, str]:
    """Write ``labels/labels.json``, a timestamped backup, and ``labels/pinned-replay.json`` under the private root."""
    tag = timestamp_tag or datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    labels_path = assert_under("labels/labels.json", root=private_root, env=env)
    backup_path = assert_under(f"labels/labels.{tag}.json", root=private_root, env=env)
    pinned_path = assert_under("labels/pinned-replay.json", root=private_root, env=env)

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-verify containment after directory creation
    labels_path = assert_under("labels/labels.json", root=private_root, env=env)
    backup_path = assert_under(f"labels/labels.{tag}.json", root=private_root, env=env)
    pinned_path = assert_under("labels/pinned-replay.json", root=private_root, env=env)

    _atomic_write_json(labels_path, labels_data)
    _atomic_write_json(backup_path, labels_data)

    surfacings = labels_data.get("surfacings", [])
    helped = [s for s in surfacings if isinstance(s, Mapping) and s.get("cause") == "used"]
    pinned_payload = {
        "helped_positives": helped,
        "need_moments": labels_data.get("need_moments", []),
        "sanity_bar_note": SANITY_BAR_NOTE,
    }
    _atomic_write_json(pinned_path, pinned_payload)

    return {
        "labels_path": str(labels_path),
        "backup_path": str(backup_path),
        "pinned_path": str(pinned_path),
    }


def evaluate_detector_validity(
    labels_data: Mapping[str, Any],
    detector_outcomes: Mapping[str, str],
) -> dict[str, Any]:
    """Evaluate the §4.7 offline detector validity bar against labelled surfacings."""
    surfacings = labels_data.get("surfacings", [])
    if not isinstance(surfacings, list):
        surfacings = []

    helped_total = 0
    helped_detected = 0
    dismissal_total = 0
    dismissal_violations = 0
    non_used_visible_total = 0
    false_positives = 0

    for row in surfacings:
        if not isinstance(row, Mapping):
            continue
        sid = str(row.get("sid") or "")
        vis = bool(row.get("visible"))
        cause = str(row.get("cause") or "")
        lbl = str(row.get("label_outcome") or "")
        det = str(detector_outcomes.get(sid, "ignored"))

        if cause == "used" or lbl in ("cited", "applied"):
            helped_total += 1
            if det in ("cited", "applied"):
                helped_detected += 1
            continue

        if lbl == "dismissed":
            dismissal_total += 1
            if det not in ("dismissed", "ignored"):
                dismissal_violations += 1

        if vis and cause != "used":
            non_used_visible_total += 1
            if det in ("cited", "applied"):
                false_positives += 1

    helped_passed = helped_total > 0 and helped_detected == helped_total
    dismissals_passed = dismissal_violations == 0
    fp_passed = false_positives <= 2

    return {
        "passed": bool(helped_passed and dismissals_passed and fp_passed),
        "helped_total": helped_total,
        "helped_detected": helped_detected,
        "helped_passed": helped_passed,
        "dismissal_total": dismissal_total,
        "dismissal_violations": dismissal_violations,
        "dismissals_passed": dismissals_passed,
        "non_used_visible_total": non_used_visible_total,
        "false_positives": false_positives,
        "max_false_positives": 2,
        "false_positives_passed": fp_passed,
        "sanity_bar_note": SANITY_BAR_NOTE,
    }
