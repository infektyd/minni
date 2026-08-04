"""Product fleet sync: keep every Minni-serviced agent host on this install.

Customer problem: the daemon/CLI package can move (pipx upgrade or git pull)
while each host still points at an old ``~/.minni/plugin/<version>`` tree.
Health already reports ``deploy.stale`` / ``plugin_dist.stale``; this module
is the *one* command that closes the gap.

Two install kinds (same as deploy honesty):

* **Packaged (wheel / pipx)** — re-wire all wire-primary platforms from the
  bundled plugin payload; refresh propagate-managed cursor + antigravity.
* **Editable checkout** — re-wire from the live repo (``--from-repo``) so
  dogfooders track ``main`` without a separate wheel cut.

This is intentionally louder and safer than hand-running mixed
``wire`` / ``propagate`` commands (D7 fleet partition).

Never force-updates git; never merges PRs; never discards a dirty checkout
when ``full=True`` (that path shells to ``update_root.sh``).
"""

from __future__ import annotations

import json
import os
import platform as py_platform
import shutil
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SyncResult:
    ok: bool
    install_kind: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "install_kind": self.install_kind,
            "steps": self.steps,
            "message": self.message,
            "next_actions": self.next_actions,
        }


def _source_checkout() -> Optional[Path]:
    try:
        from minni.minnid_runtime.deploy_honesty import _source_checkout
        return _source_checkout()
    except Exception:
        return None


def _detect_install_kind() -> tuple[str, Optional[Path]]:
    checkout = _source_checkout()
    if checkout is not None:
        return "editable-checkout", checkout
    return "packaged", None


def _propagate_py(repo: Optional[Path]) -> Optional[Path]:
    """Locate propagate.py from package payload or a known checkout."""
    import minni

    pkg = Path(minni.__file__).resolve().parent
    candidates: list[Path] = [
        pkg / "plugin_payload" / "skills" / "minni-install" / "scripts" / "propagate.py",
    ]
    if repo is not None:
        candidates.insert(
            0,
            repo / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py",
        )
    # Editable without payload staged
    if pkg.parent.name == "src":
        candidates.append(
            pkg.parent.parent
            / "plugins"
            / "minni"
            / "skills"
            / "minni-install"
            / "scripts"
            / "propagate.py"
        )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _run_wire(
    *,
    from_repo: Optional[Path],
    force_reinstall: bool,
    prune: bool,
    dry_run: bool,
) -> dict[str, Any]:
    from minni.wire.flow import run_wire

    ns = Namespace(
        platform="all",
        agent=None,
        workspace=None,
        install_root=None,
        dry_run=dry_run,
        verify_payload=False,
        prune=prune,
        no_prune=not prune,
        force_reinstall=force_reinstall,
        from_repo=str(from_repo) if from_repo else None,
        use_version=None,
    )
    # run_wire prints JSON and returns exit code — capture via subprocess for isolation
    # of stdout? Prefer in-process for tests; wire emits to stdout.
    # Call the same path as CLI.
    code = run_wire(ns)
    return {"name": "wire_all", "exit_code": code, "from_repo": str(from_repo) if from_repo else None}


def _run_propagate(platform: str, repo: Optional[Path], dry_run: bool) -> dict[str, Any]:
    script = _propagate_py(repo)
    if script is None:
        return {
            "name": f"propagate:{platform}",
            "exit_code": 2,
            "error": "propagate.py not found in package payload or checkout",
        }
    cmd = [sys.executable, str(script), "update-plugin", "--platform", platform]
    if dry_run:
        # propagate may not support dry-run universally; skip with note
        return {
            "name": f"propagate:{platform}",
            "exit_code": 0,
            "skipped": True,
            "reason": "dry-run: would run " + " ".join(cmd),
        }
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "name": f"propagate:{platform}",
            "exit_code": 1,
            "error": str(exc),
        }
    return {
        "name": f"propagate:{platform}",
        "exit_code": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-800:],
        "stderr_tail": (proc.stderr or "")[-800:],
    }


