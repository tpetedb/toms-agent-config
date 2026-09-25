"""`tac usage` and `tac usage record`: the usage reading every spawn checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from tac import usage
from tac.config import load_config
from tac.doctor import find_root
from tac.work import Bad

# Exit statuses, so a script can branch: spawning allowed, or paused.
EXIT_ALLOWED = 0
EXIT_PAUSED = 3

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


@click.group("usage", invoke_without_command=True)
@ROOT
@click.option(
    "--provider",
    "providers",
    multiple=True,
    type=click.Choice(usage.PROVIDERS),
    help="Judge only this provider; defaults to the enforced harnesses' providers.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the readings as JSON.")
@click.pass_context
def usage_group(
    ctx: click.Context,
    root: Path | None,
    providers: tuple[usage.Provider, ...],
    as_json: bool,
) -> None:
    """Print each provider's reading: ok, slow, stop or unavailable. Exit 0 when
    spawning is allowed and 3 when a provider pauses it."""
    if ctx.invoked_subcommand is not None:
        return
    base = _root(root)
    try:
        config = load_config(base)
        found = usage.readings(base, config, providers or None)
    except (Bad, usage.UsageError) as e:
        raise click.ClickException(str(e)) from None
    if as_json:
        click.echo(
            json.dumps(
                [
                    {"provider": r.provider, "state": r.state, "reason": r.reason}
                    for r in found
                ],
                indent=2,
            )
        )
    else:
        for reading in found:
            click.echo(f"{reading.provider}: {reading.state} ({reading.reason})")
    raise SystemExit(EXIT_PAUSED if any(r.paused for r in found) else EXIT_ALLOWED)


@usage_group.command("record")
@ROOT
@click.option(
    "--provider",
    required=True,
    type=click.Choice(usage.PROVIDERS),
    help="Whose windows.",
)
def record_command(root: Path | None, provider: str) -> None:
    """Read a statusline payload on stdin, as a statusline command receives it,
    keep its usage windows as the provider's sample, and print a short line for
    the status bar."""
    base = _root(root)
    try:
        config = load_config(base)
        sample = usage.record(base, config, provider, sys.stdin.read())
    except (Bad, usage.UsageError) as e:
        raise click.ClickException(str(e)) from None
    limits = sample.rate_limits
    if limits is None:
        click.echo("usage: waiting for the first response")
        return
    parts = [
        f"{label} {window.used_percentage:.0f}%"
        for label, window in (
            ("5h", limits.five_hour),
            ("7d", limits.seven_day),
            ("spend", limits.spend_limit),
        )
        if window is not None
    ]
    click.echo("usage: " + (" ".join(parts) or "no window"))
