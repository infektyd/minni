#!/usr/bin/env python3
"""Mirror of UsageDockCore rules. Runs on Linux so the contract is not Mac-only faith."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "Fixtures"


def normalize(raw: float) -> float:
    if 0 < raw < 1:
        value = raw * 100
    else:
        value = raw
    return min(100, max(0, value))


def percent_text(raw: float) -> str:
    return f"{int(round(normalize(raw)))}%"


def used_text(raw: float) -> str:
    return f"{int(round(normalize(raw)))}% Used"


def caption(resets_at: datetime, now: datetime) -> str:
    seconds = (resets_at - now).total_seconds()
    if seconds <= 0:
        return "Resetting"
    if seconds < 60 * 60:
        minutes = max(1, int(round(seconds / 60)))
        return f"Resets in {minutes} min"
    if seconds < 60 * 60 * 12:
        hours = int(round(seconds / 3600))
        return "Resets in 1 hr" if hours == 1 else f"Resets in {hours} hr"
    weekday = resets_at.strftime("%a")
    hour24 = resets_at.hour
    minute = resets_at.minute
    suffix = "PM" if hour24 >= 12 else "AM"
    hour12 = hour24 % 12 or 12
    return f"Resets {weekday} {hour12}:{minute:02d} {suffix}"


def window_from_raw(raw: dict | None, label: str) -> dict | None:
    if not raw or raw.get("utilization") is None:
        return None
    return {
        "label": label,
        "percent": normalize(float(raw["utilization"])),
        "resets_at": raw.get("resets_at"),
    }


def windows_from_payload(payload: dict) -> list[dict]:
    result: list[dict] = []
    limits = payload.get("limits") or []

    def first_limit(kind: str) -> dict | None:
        return next((entry for entry in limits if entry.get("kind") == kind), None)

    session = first_limit("session")
    if session and session.get("percent") is not None:
        result.append(
            {
                "label": "Current session",
                "percent": normalize(float(session["percent"])),
                "resets_at": session.get("resets_at"),
            }
        )
    else:
        mapped = window_from_raw(payload.get("five_hour"), "Current session")
        if mapped:
            result.append(mapped)

    weekly = first_limit("weekly_all")
    if weekly and weekly.get("percent") is not None:
        result.append(
            {
                "label": "All models",
                "percent": normalize(float(weekly["percent"])),
                "resets_at": weekly.get("resets_at"),
            }
        )
    else:
        mapped = window_from_raw(payload.get("seven_day"), "All models")
        if mapped:
            result.append(mapped)

    scoped = []
    for entry in limits:
        if entry.get("kind") != "weekly_scoped":
            continue
        name = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        if name is None or entry.get("percent") is None:
            continue
        scoped.append(
            {
                "label": name,
                "percent": normalize(float(entry["percent"])),
                "resets_at": entry.get("resets_at"),
            }
        )
    if scoped:
        result.extend(scoped)
    else:
        opus = window_from_raw(payload.get("seven_day_opus"), "Opus")
        sonnet = window_from_raw(payload.get("seven_day_sonnet"), "Sonnet")
        if opus:
            result.append(opus)
        if sonnet:
            result.append(sonnet)
    return result


def apply_refresh(
    root: dict,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    now: float,
) -> dict:
    oauth = dict(root["claudeAiOauth"])
    previous = oauth.get("expiresAt", 0)
    milliseconds = previous >= 1e11
    oauth["accessToken"] = access_token
    if refresh_token:
        oauth["refreshToken"] = refresh_token
    expiry = now + expires_in
    oauth["expiresAt"] = expiry * 1000 if milliseconds else expiry
    next_root = dict(root)
    next_root["claudeAiOauth"] = oauth
    return next_root


def epoch_date(raw: float) -> tuple[float, bool]:
    if raw >= 1e11:
        return raw / 1000, True
    return raw, False


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: got {actual!r}, expected {expected!r}")


def main() -> int:
    assert_eq(normalize(0.73), 73, "fraction")
    assert_eq(normalize(73), 73, "percent")
    assert_eq(normalize(0), 0, "zero")
    assert_eq(normalize(1), 1, "one stays 1%")
    assert_eq(normalize(-4), 0, "clamp low")
    assert_eq(normalize(140), 100, "clamp high")
    assert_eq(percent_text(0.73), "73%", "percent text")
    assert_eq(used_text(7), "7% Used", "used text")

    now = datetime(2026, 8, 29, 20, 22, tzinfo=timezone.utc)
    assert_eq(
        caption(now + timedelta(minutes=51), now),
        "Resets in 51 min",
        "51 min",
    )
    assert_eq(
        caption(now + timedelta(hours=2), now),
        "Resets in 2 hr",
        "2 hr",
    )
    thursday = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    assert_eq(caption(thursday, now), "Resets Thu 12:00 AM", "weekday")

    legacy = json.loads((FIXTURES / "claude-usage-legacy.json").read_text())
    legacy_windows = windows_from_payload(legacy)
    assert_eq(
        [w["label"] for w in legacy_windows],
        ["Current session", "All models", "Opus", "Sonnet"],
        "legacy labels",
    )
    assert_eq(legacy_windows[0]["percent"], 73, "legacy session")
    assert_eq(legacy_windows[1]["percent"], 7, "legacy weekly")

    modern = json.loads((FIXTURES / "claude-usage-limits.json").read_text())
    modern_windows = windows_from_payload(modern)
    assert_eq(
        [w["label"] for w in modern_windows],
        ["Current session", "All models", "Fable"],
        "limits labels",
    )
    assert_eq(modern_windows[0]["percent"], 73, "limits session (fraction five_hour ignored)")
    assert_eq(modern_windows[2]["percent"], 27, "fable")

    seconds, millis = epoch_date(1_893_456_000_000)
    assert_eq(millis, True, "ms flag")
    assert_eq(seconds, 1_893_456_000, "ms value")
    seconds, millis = epoch_date(1_893_456_000)
    assert_eq(millis, False, "s flag")
    assert_eq(seconds, 1_893_456_000, "s value")

    creds = json.loads((FIXTURES / "claude-credentials.sample.json").read_text())
    assert "claudeAiOauth" in creds
    assert creds["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-")

    merged = apply_refresh(
        creds,
        access_token="new",
        refresh_token="rotated",
        expires_in=3600,
        now=1_800_000_000,
    )
    oauth = merged["claudeAiOauth"]
    assert_eq(oauth["accessToken"], "new", "refresh access")
    assert_eq(oauth["refreshToken"], "rotated", "refresh rotate")
    assert_eq(oauth["subscriptionType"], "max", "round-trip extra key")
    assert_eq(oauth["expiresAt"], 1_800_003_600_000, "ms expiry write-back")

    print("UsageDock core checks passed.")
    print(f"  fixtures: {FIXTURES}")
    print(f"  legacy windows: {[w['label'] + ' ' + str(int(w['percent'])) + '%' for w in legacy_windows]}")
    print(f"  limits windows: {[w['label'] + ' ' + str(int(w['percent'])) + '%' for w in modern_windows]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
