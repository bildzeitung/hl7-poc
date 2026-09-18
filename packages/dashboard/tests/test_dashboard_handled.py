import asyncio
import json
from pathlib import Path

from hl7poc.dashboard import handle_http
from hl7poc.dashboard.forwarded import ForwardedStore
from hl7poc.dashboard.handled import HandledStore


class _FakeReader:
    """Feeds fixed lines to readline(), then a fixed body to readexactly()."""

    def __init__(self, lines: list[bytes], body: bytes = b"") -> None:
        self._lines = list(lines)
        self._body = body

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def readexactly(self, n: int) -> bytes:
        return self._body


class _FakeWriter:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _serve(
    reader: _FakeReader,
    *,
    spool_dir: Path,
    handled_store: HandledStore,
    forwarded_store: ForwardedStore | None = None,
) -> _FakeWriter:
    writer = _FakeWriter()
    asyncio.run(
        handle_http(
            reader,
            writer,
            listener_ready_url="http://localhost:8080/ready",
            spool_dir=spool_dir,
            bus_health_url="http://localhost:5300/health",
            handled_store=handled_store,
            forwarded_store=forwarded_store or ForwardedStore(),
        )
    )
    return writer


def _post_request(body: bytes) -> _FakeReader:
    return _FakeReader(
        [
            b"POST /api/handled HTTP/1.1\r\n",
            f"Content-Length: {len(body)}\r\n".encode(),
            b"\r\n",
        ],
        body=body,
    )


# ---- HandledStore -------------------------------------------------------------


def test_handled_store_counts_by_outcome() -> None:
    store = HandledStore(10)
    store.record({"outcome": "completed"})
    store.record({"outcome": "dead_lettered"})
    store.record({"outcome": "completed"})

    snapshot = store.snapshot()
    assert snapshot["completed"] == 2
    assert snapshot["dead_lettered"] == 1
    assert snapshot["total"] == 3
    assert len(snapshot["events"]) == 3


def test_handled_store_ring_buffer_caps_events_kept() -> None:
    store = HandledStore(2)
    store.record({"outcome": "completed", "n": 1})
    store.record({"outcome": "completed", "n": 2})
    store.record({"outcome": "completed", "n": 3})

    snapshot = store.snapshot()
    assert [e["n"] for e in snapshot["events"]] == [2, 3]
    assert snapshot["completed"] == 3  # totals survive eviction from the ring


# ---- POST /api/handled ---------------------------------------------------------


def test_post_handled_records_event_and_returns_204(tmp_path: Path) -> None:
    store = HandledStore(10)
    body = json.dumps({"outcome": "completed", "mrn": "1"}).encode()

    writer = _serve(_post_request(body), spool_dir=tmp_path, handled_store=store)

    assert writer.written.startswith(b"HTTP/1.1 204 No Content")
    assert store.snapshot()["completed"] == 1


def test_post_handled_malformed_body_returns_400_and_does_not_record(
    tmp_path: Path,
) -> None:
    store = HandledStore(10)

    writer = _serve(_post_request(b"not json"), spool_dir=tmp_path, handled_store=store)

    assert writer.written.startswith(b"HTTP/1.1 400 Bad Request")
    assert store.snapshot()["total"] == 0


def test_post_handled_missing_outcome_returns_400(tmp_path: Path) -> None:
    store = HandledStore(10)
    body = json.dumps({"mrn": "1"}).encode()

    writer = _serve(_post_request(body), spool_dir=tmp_path, handled_store=store)

    assert writer.written.startswith(b"HTTP/1.1 400 Bad Request")
    assert store.snapshot()["total"] == 0


def test_api_status_includes_handled_snapshot(tmp_path: Path) -> None:
    store = HandledStore(10)
    store.record({"outcome": "completed"})
    reader = _FakeReader([b"GET /api/status HTTP/1.1\r\n", b"\r\n"])

    writer = _serve(reader, spool_dir=tmp_path, handled_store=store)

    body = writer.written.split(b"\r\n\r\n", 1)[1]
    fields = json.loads(body)
    assert fields["handled"]["completed"] == 1


def test_post_handled_unknown_outcome_returns_400(tmp_path: Path) -> None:
    store = HandledStore(10)
    body = json.dumps({"outcome": "exploded"}).encode()

    writer = _serve(_post_request(body), spool_dir=tmp_path, handled_store=store)

    assert writer.written.startswith(b"HTTP/1.1 400 Bad Request")
    assert store.snapshot()["total"] == 0


def test_post_handled_oversized_or_bad_content_length_returns_400(
    tmp_path: Path,
) -> None:
    store = HandledStore(10)
    for header in (b"Content-Length: 99999999\r\n", b"Content-Length: nope\r\n"):
        reader = _FakeReader([b"POST /api/handled HTTP/1.1\r\n", header, b"\r\n"])

        writer = _serve(reader, spool_dir=tmp_path, handled_store=store)

        assert writer.written.startswith(b"HTTP/1.1 400 Bad Request")
    assert store.snapshot()["total"] == 0


# ---- ForwardedStore -----------------------------------------------------------


def test_forwarded_store_counts_records() -> None:
    store = ForwardedStore()
    store.record()
    store.record()

    assert store.total == 2


# ---- POST /api/forwarded -------------------------------------------------------


def test_post_forwarded_increments_total_and_returns_204(tmp_path: Path) -> None:
    forwarded_store = ForwardedStore()
    reader = _FakeReader(
        [b"POST /api/forwarded HTTP/1.1\r\n", b"Content-Length: 0\r\n", b"\r\n"]
    )

    writer = _serve(
        reader,
        spool_dir=tmp_path,
        handled_store=HandledStore(10),
        forwarded_store=forwarded_store,
    )

    assert writer.written.startswith(b"HTTP/1.1 204 No Content")
    assert forwarded_store.total == 1


def test_post_forwarded_bad_content_length_returns_400(tmp_path: Path) -> None:
    forwarded_store = ForwardedStore()
    reader = _FakeReader(
        [b"POST /api/forwarded HTTP/1.1\r\n", b"Content-Length: nope\r\n", b"\r\n"]
    )

    writer = _serve(
        reader,
        spool_dir=tmp_path,
        handled_store=HandledStore(10),
        forwarded_store=forwarded_store,
    )

    assert writer.written.startswith(b"HTTP/1.1 400 Bad Request")
    assert forwarded_store.total == 0


def test_api_status_includes_queue_depth(tmp_path: Path) -> None:
    handled_store = HandledStore(10)
    handled_store.record({"outcome": "completed"})
    forwarded_store = ForwardedStore()
    forwarded_store.record()
    forwarded_store.record()
    forwarded_store.record()
    reader = _FakeReader([b"GET /api/status HTTP/1.1\r\n", b"\r\n"])

    writer = _serve(
        reader,
        spool_dir=tmp_path,
        handled_store=handled_store,
        forwarded_store=forwarded_store,
    )

    body = writer.written.split(b"\r\n\r\n", 1)[1]
    fields = json.loads(body)
    assert fields["queue_depth"] == {
        "value": 2,
        "method": "derived",
        "forwarded_total": 3,
        "handled_total": 1,
    }
