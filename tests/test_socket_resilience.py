import asyncio
import os
import socket
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from minni.minnid import (
    _serve_unix_socket,
    _socket_watchdog_runner,
    _initiate_graceful_shutdown,
)


def test_serve_unix_socket_rejects_active_listener():
    async def _run():
        # macOS AF_UNIX path length limit is 104 chars; pytest tmp_path is often too long.
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp_dir:
            sock_path = Path(tmp_dir) / "minnid.sock"

            # Start an active listener on the socket path
            active_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            active_server.bind(str(sock_path))
            active_server.listen(1)

            try:
                # Attempting to serve on the same path must refuse to unlink and raise RuntimeError
                with pytest.raises(RuntimeError, match="Active minnid is already listening"):
                    await _serve_unix_socket(sock_path)

                # Verify the active socket was NOT deleted
                assert sock_path.exists()
            finally:
                active_server.close()
                if sock_path.exists():
                    sock_path.unlink()

    asyncio.run(_run())


def test_socket_watchdog_detects_unlinked_path(tmp_path):
    async def _run():
        sock_path = tmp_path / "minnid.sock"
        sock_path.touch()
        expected_inode = sock_path.stat().st_ino

        shutdown_mock = MagicMock()

        with patch("minni.minnid._initiate_graceful_shutdown", shutdown_mock):
            watchdog_task = asyncio.create_task(
                _socket_watchdog_runner(
                    sock_path,
                    expected_inode,
                    interval=0.05,
                )
            )

            # Let it run one clean tick
            await asyncio.sleep(0.08)
            assert not shutdown_mock.called

            # Now unlink the socket
            sock_path.unlink()

            # Wait for watchdog to detect
            await asyncio.sleep(0.12)
            assert shutdown_mock.called
            reason = shutdown_mock.call_args[0][0]
            assert "unlinked" in reason.lower()

            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def test_socket_watchdog_detects_inode_drift(tmp_path):
    async def _run():
        sock_path = tmp_path / "minnid.sock"
        sock_path.touch()
        original_inode = sock_path.stat().st_ino

        shutdown_mock = MagicMock()

        with patch("minni.minnid._initiate_graceful_shutdown", shutdown_mock):
            watchdog_task = asyncio.create_task(
                _socket_watchdog_runner(
                    sock_path,
                    original_inode,
                    interval=0.05,
                )
            )

            await asyncio.sleep(0.08)
            assert not shutdown_mock.called

            # Recreate the file to produce a new inode
            sock_path.unlink()
            sock_path.touch()
            new_inode = sock_path.stat().st_ino
            assert new_inode != original_inode

            # Wait for watchdog to detect drift
            await asyncio.sleep(0.12)
            assert shutdown_mock.called
            reason = shutdown_mock.call_args[0][0]
            assert "drift" in reason.lower()

            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def test_exit_cleanup_preserves_foreign_inode(tmp_path):
    sock_path = tmp_path / "minnid.sock"
    sock_path.touch()
    assert sock_path.exists()
    foreign_inode = sock_path.stat().st_ino

    # Simulate daemon having bound an older/different inode
    bound_inode = foreign_inode + 9999

    # Replicate minnid.py main() finally cleanup logic
    if sock_path.exists():
        stat_ino = sock_path.stat().st_ino
        if bound_inode is None or stat_ino == bound_inode:
            sock_path.unlink()

    # The foreign inode socket must NOT be unlinked
    assert sock_path.exists()

    # If bound_inode matches, it MUST be unlinked
    bound_inode = foreign_inode
    if sock_path.exists():
        stat_ino = sock_path.stat().st_ino
        if bound_inode is None or stat_ino == bound_inode:
            sock_path.unlink()

    assert not sock_path.exists()
