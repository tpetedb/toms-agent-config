"""`tac proof`: acceptance 8, the coverage manifest and the dev proof."""

from __future__ import annotations

from pathlib import Path

import click

from tac.config import load_config
from tac.doctor import find_root
from tac.proof import ProofError, conditions, coverage, dev
from tac.work import Bad

root_option = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
dirty_option = click.option(
    "--allow-dirty",
    is_flag=True,
    help="Run on uncommitted changes; the result then records tree_clean false.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


@click.group("proof")
def proof_group() -> None:
    """Acceptance 8: what the configuration claims, proved by tests that ran."""


@proof_group.command("coverage")
@root_option
@click.option("--list", "as_list", is_flag=True, help="Print the condition ids only.")
@click.option(
    "--map",
    "map_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The coverage map; defaults to dev/coverage/map.toml.",
)
@dirty_option
def coverage_command(
    root: Path | None, as_list: bool, map_path: Path | None, allow_dirty: bool
) -> None:
    """Every derived condition maps to a test that ran and passed.

    Refuses a condition without an entry, an entry for no condition and a test
    id that does not collect; writes dev/out/coverage.json bound to HEAD.
    """
    base = _root(root)
    try:
        if as_list:
            for condition in conditions(load_config(base), base):
                click.echo(condition.id)
            return
        result = coverage(base, allow_dirty=allow_dirty, map_path=map_path)
    except (ProofError, Bad) as exc:
        raise click.ClickException(str(exc)) from None
    for line in result.lines:
        click.echo(line)
    raise SystemExit(result.code)


@proof_group.command("dev")
@root_option
@click.option(
    "--require-live",
    is_flag=True,
    help="Exit non-zero while any live probe is skipped.",
)
@click.option(
    "--live",
    is_flag=True,
    help="Host only: run each live probe whose requirements hold through "
    "`tac receipt client`.",
)
@click.option(
    "--harness",
    type=click.Choice(["claude", "codex"]),
    default=None,
    help="The client --live probes.",
)
@click.option(
    "--doctor-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Judge the requirements on this `tac doctor --json` output instead.",
)
@dirty_option
def dev_command(
    root: Path | None,
    require_live: bool,
    live: bool,
    harness: str | None,
    doctor_json: Path | None,
    allow_dirty: bool,
) -> None:
    """The ledger's gates, the bypass and mutation runs, then the live probes.

    Prints `SKIP live: <probe>: <reason>` for each probe it cannot run and
    writes dev/out/dev-proof.json bound to HEAD.
    """
    try:
        result = dev(
            _root(root),
            require_live=require_live,
            live=live,
            harness=harness,
            allow_dirty=allow_dirty,
            doctor_json=doctor_json,
        )
    except (ProofError, Bad) as exc:
        raise click.ClickException(str(exc)) from None
    for line in result.lines:
        click.echo(line)
    raise SystemExit(result.code)
