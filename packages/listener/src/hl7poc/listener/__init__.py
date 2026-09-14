import logging

import typer

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """hl7listener CLI."""
    logging.basicConfig(level=logging.INFO)


@app.command()
def run() -> None:
    """Start the HL7 listener."""
    logging.getLogger(__name__).info("hl7listener starting")