def _kickstart_daemon() -> dict[str, Any]:
    if py_platform.system() != "Darwin":
        return {
            "name": "restart_daemon",
            "exit_code": 0,
            "skipped": True,
            "reason": "not macOS launchd; restart with: minni down && minni up",
        }
    label = f"gui/{os.getuid()}/com.minni.minnid"
    try:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", label],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "restart_daemon", "exit_code": 1, "error": str(exc)}
    if proc.returncode != 0:
        return {
            "name": "restart_daemon",
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-400:],
            "hint": "launchd agent not loaded; use: minni down && minni up",
        }
    return {"name": "restart_daemon", "exit_code": 0, "via": "launchctl kickstart"}


def run_fleet_sync(
    *,
    dry_run: bool = False,
    full: bool = False,
    force_reinstall: bool = True,
    prune: bool = True,
    restart_daemon: bool = True,
    propagate_hosts: bool = True,
) -> SyncResult:
    """Redeploy plugin payload to every fleet surface this install manages."""
    kind, checkout = _detect_install_kind()
    steps: list[dict[str, Any]] = []
    next_actions = [
        "Restart agent hosts (Claude Code / Codex / Cursor / Grok / Kilo) so they reload MCP.",
    ]

    if full:
        if kind != "editable-checkout" or checkout is None:
            return SyncResult(
                ok=False,
                install_kind=kind,
                message=(
                    "`minni sync --full` is for editable checkouts only "
                    "(git dogfood). Packaged installs: `pipx upgrade minni` "
                    "then `minni sync`."
                ),
                next_actions=[
                    "pipx upgrade minni   # or: uv tool upgrade minni",
                    "minni sync",
                ],
            )
        script = checkout / "scripts" / "update_root.sh"
        if not script.is_file():
            return SyncResult(
                ok=False,
                install_kind=kind,
                message=f"update_root.sh missing at {script}",
            )
        cmd = ["/bin/bash", str(script), "--repo", str(checkout)]
        if dry_run:
            cmd.append("--dry-run")
        try:
            proc = subprocess.run(cmd, check=False)
        except OSError as exc:
            return SyncResult(
                ok=False, install_kind=kind, message=str(exc),
            )
        steps.append({"name": "update_root", "exit_code": proc.returncode})
        ok = proc.returncode == 0
        return SyncResult(
            ok=ok,
            install_kind=kind,
            steps=steps,
            message=(
                "full checkout sync complete"
                if ok
                else "full checkout sync failed — see update_root output "
                "(dirty tree, diverge, or wire failure)"
            ),
            next_actions=next_actions if ok else [
                "Ensure the checkout is on a clean main, then retry: minni sync --full",
                "Or fleet-only: minni sync   (no git pull)",
            ],
        )

    # Fleet-only path (customer default)
    from_repo = checkout if kind == "editable-checkout" else None
    wire_step = _run_wire(
        from_repo=from_repo,
        force_reinstall=force_reinstall,
        prune=prune,
        dry_run=dry_run,
    )
    steps.append(wire_step)

    if propagate_hosts and not dry_run:
        for plat in ("antigravity", "cursor"):
            steps.append(_run_propagate(plat, checkout, dry_run=False))
    elif propagate_hosts and dry_run:
        steps.append({
            "name": "propagate:antigravity+cursor",
            "exit_code": 0,
            "skipped": True,
            "reason": "dry-run",
        })

    if restart_daemon and not dry_run:
        steps.append(_kickstart_daemon())
    elif restart_daemon and dry_run:
        steps.append({
            "name": "restart_daemon",
            "exit_code": 0,
            "skipped": True,
            "reason": "dry-run",
        })

    hard_fail = any(
        s.get("exit_code", 0) not in (0, 1)  # wire may exit 1 for partial skip
        and not s.get("skipped")
        for s in steps
        if s.get("name", "").startswith("propagate")
        and s.get("exit_code", 0) not in (0,)
    )
    # Wire: 0 = success, 1 = partial/skip, 2 = usage — treat 2 as fail
    wire_code = next(
        (s.get("exit_code", 0) for s in steps if s.get("name") == "wire_all"),
        0,
    )
    ok = wire_code in (0, 1) and not hard_fail

    msg = (
        f"fleet sync ({kind}): plugin payload redeployed to wire-primary hosts"
        + ("; cursor/antigravity propagate attempted" if propagate_hosts else "")
    )
    if kind == "editable-checkout":
        next_actions.insert(
            0,
            "To also fast-forward git + reinstall deps: minni sync --full",
        )
    else:
        next_actions.insert(
            0,
            "After a new PyPI release: pipx upgrade minni && minni sync",
        )

    return SyncResult(
        ok=ok,
        install_kind=kind,
        steps=steps,
        message=msg if ok else "fleet sync completed with failures — see steps",
        next_actions=next_actions,
    )


