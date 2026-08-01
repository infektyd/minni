"""Audit R0 regression: no tracked source file may contain a raw NUL byte.

plugins/minni/src/vault.ts carried a literal 0x00 inside a cache-key template
(``\\0`` typed as the byte itself). Consequences were not theoretical:

  - ``file`` classified the largest source file in the plugin as ``data`` and
    grep treated it as binary, SUPPRESSING every match. ``grep -rn`` returned
    nothing; only ``grep -an`` found anything.
  - Every CI step and scanner that greps source — including the credential-leak
    scanner and the public-boundary check — silently SKIPPED the file.

A file that greps as binary is a file that security tooling cannot see, so this
is a hygiene gate, not a style preference.
"""

import os
import subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SOURCE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx",
    ".sql", ".sh", ".swift", ".json", ".yml", ".yaml", ".toml", ".md",
)

# Fixtures that legitimately embed control bytes to exercise sanitizers.
_ALLOWLIST: set = set()


def _tracked_source_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT, capture_output=True, check=True,
    ).stdout
    for name in out.split(b"\0"):
        rel = name.decode("utf-8", "replace")
        if not rel or rel in _ALLOWLIST:
            continue
        if rel.endswith(_SOURCE_SUFFIXES):
            yield rel


def test_no_tracked_source_file_contains_a_nul_byte():
    offenders = []
    for rel in _tracked_source_files():
        path = os.path.join(_REPO_ROOT, rel)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue  # symlink into an unbuilt dist, submodule, etc.
        if b"\0" in data:
            line = data[: data.index(b"\0")].count(b"\n") + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "Raw NUL byte in tracked source file(s): "
        + ", ".join(offenders)
        + ". grep treats these as binary and suppresses ALL matches, so CI "
        "scanners skip the file entirely. Use the \\0 / \\x00 escape instead."
    )


def test_vault_ts_is_greppable():
    """The specific file audit R0 found, pinned so it cannot regress."""
    rel = os.path.join("plugins", "minni", "src", "vault.ts")
    path = os.path.join(_REPO_ROOT, rel)
    if not os.path.exists(path):
        return  # plugin tree absent (engine-only checkout)
    with open(path, "rb") as fh:
        data = fh.read()
    assert b"\0" not in data, f"{rel} contains a raw NUL byte again"
    # The escape must still be present: the runtime key separator is unchanged,
    # only its spelling in source is.
    assert b'\\0${term}' in data, (
        f"{rel} lost the NUL cache-key separator; the escape must stay so the "
        "runtime string is byte-identical to the pre-fix behavior"
    )
    # And it must actually grep as text now.
    proc = subprocess.run(
        ["grep", "-c", "readInboxStatus", path],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0 and int(proc.stdout.strip()) > 0, (
        f"grep (without -a) found no matches in {rel}: still binary to grep"
    )
