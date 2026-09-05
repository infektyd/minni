"""Fleet sync: keep every Minni-serviced agent host on this install.

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

import contextlib
import importlib.util
import io
import json
import os
import platform as py_platform
import re
import shutil
import signal
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# update_root.sh pulls git, reinstalls deps, wires and propagates. The launchd
# timer runs it unattended every 6h, so a hung child must not pin a sync
# process forever — every other subprocess here is already bounded.
UPDATE_ROOT_TIMEOUT = 1800

# launchctl's exit code when the label is not loaded in the domain.
_LAUNCHCTL_NO_SUCH_SERVICE = 113
_MINNID_LABEL = "com.minni.minnid"


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
    """Editable checkout backing this install, or None if genuinely packaged.

    A missing module means packaged. A module that exists but fails to import
    is a broken install, and swallowing that would downgrade a checkout to
    "packaged": wire would then redeploy from the stale bundled payload and
    the deploy audit would skip, all while every step exits 0.
    """
    if importlib.util.find_spec("minni.minnid_runtime.deploy_honesty") is None:
        return None
    from minni.minnid_runtime.deploy_honesty import _source_checkout
    return _source_checkout()


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


def _is_wire_output(doc: Any) -> bool:
    """WireOutput.emit shape — a nested ``gc: {}`` must not win the scan."""
    return (
        isinstance(doc, dict)
        and "schema" in doc
        and "status" in doc
        and "results" in doc
    )


def _wire_status(text: str) -> Optional[str]:
    """Read ``status`` out of wire's emitted JSON, or None if unreadable.

    Same decode as scripts/update_root.sh: a ``--from-repo`` run can leak npm
    banners onto stdout ahead of the document, and the pretty emit ends with
    ``"gc": {}`` so the last ``{`` is the wrong anchor. Walk brace positions
    and keep the last WireOutput-shaped object.
    """
    decoder = json.JSONDecoder()
    found: list[str] = []
    idx = text.find("{")
    while idx >= 0:
        # raw_decode, not loads: wire may print after the document, and a
        # trailing line must not make the status unreadable.
        with contextlib.suppress(ValueError):
            doc, _end = decoder.raw_decode(text[idx:])
            if _is_wire_output(doc):
                found.append(str(doc.get("status") or ""))
        idx = text.find("{", idx + 1)
    # run_wire emits exactly one report. Two disagreeing documents means noise
    # embedded something WireOutput-shaped, and picking either one lets a
    # decoy "skipped" overwrite a real "failed" — the very laundering this
    # module exists to stop. Unreadable is the safe answer: callers treat an
    # unknown status on a nonzero exit as a failure.
    unique = set(found)
    if len(unique) != 1:
        return None
    return found[0] or None


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
        # run_wire reads root CLI --socket (same default as minni_cli)
        socket=str(Path.home() / ".minni" / "run" / "minnid.sock"),
    )
    # run_wire prints its JSON report and returns an exit code. Tee stdout so
    # the operator still sees the report while we read the status out of it:
    # exit 1 alone cannot tell "wired nothing here" from "wiring failed".
    buf = io.StringIO()
    failure: Optional[str] = None
    code = 1
    try:
        with contextlib.redirect_stdout(buf):
            code = run_wire(ns)
    except Exception as exc:
        # `minni sync` owes its caller a JSON verdict, not a traceback — the
        # launchd timer's log is the only place this is ever read.
        failure = str(exc)
    finally:
        report = buf.getvalue()
        if report:
            sys.stdout.write(report)
    step: dict[str, Any] = {
        "name": "wire_all",
        "exit_code": code,
        "from_repo": str(from_repo) if from_repo else None,
    }
    if failure is not None:
        return {**step, "exit_code": 1, "status": None, "error": failure}
    status = _wire_status(report)
    step["status"] = status
    if code != 0 and status == "skipped":
        # D5: wire exits 1 when it wired nothing. Propagate still owns
        # antigravity/cursor, so nothing to wire is a skip rather than a
        # redeploy failure (scripts/update_root.sh makes the same call).
        step["skipped"] = True
        step["reason"] = "wire reported nothing to wire (status=skipped)"
    return step


def _run_propagate(platform: str, repo: Optional[Path], dry_run: bool) -> dict[str, Any]:
    from minni.wire.host_discovery import host_decision
    decision = host_decision(platform, bulk=True)
    if not decision["eligible"]:
        return {"name": f"propagate:{platform}", "exit_code": 1 if decision["status"] == "failed" else 0, "skipped": decision["status"] == "skipped", **decision}
    script = _propagate_py(repo)
    if script is None:
        return {
            "name": f"propagate:{platform}",
            "exit_code": 2,
            "error": "propagate.py not found in package payload or checkout",
        }
    cmd = [sys.executable, str(script), "update-plugin", "--platform", platform, "--existing-only"]
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


def _grok_install_root() -> Optional[Path]:
    """Active wire install root for grok, or None.

    Prefer the newest *grok* wire specifically: after a partial `wire all`, a
    global max over ``wired.json`` can pick claude-code's fresh root while grok
    still points at an older tree, and the hooks would then stamp dist paths
    grok never loads.
    """
    base = Path.home() / ".minni" / "plugin"
    entries: list[tuple[str, Path]] = []
    with contextlib.suppress(Exception):
        data = json.loads((base / "wired.json").read_text(encoding="utf-8"))
        entries = [
            (str(w.get("wired_at") or ""), Path(str(w.get("install_root"))))
            for w in data.get("wires", [])
            if isinstance(w, dict)
            and w.get("install_root")
            and str(w.get("platform") or "") == "grok"
        ]
    entries = [(when, path) for when, path in entries if path.is_dir()]
    if entries:
        return max(entries, key=lambda e: e[0])[1]
    current = base / "current"
    if current.exists():
        with contextlib.suppress(OSError):
            resolved = current.resolve()
            if resolved.is_dir():
                return resolved
    return None


def _restamp_grok_hooks(repo: Optional[Path], *, dry_run: bool) -> dict[str, Any]:
    """Refresh ~/.grok hooks + rules against the active wire install root.

    Grok hooks/rules are propagate-only — `wire` never installs them — so a
    fleet sync that skips this leaves the old manifest pointing at a versioned
    tree that wire's GC then prunes, and every Grok hook silently dies. A full
    `propagate --platform grok` is the wrong hammer: it would re-stamp MCP onto
    the legacy agents tree. update_root.sh does exactly this narrower refresh
    and counts a failure as a redeploy failure.
    """
    step: dict[str, Any] = {"name": "grok_hooks_rules"}
    from minni.wire.host_discovery import host_decision
    decision = host_decision("grok", bulk=True)
    if not decision["eligible"]:
        return {**step, "exit_code": 1 if decision["status"] == "failed" else 0, "skipped": decision["status"] == "skipped", **decision}
    if dry_run:
        return {**step, "exit_code": 0, "skipped": True, "reason": "dry-run"}
    script = _propagate_py(repo)
    if script is None:
        return {
            **step,
            "exit_code": 2,
            "error": "propagate.py not found in package payload or checkout",
        }
    root = _grok_install_root()
    if root is None:
        # Leftover ~/.grok alone does not justify failing a redeploy for an
        # optional surface — skip loud.
        return {
            **step,
            "exit_code": 0,
            "skipped": True,
            "reason": "no grok wire install root (and no ~/.minni/plugin/current)",
        }
    try:
        spec = importlib.util.spec_from_file_location("minni_propagate_grok", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {script}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.preflight_grok_native(root)
        hooks = mod.update_grok_hooks(root, existing_only=True)
        rules = mod.write_grok_rules(existing_only=True)
        # Inside the try on purpose: a propagate.py whose return contract
        # drifted must fail this step, not crash `minni sync`.
        installed = all(bool(result.get("installed") or result.get("skipped")) for result in (hooks, rules))
    except Exception as exc:
        return {**step, "exit_code": 1, "install_root": str(root), "error": str(exc)}
    return {
        **step,
        "exit_code": 0 if installed else 1,
        "install_root": str(root),
        "hooks": hooks,
        "rules": rules,
    }


def _check_deployments_py(repo: Optional[Path]) -> Optional[Path]:
    """Locate scripts/check_deployments.py in a checkout, if there is one."""
    if repo is None:
        return None
    script = repo / "scripts" / "check_deployments.py"
    return script if script.is_file() else None


# Row shape from check_deployments.py: status, padded detail, then the label.
_WORKTREE_ROW = re.compile(r"^\s*WORKTREE\s{2,}\S.*?\s{2,}(?P<label>\S.*?)\s*$")
# Printed once per run, including "No deployments discovered." — its absence
# means the audit did not complete and we must not read silence as clean.
_AUDIT_COMPLETED = ("deployment(s):", "No deployments discovered.")
_WORKTREE_TALLY = re.compile(r"(?P<n>\d+) dist symlinked at the working tree")


def _worktree_linked_labels(report: str) -> list[str]:
    """Deployment labels check_deployments.py classified WORKTREE."""
    return [
        m.group("label")
        for m in (_WORKTREE_ROW.match(line) for line in report.splitlines())
        if m is not None
    ]


def _audit_deploy_symlinks(repo: Optional[Path], *, dry_run: bool) -> dict[str, Any]:
    """Fail the sync when a deployed dist is symlinked at the working tree.

    Minni ships as a product: deployed artifacts are built, versioned copies,
    never symlinks into a working tree. A WORKTREE-linked dist executes
    uncommitted state and carries no reproducible version, so `git stash`
    changes live behavior. scripts/check_deployments.py already makes that
    call (including which symlinks are legitimate installed artifacts); this
    only carries its judgment into the verdict.

    Its exit code is deliberately not reused: it also fails on stale vintage
    and content drift, which are not this step's business.
    """
    step: dict[str, Any] = {"name": "deploy_symlink_audit"}
    if dry_run:
        return {**step, "exit_code": 0, "skipped": True, "reason": "dry-run"}
    script = _check_deployments_py(repo)
    if script is None:
        # No checkout, so no working tree for a dist to point into.
        return {
            **step,
            "exit_code": 0,
            "skipped": True,
            "reason": "no checkout: scripts/check_deployments.py unavailable",
        }
    env = {
        **os.environ,
        # Parity with update_root.sh: the staged payload under the repo is a
        # release artifact, not a live host surface.
        "MINNI_CHECK_DEPLOYMENTS_SKIP_REPO": "1",
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**step, "exit_code": 1, "error": f"audit inconclusive: {exc}"}
    report = proc.stdout or ""
    if not any(marker in report for marker in _AUDIT_COMPLETED):
        return {
            **step,
            "exit_code": 1,
            "error": "audit inconclusive: check_deployments.py produced no verdict",
            "stderr_tail": (proc.stderr or "")[-400:],
        }
    linked = _worktree_linked_labels(report)
    # Cross-check the row scrape against check_deployments' own tally: if it
    # counted symlinked dists we failed to name, the parse is broken and a
    # clean verdict would be fail-open.
    tally = _WORKTREE_TALLY.search(report)
    counted = int(tally.group("n")) if tally else 0
    if counted > len(linked):
        return {
            **step,
            "exit_code": 1,
            "error": (
                f"audit inconclusive: check_deployments counted {counted} "
                f"worktree-symlinked dist(s) but {len(linked)} could be read"
            ),
            "worktree_linked": linked,
        }
    return {
        **step,
        "exit_code": 1 if linked else 0,
        "worktree_linked": linked,
        # A clean verdict must say what it did not judge: check_deployments
        # excludes historical wire dirs, superseded marketplace caches and the
        # legacy plugin/current path, and a symlink hiding there would not
        # appear above.
        "not_judged": [
            # The repo payload exclusion is ours (SKIP_REPO below) and
            # check_deployments emits no NOTE for it, so name it here or the
            # blind-spot list would quietly under-report.
            f"{repo}/src/minni/plugin_payload: skipped "
            "(staged release artifact, not a live host surface)",
            *(
                line.removeprefix("NOTE").strip()
                for line in report.splitlines()
                if line.startswith("NOTE")
            ),
        ],
    }


def _minnid_is_live(timeout: float = 5.0) -> bool:
    """True if minnid completes a ``ping`` round-trip.

    A bare connect is not enough: the kernel completes connect() against a
    listening socket while the daemon's event loop is wedged, so a daemon
    broken by the very upgrade we just deployed would read as healthy and
    excuse the restart we failed to perform.
    """
    from minni.minni_cli import RpcError, _rpc

    path = Path.home() / ".minni" / "run" / "minnid.sock"
    try:
        _rpc(path, "ping", timeout=timeout)
        return True
    except (RpcError, OSError):
        return False


def _kickstart_daemon() -> dict[str, Any]:
    if py_platform.system() != "Darwin":
        return {
            "name": "restart_daemon",
            "exit_code": 0,
            "skipped": True,
            "reason": "not macOS launchd; restart with: minni down && minni up",
            # Definitively not restarted here, so the operator must be told.
            "manual_restart": True,
        }
    label = f"gui/{os.getuid()}/{_MINNID_LABEL}"
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
        stderr = proc.stderr or ""
        not_loaded = (
            proc.returncode == _LAUNCHCTL_NO_SUCH_SERVICE
            and _MINNID_LABEL in stderr
        )
        if not_loaded:
            # The launchd unit is optional — `minni up` runs minnid from a PID
            # file instead. But "no unit" only excuses the restart if a daemon
            # is actually answering: an agent that was unloaded by a botched
            # upgrade reports exactly the same way, and that IS a failure.
            if _minnid_is_live():
                return {
                    "name": "restart_daemon",
                    "exit_code": 0,
                    "skipped": True,
                    "reason": "minnid is not launchd-managed and is live",
                    "manual_restart": True,
                }
            return {
                "name": "restart_daemon",
                "exit_code": 1,
                "stderr_tail": stderr[-400:],
                "hint": "no launchd unit and no daemon answering; start it: minni up",
            }
        return {
            "name": "restart_daemon",
            "exit_code": proc.returncode,
            "stderr_tail": stderr[-400:],
            "hint": (
                "launchd reports no such service"
                if proc.returncode == _LAUNCHCTL_NO_SUCH_SERVICE
                else "launchd agent loaded but kickstart failed"
            ),
        }
    return {"name": "restart_daemon", "exit_code": 0, "via": "launchctl kickstart"}


def _step_failed(step: dict[str, Any]) -> bool:
    """A step failed unless it exited 0 or declared itself skipped.

    A step dict with no ``exit_code`` never established success, so it counts
    as a failure: this is the trust boundary for the whole verdict and it must
    not default to optimism.
    """
    return not step.get("skipped") and step.get("exit_code", 1) != 0


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
    try:
        kind, checkout = _detect_install_kind()
    except Exception as exc:
        # Loud, but still a verdict: a half-installed package must not hand the
        # launchd timer a traceback in place of machine-readable JSON.
        return SyncResult(
            ok=False,
            install_kind="unknown",
            message=f"cannot determine install kind (broken install?): {exc}",
            next_actions=["Reinstall the package: pipx reinstall minni"],
        )
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
            # Own process group: update_root.sh spawns git/pip/npm, and killing
            # only bash would leave a credential-blocked `git pull` holding
            # .git/index.lock for the next timer run.
            proc = subprocess.Popen(cmd, start_new_session=True)
        except OSError as exc:
            return SyncResult(ok=False, install_kind=kind, message=str(exc))
        try:
            returncode = proc.wait(timeout=UPDATE_ROOT_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=30)
            return SyncResult(
                ok=False,
                install_kind=kind,
                steps=[{"name": "update_root", "exit_code": 1, "timeout": UPDATE_ROOT_TIMEOUT}],
                message=(
                    f"update_root.sh timed out after {UPDATE_ROOT_TIMEOUT}s — "
                    "the checkout may be mid-pull or waiting on input"
                ),
                next_actions=[
                    "Run it in the foreground to see where it hangs: "
                    "bash scripts/update_root.sh --repo <checkout>",
                ],
            )
        steps.append({"name": "update_root", "exit_code": returncode})
        ok = returncode == 0
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

    # Grok hooks/rules are propagate-only; wire never installs them. Without
    # this the hook manifest keeps pointing at a pruned versioned tree.
    # --wire-only means "propagate-managed surfaces are not mine to touch",
    # and this is one of them.
    if propagate_hosts:
        steps.append(_restamp_grok_hooks(checkout, dry_run=dry_run))

    # Deployed artifacts must be built copies; a dist symlinked at the working
    # tree is a redeploy failure, not a convenience.
    steps.append(_audit_deploy_symlinks(checkout, dry_run=dry_run))

    if restart_daemon and not dry_run:
        steps.append(_kickstart_daemon())
    elif restart_daemon and dry_run:
        steps.append({
            "name": "restart_daemon",
            "exit_code": 0,
            "skipped": True,
            "reason": "dry-run",
        })

    # Every step is load-bearing: a nonzero exit is a failed fleet redeploy,
    # never "sync complete". Steps that legitimately did nothing mark
    # themselves ``skipped`` (dry-run, absent surface) and are excluded.
    failed = [s for s in steps if _step_failed(s)]
    ok = not failed

    # Say what actually ran. A run where every step skipped must not claim the
    # payload was redeployed — that is the same overstatement in prose that
    # ok=true used to make in JSON.
    skipped = [s for s in steps if s.get("skipped")]
    applied = [s for s in steps if not s.get("skipped")]
    msg = (
        f"fleet sync ({kind}): {len(applied)} step(s) applied"
        + (f", {len(skipped)} skipped ({', '.join(str(s.get('name')) for s in skipped)})"
           if skipped else "")
    )
    if not applied:
        msg = f"fleet sync ({kind}): nothing to do — every step skipped"
    if any(s.get("manual_restart") for s in steps):
        next_actions.insert(0, (
            "minnid is not launchd-managed and still runs the pre-sync code — "
            "restart it: minni down && minni up"
        ))
    not_judged = [n for s in steps for n in s.get("not_judged", [])]
    if not_judged:
        next_actions.append(
            f"Deploy audit did not judge {len(not_judged)} location(s) — see "
            "steps[].not_judged",
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

    if not ok:
        detail = ", ".join(
            f"{s.get('name')} (exit {s.get('exit_code')})" for s in failed
        )
        msg = f"fleet sync ({kind}) FAILED — {len(failed)} step(s) failed: {detail}"
        next_actions = [
            "Re-run after fixing the failing step: minni sync",
            *next_actions,
        ]
        linked = [
            label
            for s in failed
            for label in s.get("worktree_linked", [])
        ]
        if linked:
            next_actions.insert(0, (
                "Deployed dists must be built copies, not symlinks into a "
                "working tree — replace with a built artifact: "
                + ", ".join(linked)
            ))

    return SyncResult(
        ok=ok,
        install_kind=kind,
        steps=steps,
        message=msg,
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
