"""hl7dashboard: single-page status view for the by-hand full-chain demo.

Stdlib HTTP only -- no web framework, mirroring hl7poc.probe. Serves GET /
(a static HTML page whose inline JS polls /api/status every ~2s) and GET
/api/status (JSON, built by hl7poc.dashboard.status.assemble_status). This
process only READS the listener's /ready, the spool dir, and the bus
emulator's /health -- it never affects listener/worker behaviour, and a
source being unreachable never turns into a 500 (see status.py).

No import-time side effects: every env-derived value is a CLI option
resolved when the command runs, not at module scope.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Annotated

import typer

from hl7poc.dashboard.status import assemble_status

app = typer.Typer(add_completion=False)
logger = logging.getLogger(__name__)

READ_TIMEOUT = 3  # seconds; matches hl7poc.probe.READ_TIMEOUT

INDEX_HTML = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>hl7-poc demo dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }
  h1 { font-size: 1.25rem; }
  table { border-collapse: collapse; margin-top: 1rem; }
  td, th { padding: 0.4rem 1rem; text-align: left; border-bottom: 1px solid #333; }
  .ok { color: #4caf50; }
  .down { color: #e53935; }
  .unknown { color: #999; }
  #updated { color: #999; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>hl7-poc demo dashboard</h1>
<table id="status">
  <tr><th>Source</th><th>State</th><th>Detail</th></tr>
  <tr><td>Listener /ready</td><td id="listener-state">unknown</td><td id="listener-detail"></td></tr>
  <tr><td>Spool count</td><td id="spool-state">unknown</td><td id="spool-detail"></td></tr>
  <tr><td>Bus /health</td><td id="bus-state">unknown</td><td id="bus-detail"></td></tr>
</table>
<p id="updated"></p>
<script>
function setRow(key, ok, detail) {
  const state = document.getElementById(key + "-state");
  const cls = ok === null ? "unknown" : (ok ? "ok" : "down");
  state.textContent = ok === null ? "unknown" : (ok ? "up" : "down");
  state.className = cls;
  document.getElementById(key + "-detail").textContent = detail;
}

async function poll() {
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();
    setRow("listener", data.listener.ok, data.listener.ok
      ? JSON.stringify(data.listener.fields) : data.listener.error);
    setRow("spool", data.spool.ok, data.spool.ok
      ? String(data.spool.count) : data.spool.error);
    setRow("bus", data.bus.ok, data.bus.ok
      ? JSON.stringify(data.bus.fields) : data.bus.error);
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById("updated").textContent = "poll failed: " + err;
  }
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    listener_ready_url: str,
    spool_dir: Path,
    bus_health_url: str,
) -> None:
    """Serve GET / (the static page) and GET /api/status (JSON)."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
        path = request_line.split(b" ")[1].decode() if b" " in request_line else "/"
        if path == "/":
            status = "200 OK"
            content_type = "text/html; charset=utf-8"
            body = INDEX_HTML
        elif path == "/api/status":
            # Blocking urllib calls off the event loop so one slow/unreachable
            # source can't stall the listener socket for other requests.
            fields = await asyncio.to_thread(
                assemble_status,
                listener_ready_url=listener_ready_url,
                spool_dir=spool_dir,
                bus_health_url=bus_health_url,
            )
            status = "200 OK"
            content_type = "application/json"
            body = json.dumps(fields).encode()
        else:
            status, content_type, body = "404 Not Found", "text/plain", b""
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except TimeoutError, IndexError, ConnectionResetError:
        pass
    finally:
        writer.close()


async def serve(
    *,
    port: int,
    listener_ready_url: str,
    spool_dir: Path,
    bus_health_url: str,
) -> None:
    server = await asyncio.start_server(
        functools.partial(
            handle_http,
            listener_ready_url=listener_ready_url,
            spool_dir=spool_dir,
            bus_health_url=bus_health_url,
        ),
        "0.0.0.0",
        port,
    )
    logger.info("dashboard on :%d", port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with server:
        await stop.wait()


@app.command()
def dashboard(
    port: Annotated[int, typer.Option("--port", envvar="DASHBOARD_PORT")] = 8082,
    listener_ready_url: Annotated[
        str, typer.Option("--listener-ready-url", envvar="LISTENER_READY_URL")
    ] = "http://localhost:8080/ready",
    spool_dir: Annotated[Path, typer.Option("--spool-dir", envvar="SPOOL_DIR")] = Path(
        "./spool"
    ),
    bus_health_url: Annotated[
        str, typer.Option("--bus-health-url", envvar="BUS_HEALTH_URL")
    ] = "http://localhost:5300/health",
) -> None:
    """Start the demo status dashboard."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            port=port,
            listener_ready_url=listener_ready_url,
            spool_dir=spool_dir,
            bus_health_url=bus_health_url,
        )
    )


if __name__ == "__main__":
    sys.exit(app())
