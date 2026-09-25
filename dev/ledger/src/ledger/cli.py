"""The `ledger` command line: ingest a CSV, print a month."""

from __future__ import annotations

from pathlib import Path

import click

from ledger import __version__
from ledger.ingest import Drift, ingest
from ledger.report import monthly

# The exit status of a file refused for schema drift, apart from click's 1 and 2
# so the drift gate can tell a refusal from a crash or a usage error.
DRIFT_EXIT = 3

db_option = click.option(
    "--db",
    "db",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="The DuckDB file; created on first use.",
)


@click.group()
@click.version_option(__version__, prog_name="ledger")
def cli() -> None:
    """Expenses from CSV into DuckDB, and the monthly report."""


@cli.command("ingest")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@db_option
def ingest_command(source: Path, db: Path) -> None:
    """Validate SOURCE against schema.json and store its rows once."""
    try:
        loaded = ingest(source, db)
    except Drift as exc:
        for difference in exc.differences:
            click.echo(f"schema drift: {difference}", err=True)
        raise SystemExit(DRIFT_EXIT) from None
    if loaded.already:
        click.echo(f"{source.name}: already loaded, {loaded.rows} rows, nothing added")
    else:
        click.echo(f"{source.name}: loaded {loaded.rows} rows")


@cli.command("report")
@db_option
@click.option("--month", required=True, help="The month to report, YYYY-MM.")
def report_command(db: Path, month: str) -> None:
    """Print the total and the total per category for one month."""
    try:
        report = monthly(db, month)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--month") from None
    click.echo(report.text())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
