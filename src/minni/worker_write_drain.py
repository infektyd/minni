"""minnid tick: kick leftover worker-write Q. Not Thread SoT.

``drainPendingWorkerWritesForVault`` stays the apply entry. This module
only names who calls it when nobody boots MCP on that vault. It does
not write the journal, does not become minnid-canonical, and is not G3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("minnid")

STANDING_DRAIN_TRIGGER = "minnid tick"
_DEFAULT_INTERVAL_S = 5.0
_MIN_INTERVAL_S = 0.05


def worker_write_drain_enabled() -> bool:
    return (os.environ.get("MINNI_WORKER_WRITE_DRAIN", "on") or "on").strip().lower() != "off"


def worker_write_drain_interval() -> float:
    raw = (os.environ.get("MINNI_WORKER_WRITE_DRAIN_INTERVAL") or "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_INTERVAL_S
    if value < _MIN_INTERVAL_S:
        return _MIN_INTERVAL_S
    return value


def _source_checkout() -> Path | None:
    pkg = Path(__file__).resolve().parent
    if pkg.name != "minni" or pkg.parent.name != "src":
        return None
    repo = pkg.parent.parent
    if not (repo / "plugins" / "minni").is_dir():
        return None
    return repo


def standing_drain_tick_js() -> Path | None:
    override = (os.environ.get("MINNI_STANDING_DRAIN_TICK_JS") or "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    checkout = _source_checkout()
    if checkout is None:
        return None
    path = checkout / "plugins" / "minni" / "dist" / "standing-drain-tick.js"
    return path if path.is_file() else None


def _has_pending_q(vault: Path) -> bool:
    locks = vault / ".runtime" / "thread-locks"
    if not locks.is_dir():
        return False
    try:
        names = list(locks.iterdir())
    except OSError:
        return False
    for qdir in names:
        if not qdir.is_dir() or not qdir.name.endswith(".q"):
            continue
        try:
            files = list(qdir.iterdir())
        except OSError:
            continue
        if any(f.name.endswith(".json") and f.name != "progress.json" for f in files):
            return True
    return False


def _known_vaults() -> list[Path]:
    found: list[Path] = []
    env_vault = (os.environ.get("MINNI_VAULT_PATH") or "").strip()
    if env_vault:
        found.append(Path(env_vault).expanduser())
    home_raw = (os.environ.get("MINNI_HOME") or "").strip()
    home = Path(home_raw).expanduser() if home_raw else Path.home() / ".minni"
    try:
        from minni.index_all import discover_agent_vaults

        found.extend(discover_agent_vaults(home))
    except Exception:
        pass
    try:
        from minni.config import DEFAULT_CONFIG

        found.append(Path(DEFAULT_CONFIG.vault_path).expanduser())
    except Exception:
        pass
    if home.is_dir():
        try:
            children = list(home.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and _has_pending_q(child):
                found.append(child)
    unique: list[Path] = []
    seen: set[str] = set()
    for vault in found:
        try:
            key = str(vault.resolve()) if vault.exists() else str(vault)
        except OSError:
            key = str(vault)
        if key in seen:
            continue
        seen.add(key)
        unique.append(vault)
    return unique


def vaults_needing_worker_write_drain() -> list[Path]:
    return [vault for vault in _known_vaults() if _has_pending_q(vault)]


def kick_pending_worker_write_drain_for_vault(vault_path: str | Path) -> dict[str, Any]:
    """Kick ``drainPendingWorkerWritesForVault``. Does not write the journal."""
    tick_js = standing_drain_tick_js()
    if tick_js is None:
        logger.warning(
            "minnid tick: standing-drain-tick.js missing; leftover Q stays pending"
        )
        return {"trigger": STANDING_DRAIN_TRIGGER, "planIds": [], "kicked": False}
    node = shutil.which("node")
    if node is None:
        logger.warning("minnid tick: node missing; leftover Q stays pending")
        return {"trigger": STANDING_DRAIN_TRIGGER, "planIds": [], "kicked": False}
    vault = str(Path(vault_path).expanduser())
    env = os.environ.copy()
    env["MINNI_STANDING_DRAIN_VAULT"] = vault
    try:
        completed = subprocess.run(
            [node, str(tick_js), vault],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=70,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("minnid tick: kick failed for %s: %s", vault, exc)
        return {"trigger": STANDING_DRAIN_TRIGGER, "planIds": [], "kicked": False}
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()
        logger.warning(
            "minnid tick: kick exit %s for %s%s",
            completed.returncode,
            vault,
            f": {err}" if err else "",
        )
        return {"trigger": STANDING_DRAIN_TRIGGER, "planIds": [], "kicked": False}
    plan_ids: list[str] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            raw_ids = payload.get("planIds")
            if isinstance(raw_ids, list):
                plan_ids.extend(str(item) for item in raw_ids)
    return {
        "trigger": STANDING_DRAIN_TRIGGER,
        "planIds": plan_ids,
        "kicked": True,
        "vault": vault,
    }


def minnid_tick_once() -> list[dict[str, Any]]:
    """One minnid tick: kick apply on each vault with leftover Q."""
    results: list[dict[str, Any]] = []
    for vault in vaults_needing_worker_write_drain():
        try:
            results.append(kick_pending_worker_write_drain_for_vault(vault))
        except Exception:
            logger.exception("minnid tick: kick raised for %s", vault)
    return results


async def minnid_tick_runner() -> None:
    """Standing minnid tick. Stays up. Kick only. Not persist authority."""
    interval = worker_write_drain_interval()
    logger.info(
        "Standing drain enabled: %s every %ss (kick drainPendingWorkerWritesForVault)",
        STANDING_DRAIN_TRIGGER,
        interval,
    )
    while True:
        try:
            await asyncio.to_thread(minnid_tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("minnid tick failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


def run_standing_drain_loop() -> None:
    asyncio.run(minnid_tick_runner())


if __name__ == "__main__":
    run_standing_drain_loop()
