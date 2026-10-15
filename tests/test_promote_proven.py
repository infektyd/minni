"""Tests for Rootside-First Promotion (`scripts/promote-proven.sh` and `.githooks/pre-push`)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMOTE_SCRIPT = REPO_ROOT / "scripts" / "promote-proven.sh"
PRE_PUSH_HOOK = REPO_ROOT / ".githooks" / "pre-push"
CHECK_BOUNDARY = REPO_ROOT / "scripts" / "check-public-boundary.sh"
CHECK_MIGRATIONS = REPO_ROOT / "scripts" / "check-migrations.sh"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture()
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a hermetic git repository wired with promote-proven.sh and pre-push hook."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Operator")

    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    if PROMOTE_SCRIPT.exists():
        shutil.copy2(PROMOTE_SCRIPT, scripts_dir / "promote-proven.sh")
    if CHECK_BOUNDARY.exists():
        shutil.copy2(CHECK_BOUNDARY, scripts_dir / "check-public-boundary.sh")
    if CHECK_MIGRATIONS.exists():
        shutil.copy2(CHECK_MIGRATIONS, scripts_dir / "check-migrations.sh")

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    if PRE_PUSH_HOOK.exists():
        shutil.copy2(PRE_PUSH_HOOK, hooks_dir / "pre-push")

    migrations_dir = repo / "src" / "minni" / "migrations"
    migrations_dir.mkdir(parents=True)
    (migrations_dir / "001_init.sql").write_text("SELECT 1;\n", encoding="utf-8")

    # Fast deterministic Makefile with `check:` target that logs invocations and stamps
    makefile = repo / "Makefile"
    makefile.write_text(
        ".PHONY: check\n"
        "check:\n"
        "\t@echo 'ran-make-check' >> .make-check-runs\n"
        "\t@bash scripts/promote-proven.sh --stamp-only make-check\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".make-check-runs\n", encoding="utf-8")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")

    _git(repo, "add", ".gitignore", "README.md", "Makefile", "src/minni/migrations/001_init.sql")
    if (scripts_dir / "promote-proven.sh").exists():
        _git(repo, "add", "scripts/promote-proven.sh")
    if (scripts_dir / "check-public-boundary.sh").exists():
        _git(repo, "add", "scripts/check-public-boundary.sh")
    if (scripts_dir / "check-migrations.sh").exists():
        _git(repo, "add", "scripts/check-migrations.sh")
    if (hooks_dir / "pre-push").exists():
        _git(repo, "add", ".githooks/pre-push")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _run_script(
    repo: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    script = repo / "scripts" / "promote-proven.sh"
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )


def test_promote_script_exists_and_matches_global_copy() -> None:
    """scripts/promote-proven.sh must exist as a real executable file matching ~/.agents/scripts/promote-proven.sh."""
    assert PROMOTE_SCRIPT.exists(), "scripts/promote-proven.sh is missing"
    assert not PROMOTE_SCRIPT.is_symlink(), "scripts/promote-proven.sh must not be a symlink"
    mode = PROMOTE_SCRIPT.stat().st_mode
    assert bool(mode & stat.S_IXUSR), "scripts/promote-proven.sh must be executable"

    global_script = Path.home() / ".agents" / "scripts" / "promote-proven.sh"
    if global_script.exists():
        assert not global_script.is_symlink()
        assert PROMOTE_SCRIPT.read_bytes() == global_script.read_bytes()


def test_receipt_stamping_via_stamp_only_and_attest(temp_git_repo: Path) -> None:
    """--stamp-only and --attest create .git/.proven-shas with '<tree_sha> <commit_sha> <ts> <gate>'."""
    expected_tree = _git(temp_git_repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    expected_commit = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    receipt_path = temp_git_repo / ".git" / ".proven-shas"

    # 1. --stamp-only creates receipt
    res = _run_script(temp_git_repo, "--stamp-only", "unit-gate")
    assert res.returncode == 0, res.stderr
    assert receipt_path.is_file() and not receipt_path.is_symlink()
    lines = receipt_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parts = lines[0].split()
    assert len(parts) == 4
    assert parts[0] == expected_tree
    assert parts[1] == expected_commit
    assert int(parts[2]) > 1_700_000_000
    assert parts[3] == "unit-gate"

    # 2. --stamp-only on dirty tree silently exits 0 without updating or poisoning receipt
    receipt_path.unlink()
    (temp_git_repo / "README.md").write_text("# Dirty\n", encoding="utf-8")
    dirty_res = _run_script(temp_git_repo, "--stamp-only", "poison-gate")
    assert dirty_res.returncode == 0
    assert not receipt_path.exists()
    _git(temp_git_repo, "checkout", "--", "README.md")

    # 3. --attest runs the verification gate when receipt is missing and stamps .proven-shas
    attest_res = _run_script(
        temp_git_repo,
        "--attest",
        env_overrides={"PROMOTE_VERIFY_CMD": "true"},
    )
    assert attest_res.returncode == 0, attest_res.stderr
    assert receipt_path.is_file()
    parts2 = receipt_path.read_text(encoding="utf-8").strip().splitlines()[-1].split()
    assert parts2[0] == expected_tree
    assert parts2[1] == expected_commit
    assert parts2[3] == "custom-verify-cmd"

    # 4. --stamp-only and --check-receipt do not pathname-expand glob characters in gate_label
    glob_res = _run_script(temp_git_repo, "--stamp-only", "gate*?[a-z]")
    assert glob_res.returncode == 0, glob_res.stderr
    check_glob = _run_script(temp_git_repo, "--check-receipt")
    assert check_glob.returncode == 0, check_glob.stderr
    assert "gate=gate*?[a-z]" in check_glob.stdout


def test_check_receipt_happy_path_and_rejection_cases(temp_git_repo: Path) -> None:
    """--check-receipt exits 0 on fresh matching tree SHA and non-zero on missing/stale/changed/dirty/symlink."""
    receipt_path = temp_git_repo / ".git" / ".proven-shas"

    # Case A: missing .proven-shas -> non-zero
    missing_res = _run_script(temp_git_repo, "--check-receipt")
    assert missing_res.returncode != 0

    # Stamp a valid receipt -> --check-receipt exits 0 in <0.2s
    assert _run_script(temp_git_repo, "--stamp-only", "make-check").returncode == 0
    t0 = time.monotonic()
    ok_res = _run_script(temp_git_repo, "--check-receipt")
    elapsed = time.monotonic() - t0
    assert ok_res.returncode == 0, ok_res.stderr
    assert "Fresh rootside receipt verified" in ok_res.stdout
    assert elapsed < 0.5

    # Case B: HEAD^{tree} changes (new commit with different tree) -> non-zero
    (temp_git_repo / "README.md").write_text("# Updated tree\n", encoding="utf-8")
    _git(temp_git_repo, "add", "README.md")
    _git(temp_git_repo, "commit", "-m", "change tree")
    changed_res = _run_script(temp_git_repo, "--check-receipt")
    assert changed_res.returncode != 0

    # Re-stamp for the new tree and verify it passes
    assert _run_script(temp_git_repo, "--stamp-only", "make-check").returncode == 0
    assert _run_script(temp_git_repo, "--check-receipt").returncode == 0

    # Case C: receipt timestamp is older than PROVEN_TTL_SECONDS -> non-zero
    tree_sha = _git(temp_git_repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    commit_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    old_ts = int(time.time()) - 5
    receipt_path.write_text(f"{tree_sha} {commit_sha} {old_ts} make-check\n", encoding="utf-8")
    expired_res = _run_script(
        temp_git_repo,
        "--check-receipt",
        env_overrides={"PROVEN_TTL_SECONDS": "1"},
    )
    assert expired_res.returncode != 0

    # Restore fresh receipt
    assert _run_script(temp_git_repo, "--stamp-only", "make-check").returncode == 0

    # Case D1: tracked working tree dirty (unstaged modification) -> non-zero
    (temp_git_repo / "README.md").write_text("# Unstaged dirty\n", encoding="utf-8")
    dirty_unstaged = _run_script(temp_git_repo, "--check-receipt")
    assert dirty_unstaged.returncode != 0
    _git(temp_git_repo, "checkout", "--", "README.md")

    # Case D2: tracked working tree dirty (staged modification) -> non-zero
    (temp_git_repo / "README.md").write_text("# Staged dirty\n", encoding="utf-8")
    _git(temp_git_repo, "add", "README.md")
    dirty_staged = _run_script(temp_git_repo, "--check-receipt")
    assert dirty_staged.returncode != 0
    _git(temp_git_repo, "reset", "HEAD", "README.md")
    _git(temp_git_repo, "checkout", "--", "README.md")

    # Case E: .proven-shas is a symlink (TOCTOU/symlink guard rejects it) -> non-zero
    target_file = temp_git_repo / ".git" / "planted-target"
    target_file.write_text(receipt_path.read_text(encoding="utf-8"), encoding="utf-8")
    receipt_path.unlink()
    receipt_path.symlink_to(target_file)
    symlink_check = _run_script(temp_git_repo, "--check-receipt")
    assert symlink_check.returncode != 0
    assert "symlink" in symlink_check.stderr.lower()
    symlink_stamp = _run_script(temp_git_repo, "--stamp-only", "make-check")
    assert symlink_stamp.returncode != 0
    assert "symlink" in symlink_stamp.stderr.lower()


def test_forbidden_files_and_public_boundary_grep_fallback(temp_git_repo: Path) -> None:
    """check-public-boundary.sh works without rg (grep -E fallback) and forbidden-files scan blocks scratch files."""
    # Run check-public-boundary.sh with PATH restricted to /usr/bin:/bin (no /opt/homebrew/bin/rg)
    env_no_rg = os.environ.copy()
    env_no_rg["PATH"] = "/usr/bin:/bin"
    boundary_ok = subprocess.run(
        ["bash", str(temp_git_repo / "scripts" / "check-public-boundary.sh")],
        cwd=temp_git_repo,
        env=env_no_rg,
        text=True,
        capture_output=True,
    )
    assert boundary_ok.returncode == 0, boundary_ok.stderr

    # Add an allowed .env.example -> passes --check-receipt
    (temp_git_repo / ".env.example").write_text("FOO=bar\n", encoding="utf-8")
    _git(temp_git_repo, "add", ".env.example")
    _git(temp_git_repo, "commit", "-m", "add .env.example")
    assert _run_script(temp_git_repo, "--stamp-only", "make-check").returncode == 0
    assert _run_script(temp_git_repo, "--check-receipt", env_overrides={"PATH": "/usr/bin:/bin"}).returncode == 0

    # Add a forbidden scratch file pr_description.md -> --check-receipt fails even when stamped
    (temp_git_repo / "pr_description.md").write_text("scratch\n", encoding="utf-8")
    _git(temp_git_repo, "add", "pr_description.md")
    _git(temp_git_repo, "commit", "-m", "add forbidden pr_description.md")
    assert _run_script(temp_git_repo, "--stamp-only", "make-check").returncode == 0
    forbidden_res = _run_script(temp_git_repo, "--check-receipt")
    assert forbidden_res.returncode != 0
    assert "pr_description.md" in forbidden_res.stderr


def test_pre_push_hook_skips_make_check_on_fresh_receipt_and_runs_when_missing_or_expired(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    """pre-push skips duplicate make check (<1s) when tree is freshly stamped, and runs make check when unstamped/expired."""
    fake_home = tmp_path / "fake_home"
    global_hooks = fake_home / ".config" / "git" / "hooks"
    global_hooks.mkdir(parents=True)
    global_stdin_log = fake_home / "global_hook_stdin.log"
    global_pre_push = global_hooks / "pre-push"
    global_pre_push.write_text(
        "#!/usr/bin/env bash\n"
        f"cat > '{global_stdin_log}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    global_pre_push.chmod(0o755)

    head_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    tree_sha = _git(temp_git_repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    zero_sha = "0" * 40
    stdin_payload = f"refs/heads/main {head_sha} refs/heads/promote/{head_sha[:8]} {zero_sha}\n"

    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    # 1. Stamp fresh receipt -> pre-push skips make check in <1s and forwards stdin to global hook
    assert _run_script(temp_git_repo, "--stamp-only", "make-check", env_overrides={"HOME": str(fake_home)}).returncode == 0
    runs_file = temp_git_repo / ".make-check-runs"
    assert not runs_file.exists()

    t0 = time.monotonic()
    res_skip = subprocess.run(
        ["bash", str(temp_git_repo / ".githooks" / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=temp_git_repo,
        env=env,
        input=stdin_payload,
        text=True,
        capture_output=True,
    )
    elapsed_skip = time.monotonic() - t0
    combined_skip = res_skip.stdout + res_skip.stderr
    assert res_skip.returncode == 0, combined_skip
    assert "Fresh rootside receipt verified" in combined_skip
    assert elapsed_skip < 1.0
    assert not runs_file.exists(), "make check should NOT have run when a fresh receipt was present"
    assert global_stdin_log.read_text(encoding="utf-8") == stdin_payload

    # 2. Expired receipt -> pre-push runs make check and refreshes receipt
    receipt_path = temp_git_repo / ".git" / ".proven-shas"
    old_ts = int(time.time()) - 10
    receipt_path.write_text(f"{tree_sha} {head_sha} {old_ts} make-check\n", encoding="utf-8")
    env_expired = env.copy()
    env_expired["PROVEN_TTL_SECONDS"] = "1"
    res_expired = subprocess.run(
        ["bash", str(temp_git_repo / ".githooks" / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=temp_git_repo,
        env=env_expired,
        input=stdin_payload,
        text=True,
        capture_output=True,
    )
    assert res_expired.returncode == 0, res_expired.stdout + res_expired.stderr
    assert runs_file.exists() and runs_file.read_text(encoding="utf-8").splitlines() == ["ran-make-check"]

    # 3. Missing receipt -> pre-push runs make check and stamps .git/.proven-shas
    receipt_path.unlink()
    res_unstamped = subprocess.run(
        ["bash", str(temp_git_repo / ".githooks" / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=temp_git_repo,
        env=env,
        input=stdin_payload,
        text=True,
        capture_output=True,
    )
    assert res_unstamped.returncode == 0, res_unstamped.stdout + res_unstamped.stderr
    assert len(runs_file.read_text(encoding="utf-8").splitlines()) == 2
    assert receipt_path.is_file()
    assert tree_sha in receipt_path.read_text(encoding="utf-8")


def test_github_workflows_fast_pass_and_hygiene_guards() -> None:
    """Verify pr-hygiene.yml, ci.yml, and docker.yml wire the promote/* fast-pass and public boundary guards."""
    pr_hygiene = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "pr-hygiene.yml").read_text(encoding="utf-8"))
    ci_wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    docker_wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8"))

    hygiene_steps = pr_hygiene["jobs"]["forbidden-files"]["steps"]
    step_names = [s.get("name", "") for s in hygiene_steps]
    assert "Public boundary & migration guard" in step_names

    expected_if = "${{ !(github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository && startsWith(github.head_ref, 'promote/')) }}"
    assert ci_wf["jobs"]["cloud-smoke"].get("if") == expected_if
    assert docker_wf["jobs"]["build"].get("if") == expected_if
