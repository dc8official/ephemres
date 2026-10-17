import os
import socket
import tempfile
import asyncio
import unittest
from unittest.mock import patch

from app.core.watchdog import notify_systemd, start_systemd_watchdog


class TestSystemdWatchdog(unittest.IsolatedAsyncioTestCase):

    def test_notify_systemd_no_socket(self):
        with patch.dict(os.environ, {}, clear=True):
            result = notify_systemd("WATCHDOG=1")
            self.assertFalse(result)

    def test_notify_systemd_with_socket(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "notify.sock")
            # Create a server socket to receive the datagram
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(sock_path)

            try:
                with patch.dict(os.environ, {"NOTIFY_SOCKET": sock_path}):
                    result = notify_systemd("READY=1")
                    self.assertTrue(result)
                    data, _ = server.recvfrom(1024)
                    self.assertEqual(data.decode("utf-8"), "READY=1")

                    result2 = notify_systemd("WATCHDOG=1")
                    self.assertTrue(result2)
                    data2, _ = server.recvfrom(1024)
                    self.assertEqual(data2.decode("utf-8"), "WATCHDOG=1")
            finally:
                server.close()

    async def test_start_systemd_watchdog_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(sock_path)
            server.setblocking(False)

            try:
                with patch.dict(os.environ, {"NOTIFY_SOCKET": sock_path}):
                    task = await start_systemd_watchdog(interval_seconds=0.05)
                    await asyncio.sleep(0.12)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                    # Read all messages received
                    received = []
                    while True:
                        try:
                            data, _ = server.recvfrom(1024)
                            received.append(data.decode("utf-8"))
                        except BlockingIOError:
                            break

                    self.assertIn("READY=1", received)
                    self.assertIn("WATCHDOG=1", received)
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
