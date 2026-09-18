import asyncio
import json
from pathlib import Path

from hl7poc.listener import _REPORT_EXECUTOR, _forward, report
from hl7poc.model import CanonicalMessage, MessageHeader, Patient


def _model() -> CanonicalMessage:
    return CanonicalMessage(
        header=MessageHeader(
            msg_type="ADT",
            event="A01",
            control_id="MSG001",
            sending_app="SND",
            sending_fac="FAC",
            message_ts="20240101120000",
            hl7_version="2.5",
        ),
        patient=Patient(mrn="1"),
    )


class _StubSender:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_messages(self, message) -> None:
        self.sent.append(message)


class _StateStub:
    def __init__(self) -> None:
        self.sb_healthy = False


# ---- report ------------------------------------------------------------------


def test_report_posts_empty_body(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "hl7poc.listener.urllib.request.urlopen",
        lambda req, timeout=None: calls.append(req),
    )
    report("http://dashboard.example/api/forwarded")

    assert len(calls) == 1
    assert calls[0].full_url == "http://dashboard.example/api/forwarded"
    assert calls[0].data == b""


def test_report_swallows_failure(monkeypatch) -> None:
    def _raising_urlopen(req, timeout=None):
        raise OSError("dashboard down")

    monkeypatch.setattr("hl7poc.listener.urllib.request.urlopen", _raising_urlopen)

    report("http://dashboard.example/api/forwarded")  # must not raise


# ---- _forward: reporting ------------------------------------------------------


def test_forward_no_report_url_makes_no_http_call(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "hl7poc.listener.report", lambda *a, **kw: calls.append((a, kw))
    )
    sender = _StubSender()
    file = tmp_path / "msg.hl7"
    file.write_text(json.dumps({}))

    asyncio.run(
        _forward(
            _model(),
            file,
            sender=sender,
            send_lock=asyncio.Lock(),
            state=_StateStub(),
        )
    )  # report_url defaults to None

    assert calls == []
    assert not file.exists()


def test_forward_with_report_url_reports_once(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("hl7poc.listener.report", lambda url: calls.append(url))
    sender = _StubSender()
    file = tmp_path / "msg.hl7"
    file.write_text(json.dumps({}))

    asyncio.run(
        _forward(
            _model(),
            file,
            sender=sender,
            send_lock=asyncio.Lock(),
            state=_StateStub(),
            report_url="http://dashboard.example/api/forwarded",
        )
    )
    _REPORT_EXECUTOR.submit(lambda: None).result()  # drain the FIFO report thread

    assert calls == ["http://dashboard.example/api/forwarded"]


def test_forward_failed_send_does_not_report(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("hl7poc.listener.report", lambda url: calls.append(url))

    class _FailingSender:
        async def send_messages(self, message) -> None:
            raise RuntimeError("send failed")

    file = tmp_path / "msg.hl7"
    file.write_text(json.dumps({}))

    asyncio.run(
        _forward(
            _model(),
            file,
            sender=_FailingSender(),
            send_lock=asyncio.Lock(),
            state=_StateStub(),
            report_url="http://dashboard.example/api/forwarded",
        )
    )
    _REPORT_EXECUTOR.submit(lambda: None).result()  # drain the FIFO report thread

    assert calls == []
    assert file.exists()  # left in spool, matches the un-reported forward
