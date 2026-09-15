"""Shared probe HTTP server: `/live` always, `/ready` when a service supplies one.

Stdlib-free-of-frameworks on purpose -- both services ship one tiny asyncio
handler instead of pulling in a web framework for two routes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

READ_TIMEOUT = 3  # seconds; see docs/configuration.md's "Probe read timeout" row

ReadyFn = Callable[[], tuple[bool, dict]]


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    ready: ReadyFn | None = None,
) -> None:
    """Serve `/live` (always 200) and, when `ready` is given, `/ready`.

    `ready` returns (is_ready, body_fields) so the body can report additional
    state (e.g. sb_healthy) regardless of the readiness verdict itself.
    """
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
        path = request_line.split(b" ")[1].decode() if b" " in request_line else "/"
        if path == "/live":
            status, body = "200 OK", b"ok"
        elif path == "/ready" and ready is not None:
            is_ready, fields = ready()
            status = "200 OK" if is_ready else "503 Service Unavailable"
            body = json.dumps(fields).encode()
        else:
            status, body = "404 Not Found", b""
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except TimeoutError, IndexError, ConnectionResetError:
        pass
    finally:
        writer.close()
