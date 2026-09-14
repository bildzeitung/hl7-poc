import logging

import typer

app = typer.Typer(add_completion=False)


@app.command()
def run() -> None:
    """Start the HL7 worker."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("hl7worker starting")
