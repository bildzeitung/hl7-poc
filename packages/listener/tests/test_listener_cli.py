from hl7poc.listener import app
from typer.testing import CliRunner

runner = CliRunner()


def test_help_exits_zero() -> None:
    # A bare invocation runs the service forever -- --help is the only
    # CliRunner-safe path; the serving coroutine is never invoked by tests.
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
