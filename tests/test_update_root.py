"""scripts/update_root.sh refusal paths and dry-run.

The sync script's one hard promise is that it NEVER discards local state: a
dirty tree, a non-main branch, or local commits origin/main lacks all abort
before anything is touched. These tests run the real script against throwaway
git repos; the refusals fire before any install/deploy step, so nothing
outside the tmp dirs is reached.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "update_root.sh"

# These fixtures push to throwaway bare repos named `main`; a host-level git
# guard hook may block that in non-interactive runs. The bypass is scoped to
# the tmp repos these tests create — nothing here touches a real remote.
_ENV = {**os.environ, "GIT_BYPASS": "1"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True, env=_ENV,
    ).strip()


@pytest.fixture
def cloned(tmp_path):
    """(origin, clone): a bare origin with one commit on main, and a clone."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "one")
    subprocess.check_call(["git", "clone", "-q", "--bare", str(seed), str(origin)], env=_ENV)

    clone = tmp_path / "clone"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(clone)], env=_ENV)
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    return origin, clone


def _fake_launchctl(tmp_path: Path, *, loaded: bool) -> Path:
    """Stub launchctl so dry-run / daemon tests are host-independent."""
    bindir = tmp_path / ("launchctl-loaded" if loaded else "launchctl-missing")
    bindir.mkdir(exist_ok=True)
    path = bindir / "launchctl"
    if loaded:
        path.write_text(
            "#!/bin/sh\n"
            "# print: pretend agent is loaded; kickstart: no-op success\n"
            "exit 0\n",
            encoding="utf-8",
        )
    else:
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = print ]; then exit 1; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
    path.chmod(0o755)
    return bindir


def _run(
    repo: Path,
    *args: str,
    path_prefix: Path | None = None,
    lockdir: Path | None = None,
) -> subprocess.CompletedProcess:
    env = dict(_ENV)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    # Isolate the sync lock from the host machine and parallel tests.
    if lockdir is None:
        lockdir = repo.parent / f"sync-lock-{repo.name}"
    env["MINNI_SYNC_LOCKDIR"] = str(lockdir)
    # Dry-run now evaluates verify gates read-only; point them at an empty
    # home so host drift cannot fail the plan tests.
    empty_home = repo.parent / f"check-home-{repo.name}"
    empty_home.mkdir(exist_ok=True)
    env["MINNI_CHECK_VERSIONS_HOME"] = str(empty_home)
    env["MINNI_CHECK_DEPLOYMENTS_HOME"] = str(empty_home)
    # Hold installed layer at a known value (matches pyproject or any override).
    import re
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    can = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject, re.M)
    if can:
        env["MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE"] = can.group(1)
    return subprocess.run(
        ["bash", str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True, text=True, env=env,
    )


def test_refuses_dirty_tree(cloned):
    _origin, clone = cloned
    (clone / "f.txt").write_text("local edit\n", encoding="utf-8")
    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "dirty" in proc.stderr
    # Local state untouched.
    assert (clone / "f.txt").read_text(encoding="utf-8") == "local edit\n"


def test_refuses_diverged_branch(cloned):
    origin, clone = cloned
    # Advance origin/main from a second clone.
    other = origin.parent / "other"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(other)], env=_ENV)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "f.txt").write_text("remote two\n", encoding="utf-8")
    _git(other, "commit", "-aqm", "remote two")
    _git(other, "push", "-q", "origin", "main")
    # And give the clone its own local commit -> diverged.
    (clone / "g.txt").write_text("local\n", encoding="utf-8")
    _git(clone, "add", "g.txt")
    _git(clone, "commit", "-qm", "local only")
    local_head = _git(clone, "rev-parse", "HEAD")

    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "diverged" in proc.stderr
    assert _git(clone, "rev-parse", "HEAD") == local_head, "history must be untouched"


def test_refuses_non_main_branch(cloned):
    _origin, clone = cloned
    _git(clone, "checkout", "-q", "-b", "feature")
    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "feature" in proc.stderr


