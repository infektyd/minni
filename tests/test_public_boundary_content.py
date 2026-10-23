"""Fixture tests for the content scan in ``scripts/check-public-content.sh``.

Each test builds a throwaway git repo, copies the scan script in (plus an
allowlist when the test needs one), stages fixture files and runs the script.

Every secret-shaped fixture is assembled at runtime from fragments, so this
file's own source never matches a pattern and the real scan of the public
tree stays clean without an allowlist entry for it.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-public-content.sh"
BOUNDARY_SCRIPT = REPO_ROOT / "scripts" / "check-public-boundary.sh"
# sha256 of scripts/check-public-boundary.sh as it is on main (52a14346): the
# path-only script that the push and receipt fast paths run.
MAIN_BOUNDARY_SHA256 = "dc14332b4fd8bc9c0c833582bb2f6781c1fec7cd192df47acfc4180272c2fc88"
BASH = shutil.which("bash") or "/bin/bash"


def _j(*parts: str) -> str:
    return "".join(parts)


# One sample per pattern alternative. Every sample must trip exactly the class
# it is listed under.
SAMPLES: list[tuple[str, str]] = [
    ("home-path", _j("/Us", "ers/", "zqfixture", "/notes.txt")),
    ("home-path", _j("/ho", "me/", "zqfixture", "/notes.txt")),
    ("home-path", _j("/us", "ers/", "zqfixture", "/notes.txt")),
    ("home-path", _j("/HO", "ME/", "zqfixture", "/notes.txt")),
    ("vault-deep-path", _j("~/.mi", "nni/", "codex-va", "ult/", "wiki/note.md")),
    ("vault-deep-path", _j("$HO", "ME/.mi", "nni/", "codex-va", "ult/", "wiki/note.md")),
    ("vault-deep-path", _j("${HO", "ME}/.mi", "nni/", "codex-va", "ult/", "wiki/note.md")),
    ("vault-deep-path", _j("/ho", "me/", "someuser", "/.mi", "nni/", "codex-va", "ult/", "wiki/note.md")),
    ("vault-deep-path", _j("/Us", "ers/", "someuser", "/.mi", "nni/", "codex-va", "ult/", "inbox/x.json")),
    ("plan-id", _j("pl", "an-", "0123456789abcdef")),
    ("token-assignment", _j("MY_API", "_KEY", " = ", "zq", "Q" * 10)),
    ("token-assignment", _j("MY_PASS", 'WORD = "', "zq", "Q" * 10, '"')),
    ("token-assignment", _j("APP_SEC", "RET='", "zq", "Q" * 10, "'")),
    ("token-sk", _j("s", "k-proj-", "Q" * 24)),
    ("token-sk", _j("s", "k-svcacct-", "Q" * 24)),
    ("token-sk", _j("s", "k-admin-", "Q" * 24)),
    ("token-anthropic", _j("s", "k-ant-", "zq", "Q" * 12)),
    ("token-sk", _j("s", "k-", "Q" * 24)),
    ("token-github", _j("gh", "p_", "Q" * 24)),
    ("token-github", _j("gh", "o_", "Q" * 24)),
    ("token-github", _j("gh", "s_", "Q" * 24)),
    ("token-github", _j("git", "hub_pat_", "Q" * 12)),
    ("token-slack", _j("xo", "xb-", "Q" * 12)),
    ("token-google", _j("AI", "za", "Q" * 24)),
    ("token-aws", _j("AK", "IA", "Q" * 16)),
    ("token-aws", _j("AS", "IA", "Q" * 16)),
    ("token-gitlab", _j("gl", "pat-", "Q" * 12)),
    ("token-digitalocean", _j("do", "p_v1_", "Q" * 12)),
    ("token-huggingface", _j("h", "f_", "Q" * 24)),
    ("token-replicate", _j("r", "8_", "Q" * 24)),
    ("token-stripe", _j("p", "k_live_", "Q" * 12)),
    ("token-stripe", _j("s", "k_live_", "Q" * 12)),
    ("private-key", _j("-----BEGIN ", "EC PRIV", "ATE KEY-----")),
]

def _is_secret_class(cls: str) -> bool:
    return cls.startswith("token-") or cls == "private-key"


def _hit(path: str, line: int, cls: str) -> str:
    """Start of a reported hit. Secret classes print no excerpt, only the length."""
    return f"{path}:{line}: {cls} (" if _is_secret_class(cls) else f"{path}:{line}: {cls}: "


# (class, public prefix, match length as a function of the random body length)
TOKEN_PREFIXES: list[tuple[str, str, Callable[[int], int]]] = [
    ("token-assignment", _j("MY_API", "_KEY = "), lambda n: len("_API_KEY = ") + n),
    ("token-assignment", _j("MY_PASS", 'WORD = "'), lambda n: len("PASS" "WORD = '") + n),
    ("token-anthropic", _j("s", "k-ant-"), lambda n: 7),
    ("token-sk", _j("s", "k-"), lambda n: 3 + n),
    ("token-sk", _j("s", "k-proj-"), lambda n: 8 + n),
    ("token-github", _j("gh", "p_"), lambda n: 4 + n),
    ("token-slack", _j("xo", "xb-"), lambda n: 5),
    ("token-google", _j("AI", "za"), lambda n: 4 + n),
    ("token-aws", _j("AK", "IA"), lambda n: 4 + n),
    ("token-gitlab", _j("gl", "pat-"), lambda n: 6),
    ("token-digitalocean", _j("do", "p_v1_"), lambda n: 7),
    ("token-huggingface", _j("h", "f_"), lambda n: 3 + n),
    ("token-replicate", _j("r", "8_"), lambda n: 3 + n),
    ("token-stripe", _j("s", "k_live_"), lambda n: 8),
]

# Random bodies use uppercase letters that never occur three in a row in the
# scan's own fixed output text, so a 3-character coincidence cannot flake.
BODY_ALPHABET = "GHJKLMNPQRSUVWXYZ"


def _random_body() -> str:
    length = 32 + secrets.randbelow(9)
    return "".join(secrets.choice(BODY_ALPHABET) for _ in range(length))


NEUTRAL_NAMES = [
    "someuser", "user", "username", "me", "you", "example", "test", "alice", "bob", "x", "<user>", "$USER",
    "<operator>", "<name>", "REPLACE_WITH_YOUR_USERNAME",
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(
    tmp_path: Path,
    files: dict[str, str | bytes],
    allow: list[tuple[str, str, str, str]] | None = None,
    script: Path = SCRIPT,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copy2(script, repo / "scripts" / "check-public-content.sh")
    shutil.copy2(BOUNDARY_SCRIPT, repo / "scripts" / "check-public-boundary.sh")
    paths = ["scripts/check-public-content.sh", "scripts/check-public-boundary.sh"]
    if allow is not None:
        lines = ["# test allowlist"] + ["\t".join(entry) for entry in allow]
        (repo / "scripts" / "public-boundary-allow.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append("scripts/public-boundary-allow.txt")
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        paths.append(rel)
    _git(repo, "add", "--", *paths)
    return repo


def _scan(
    repo: Path,
    env: dict[str, str] | None = None,
    script_name: str = "check-public-content.sh",
    args: tuple[str, ...] = (),
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.pop("MINNI_BOUNDARY_FORBIDDEN_NAMES", None)
    run_env.pop("GITHUB_ACTIONS", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [BASH, str(repo / "scripts" / script_name), *args],
        cwd=repo,
        env=run_env,
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes_including_the_script_itself(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/readme.md": "nothing private here\n"})
    result = _scan(repo)
    assert result.returncode == 0, result.stderr
    assert "Public-content check passed." in result.stdout


def test_boundary_script_is_mains_path_only_script_and_skips_content(tmp_path: Path) -> None:
    # The push and receipt fast paths run check-public-boundary.sh; it must stay
    # main's path-only script, byte for byte, and never run the content scan.
    assert hashlib.sha256(BOUNDARY_SCRIPT.read_bytes()).hexdigest() == MAIN_BOUNDARY_SHA256
    plan = _j("pl", "an-", "0123456789abcdef")
    repo = _make_repo(tmp_path, {"docs/a.md": f"{plan}\n"})
    boundary = _scan(repo, script_name="check-public-boundary.sh")
    assert boundary.returncode == 0, boundary.stderr
    assert "Public-boundary check passed." in boundary.stdout
    assert "Content check" not in boundary.stdout + boundary.stderr
    content = _scan(repo)
    assert content.returncode == 1
    assert "docs/a.md:1: plan-id: " in content.stderr


def test_scanner_sources_do_not_trip_the_scan(tmp_path: Path) -> None:
    # The script is tracked by _make_repo. The real allowlist and this test
    # file are tracked as plain content under other names, so they are scanned
    # but not used as an allowlist.
    files = {
        "docs/allow-copy.txt": (REPO_ROOT / "scripts" / "public-boundary-allow.txt").read_text(encoding="utf-8"),
        "docs/test-copy.py": Path(__file__).read_text(encoding="utf-8"),
    }
    result = _scan(_make_repo(tmp_path, files))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("cls", "sample"), SAMPLES, ids=[f"{c}-{i}" for i, (c, _) in enumerate(SAMPLES)])
def test_each_pattern_class_is_caught_and_never_printed_in_full(tmp_path: Path, cls: str, sample: str) -> None:
    repo = _make_repo(tmp_path, {"docs/leak.md": f"line one\nsee {sample} here\n"})
    result = _scan(repo)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert _hit("docs/leak.md", 2, cls) in result.stderr
    assert f"  {cls}: 1" in result.stderr
    assert sample not in output
    assert sample[-8:] not in output


def _assert_secret_body_absent(output: str, body: str) -> None:
    leaked = [body[i : i + 3] for i in range(len(body) - 2) if body[i : i + 3] in output]
    assert not leaked, f"{len(leaked)} 3-char pieces of the secret body appear in the output"


@pytest.mark.parametrize(("cls", "prefix", "match_len"), TOKEN_PREFIXES, ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(TOKEN_PREFIXES)])
def test_token_classes_print_no_excerpt_of_a_random_body(
    tmp_path: Path, cls: str, prefix: str, match_len: Callable[[int], int]
) -> None:
    body = _random_body()
    repo = _make_repo(tmp_path, {"docs/leak.md": f"line one\nsee {prefix}{body} here\n"})
    result = _scan(repo)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"docs/leak.md:2: {cls} ({match_len(len(body))} chars)" in result.stderr.splitlines()
    assert f"  {cls}: 1" in result.stderr
    _assert_secret_body_absent(output, body)
    assert prefix + body[:1] not in output


def test_private_key_prints_no_excerpt_of_a_random_body(tmp_path: Path) -> None:
    header = _j("-----BEGIN ", "RSA PRIV", "ATE KEY-----")
    body = _random_body()
    repo = _make_repo(tmp_path, {"docs/key.txt": f"{header}\n{body}\n"})
    result = _scan(repo)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"docs/key.txt:1: private-key ({len(header)} chars)" in result.stderr.splitlines()
    assert "  private-key: 1" in result.stderr
    _assert_secret_body_absent(output, body)


def test_vault_root_layout_form_passes(tmp_path: Path) -> None:
    root = _j("~/.mi", "nni/")
    home_root = _j("$HO", "ME/.mi", "nni/")
    braced_root = _j("${HO", "ME}/.mi", "nni/")
    abs_root = _j("/ho", "me/", "someuser", "/.mi", "nni/")
    text = (
        f"Each agent gets {root}<agent>-vault as its vault.\n"
        f"Codex uses `{root}codex-vault` and Claude Code uses {root}claudecode-vault/\n"
        f"(see `{root}grok-vault/`).\n"
        f"Also {home_root}codex-vault, `{braced_root}<agent>-vault/` and {abs_root}codex-vault\n"
    )
    repo = _make_repo(tmp_path, {"docs/layout.md": text})
    result = _scan(repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", NEUTRAL_NAMES)
def test_neutral_home_names_pass(tmp_path: Path, name: str) -> None:
    text = _j("/Us", "ers/", name, "/x.txt and /ho", "me/", name, "/y.txt and /us", "ers/", name, "/z\n")
    repo = _make_repo(tmp_path, {"docs/fixture.md": text})
    result = _scan(repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["someuser2", "Test", "me,you", "users"])
def test_near_neutral_home_names_fail(tmp_path: Path, name: str) -> None:
    repo = _make_repo(tmp_path, {"docs/fixture.md": _j("/Us", "ers/", name, "/x.txt\n")})
    result = _scan(repo)
    assert result.returncode == 1, result.stderr
    assert "docs/fixture.md:1: home-path: " in result.stderr


def test_allowlisted_line_passes_and_same_text_elsewhere_fails(tmp_path: Path) -> None:
    sample = _j("gh", "p_", "Q" * 24)
    allow = [("tests/fixture.py", "token-github", "FAKE_[G]H = \"[g]hp_", "synthetic token in a redaction test")]
    files = {
        "tests/fixture.py": f'FAKE_GH = "{sample}"\n',
        "docs/notes.md": f'FAKE_GH = "{sample}"\n',
    }
    result = _scan(_make_repo(tmp_path, files, allow=allow))
    assert result.returncode == 1, result.stderr
    assert _hit("docs/notes.md", 1, "token-github") in result.stderr
    assert "tests/fixture.py" not in result.stderr

    only_allowed = tmp_path / "second"
    only_allowed.mkdir()
    result = _scan(_make_repo(only_allowed, {"tests/fixture.py": files["tests/fixture.py"]}, allow=allow))
    assert result.returncode == 0, result.stderr


def test_allowlist_entry_covers_only_the_match_it_overlaps_on_its_line(tmp_path: Path) -> None:
    runner = _j("/ho", "me/", "runner", "/work")
    other = _j("/Us", "ers/", "zqother", "/secret/notes")
    allow = [("docs/a.md", "home-path", "/[h]ome/runner/", "CI runner example")]
    result = _scan(_make_repo(tmp_path, {"docs/a.md": f"see {runner} and also {other}\n"}, allow=allow))
    assert result.returncode == 1, result.stderr
    assert "docs/a.md:1: home-path: /Use... " in result.stderr
    assert "docs/a.md:1: home-path: /hom" not in result.stderr
    assert "unused allowlist entry" not in result.stderr


def test_allowlist_entry_never_covers_another_line_of_its_file(tmp_path: Path) -> None:
    # Kills a mutation that lets path + class exempt the whole file.
    tok1 = _j("gh", "p_", "Q" * 24)
    tok2 = _j("gh", "p_", "R" * 24)
    home1 = _j("/ho", "me/", "runner", "/work")
    home2 = _j("/ho", "me/", "zqother", "/work")
    files = {
        "tests/fixture.py": f'FAKE_GH = "{tok1}"\nREAL_GH = "{tok2}"\n',
        "docs/a.md": f"see {home1}\nsee {home2}\n",
    }
    allow = [
        ("tests/fixture.py", "token-github", "FAKE_[G]H = \"[g]hp_", "synthetic token"),
        ("docs/a.md", "home-path", "/[h]ome/runner/", "CI runner example"),
    ]
    result = _scan(_make_repo(tmp_path, files, allow=allow))
    assert result.returncode == 1, result.stderr
    assert _hit("tests/fixture.py", 2, "token-github") in result.stderr
    assert _hit("tests/fixture.py", 1, "token-github") not in result.stderr
    assert "docs/a.md:2: home-path: " in result.stderr
    assert "docs/a.md:1: home-path: " not in result.stderr
    assert "unused allowlist entry" not in result.stderr


def _line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def test_line_sha256_entry_exempts_only_its_exact_line(tmp_path: Path) -> None:
    # The reviewer's probe: a name-wildcard ERE would exempt any username in
    # this context once the forbidden layer is inert (CI). A pin cannot.
    pinned = _j('summary: "Implemented under /Us', "ers/", "zqpinned", '/Projects/Minni "')
    intruder = _j('summary: "Implemented under /Us', "ers/", "zqintruder", '/Projects/Minni "')
    allow = [("tests/b.mjs", "home-path", "line-sha256:" + _line_sha(pinned), "pinned fixture line")]
    repo = _make_repo(tmp_path, {"tests/b.mjs": f"{pinned}\n{intruder}\n"}, allow=allow)
    for env in ({}, {"GITHUB_ACTIONS": "true"}):
        result = _scan(repo, env=env)
        assert result.returncode == 1, result.stderr
        assert "tests/b.mjs:2: home-path: " in result.stderr
        assert "tests/b.mjs:1: home-path: " not in result.stderr
        assert "unused allowlist entry" not in result.stderr

    only_dir = tmp_path / "only"
    only_dir.mkdir()
    only = _make_repo(only_dir, {"tests/b.mjs": f"{pinned}\n"}, allow=allow)
    assert _scan(only, env={"GITHUB_ACTIONS": "true"}).returncode == 0


def test_line_sha256_entry_stops_matching_when_the_line_changes(tmp_path: Path) -> None:
    pinned = _j("see /Us", "ers/", "zqpinned", "/x")
    allow = [("docs/a.md", "home-path", "line-sha256:" + _line_sha(pinned), "pinned fixture line")]
    result = _scan(_make_repo(tmp_path, {"docs/a.md": pinned + " \n"}, allow=allow))
    assert result.returncode == 1, result.stderr
    assert "docs/a.md:1: home-path: " in result.stderr
    assert "scripts/public-boundary-allow.txt:2: unused allowlist entry (docs/a.md, home-path)" in result.stderr


def test_line_sha256_needs_a_hash_tool_or_fails_closed(tmp_path: Path) -> None:
    sandbox = tmp_path / "bin"
    sandbox.mkdir()
    for tool in ["bash", "git", "grep", "sed", "tr", "id", "mktemp", "rm", "cat"]:
        found = shutil.which(tool)
        assert found, tool
        (sandbox / tool).symlink_to(found)
    for tool in ["sha256sum", "shasum", "openssl"]:
        assert shutil.which(tool, path=str(sandbox)) is None
    allow = [("docs/a.md", "plan-id", "line-sha256:" + "0" * 64, "pinned")]
    result = _scan(_make_repo(tmp_path, {"docs/a.md": "clean\n"}, allow=allow), env={"PATH": str(sandbox)})
    assert result.returncode == 2, result.stderr
    assert "line-sha256 entries need sha256sum, shasum or openssl" in result.stderr


def test_allowlist_is_scoped_to_its_class(tmp_path: Path) -> None:
    sample = _j("gh", "p_", "Q" * 24)
    allow = [("tests/fixture.py", "token-sk", "FAKE_[G]H = \"[g]hp_", "wrong class on purpose")]
    result = _scan(_make_repo(tmp_path, {"tests/fixture.py": f'FAKE_GH = "{sample}"\n'}, allow=allow))
    assert result.returncode == 1
    assert _hit("tests/fixture.py", 1, "token-github") in result.stderr
    assert "unused allowlist entry" in result.stderr


def test_unused_allowlist_entry_fails(tmp_path: Path) -> None:
    allow = [("docs/gone.md", "plan-id", "anything", "stale entry")]
    result = _scan(_make_repo(tmp_path, {"docs/readme.md": "clean\n"}, allow=allow))
    assert result.returncode == 1
    assert "scripts/public-boundary-allow.txt:2: unused allowlist entry (docs/gone.md, plan-id)" in result.stderr


@pytest.mark.parametrize(
    "entry",
    [
        ("docs/a.md", "plan-id", "x"),
        ("docs/a.md", "no-such-class", "x", "reason"),
        ("docs/a.md", "plan-id", "(", "reason"),
        ("docs/a.md", "plan-id", "x*", "matches an empty line"),
        ("docs/a.md", "plan-id", "line-sha256:abc123", "short hash"),
        ("docs/a.md", "plan-id", "line-sha256:" + "A" * 64, "uppercase hash"),
    ],
)
def test_malformed_allowlist_entry_fails_closed(tmp_path: Path, entry: tuple[str, ...]) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"}, allow=[entry])  # type: ignore[list-item]
    result = _scan(repo)
    assert result.returncode == 2, result.stderr
    assert "Content check error" in result.stderr


def test_forbidden_name_overrides_allowlist_and_neutral_set(tmp_path: Path) -> None:
    placeholder = "zq" + secrets.token_hex(6)
    # The placeholder is random per run, so no tracked file may contain it.
    in_repo = subprocess.run(
        ["git", "grep", "--cached", "-q", "-F", "-e", placeholder],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert in_repo.returncode == 1

    line = _j("/Us", "ers/", placeholder.upper(), "/notes.txt\n")
    allow = [("docs/a.md", "home-path", f"/[U]sers/{placeholder.upper()}/", "would exempt it")]
    repo = _make_repo(tmp_path, {"docs/a.md": line}, allow=allow)

    unforced = _scan(repo)
    assert unforced.returncode == 0, unforced.stderr

    forced = _scan(repo, env={"MINNI_BOUNDARY_FORBIDDEN_NAMES": f"someone-else, {placeholder}"})
    output = forced.stdout + forced.stderr
    assert forced.returncode == 1, output
    assert "docs/a.md:1: home-path: " in forced.stderr
    assert "forbidden name" in forced.stderr
    assert placeholder.lower() not in output.lower()

    neutral_repo_dir = tmp_path / "neutral"
    neutral_repo_dir.mkdir()
    neutral_repo = _make_repo(neutral_repo_dir, {"docs/a.md": _j("/ho", "me/", "test", "/x\n")})
    assert _scan(neutral_repo).returncode == 0
    assert _scan(neutral_repo, env={"MINNI_BOUNDARY_FORBIDDEN_NAMES": "test"}).returncode == 1


def test_os_user_is_forced_locally_but_not_under_github_actions(tmp_path: Path) -> None:
    os_user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    if not os_user or "/" in os_user or any(ch.isspace() for ch in os_user):
        pytest.skip("no usable OS user name")
    line = _j("/Us", "ers/", os_user, "/notes.txt\n")
    allow = [("docs/a.md", "home-path", f"/[U]sers/{os_user}/notes", "allowlist entry the OS-user rule must override")]
    repo = _make_repo(tmp_path, {"docs/a.md": line}, allow=allow)

    local = _scan(repo)
    assert local.returncode == 1, local.stderr
    assert "docs/a.md:1: home-path: " in local.stderr
    assert "forbidden name" in local.stderr
    assert os_user not in local.stdout + local.stderr

    ci = _scan(repo, env={"GITHUB_ACTIONS": "true"})
    assert ci.returncode == 0, ci.stderr

    # The env-var source still applies under GitHub Actions.
    ci_env = _scan(repo, env={"GITHUB_ACTIONS": "true", "MINNI_BOUNDARY_FORBIDDEN_NAMES": os_user})
    assert ci_env.returncode == 1, ci_env.stderr
    assert "forbidden name" in ci_env.stderr


def _fake_git(tmp_path: Path, version_line: str) -> str:
    real_git = shutil.which("git")
    assert real_git
    fake_dir = tmp_path / "fakegit"
    fake_dir.mkdir()
    fake = fake_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n'
        'for a in "$@"; do case "$a" in --attr-source=*) echo "unknown option: $a" >&2; exit 129;; esac; done\n'
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def test_old_git_fails_closed_with_a_clear_message(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    result = _scan(repo, env={"PATH": _fake_git(tmp_path, "git version 2.39.5")})
    assert result.returncode == 2, result.stderr
    assert "git version 2.39.5 is older than 2.42" in result.stderr
    assert "failing closed" in result.stderr
    assert "Public-content check passed." not in result.stdout


def test_git_without_attr_source_fails_closed_even_when_version_looks_new(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    result = _scan(repo, env={"PATH": _fake_git(tmp_path, "git version 2.99.0.custom")})
    assert result.returncode == 2, result.stderr
    assert "does not support --attr-source" in result.stderr
    assert "failing closed" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        _j("ENV HOME=/ho", "me/", "zqfixture"),
        _j("mount at /Us", "ers/", "zqfixture", " now"),
        _j('real_home = "/Us', "ers/", "zqfixture", '"'),
        _j("in `/ho", "me/", "zqfixture", "`"),
        _j("path: /Us", "ers/", "zqfixture", ": gone"),
        _j("(see /ho", "me/", "zqfixture", ")"),
        _j("tab\t/Us", "ers/", "zqfixture", "\tthen"),
    ],
)
def test_bare_home_dir_without_trailing_slash_is_caught(tmp_path: Path, line: str) -> None:
    result = _scan(_make_repo(tmp_path, {"docs/a.md": line + "\n"}))
    assert result.returncode == 1, result.stderr
    assert "docs/a.md:1: home-path: " in result.stderr


def test_bare_home_dir_neutral_and_forbidden_names(tmp_path: Path) -> None:
    neutral = _j('see "/Us', "ers/", "someuser", '" and /ho', "me/", "<operator>", "\n")
    assert _scan(_make_repo(tmp_path, {"docs/a.md": neutral})).returncode == 0

    forbidden_dir = tmp_path / "forbidden"
    forbidden_dir.mkdir()
    line = _j("cd /Us", "ers/", "ZQPinned", "\n")
    allow = [("docs/a.md", "home-path", "cd /[U]sers/ZQPinned$", "would exempt it")]
    repo = _make_repo(forbidden_dir, {"docs/a.md": line}, allow=allow)
    assert _scan(repo).returncode == 0
    forced = _scan(repo, env={"MINNI_BOUNDARY_FORBIDDEN_NAMES": "zqpinned"})
    assert forced.returncode == 1
    assert "forbidden name" in forced.stderr


def test_comma_name_never_matches_the_forbidden_separators(tmp_path: Path) -> None:
    # A docstring that lists bare directories (the home dir, then a comma)
    # yields the name ",".
    line = _j('"""/ho', "me/, /opt/, /root/ absolute paths"'"""', "\n")
    allow = [("docs/a.py", "home-path", '"""/[h]ome/, /opt/', "directory list, not a username")]
    repo = _make_repo(tmp_path, {"docs/a.py": line}, allow=allow)
    for env in ({}, {"GITHUB_ACTIONS": "true"}, {"GITHUB_ACTIONS": "true", "MINNI_BOUNDARY_FORBIDDEN_NAMES": ""}):
        result = _scan(repo, env=env)
        assert result.returncode == 0, (env, result.stderr)


