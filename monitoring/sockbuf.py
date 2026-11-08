"""Socket receive buffers for the ICMP and flow sockets.

Linux caps SO_RCVBUF at net.core.rmem_max (212992 bytes on a stock Ubuntu install), and
asks for more fail silently. A small buffer overflows when thousands of replies or flow
packets arrive at once, and the kernel drops the overflow without telling the program:
at 5,000 endpoints the engine lost 3.9% of ping replies with the default buffer. The
installer raises rmem_max (deploy/sysctl-ephemres.conf), and this logs the size each
socket actually got so a capped host is visible in the journal.
"""
from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)

RECEIVE_BUFFER_BYTES = 4 * 1024 * 1024


def set_receive_buffer(sock: socket.socket, what: str, size: int = RECEIVE_BUFFER_BYTES) -> int:
    """Ask for a `size`-byte receive buffer and return the size the kernel granted.

    Linux doubles the request to cover its own bookkeeping and reports the doubled value,
    so a full grant reads back as 2 * size. Logs a warning naming the fix when capped.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
    except OSError as exc:
        logger.warning("%s: could not set the receive buffer to %d bytes: %s", what, size, exc)
    try:
        granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError:
        return 0
    if not isinstance(granted, int):
        return 0
    if granted < 2 * size:
        logger.warning(
            "%s: receive buffer is %d bytes (kernel-reported), asked for %d; "
            "net.core.rmem_max caps it, and packets will be dropped under load. Run "
            "'sysctl -w net.core.rmem_max=8388608', or upgrade to install the sysctl file.",
            what, granted, size,
        )
    else:
        logger.info("%s: receive buffer is %d bytes (kernel-reported)", what, granted)
    return granted
