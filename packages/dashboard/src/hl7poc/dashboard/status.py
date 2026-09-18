"""Status assembly: poll the listener, the spool dir, and the bus emulator.

Each source is fetched independently and never raises out to the caller --
a source that is down or unreachable reports {"ok": False, "error": ...} for
just that key, so one dead source can't blank the page or 500 /api/status.
Pure functions (no asyncio, no server) so status assembly is unit-testable
without a running HTTP stack.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hl7poc.probe import READ_TIMEOUT as FETCH_TIMEOUT


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read())


def _fetch_status(url: str) -> dict[str, Any]:
    """Fetch a JSON status endpoint, reporting failure rather than raising."""
    try:
        return {"ok": True, "fields": _fetch_json(url)}
    except urllib.error.HTTPError as err:
        # /ready answers 503 with its flags in the body -- the not-ready case is
        # exactly when those flags matter, so keep them rather than just the code.
        try:
            fields = json.loads(err.read())
        except ValueError, OSError:
            return {"ok": False, "error": str(err)}
        return {"ok": False, "error": str(err), "fields": fields}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        return {"ok": False, "error": str(err)}


def spool_status(spool_dir: Path) -> dict[str, Any]:
    """Count *.hl7 files directly under spool_dir (excludes rejected/)."""
    try:
        count = sum(1 for _ in spool_dir.glob("*.hl7"))
        return {"ok": True, "count": count}
    except OSError as err:
        return {"ok": False, "error": str(err)}


def derive_queue_depth(
    forwarded_total: int, handled_total: int, *, bus_ok: bool
) -> dict[str, Any]:
    """Approximate hl7-events depth as forwarded-but-not-yet-handled.

    There is no working way to read this from the Service Bus emulator
    directly: ServiceBusAdministrationClient.get_queue_runtime_properties
    builds its management endpoint on port 443 from the sb://<host>
    connection string, and the emulator never opens that HTTPS listener at
    all (connection refused); the emulator's own :5300 admin port serves an
    Atom/XML QueueDescription feed instead, but its MessageCount field does
    not update after a send (verified by hand against a running emulator --
    see hl7-poc-285.2's bd design note), so it cannot be used either.

    Instead this is derived from two independently-reported running totals:
    the listener's forwarded-message count (POST /api/forwarded) minus the
    worker's handled count (POST /api/handled, completed + dead_lettered).
    Known accuracy limits: both totals live only in this dashboard process,
    so a dashboard restart zeroes both while messages forwarded before it may
    still be handled after it (undercounting depth); a report the listener or
    worker fails to deliver is simply lost; and the two POSTs can race by one
    message -- all make this an approximation, not an exact depth, which is why it
    is always labelled "derived" rather than a queried value like bus/listener.

    The estimate is withheld (value=None, method="unknown") rather than shown
    with false confidence in two cases: the bus is down (bus_ok=False) -- the
    listener spools instead of forwarding, so the derived total silently stops
    growing and would show a stale/misleadingly-shrinking number; or
    forwarded_total is 0 while handled_total > 0, which means the listener
    evidently isn't reporting (FORWARDED_REPORT_URL unset, or a dashboard
    restart with pre-restart messages still draining) and
    any subtraction against it is meaningless, not just imprecise.
    """
    totals = {"forwarded_total": forwarded_total, "handled_total": handled_total}
    if not bus_ok:
        reason = "bus down"
    elif forwarded_total == 0 and handled_total > 0:
        reason = "listener not reporting"
    else:
        value = max(0, forwarded_total - handled_total)
        return {"value": value, "method": "derived", **totals}
    return {"value": None, "method": "unknown", "reason": reason, **totals}


def assemble_status(
    *, listener_ready_url: str, spool_dir: Path, bus_health_url: str
) -> dict[str, Any]:
    """Build the /api/status body. Never raises -- each source is independent."""
    return {
        "listener": _fetch_status(listener_ready_url),
        "spool": spool_status(spool_dir),
        "bus": _fetch_status(bus_health_url),
    }
