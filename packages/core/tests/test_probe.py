import asyncio

from hl7poc.probe import handle_http


class _FakeReader:
    def __init__(self, line: bytes) -> None:
        self._line = line

    async def readline(self) -> bytes:
        return self._line


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


def _serve(request_line: bytes, *, ready=None) -> _FakeWriter:
    writer = _FakeWriter()
    asyncio.run(handle_http(_FakeReader(request_line), writer, ready=ready))
    return writer


def test_live_returns_200_with_no_ready_callable() -> None:
    writer = _serve(b"GET /live HTTP/1.1\r\n")
    assert writer.written.startswith(b"HTTP/1.1 200 OK")
    assert writer.closed


def test_ready_route_404s_when_service_has_no_ready_callable() -> None:
    writer = _serve(b"GET /ready HTTP/1.1\r\n")
    assert writer.written.startswith(b"HTTP/1.1 404 Not Found")


def test_ready_route_reports_200_and_body_when_ready() -> None:
    writer = _serve(b"GET /ready HTTP/1.1\r\n", ready=lambda: (True, {"ok": True}))
    assert writer.written.startswith(b"HTTP/1.1 200 OK")
    assert b'{"ok": true}' in writer.written


def test_ready_route_reports_503_when_not_ready() -> None:
    writer = _serve(b"GET /ready HTTP/1.1\r\n", ready=lambda: (False, {"ok": False}))
    assert writer.written.startswith(b"HTTP/1.1 503 Service Unavailable")


def test_unknown_path_404s() -> None:
    writer = _serve(b"GET /nope HTTP/1.1\r\n")
    assert writer.written.startswith(b"HTTP/1.1 404 Not Found")
