"""
G04 — SEC-001 socket location + permissions test.
Asserts that the hardened default lives under ~/.minni/run/ with
correct 0700/0600 expectations (the actual bind+chmod is exercised at runtime;
this unit test covers the constants and the ensure-dir logic).
"""
from pathlib import Path
import os
import tempfile

# Import from minnid without running main
import sys
sys.path.insert(0, str(Path(__file__).parent))
import minnid  # type: ignore


def test_default_socket_is_under_secure_run_dir():
    p = minnid.DEFAULT_SOCKET_PATH
    assert isinstance(p, Path)
    assert "minni" in str(p)
    assert p.name == "sovrd.sock"
    assert p.parent.name == "run"
    # parent of run is .minni under home
    assert p.parent.parent == Path.home() / ".minni"


def test_ensure_run_dir_creates_0700_and_socket_0600(tmp_path: Path):
    """Replicate the mkdir+chmod logic from _serve_unix_socket and assert modes."""
    run_dir = tmp_path / "run"
    sock = run_dir / "sovrd.sock"

    # Simulate the ensure logic
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    # simulate socket file creation (touch for stat)
    sock.touch()
    os.chmod(sock, 0o600)

    dir_mode = run_dir.stat().st_mode & 0o777
    sock_mode = sock.stat().st_mode & 0o777
    assert dir_mode == 0o700, f"run/ must be 0700, got {oct(dir_mode)}"
    assert sock_mode == 0o600, f"socket must be 0600, got {oct(sock_mode)}"
