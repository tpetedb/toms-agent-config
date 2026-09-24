"""`tac pipeline check|plan`: judge the declared pipelines and show a run's order."""

from __future__ import annotations

import json
from pathlib import Path

import click

from tac import pipelines
from tac.doctor import find_root
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


@click.group("pipeline")
def pipeline_group() -> None:
    """The pipelines under .agents/config/pipelines/: check them, plan a run."""


@pipeline_group.command("check")
@ROOT
@click.argument("names", nargs=-1)
def check(root: Path | None, names: tuple[str, ...]) -> None:
    """Refuse a pipeline for every reason it is unsound; exit 1 on any."""
    base = _root(root)
    problems = pipelines.check_pipelines(base, names)
    if problems:
        for line in problems:
            click.echo(line, err=True)
        raise SystemExit(1)
    checked = names or tuple(pipelines.pipeline_files(base))
    click.echo(f"pipelines sound: {', '.join(checked)}")


@pipeline_group.command("plan")
@ROOT
@click.option("--json", "as_json", is_flag=True, help="Print the stages as JSON.")
@click.argument("name")
def plan(root: Path | None, as_json: bool, name: str) -> None:
    """Print the order a run of NAME takes its stages in; nothing runs."""
    try:
        pipeline = pipelines.load_plan(_root(root), name)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    if as_json:
        rows = pipelines.plan_rows(pipeline)
        payload = {
            "pipeline": pipeline.name,
            "entry_contracts": list(pipeline.entry_contracts),
            "stages": rows,
        }
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(pipelines.plan_text(pipeline), nl=False)
