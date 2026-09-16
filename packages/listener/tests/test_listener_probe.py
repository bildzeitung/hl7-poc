from pathlib import Path

from hl7poc.listener import ListenerState


def test_ready_ignores_service_bus_outage(tmp_path: Path) -> None:
    state = ListenerState(tmp_path)
    state.mllp_listening = True
    state.sb_healthy = False  # Service Bus down

    is_ready, fields = state.ready()

    assert is_ready is True
    assert fields["sb_healthy"] is False


def test_ready_false_when_mllp_not_listening(tmp_path: Path) -> None:
    state = ListenerState(tmp_path)
    state.sb_healthy = True

    is_ready, _ = state.ready()

    assert is_ready is False


def test_ready_false_when_shutting_down(tmp_path: Path) -> None:
    state = ListenerState(tmp_path)
    state.mllp_listening = True
    state.shutting_down = True

    is_ready, _ = state.ready()

    assert is_ready is False


def test_ready_false_when_spool_dir_not_writable(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    spool_dir.chmod(0o500)
    state = ListenerState(spool_dir)
    state.mllp_listening = True

    try:
        is_ready, fields = state.ready()
        assert is_ready is False
        assert fields["spool_writable"] is False
    finally:
        spool_dir.chmod(0o700)
