"""Shared durability predicate for learnings and vault notes (recall-to-use rev5 §5.2.3).

Mirrors ``plugins/minni/src/durability.ts`` and shares ``tests/fixtures/durability_cases.json``
across ``tests/test_durability_parity.py`` and ``plugins/minni/tests/durability-parity.test.mjs``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import math
import os
import re
from typing import Any

from minni.afm_passes.harness_envelopes import classify_prompt_envelope

DURABILITY_RULES_VERSION = 1

CATEGORY_SYNONYMS: dict[str, str] = {
    "decision": "decision",
    "adr": "decision",
    "fix": "fix",
    "bugfix": "fix",
    "scar": "fix",
    "procedure": "procedure",
    "architecture": "architecture",
    "preference": "preference",
}

DURABLE_CATEGORIES: tuple[str, ...] = (
    "decision",
    "fix",
    "procedure",
    "architecture",
)
_DURABLE_CATEGORY_SET = frozenset(DURABLE_CATEGORIES)

CONTENT_TEST_PREFIXES: tuple[str, ...] = (
    "Decision:",
    "Fix:",
    "Root cause:",
    "Resolution:",
    "Invariant:",
    "Never ",
    "Always ",
)

CONTENT_SECTION_PATTERN = r"^##\s+(?:Resolution|Fix)\b"

DURABILITY_RULE_IDS: tuple[str, ...] = (
    "noise.envelope_skip",
    "noise.envelope_wrapped",
    "noise.envelope_system_reminder",
    "noise.compaction_source",
    "noise.compaction_path",
    "noise.plan_journal",
    "noise.plan_complete",
    "noise.plan_candidate_resolved",
    "durable.category",
    "durable.content_prefix",
    "durable.content_section",
    "durable.preference_post_gate",
    "ordinary.default",
)

_CONTENT_SECTION_RE = re.compile(CONTENT_SECTION_PATTERN, re.MULTILINE)
_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z0-9_-]+)[ \t]*:[ \t]*(.*?)[ \t]*$")
_FRONTMATTER_STRIP_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:.*?\r?\n)?---[ \t]*(?:\r?\n)?", re.DOTALL
)
_LEADING_H1_RE = re.compile(r"\A#[ \t]+[^\r\n]*(?:\r?\n|\Z)")
_CHECKBOX_OPEN_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[[ \t]\][ \t]+")
_CHECKBOX_DONE_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[[xX]\][ \t]+")
_COMPACTION_PATH_RE = re.compile(r"(?:^|.*/)wiki/sessions/[^/]*-compact-[^/]*$")
_PLAN_JOURNAL_PATH_RE = re.compile(r"(?:^|.*/)wiki/artifacts/plan-[^/]*\.log(?:\.md)?$")
_PLAN_NOTE_PATH_RE = re.compile(r"(?:^|.*/)wiki/artifacts/plan-[^/]+\.md$")
_NUMERIC_STR_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def durability_rule_ids() -> list[str]:
    """Return every durability rule_id in canonical evaluation order."""
    return list(DURABILITY_RULE_IDS)


def normalize_category(raw_category: Any) -> str:
    """Normalize a learning or vault note category with vault synonyms."""
    if not isinstance(raw_category, str):
        return ""
    cleaned = raw_category.strip().lower()
    return CATEGORY_SYNONYMS.get(cleaned, cleaned)


def _parse_scalar_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_BLOCK_RE.match(text or "")
    if not match or not match.group(1):
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        kv = _FRONTMATTER_KV_RE.match(line)
        if not kv:
            continue
        key = kv.group(1).strip().lower()
        val = kv.group(2).strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        if key and val and key not in out:
            out[key] = val
    return out


def _strip_frontmatter_and_h1(text: str) -> str:
    body = _FRONTMATTER_STRIP_RE.sub("", text or "").lstrip()
    body = _LEADING_H1_RE.sub("", body).lstrip()
    return body


def _match_content_test_rule(text: str) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    body = _strip_frontmatter_and_h1(text)
    if any(body.startswith(prefix) for prefix in CONTENT_TEST_PREFIXES):
        return "durable.content_prefix"
    if _CONTENT_SECTION_RE.search(body):
        return "durable.content_section"
    return None


def matches_content_test(text: str) -> bool:
    """Return True when body text matches the §5.2.3 content durability test."""
    return _match_content_test_rule(text) is not None


def _normalize_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = raw.replace("\\", "/").strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    return cleaned.lstrip("/")


def _first_non_empty(*values: Any) -> str:
    for val in values:
        if val is None:
            continue
        s = val if isinstance(val, str) else str(val)
        if s:
            return s
    return ""


def _first_trimmed(*values: Any) -> str:
    for val in values:
        if val is None:
            continue
        s = (val if isinstance(val, str) else str(val)).strip()
        if s:
            return s
    return ""


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if _NUMERIC_STR_RE.match(s):
            try:
                fval = float(s)
                return fval if math.isfinite(fval) else None
            except ValueError:
                return None
        norm = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            dt = datetime.fromisoformat(norm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def classify_durability(
    item: Mapping[str, Any],
    *,
    provenance_gate_since: str | float | None = None,
    content_test_enabled: bool = True,
) -> dict[str, str]:
    """Classify a learning or vault note as ``{kind, rule_id}`` (§5.2.3)."""
    if not isinstance(item, Mapping):
        return {"kind": "ordinary", "rule_id": "ordinary.default"}

    content = _first_non_empty(item.get("content"), item.get("body"), item.get("text"))
    fm = _parse_scalar_frontmatter(content) if content.startswith("---") else {}

    rel_path = _normalize_path(
        _first_trimmed(item.get("path"), item.get("rel_path"), item.get("note"))
    )
    source = _first_trimmed(item.get("source"), fm.get("source"))
    norm_source_path = _normalize_path(source)
    status = _first_trimmed(
        item.get("status"),
        item.get("review_state"),
        item.get("page_status"),
        fm.get("status"),
    ).lower()
    note_type = _first_trimmed(
        item.get("type"),
        item.get("page_type"),
        fm.get("type"),
    ).lower()
    item_cat = item.get("category")
    raw_category = (
        item_cat
        if isinstance(item_cat, str) and item_cat.strip()
        else (fm.get("category") if item_cat is None or item_cat == "" else item_cat)
    )

    # 1. Noise checks
    if content.strip():
        env = classify_prompt_envelope(content)
        if env["kind"] == "skip":
            return {"kind": "noise", "rule_id": "noise.envelope_skip"}
        if env["kind"] == "wrapped":
            return {"kind": "noise", "rule_id": "noise.envelope_wrapped"}
        if (
            env["kind"] == "human"
            and env["subkind"] == "empty"
            and env.get("rule_id") == "block.system_reminder"
        ):
            return {"kind": "noise", "rule_id": "noise.envelope_system_reminder"}

    if source.startswith("compact_distillation") or "compact_distillation:" in content:
        return {"kind": "noise", "rule_id": "noise.compaction_source"}

    candidate_paths = [p for p in (rel_path, norm_source_path) if p]
    if any(_COMPACTION_PATH_RE.match(p) for p in candidate_paths):
        return {"kind": "noise", "rule_id": "noise.compaction_path"}

    if any(_PLAN_JOURNAL_PATH_RE.match(p) for p in candidate_paths):
        return {"kind": "noise", "rule_id": "noise.plan_journal"}

    is_plan_note = note_type == "plan" or any(
        _PLAN_NOTE_PATH_RE.match(p) and not p.endswith(".log.md") for p in candidate_paths
    )
    if is_plan_note and status != "accepted":
        if status == "complete":
            return {"kind": "noise", "rule_id": "noise.plan_complete"}
        if status == "candidate":
            if item.get("all_slices_resolved") is True:
                return {"kind": "noise", "rule_id": "noise.plan_candidate_resolved"}
            done_boxes = _CHECKBOX_DONE_RE.findall(content)
            open_boxes = _CHECKBOX_OPEN_RE.findall(content)
            if done_boxes and not open_boxes:
                return {"kind": "noise", "rule_id": "noise.plan_candidate_resolved"}

    # 2. Durable checks
    norm_cat = normalize_category(raw_category)
    if norm_cat in _DURABLE_CATEGORY_SET:
        return {"kind": "durable", "rule_id": "durable.category"}

    if content_test_enabled:
        content_rule = _match_content_test_rule(content)
        if content_rule is not None:
            return {"kind": "durable", "rule_id": content_rule}

    if norm_cat == "preference":
        origin = _first_trimmed(
            item.get("origin"),
            item.get("origin_kind"),
            fm.get("origin"),
            fm.get("origin_kind"),
        ).lower()
        gate_since = (
            provenance_gate_since
            if provenance_gate_since is not None
            else os.environ.get("MINNI_PROVENANCE_GATE_SINCE")
        )
        if origin == "human" and gate_since is not None:
            gate_ts = _parse_ts(gate_since)
            raw_created = None
            for cand in (
                item.get("created_at"),
                item.get("created"),
                fm.get("created_at"),
                fm.get("created"),
            ):
                if cand is not None and cand != "":
                    raw_created = cand
                    break
            created_ts = _parse_ts(raw_created)
            if gate_ts is not None and created_ts is not None and created_ts >= gate_ts:
                return {"kind": "durable", "rule_id": "durable.preference_post_gate"}

    return {"kind": "ordinary", "rule_id": "ordinary.default"}


def durability_of(
    item: Mapping[str, Any],
    *,
    provenance_gate_since: str | float | None = None,
    content_test_enabled: bool = True,
) -> str:
    """Classify a learning or vault note as ``noise``, ``durable``, or ``ordinary`` (§5.2.3)."""
    return classify_durability(
        item,
        provenance_gate_since=provenance_gate_since,
        content_test_enabled=content_test_enabled,
    )["kind"]