def test_binary_files_are_skipped_but_attributes_cannot_hide_text(tmp_path: Path) -> None:
    sample = _j("AK", "IA", "Q" * 16)
    files: dict[str, str | bytes] = {
        "assets/blob.bin": b"\x00\x01binary\x00" + sample.encode() + b"\n",
        ".gitattributes": "hidden.txt -diff\n*.csv binary\n",
        "data/hidden.txt": f"{sample}\n",
        "data/table.csv": f"a,b\n1,{sample}\n",
    }
    result = _scan(_make_repo(tmp_path, files))
    assert result.returncode == 1, result.stderr
    assert "assets/blob.bin" not in result.stderr
    assert _hit("data/hidden.txt", 1, "token-aws") in result.stderr
    assert _hit("data/table.csv", 2, "token-aws") in result.stderr


def test_odd_paths_and_non_utf8_bytes_are_reported_exactly(tmp_path: Path) -> None:
    plan = _j("pl", "an-", "0123456789abcdef")
    files: dict[str, str | bytes] = {
        "docs/a b:c ü.md": f"id {plan}\n",
        "docs/latin1.txt": b"caf\xe9 " + plan.encode() + b"\n",
        "docs/crlf.txt": f"x\r\ncrlf {plan}\r\n",
    }
    result = _scan(_make_repo(tmp_path, files))
    assert result.returncode == 1, result.stderr
    assert "docs/a b:c ü.md:1: plan-id: " in result.stderr
    assert "docs/latin1.txt:1: plan-id: " in result.stderr
    assert "docs/crlf.txt:2: plan-id: " in result.stderr


