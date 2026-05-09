"""Tests for SEC-001 socket / capability hardening.

Spins the daemon up on a temporary HOME so the run-dir, token, and socket all
land in a sandbox we can inspect. Each test patches HOME via os.environ and
imports `sovrd` fresh so module-level Path.home() calls resolve under the
sandbox.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that talks to a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class DaemonHarness:
    """Start the SEC-001-hardened daemon on an isolated HOME."""

    def __init__(self, allowed_roots: list[Path] | None = None):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        # Provide a default allowed root inside the sandbox so /read can succeed.
        self.allowed_roots = list(allowed_roots) if allowed_roots else [self.home / "vault"]
        for r in self.allowed_roots:
            r.mkdir(parents=True, exist_ok=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        # Reload sovrd so module-level Path.home() picks up the new HOME.
        sys.modules.pop("sovrd", None)
        self.sovrd = importlib.import_module("sovrd")
        self.run_dir = self.home / ".sovereign-memory" / "run"
        self.socket_path = self.run_dir / "openclaw.sock"
        self.token_path = self.run_dir / "openclaw.token"
        self.server = self.sovrd.build_server(
            socket_path=str(self.socket_path),
            allowed_roots=self.allowed_roots,
        )
        self.token = self.sovrd.AUTH_TOKEN
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        # Wait until the socket is reachable.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self.socket_path.exists():
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(0.2)
                        s.connect(str(self.socket_path))
                        break
                except OSError:
                    pass
            time.sleep(0.02)

    def request(self, method: str, path: str, *,
                body: bytes | str | None = None,
                headers: dict | None = None,
                token: str | None | object = ...,  # sentinel: ... means "use real token"
                extra_headers: dict | None = None):
        conn = _UnixHTTPConnection(str(self.socket_path))
        h = dict(headers or {})
        if token is ...:
            h.setdefault("Authorization", f"Bearer {self.token}")
        elif token is None:
            pass  # no auth header
        else:
            h["Authorization"] = f"Bearer {token}"
        if extra_headers:
            h.update(extra_headers)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is not None and "Content-Length" not in h and "content-length" not in h:
            h["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmpdir.cleanup()


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class SocketHardeningTests(unittest.TestCase):
    def setUp(self):
        self.harness = DaemonHarness()

    def tearDown(self):
        self.harness.stop()

    # ---- file-system perimeter --------------------------------------------------

    def test_socket_mode_is_0600(self):
        self.assertEqual(_mode(self.harness.socket_path), 0o600)

    def test_run_dir_mode_is_0700(self):
        self.assertEqual(_mode(self.harness.run_dir), 0o700)

    def test_token_file_mode_is_0600(self):
        self.assertEqual(_mode(self.harness.token_path), 0o600)
        self.assertGreaterEqual(len(self.harness.token), 32)

    # ---- auth -------------------------------------------------------------------

    def test_read_without_auth_header_is_401(self):
        target = self.harness.allowed_roots[0] / "ok.txt"
        target.write_text("hello")
        status, _ = self.harness.request(
            "POST", "/read",
            body=json.dumps({"path": str(target)}),
            token=None,
            extra_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_read_with_wrong_token_is_401(self):
        target = self.harness.allowed_roots[0] / "ok.txt"
        target.write_text("hello")
        status, _ = self.harness.request(
            "POST", "/read",
            body=json.dumps({"path": str(target)}),
            token="not-the-real-token",
            extra_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_read_with_correct_token_succeeds_inside_allowlist(self):
        target = self.harness.allowed_roots[0] / "hello.txt"
        target.write_text("hi there")
        status, body = self.harness.request(
            "POST", "/read",
            body=json.dumps({"path": str(target)}),
            extra_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["text"], "hi there")

    # ---- path containment -------------------------------------------------------

    def test_read_absolute_outside_allowlist_is_403(self):
        # /etc/hosts exists on macOS+Linux and is outside the sandbox roots.
        status, _ = self.harness.request(
            "POST", "/read",
            body=json.dumps({"path": "/etc/hosts"}),
            extra_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_read_dotdot_traversal_is_400_or_403(self):
        status, _ = self.harness.request(
            "POST", "/read",
            body=json.dumps({"path": "../../etc/passwd"}),
            extra_headers={"Content-Type": "application/json"},
        )
        self.assertIn(status, (400, 403))

    # ---- body cap ---------------------------------------------------------------

    def test_learn_oversize_body_is_413(self):
        # The daemon rejects on Content-Length alone, *before* reading the body,
        # so it may close the socket while we are still sending. Talk raw HTTP
        # so we can read whatever response the server managed to send even when
        # our write half breaks.
        body = b"x" * (64 * 1024 + 16)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(str(self.harness.socket_path))
        try:
            request = (
                f"POST /learn HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Authorization: Bearer {self.harness.token}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n"
            ).encode("ascii")
            try:
                sock.sendall(request)
                # Best-effort body send; ignore broken pipe.
                sock.sendall(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            # Drain any response.
            chunks = []
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except (socket.timeout, ConnectionResetError, OSError):
                pass
            data = b"".join(chunks)
        finally:
            sock.close()
        self.assertTrue(data, "expected an HTTP response for oversize body")
        first_line = data.split(b"\r\n", 1)[0]
        self.assertIn(b" 413 ", first_line, msg=f"got: {first_line!r}")

    # ---- method/path allowlist --------------------------------------------------

    def test_delete_method_is_404(self):
        status, _ = self.harness.request("DELETE", "/health")
        self.assertEqual(status, 404)

    def test_unknown_path_is_404(self):
        status, _ = self.harness.request("GET", "/recall?q=foo")
        self.assertEqual(status, 404)

    def test_health_does_not_require_auth(self):
        status, body = self.harness.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
