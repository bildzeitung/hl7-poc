from typer.testing import CliRunner

from hl7poc.listener import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_run_exits_zero() -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
