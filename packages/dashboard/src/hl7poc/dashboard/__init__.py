"""hl7dashboard: single-page status view for the by-hand full-chain demo.

Stdlib HTTP only -- no web framework, mirroring hl7poc.probe. Serves GET /
(a static HTML page whose inline JS polls /api/status every ~2s), GET
/api/status (JSON, built by hl7poc.dashboard.status.assemble_status), POST
/api/handled and POST /api/forwarded. This process only READS the
listener's /ready, the spool dir, and the bus emulator's /health -- it
never affects listener/worker behaviour, and a source being unreachable
never turns into a 500 (see status.py).

/api/status's queue_depth is DERIVED, not queried from the bus: the Service
Bus emulator has no working admin API for queue depth (see
hl7poc.dashboard.status.derive_queue_depth's docstring), so it is instead
computed from the listener's forwarded-message total (POST /api/forwarded)
minus the worker's handled total (POST /api/handled).

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
from typing import Annotated, Any

import typer

from hl7poc.dashboard.forwarded import ForwardedStore
from hl7poc.dashboard.handled import HandledStore
from hl7poc.dashboard.status import assemble_status, derive_queue_depth
from hl7poc.probe import READ_TIMEOUT

app = typer.Typer(add_completion=False)
logger = logging.getLogger(__name__)

HANDLED_RING_SIZE = 50  # last-N events kept in memory; see docs/configuration.md
MAX_HANDLED_BODY = 64 * 1024  # bytes; bounds what one POST can pin in the ring
_OUTCOMES = frozenset({"completed", "dead_lettered"})
_EVENT_FIELDS = ("mrn", "session_id", "msg_type", "outcome", "notified", "reported_at")

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
  <tr><td>Queue depth (derived)</td><td id="queue-depth-state">unknown</td><td id="queue-depth-detail"></td></tr>
  <tr><td>Handled (completed / dead-lettered)</td><td id="handled-state">unknown</td><td id="handled-detail"></td></tr>
</table>
<h2>Last handled</h2>
<table id="handled-events">
  <tr><th>Reported at</th><th>MRN</th><th>Type</th><th>Outcome</th><th>Notified</th></tr>
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
    setRow("listener", data.listener.ok, data.listener.fields
      ? JSON.stringify(data.listener.fields) : data.listener.error);
    setRow("spool", data.spool.ok, data.spool.ok
      ? String(data.spool.count) : data.spool.error);
    setRow("bus", data.bus.ok, data.bus.fields
      ? JSON.stringify(data.bus.fields) : data.bus.error);
    const queueDepthState = document.getElementById("queue-depth-state");
    if (data.queue_depth.value === null) {
      queueDepthState.textContent = "unknown";
      queueDepthState.className = "unknown";
    } else {
      queueDepthState.textContent = String(data.queue_depth.value);
      queueDepthState.className = "ok";
    }
    document.getElementById("queue-depth-detail").textContent =
      "forwarded " + data.queue_depth.forwarded_total +
      " - handled " + data.queue_depth.handled_total +
      (data.queue_depth.reason ? " (" + data.queue_depth.reason + ")" : "");
    const handledState = document.getElementById("handled-state");
    handledState.textContent =
      data.handled.completed + " / " + data.handled.dead_lettered;
    handledState.className = "ok";
    document.getElementById("handled-detail").textContent =
      "total " + data.handled.total;
    const events = document.getElementById("handled-events");
    while (events.rows.length > 1) events.deleteRow(1);
    for (const e of data.handled.events.slice().reverse()) {
      const row = events.insertRow();
      for (const v of [e.reported_at, e.mrn, e.msg_type, e.outcome, e.notified]) {
        row.insertCell().textContent = String(v);
      }
    }
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


def _parse_handled_event(raw: bytes) -> dict[str, Any] | None:
    """Parse a POST /api/handled body. Returns None on anything malformed."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("outcome") not in _OUTCOMES:
        return None
    return {k: event.get(k) for k in _EVENT_FIELDS}


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    listener_ready_url: str,
    spool_dir: Path,
    bus_health_url: str,
    handled_store: HandledStore,
    forwarded_store: ForwardedStore,
) -> None:
    """Serve GET / (the page), GET /api/status (JSON), POST /api/handled,
    POST /api/forwarded."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
        parts = request_line.split(b" ")
        method = parts[0].decode() if parts else "GET"
        path = parts[1].decode() if len(parts) > 1 else "/"

        content_length = 0
        while True:
            header_line = await asyncio.wait_for(
                reader.readline(), timeout=READ_TIMEOUT
            )
            if header_line in (b"\r\n", b""):
                break
            name, _, value = header_line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = -1

        if method == "POST" and path == "/api/handled":
            event = None
            if 0 <= content_length <= MAX_HANDLED_BODY:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=READ_TIMEOUT
                )
                event = _parse_handled_event(raw_body)
            if event is None:
                status, content_type, body = "400 Bad Request", "text/plain", b""
            else:
                handled_store.record(event)
                status, content_type, body = "204 No Content", "text/plain", b""
        elif method == "POST" and path == "/api/forwarded":
            if 0 <= content_length <= MAX_HANDLED_BODY:
                await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=READ_TIMEOUT
                )
                forwarded_store.record()
                status, content_type, body = "204 No Content", "text/plain", b""
            else:
                status, content_type, body = "400 Bad Request", "text/plain", b""
        elif path == "/":
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
            fields["handled"] = handled_store.snapshot()
            fields["queue_depth"] = derive_queue_depth(
                forwarded_store.total,
                fields["handled"]["total"],
                bus_ok=fields["bus"]["ok"],
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
    except TimeoutError, IndexError, ConnectionResetError, asyncio.IncompleteReadError:
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
    handled_store = HandledStore(HANDLED_RING_SIZE)
    forwarded_store = ForwardedStore()
    server = await asyncio.start_server(
        functools.partial(
            handle_http,
            listener_ready_url=listener_ready_url,
            spool_dir=spool_dir,
            bus_health_url=bus_health_url,
            handled_store=handled_store,
            forwarded_store=forwarded_store,
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
