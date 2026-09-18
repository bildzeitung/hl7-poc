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

FETCH_TIMEOUT = 3  # seconds; matches hl7poc.probe.READ_TIMEOUT on the other side


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


def assemble_status(
    *, listener_ready_url: str, spool_dir: Path, bus_health_url: str
) -> dict[str, Any]:
    """Build the /api/status body. Never raises -- each source is independent."""
    return {
        "listener": _fetch_status(listener_ready_url),
        "spool": spool_status(spool_dir),
        "bus": _fetch_status(bus_health_url),
    }
