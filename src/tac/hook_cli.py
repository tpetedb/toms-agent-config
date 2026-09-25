"""`tac hook`: what the stamped guard runs, and the verdict's contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from tac.hook import EVENTS, evaluate, verdict_json_schema
from tac.work import Bad


@click.group("hook")
def hook_group() -> None:
    """The checker behind every Claude and Codex hook, run by .agents/hooks/run.py."""


@hook_group.command("run")
@click.option("--client", required=True, type=click.Choice(sorted(EVENTS)))
@click.option("--event", required=True, help="The hook event, as the client names it.")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="The checkout the guard belongs to.",
)
def run_command(client: str, event: str, root: Path) -> None:
    """Judge the event on stdin and print one verdict as JSON. Any failure exits
    non-zero, which the guard turns into a refusal where a refusal counts."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise Bad("the event on stdin is not a JSON object")
        verdict = evaluate(root.resolve(), client, event, payload)
    except (Bad, ValueError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(verdict.model_dump_json())


@hook_group.command("schema")
def schema_command() -> None:
    """Print the verdict's JSON Schema (contracts/hook-verdict.schema.json)."""
    click.echo(json.dumps(verdict_json_schema(), indent=2, sort_keys=True))
