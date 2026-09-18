import json
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from hl7poc.dashboard.status import assemble_status, derive_queue_depth, spool_status


def _fake_response(body: dict) -> BytesIO:
    return BytesIO(json.dumps(body).encode())


def test_spool_status_counts_hl7_files(tmp_path: Path) -> None:
    (tmp_path / "a.hl7").write_bytes(b"x")
    (tmp_path / "b.hl7").write_bytes(b"y")
    (tmp_path / "not-hl7.txt").write_bytes(b"z")

    result = spool_status(tmp_path)

    assert result == {"ok": True, "count": 2}


def test_spool_status_excludes_rejected_subdir(tmp_path: Path) -> None:
    (tmp_path / "a.hl7").write_bytes(b"x")
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    (rejected / "bad.hl7").write_bytes(b"z")

    result = spool_status(tmp_path)

    assert result == {"ok": True, "count": 1}


def test_spool_status_missing_dir_reports_zero_not_error(tmp_path: Path) -> None:
    # Path.glob on a dir that doesn't exist yet yields nothing rather than
    # raising -- the dashboard may start before the listener creates spool_dir.
    result = spool_status(tmp_path / "not-created-yet")

    assert result == {"ok": True, "count": 0}


def test_spool_status_glob_failure_reports_error(tmp_path: Path) -> None:
    with patch("hl7poc.dashboard.status.Path.glob", side_effect=OSError("boom")):
        result = spool_status(tmp_path)

    assert result["ok"] is False
    assert "error" in result


def test_assemble_status_all_sources_up(tmp_path: Path) -> None:
    (tmp_path / "a.hl7").write_bytes(b"x")

    def fake_urlopen(url, timeout=None):
        if "8080" in url:
            return _fake_response({"mllp_listening": True})
        return _fake_response({"status": "healthy"})

    with patch(
        "hl7poc.dashboard.status.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        result = assemble_status(
            listener_ready_url="http://localhost:8080/ready",
            spool_dir=tmp_path,
            bus_health_url="http://localhost:5300/health",
        )

    assert result["listener"] == {"ok": True, "fields": {"mllp_listening": True}}
    assert result["spool"] == {"ok": True, "count": 1}
    assert result["bus"] == {"ok": True, "fields": {"status": "healthy"}}


def test_assemble_status_unreachable_source_reports_unknown_not_raise(
    tmp_path: Path,
) -> None:
    def fake_urlopen(url, timeout=None):
        if "8080" in url:
            raise urllib.error.URLError("connection refused")
        return _fake_response({"status": "healthy"})

    with patch(
        "hl7poc.dashboard.status.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        result = assemble_status(
            listener_ready_url="http://localhost:8080/ready",
            spool_dir=tmp_path,
            bus_health_url="http://localhost:5300/health",
        )

    assert result["listener"]["ok"] is False
    assert "error" in result["listener"]
    assert result["bus"]["ok"] is True


def test_assemble_status_all_sources_down(tmp_path: Path) -> None:
    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch(
        "hl7poc.dashboard.status.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        result = assemble_status(
            listener_ready_url="http://localhost:8080/ready",
            spool_dir=tmp_path / "missing-spool",
            bus_health_url="http://localhost:5300/health",
        )

    assert result["listener"]["ok"] is False
    assert result["bus"]["ok"] is False
    # A missing (never-created) spool dir globs to zero, not an error.
    assert result["spool"] == {"ok": True, "count": 0}


def test_assemble_status_not_ready_503_keeps_flags(tmp_path: Path) -> None:
    def fake_urlopen(url, timeout=None):
        if "8080" in url:
            raise urllib.error.HTTPError(
                url, 503, "Service Unavailable", {}, _fake_response({"bus_up": False})
            )
        return _fake_response({"status": "healthy"})

    with patch(
        "hl7poc.dashboard.status.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        result = assemble_status(
            listener_ready_url="http://localhost:8080/ready",
            spool_dir=tmp_path,
            bus_health_url="http://localhost:5300/health",
        )

    assert result["listener"]["ok"] is False
    assert result["listener"]["fields"] == {"bus_up": False}
    assert "503" in result["listener"]["error"]


# ---- derive_queue_depth --------------------------------------------------------


def test_derive_queue_depth_subtracts_handled_from_forwarded() -> None:
    result = derive_queue_depth(forwarded_total=10, handled_total=7)

    assert result == {
        "value": 3,
        "method": "derived",
        "forwarded_total": 10,
        "handled_total": 7,
    }


def test_derive_queue_depth_never_goes_negative() -> None:
    # handled_total can exceed forwarded_total after a listener restart
    # resets its counter while the worker's keeps counting.
    result = derive_queue_depth(forwarded_total=2, handled_total=5)

    assert result["value"] == 0