def test_newline_in_a_tracked_path_fails_closed(tmp_path: Path) -> None:
    plan = _j("pl", "an-", "0123456789abcdef")
    result = _scan(_make_repo(tmp_path, {"docs/new\nline.md": f"{plan}\n"}))
    assert result.returncode == 2, result.stderr
    assert "unparseable" in result.stderr


def test_symlink_targets_are_scanned(tmp_path: Path) -> None:
    home_target = _j("/Us", "ers/", "zqfixture", "/notes.txt")
    repo = _make_repo(tmp_path, {"docs/readme.md": "clean\n"})
    (repo / "docs" / "to-home").symlink_to(home_target)
    (repo / "docs" / "to-readme").symlink_to("readme.md")
    (repo / "docs" / "to-neutral").symlink_to(_j("/ho", "me/", "someuser", "/x"))
    _git(repo, "add", "--", "docs/to-home", "docs/to-readme", "docs/to-neutral")
    result = _scan(repo)
    assert result.returncode == 1, result.stderr
    assert "docs/to-home:1: home-path: " in result.stderr
    assert "to-readme" not in result.stderr
    assert "to-neutral" not in result.stderr
    assert home_target not in result.stdout + result.stderr

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo2 = _make_repo(
        allowed,
        {"docs/readme.md": "clean\n"},
        allow=[("docs/to-home", "home-path", "^/[U]sers/zqfixture/notes\\.txt$", "fixture symlink")],
    )
    (repo2 / "docs" / "to-home").symlink_to(home_target)
    _git(repo2, "add", "--", "docs/to-home")
    result2 = _scan(repo2)
    assert result2.returncode == 0, result2.stderr


