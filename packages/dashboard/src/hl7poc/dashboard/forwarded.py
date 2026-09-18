"""In-memory counter for POST /api/forwarded: how many messages the listener
has forwarded to the bus. Paired with HandledStore's totals in status.py to
derive an approximate hl7-events queue depth, since the Service Bus emulator
has no working admin/runtime-properties API (see hl7-poc-285.2's bd design
note for the spike that established this).

State lives only in this process's memory -- restarting the dashboard resets
it, same tradeoff as HandledStore.
"""

from __future__ import annotations


class ForwardedStore:
    def __init__(self) -> None:
        self._total = 0

    def record(self) -> None:
        self._total += 1

    @property
    def total(self) -> int:
        return self._total
