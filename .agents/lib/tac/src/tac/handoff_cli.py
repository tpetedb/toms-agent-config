"""`tac handoff render|validate|schema`: the declared way one stage hands the next."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from tac import handoff
from tac.doctor import find_root
from tac.work import Bad

# Exit statuses of `tac handoff validate`, so a runner can branch without parsing.
EXIT_ACCEPTED = 0
EXIT_REFUSED = 1
EXIT_REPAIR = 3
EXIT_ESCALATED = 4

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@click.group("handoff")
def handoff_group() -> None:
    """Render a stage's prompt from its template and contract, and judge its result."""


@handoff_group.command("render")
@ROOT
@click.argument("template")
@click.option("--run", "run_id", required=True, help="The run id.")
@click.option("--stage", "stage_id", required=True, help="The stage id in the run.")
@click.option("--order", "order_id", default=None, help="The order id, if any.")
@click.option("--role", default=None, help="The role the prompt is for.")
@click.option(
    "--input",
    "inputs",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="An upstream stage's envelope that passed; repeat for each.",
)
@click.option(
    "--entry",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The pipeline's entry payload, for a template that reads one contract.",
)
@click.option("--skill", "skills", multiple=True, help="A skill under .agents/skills.")
@click.option("--topic", "topics", multiple=True, help="A topic tag for the envelope.")
@click.option("--provider", default=None, help="Recorded from the launch.")
@click.option("--harness", default=None, help="Recorded from the launch.")
@click.option("--model", default=None, help="Recorded from the launch.")
@click.option("--effort", default=None, help="Recorded from the launch.")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where the envelope goes. Defaults to runs/<run>/NN-<stage>.json in the "
    "worker store.",
)
@click.option(
    "--prompt-out",
    default=None,
    help="Where the prompt goes; - prints it. Defaults to NN-<stage>.prompt.md "
    "next to the envelope.",
)
def render_command(
    root: Path | None,
    template: str,
    run_id: str,
    stage_id: str,
    order_id: str | None,
    role: str | None,
    inputs: tuple[Path, ...],
    entry: Path | None,
    skills: tuple[str, ...],
    topics: tuple[str, ...],
    provider: str | None,
    harness: str | None,
    model: str | None,
    effort: str | None,
    out: Path | None,
    prompt_out: str | None,
) -> None:
    """Check the payloads TEMPLATE reads, render its prompt, and write the
    dispatch envelope that binds the template, the contract and the prompt.

    TEMPLATE is a path under templates/, such as handoffs/build.md.j2."""
    base = _root(root)
    try:
        read = handoff.read_handoff_template(base, template)
        given = [handoff.upstream(base, p) for p in inputs]
        if entry is not None:
            if len(read.reads) != 1:
                raise Bad(
                    f"--entry needs a template that reads one contract; "
                    f"{template} reads {', '.join(read.reads) or 'nothing'}"
                )
            given.append(handoff.entry(base, entry, read.reads[0]))
        stage = handoff.Stage(
            id=stage_id,
            run_id=run_id,
            order_id=order_id,
            role=role,
            topic=topics,
            provider=provider,
            harness=harness,
            model=model,
            effort=effort,
        )
        done = handoff.render(base, template, stage, given, skills)
        target = out or handoff.next_envelope_path(
            handoff.worker_runs(base), run_id, stage_id
        )
    except Bad as e:
        raise click.ClickException(str(e)) from None
    _write(target, done.envelope.to_json())
    if prompt_out == "-":
        sys.stdout.write(done.prompt)
        sys.stdout.flush()
        return
    prompt_path = (
        Path(prompt_out) if prompt_out else handoff.sibling(target, ".prompt.md")
    )
    _write(prompt_path, done.prompt)
    click.echo(f"envelope: {target}")
    click.echo(f"prompt:   {prompt_path}")
    click.echo(f"rendered_input_sha256: {done.envelope.rendered_input_sha256}")


@handoff_group.command("validate")
@ROOT
@click.option(
    "--envelope",
    "envelope_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The dispatch envelope `tac handoff render` wrote; it is updated in place.",
)
@click.option(
    "--repair-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where the repair prompt goes. Defaults to <envelope>.repair.md.",
)
@click.option(
    "--human-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where the human item goes. Defaults to <envelope>.human.json.",
)
@click.argument("result", type=click.Path(dir_okay=False, allow_dash=True))
def validate_command(
    root: Path | None,
    envelope_path: Path | None,
    repair_out: Path | None,
    human_out: Path | None,
    result: str,
) -> None:
    """Judge RESULT, a stage's JSON output, against the contract its dispatch
    envelope names. Exit 0 when it passes, 3 with a repair prompt on a first
    failure, 4 with a human item on a failure after the repair, 1 when refused,
    as a result without its envelope is."""
    base = _root(root)
    try:
        if envelope_path is None:
            raise Bad(
                "no --envelope: a result without the envelope of its dispatch "
                "is refused"
            )
        envelope = handoff.read_envelope(envelope_path)
        text = (
            sys.stdin.read()
            if result == "-"
            else Path(result).read_text(encoding="utf-8", errors="replace")
        )
        judged = handoff.validate(
            base, envelope, text, envelope_path=envelope_path.name
        )
    except (Bad, OSError) as e:
        raise click.ClickException(str(e)) from None
    _write(envelope_path, judged.envelope.to_json())
    if judged.outcome in ("pass", "degraded"):
        click.echo(f"{judged.outcome}: {envelope_path}")
        raise SystemExit(EXIT_ACCEPTED)
    for error in judged.envelope.validation.errors:
        click.echo(f"{error.code} at {error.subject}: {error.message}", err=True)
    if judged.outcome == "repair":
        assert judged.repair_prompt is not None
        target = repair_out or handoff.sibling(envelope_path, ".repair.md")
        _write(target, judged.repair_prompt)
        click.echo(f"repair: one pass, prompt in {target}")
        raise SystemExit(EXIT_REPAIR)
    assert judged.human is not None
    target = human_out or handoff.sibling(envelope_path, ".human.json")
    _write(target, judged.human.to_json())
    click.echo(f"escalated: human item {judged.human.id} in {target}")
    raise SystemExit(EXIT_ESCALATED)


@handoff_group.command("schema")
@ROOT
@click.option(
    "--write", is_flag=True, help="Write the envelope and human item contracts."
)
def schema_command(root: Path | None, write: bool) -> None:
    """Print, or write, the JSON Schema draft-07 of the envelope and the human
    item, generated from their models."""
    schemas = handoff.contract_json_schemas()
    if not write:
        for rel, body in schemas.items():
            click.echo(f"# {rel}")
            click.echo(handoff.schema_text(body), nl=False)
        return
    base = _root(root)
    for rel, body in schemas.items():
        path, text = base / rel, handoff.schema_text(body)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            _write(path, text)
            click.echo(f"wrote {rel}")
