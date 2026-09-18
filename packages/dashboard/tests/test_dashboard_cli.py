from typer.testing import CliRunner

from hl7poc.dashboard import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    # A bare invocation serves forever -- --help is the only CliRunner-safe
    # path; the serving coroutine is never invoked by tests.
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