def install_auto_sync_agent(*, repo: Optional[Path] = None) -> SyncResult:
    """Install macOS launchd agent for unattended checkout sync (operator opt-in)."""
    if py_platform.system() != "Darwin":
        return SyncResult(
            ok=False,
            install_kind="n/a",
            message="Auto-sync agent is only implemented for macOS launchd today.",
            next_actions=[
                "Linux: systemd user timer pointing at scripts/update_root.sh",
                "Or cron: 0 */6 * * * make -C /path/to/minni sync-root",
            ],
        )
    kind, checkout = _detect_install_kind()
    repo = repo or checkout
    if repo is None:
        return SyncResult(
            ok=False,
            install_kind=kind,
            message=(
                "Auto-sync pulls origin/main into an editable checkout. "
                "Packaged (pipx) installs should upgrade the package then "
                "`minni sync` — not a git timer."
            ),
            next_actions=["pipx upgrade minni && minni sync"],
        )
    template = repo / "deploy" / "com.minni.sync-root.plist.template"
    if not template.is_file():
        return SyncResult(
            ok=False,
            install_kind=kind,
            message=f"missing template {template}",
        )
    home = Path.home()
    log_dir = home / ".minni" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    dest = agents / "com.minni.sync-root.plist"
    text = template.read_text(encoding="utf-8")
    text = text.replace("__REPO__", str(repo.resolve()))
    text = text.replace("__HOME__", str(home))
    dest.write_text(text, encoding="utf-8")
    # reload
    uid = os.getuid()
    label = f"gui/{uid}/com.minni.sync-root"
    subprocess.run(["launchctl", "bootout", label], capture_output=True, check=False)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    ok = proc.returncode == 0
    return SyncResult(
        ok=ok,
        install_kind=kind,
        steps=[{
            "name": "launchd_install",
            "exit_code": proc.returncode,
            "plist": str(dest),
            "stderr_tail": (proc.stderr or "")[-400:],
        }],
        message=(
            "Installed com.minni.sync-root (every 6h). "
            "Logs: ~/.minni/logs/sync-root.log"
            if ok
            else f"launchctl bootstrap failed: {proc.stderr}"
        ),
        next_actions=[
            f"launchctl print gui/{uid}/com.minni.sync-root",
            "minni sync --auto-status",
            "Keep the checkout clean on main or scheduled runs will refuse.",
        ],
    )


def uninstall_auto_sync_agent() -> SyncResult:
    if py_platform.system() != "Darwin":
        return SyncResult(ok=True, install_kind="n/a", message="nothing to uninstall")
    uid = os.getuid()
    label = f"gui/{uid}/com.minni.sync-root"
    subprocess.run(["launchctl", "bootout", label], capture_output=True, check=False)
    dest = Path.home() / "Library" / "LaunchAgents" / "com.minni.sync-root.plist"
    if dest.is_file():
        dest.unlink()
    return SyncResult(
        ok=True,
        install_kind="n/a",
        message="Removed com.minni.sync-root launchd agent (if present).",
    )


def auto_sync_status() -> SyncResult:
    if py_platform.system() != "Darwin":
        return SyncResult(
            ok=True, install_kind="n/a",
            message="Auto-sync status is macOS-only (launchd).",
        )
    uid = os.getuid()
    label = f"gui/{uid}/com.minni.sync-root"
    proc = subprocess.run(
        ["launchctl", "print", label],
        capture_output=True,
        text=True,
        check=False,
    )
    loaded = proc.returncode == 0
    return SyncResult(
        ok=loaded,
        install_kind="n/a",
        message="loaded" if loaded else "not loaded",
        steps=[{
            "name": "launchctl_print",
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-1200:],
        }],
        next_actions=[] if loaded else ["minni sync --install-auto"],
    )
