"""Determinism of built_at helpers (#352)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from minni.wire.manifest import deterministic_built_at


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('version = "0.3.0"\n', encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "init")
    return repo


def test_deterministic_built_at_stable_twice(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    repo = _init_repo(tmp_path)
    a = deterministic_built_at(repo)
    b = deterministic_built_at(repo)
    assert a == b
    # Must equal the git HEAD commit date normalized to Z.
    raw = subprocess.check_output(
        ["git", "log", "-1", "--format=%cI"], cwd=repo, text=True
    ).strip()
    dt = datetime.fromisoformat(raw).astimezone(UTC)
    expected = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert a == expected


def test_deterministic_built_at_source_date_epoch_wins(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1609459200")  # 2021-01-01T00:00:00Z
    val = deterministic_built_at(repo)
    assert val == "2021-01-01T00:00:00Z"
    # Second call also stable.
    assert deterministic_built_at(repo) == val


def test_deterministic_built_at_epoch_wins_even_without_repo(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert deterministic_built_at(None) == "1970-01-01T00:00:00Z"
    assert deterministic_built_at() == "1970-01-01T00:00:00Z"


def test_strict_parse_rejects_what_both_sides_must_reject(tmp_path, monkeypatch):
    """Parity table (#361 review): every lenient-parse divergence between
    Number() and int() must fall through to the git date on the Python side.
    The mjs counterpart pins the same set in deterministic-build.test.mjs."""
    from minni.wire.manifest import deterministic_built_at

    repo = Path(__file__).resolve().parent.parent
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    expected = deterministic_built_at(repo)
    for bad in ("1.5", "0x10", "1_000", "1e30", "Infinity", "99999999999999999999",
                "1754447207000000", " ", "abc"):
        monkeypatch.setenv("SOURCE_DATE_EPOCH", bad)
        assert deterministic_built_at(repo) == expected, f"epoch {bad!r} must fall through"
    # Boundary that must be ACCEPTED on both sides (year 9999).
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "253402300799")
    assert deterministic_built_at(repo) == "9999-12-31T23:59:59Z"