def test_dry_run_fast_forwards_nothing(cloned, tmp_path):
    origin, clone = cloned
    other = origin.parent / "other2"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(other)], env=_ENV)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "f.txt").write_text("remote two\n", encoding="utf-8")
    _git(other, "commit", "-aqm", "remote two")
    _git(other, "push", "-q", "origin", "main")
    before = _git(clone, "rev-parse", "HEAD")

    # Host-independent: pretend launchd agent is loaded so a clean dry-run
    # plan exits 0 (would kickstart, not "would fail").
    proc = _run(clone, "--dry-run", path_prefix=_fake_launchctl(tmp_path, loaded=True))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Round-1 finding: fetch must run even in dry-run (it never touches the
    # local branch/worktree) so the plan for a BEHIND clone truthfully shows
    # the merge step instead of "already at origin/main" off stale refs.
    assert "would run: git merge --ff-only origin/main" in proc.stdout
    assert "already at origin/main" not in proc.stdout
    assert _git(clone, "rev-parse", "HEAD") == before, "dry run must not move HEAD"
    remote_ref = _git(clone, "rev-parse", "origin/main")
    assert remote_ref != before, "fetch must have updated the remote-tracking ref"
    # D7: redeploy plan must wire the fleet + explicit antigravity/cursor,
    # not a bulk propagate --platform all (sync-root uses explicit targets).
    assert "wire all" in proc.stdout
    assert "--prune" in proc.stdout
    assert "update-plugin --platform antigravity" in proc.stdout
    assert "update-plugin --platform cursor" in proc.stdout
    assert "update-plugin --platform all" not in proc.stdout


def test_dry_run_exits_nonzero_when_daemon_would_not_restart(cloned, tmp_path):
    """Round-3 Med: dry-run must not exit 0 when the plan would refuse at
    daemon restart (launchd agent not loaded)."""
    _origin, clone = cloned
    proc = _run(clone, "--dry-run", path_prefix=_fake_launchctl(tmp_path, loaded=False))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "would fail" in combined
    assert "sync complete" not in combined


def test_refuses_when_another_sync_holds_the_lock(cloned, tmp_path):
    """Round-4 Low: concurrent sync-root must not interleave writers."""
    _origin, clone = cloned
    lockdir = tmp_path / "held-lock"
    lockdir.mkdir()
    (lockdir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    proc = _run(clone, "--dry-run", lockdir=lockdir,
                path_prefix=_fake_launchctl(tmp_path, loaded=True))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "already running" in (proc.stdout + proc.stderr)


def test_reclaims_stale_lock_from_dead_pid(cloned, tmp_path):
    """Round-5 Med: a SIGKILL'd run must not freeze all future syncs."""
    _origin, clone = cloned
    lockdir = tmp_path / "stale-lock"
    lockdir.mkdir()
    (lockdir / "pid").write_text("999999999", encoding="utf-8")  # not a live pid
    proc = _run(clone, "--dry-run", lockdir=lockdir,
                path_prefix=_fake_launchctl(tmp_path, loaded=True))
    # Gets past the lock gate (may still fail later for other dry-run reasons).
    assert "already running" not in (proc.stdout + proc.stderr)
    assert "reclaiming stale sync lock" in (proc.stdout + proc.stderr)


def test_refuses_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _run(plain)
    assert proc.returncode == 1
    assert "REFUSING" in proc.stderr
    assert "not a git checkout" in proc.stderr


def test_accepts_worktree_git_file(cloned, tmp_path):
    """Round-2 Med: a linked worktree has `.git` as a *file* (gitdir: …).
    `-d .git` alone refused every worktree as 'not a git checkout'."""
    _origin, clone = cloned
    wt = tmp_path / "linked-wt"
    # Detached HEAD worktree: main is already checked out in `clone`.
    head = _git(clone, "rev-parse", "HEAD")
    _git(clone, "worktree", "add", "--detach", str(wt), head)
    assert (wt / ".git").is_file(), "fixture must be a linked worktree"
    # Dry-run only: real run would refuse non-main / non-launchd. We only
    # need to prove the script gets past the git-checkout gate.
    proc = _run(wt, "--dry-run")
    combined = proc.stdout + proc.stderr
    assert "not a git checkout" not in combined, combined
    assert "is not a git checkout" not in combined
    # Detached HEAD is not main — refusal after the git gate is expected.
    assert "REFUSING" in proc.stderr or "not main" in combined


def test_grok_hooks_root_selection_prefers_platform_grok():
    """Partial wire all can leave codex on a newer root than grok; hooks must
    stamp the *grok* install root, not global max wired_at."""
    from pathlib import Path

    text = SCRIPT.read_text(encoding="utf-8")
    assert 'str(w.get("platform") or "") == "grok"' in text
    assert "Prefer the latest *grok*" in text

    # Mirror the selection logic used by the embedded Python block.
    wires = [
        {"platform": "claude-code", "install_root": "/tmp/V2-claude",
         "wired_at": "2026-08-02T02:00:00Z"},
        {"platform": "grok", "install_root": "/tmp/V1-grok",
         "wired_at": "2026-08-02T01:00:00Z"},
        {"platform": "grok", "install_root": "/tmp/V2-grok",
         "wired_at": "2026-08-02T01:30:00Z"},
    ]
    grok_entries = [
        (str(w.get("wired_at") or ""), Path(str(w.get("install_root"))))
        for w in wires
        if str(w.get("platform") or "") == "grok" and w.get("install_root")
    ]
    root = max(grok_entries, key=lambda t: t[0])[1]
    assert root == Path("/tmp/V2-grok")
    # Global-max would wrongly pick V2-claude:
    global_entries = [
        (str(w.get("wired_at") or ""), Path(str(w.get("install_root"))))
        for w in wires if w.get("install_root")
    ]
    assert max(global_entries, key=lambda t: t[0])[1] == Path("/tmp/V2-claude")


def test_deploy_probe_hard_fails_null_stale_for_editable():
    """Post-kickstart probe: stale=null is soft only for wheel installs."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'install_kind") == "wheel"' in text
    assert "expected boolean for editable checkout" in text
    assert "sys.exit(3)" in text


def test_kickstart_failure_not_misdiagnosed_as_not_loaded():
    """Kickstart failure and agent-not-loaded must use distinct refuse text."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DAEMON_NOT_LOADED" in text
    assert "DAEMON_RESTART_FAILED" in text
    assert "launchctl kickstart failed (agent" in text
    assert "DAEMON_MISSING" not in text or text.count("DAEMON_MISSING") == 0