def _sandbox_path_without_rg(tmp_path: Path) -> str:
    sandbox = tmp_path / "bin"
    sandbox.mkdir()
    for tool in ["bash", "git", "grep", "sed", "tr", "id", "mktemp", "rm", "cat"]:
        found = shutil.which(tool)
        assert found, tool
        (sandbox / tool).symlink_to(found)
    assert shutil.which("rg", path=str(sandbox)) is None
    return str(sandbox)


def test_scan_works_without_rg(tmp_path: Path) -> None:
    path = _sandbox_path_without_rg(tmp_path)
    sample = _j("pl", "an-", "0123456789abcdef")
    dirty_dir = tmp_path / "dirty"
    dirty_dir.mkdir()
    dirty = _scan(_make_repo(dirty_dir, {"docs/a.md": f"{sample}\n"}), env={"PATH": path})
    assert dirty.returncode == 1, dirty.stderr
    assert "docs/a.md:1: plan-id: " in dirty.stderr

    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    clean = _scan(_make_repo(clean_dir, {"docs/a.md": "clean\n"}), env={"PATH": path})
    assert clean.returncode == 0, clean.stderr

    # The path checks fall back to grep -E as well.
    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    blocked = _scan(
        _make_repo(blocked_dir, {"logs/run.log": "x\n"}), env={"PATH": path}, script_name="check-public-boundary.sh"
    )
    assert blocked.returncode == 1
    assert "logs/run.log" in blocked.stderr


