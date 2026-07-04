"""Canonical filesystem paths for wire installs."""

from __future__ import annotations

import os
import re
from pathlib import Path

VERSION_DIR_RE = re.compile(r"^[\w.+-]+$")


def is_safe_version_segment(value: str) -> bool:
    return (
        bool(value)
        and VERSION_DIR_RE.match(value) is not None
        and ".." not in value
        and "/" not in value
    )


def user_home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def plugin_base() -> Path:
    return user_home() / ".minni" / "plugin"


def install_lock_path(version: str) -> Path:
    return plugin_base() / f".install-{version}.lock"


def staging_dir(version: str) -> Path:
    return plugin_base() / f".staging-{version}-{os.getpid()}"


def quarantine_dir(version: str) -> Path:
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return plugin_base() / f".quarantine-{version}-{stamp}"


def current_symlink() -> Path:
    return plugin_base() / "current"