# --- pre-push range mode -----------------------------------------------------

Z40 = "0" * 40


def _commit(repo: Path, message: str, files: dict[str, str] | None = None) -> str:
    # Fixture history is built with plumbing (write-tree, commit-tree,
    # update-ref), which runs no hooks, so it does not depend on whatever
    # machine-global commit hooks the host has (CI has none).
    for rel, content in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", rel)

    def out(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    tree = out("write-tree")
    parent = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=repo, capture_output=True, text=True)
    parents = ["-p", parent.stdout.strip()] if parent.returncode == 0 else []
    sha = out("-c", "user.name=t", "-c", "user.email=t@example.com", "commit-tree", tree, *parents, "-m", message)
    _git(repo, "update-ref", "HEAD", sha)
    return sha


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _push(repo: Path, *lines: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _scan(repo, env=env, args=("--pre-push", "origin"), stdin="".join(line + "\n" for line in lines))


def _set_origin_main(repo: Path, sha: str, with_head: bool = True) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    if with_head:
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def test_pre_push_flags_a_planted_home_path_in_the_range(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "plant", {"docs/a.md": "clean\n" + _j("see /Us", "ers/", "zqfixture", "/notes\n")})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "Content check failed (pre-push)." in result.stderr
    assert "docs/a.md:2: home-path: /Use... " in result.stderr


def test_pre_push_clean_range_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "more", {"docs/a.md": "clean\nstill clean\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 0, result.stderr
    assert "Public-content check passed (pre-push: 1 added lines in 1 ranges)." in result.stdout


def test_pre_push_ignores_lines_outside_the_range(tmp_path: Path) -> None:
    plan = _j("pl", "an-", "0123456789abcdef")
    runner = _j("/ho", "me/", "runner", "/work")
    allow = [("docs/allowed.md", "home-path", "/[h]ome/runner/", "CI runner example")]
    repo = _make_repo(tmp_path, {"docs/old.md": f"{plan}\n", "docs/allowed.md": f"{runner}\n"}, allow=allow)
    base = _commit(repo, "base with pre-existing hits")
    tip = _commit(repo, "clean change", {"docs/new.md": "clean\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 0, result.stderr
    assert "unused allowlist entry" not in result.stderr
    assert "Public-content check passed (pre-push: 1 added lines in 1 ranges)." in result.stdout
    # The full scan of the same tree does see the pre-existing plan id.
    full = _scan(repo)
    assert full.returncode == 1
    assert "docs/old.md:1: plan-id: " in full.stderr


def test_pre_push_new_branch_uses_the_merge_base(tmp_path: Path) -> None:
    plan = _j("pl", "an-", "0123456789abcdef")
    repo = _make_repo(tmp_path, {"docs/old.md": f"{plan}\n"})
    base = _commit(repo, "base on the default branch, with a pre-existing hit")
    _set_origin_main(repo, base)
    clean_tip = _commit(repo, "feature: clean", {"docs/feature.md": "clean\n"})
    result = _push(repo, f"refs/heads/feature {clean_tip} refs/heads/feature {Z40}")
    assert result.returncode == 0, result.stderr
    assert "Public-content check passed (pre-push: 1 added lines in 1 ranges)." in result.stdout

    dirty_tip = _commit(repo, "feature: plant", {"docs/feature.md": "clean\n" + _j("/Us", "ers/", "zqfixture", "/x\n")})
    result = _push(repo, f"refs/heads/feature {dirty_tip} refs/heads/feature {Z40}")
    assert result.returncode == 1, result.stderr
    assert "docs/feature.md:2: home-path: " in result.stderr
    assert "docs/old.md" not in result.stderr

    # Without refs/remotes/origin/HEAD, refs/remotes/origin/main is the default.
    _git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    result = _push(repo, f"refs/heads/feature {clean_tip} refs/heads/feature {Z40}")
    assert result.returncode == 0, result.stderr


def test_pre_push_deletion_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": _j("/Us", "ers/", "zqfixture", "/x\n")})
    base = _commit(repo, "base with a hit")
    result = _push(repo, f"(delete) {Z40} refs/heads/gone {base}")
    assert result.returncode == 0, result.stderr
    assert "Public-content check passed (pre-push: 0 added lines in 0 ranges)." in result.stdout


@pytest.mark.parametrize("case", ["unknown-remote-sha", "no-default-branch", "no-merge-base", "malformed", "not-hex"])
def test_pre_push_range_that_cannot_be_computed_fails_closed(tmp_path: Path, case: str) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "tip", {"docs/b.md": "clean\n"})
    if case == "unknown-remote-sha":
        line = f"refs/heads/main {tip} refs/heads/main {'ab' * 20}"
    elif case == "no-default-branch":
        line = f"refs/heads/feature {tip} refs/heads/feature {Z40}"
    elif case == "no-merge-base":
        _set_origin_main(repo, base)
        tree = _rev(repo, "HEAD^{tree}")
        orphan = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit-tree", tree, "-m", "orphan"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        line = f"refs/heads/other {orphan} refs/heads/other {Z40}"
    elif case == "malformed":
        line = f"refs/heads/main {tip} refs/heads/main"
    else:
        line = f"refs/heads/main {tip} refs/heads/main {'zz' * 20}"
    result = _push(repo, line)
    assert result.returncode == 2, result.stderr
    assert "Content check error (pre-push):" in result.stderr
    assert "failing closed" in result.stderr


def test_pre_push_planted_token_prints_no_excerpt(tmp_path: Path) -> None:
    prefix = _j("gh", "p_")
    body = _random_body()
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "plant", {"docs/a.md": f"clean\ntoken {prefix}{body} here\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Content check failed (pre-push)." in result.stderr
    assert f"docs/a.md:2: token-github ({4 + len(body)} chars)" in result.stderr.splitlines()
    _assert_secret_body_absent(output, body)
    assert prefix + body[:1] not in output


def test_pre_push_uses_new_line_numbers_and_the_allowlist(tmp_path: Path) -> None:
    runner = _j("/ho", "me/", "runner", "/work")
    other = _j("/Us", "ers/", "zqfixture", "/x")
    allow = [("docs/a.md", "home-path", "/[h]ome/runner/", "CI runner example")]
    repo = _make_repo(tmp_path, {"docs/a.md": "one\ntwo\nthree\n"}, allow=allow)
    base = _commit(repo, "base")
    tip = _commit(repo, "edit", {"docs/a.md": f"one\n{other}\ntwo\n{runner}\nthree\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "Content check failed (pre-push)." in result.stderr
    assert "docs/a.md:2: home-path: /Use... " in result.stderr
    assert "docs/a.md:4: home-path" not in result.stderr


def test_pre_push_attributes_cannot_hide_added_text(tmp_path: Path) -> None:
    sample = _j("AK", "IA", "Q" * 16)
    repo = _make_repo(tmp_path, {".gitattributes": "*.csv binary\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "csv", {"data/t.csv": f"a,b\n1,{sample}\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "Content check failed (pre-push)." in result.stderr
    assert _hit("data/t.csv", 2, "token-aws") in result.stderr


def _hook_repo(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n", "Makefile": ".PHONY: check\ncheck:\n\t@echo make-check-ran\n"})
    (repo / ".githooks").mkdir()
    shutil.copy2(REPO_ROOT / ".githooks" / "pre-push", repo / ".githooks" / "pre-push")
    _git(repo, "add", "--", ".githooks/pre-push")
    return repo


def _run_hook(repo: Path, tmp_path: Path, stdin: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    run_env = os.environ.copy()
    for key in ("MINNI_BOUNDARY_FORBIDDEN_NAMES", "GITHUB_ACTIONS", "MINNI_SKIP_HOOKS"):
        run_env.pop(key, None)
    run_env["HOME"] = str(fake_home)
    if env:
        run_env.update(env)
    return subprocess.run(
        [BASH, str(repo / ".githooks" / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=repo, env=run_env, input=stdin, capture_output=True, text=True,
    )


def test_pre_push_hook_runs_the_range_check_and_keeps_its_bypass(tmp_path: Path) -> None:
    repo = _hook_repo(tmp_path)
    base = _commit(repo, "base")
    clean = _commit(repo, "clean", {"docs/a.md": "clean\nfine\n"})
    result = _run_hook(repo, tmp_path, f"refs/heads/main {clean} refs/heads/main {base}\n")
    assert result.returncode == 0, result.stderr
    assert "Public-content check passed (pre-push: 1 added lines in 1 ranges)." in result.stdout

    dirty = _commit(repo, "plant", {"docs/a.md": "clean\nfine\n" + _j("/Us", "ers/", "zqfixture", "/x\n")})
    stdin = f"refs/heads/main {dirty} refs/heads/main {clean}\n"
    result = _run_hook(repo, tmp_path, stdin)
    assert result.returncode != 0
    assert "docs/a.md:3: home-path: " in result.stderr
    assert "make-check-ran" not in result.stdout

    bypass = _run_hook(repo, tmp_path, stdin, env={"MINNI_SKIP_HOOKS": "1"})
    assert bypass.returncode == 0, bypass.stderr
    assert "Content check" not in bypass.stderr


# --- exact columns on multi-match lines (review probes A and E) ---------------

PROBE_A = _j("see /ho", "me/runner/work and /Us", "ers/zqreal/secret/ and /ho", "me/runner/other")
PROBE_E = _j("see /Us", "ers/zqreal/secret/ and also then later /ho", "me/runner/work")


@pytest.mark.parametrize(
    ("line", "flagged_excerpt"),
    [
        (PROBE_A, "/Use"),
        (PROBE_E, "/Use"),
        ("üü " + PROBE_A, "/Use"),
    ],
    ids=["probe-A", "probe-E", "probe-A-non-ascii-prefix"],
)
def test_allowlist_overlap_uses_exact_columns_on_multi_match_lines(
    tmp_path: Path, line: str, flagged_excerpt: str
) -> None:
    allow = [("docs/a.md", "home-path", "/[h]ome/runner/", "CI runner example")]
    result = _scan(_make_repo(tmp_path, {"docs/a.md": line + "\n"}, allow=allow))
    assert result.returncode == 1, result.stderr
    hits = [ln for ln in result.stderr.splitlines() if ln.startswith("docs/a.md:1: home-path: ")]
    assert hits == [f"docs/a.md:1: home-path: {flagged_excerpt}... (14 chars)"], result.stderr
    assert "unused allowlist entry" not in result.stderr


def test_pre_push_overlap_uses_exact_columns_on_multi_match_lines(tmp_path: Path) -> None:
    allow = [("docs/a.md", "home-path", "/[h]ome/runner/", "CI runner example")]
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"}, allow=allow)
    base = _commit(repo, "base")
    tip = _commit(repo, "probe A", {"docs/a.md": f"clean\n{PROBE_A}\n"})
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    hits = [ln for ln in result.stderr.splitlines() if ln.startswith("docs/a.md:2: home-path: ")]
    assert hits == ["docs/a.md:2: home-path: /Use... (14 chars)"], result.stderr


# --- range mode reads only the pushed allowlist (review probe D) -------------


@pytest.mark.parametrize("ere", ["/[U]sers/zqreal/", ".*"], ids=["would-exempt", "probe-D-wildcard"])
def test_pre_push_ignores_a_working_tree_only_allowlist_entry(tmp_path: Path, ere: str) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    tip = _commit(repo, "plant", {"docs/a.md": "clean\n" + _j("see /Us", "ers/", "zqreal", "/secret/notes\n")})
    # Written after the commits and never staged: only the working tree has it.
    (repo / "scripts" / "public-boundary-allow.txt").write_text(
        f"# t\ndocs/a.md\thome-path\t{ere}\tuncommitted entry\n", encoding="utf-8"
    )
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "docs/a.md:2: home-path: " in result.stderr
    assert "Content check error" not in result.stderr


@pytest.mark.parametrize("ere", [".*", "(", "x*"], ids=["wildcard", "invalid", "empty-match"])
def test_pre_push_consulted_entry_is_validated_like_full_mode(tmp_path: Path, ere: str) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    allow_text = f"# t\ndocs/a.md\thome-path\t{ere}\tpushed entry\n"
    tip = _commit(
        repo,
        "plant with a bad entry",
        {
            "docs/a.md": "clean\n" + _j("see /Us", "ers/", "zqreal", "/secret/notes\n"),
            "scripts/public-boundary-allow.txt": allow_text,
        },
    )
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 2, result.stderr
    assert "Content check error:" in result.stderr
    assert "scripts/public-boundary-allow.txt:2 has" in result.stderr


# --- every commit in the range is scanned ------------------------------------


def test_pre_push_catches_a_line_added_then_removed_inside_the_range(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    _commit(repo, "add", {"docs/a.md": "clean\n" + _j("see /Us", "ers/", "zqfixture", "/notes\n")})
    tip = _commit(repo, "remove again", {"docs/a.md": "clean\n"})
    assert _scan(repo).returncode == 0  # the tip tree itself is clean
    result = _push(repo, f"refs/heads/main {tip} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "docs/a.md:2: home-path: /Use... " in result.stderr


def test_pre_push_scans_commits_on_a_merged_side_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"docs/a.md": "clean\n"})
    base = _commit(repo, "base")
    side = _commit(repo, "side: plant", {"docs/side.md": _j("/Us", "ers/", "zqfixture", "/x\n")})
    _git(repo, "update-ref", "HEAD", base)
    _git(repo, "read-tree", "HEAD")
    main2 = _commit(repo, "main: clean", {"docs/a.md": "clean\nmore\n"})
    _git(repo, "read-tree", "-m", "-u", main2)
    (repo / "docs" / "side.md").write_text(_j("/Us", "ers/", "zqfixture", "/x\n"), encoding="utf-8")
    _git(repo, "add", "--", "docs/side.md")
    tree = subprocess.run(["git", "write-tree"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    merge = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit-tree", tree,
         "-p", main2, "-p", side, "-m", "merge side"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    result = _push(repo, f"refs/heads/main {merge} refs/heads/main {base}")
    assert result.returncode == 1, result.stderr
    assert "docs/side.md:1: home-path: " in result.stderr
