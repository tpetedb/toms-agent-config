"""`tac runner` and `tac receipt`: the host process and the receipts it signs."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import click
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import tac
from tac.receipts import (
    RECEIPTS_GLOB,
    RUNNER_PUB,
    Binding,
    MissingTrustRoot,
    ReceiptError,
    load_public_key,
    parse,
    policy_hash,
    receipt_json_schema,
    resolve,
    trusted_key_from_revision,
    verify,
    verify_committed,
)
from tac.runner import (
    RunnerError,
    bind,
    controller_store,
    create_key,
    ensure_store,
    install_command,
    open_runner,
    pub_file_text,
    receipt_from,
    refuse_agent_parent,
    repo_top,
    request,
    require_own_venv,
    runner_venv,
    socket_path,
)

ORDER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

repo_option = click.option(
    "--repo",
    "repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Any folder inside the repository.",
)
socket_option = click.option(
    "--socket",
    "sock",
    type=click.Path(path_type=Path),
    default=None,
    help="The runner's socket. Defaults to the one in the controller store.",
)


def fail(message: str) -> None:
    click.echo(f"tac: {message}", err=True)
    raise SystemExit(1)


def default_socket(repo: Path) -> Path:
    return socket_path(controller_store(repo_top(repo), os.environ))


@click.group("runner")
def runner_group() -> None:
    """The host process that observes, signs receipts and keeps the store."""


@runner_group.command("where")
@repo_option
def runner_where(repo: Path) -> None:
    """Print the controller store of this repository."""
    try:
        click.echo(str(controller_store(repo_top(repo), os.environ)))
    except RunnerError as exc:
        fail(str(exc))


@runner_group.command("init")
@repo_option
@click.option(
    "--write-pub",
    is_flag=True,
    help=f"Also write the public key to {RUNNER_PUB}, to land by pull request.",
)
def runner_init(repo: Path, write_pub: bool) -> None:
    """Create the controller store and the signing key; host only, idempotent."""
    try:
        refuse_agent_parent(os.environ)
        top = repo_top(repo)
        store = ensure_store(controller_store(top, os.environ))
        private = create_key(store)
    except RunnerError as exc:
        fail(str(exc))
        return
    text = pub_file_text(private)
    click.echo(f"controller store: {store}")
    if write_pub:
        target = top / RUNNER_PUB
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        click.echo(f"wrote {RUNNER_PUB}; commit it and land it through a pull request")
    else:
        click.echo(text, nl=False)


@runner_group.command("install")
@repo_option
def runner_install(repo: Path) -> None:
    """Build the runner's own venv in the controller store; host only."""
    try:
        refuse_agent_parent(os.environ)
        top = repo_top(repo)
        store = ensure_store(controller_store(top, os.environ))
        argv, env = install_command(top, store, os.environ)
    except RunnerError as exc:
        fail(str(exc))
        return
    done = subprocess.run(argv, env=env, check=False)
    if done.returncode != 0:
        fail(f"uv sync into {runner_venv(store)} failed")
    click.echo(f"runner venv: {runner_venv(store)}")


@runner_group.command("serve")
@repo_option
@click.option(
    "--repository",
    default=None,
    help="owner/name to bind receipts to. Defaults to the origin remote.",
)
def runner_serve(repo: Path, repository: str | None) -> None:
    """Serve the socket in the foreground until interrupted; host only."""
    try:
        refuse_agent_parent(os.environ)
        runner = open_runner(repo, os.environ, repository=repository)
        require_own_venv(runner.store, Path(tac.__file__))
        server = bind(runner)
    except (RunnerError, ReceiptError) as exc:
        fail(str(exc))
        return
    click.echo(f"tac runner for {runner.repository} on {socket_path(runner.store)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("tac runner stopped")
    finally:
        server.server_close()


@runner_group.command("status")
@repo_option
@socket_option
def runner_status(repo: Path, sock: Path | None) -> None:
    """Ask the runner whether it is up, and with which key."""
    try:
        reply = request(sock or default_socket(repo), {"op": "ping"})
    except RunnerError as exc:
        fail(str(exc))
        return
    click.echo(f"runner is up, key id {reply['key_id']}")


@click.group("receipt")
def receipt_group() -> None:
    """Ask the runner for a signed receipt, or verify receipts from a trusted key."""


@receipt_group.command("schema")
def receipt_schema() -> None:
    """Print the JSON Schema of a signed receipt (contracts/receipt.schema.json)."""
    click.echo(json.dumps(receipt_json_schema(), indent=2, sort_keys=True))


@receipt_group.command("client")
@click.option("--harness", required=True, help="claude or codex.")
@click.option("--probe", "probe", required=True, help="A probe in probes.toml.")
@click.option("--order", "order", default=None, help="Also copy into this order.")
@click.option("--run", "run_id", default=None, help="Run id; defaults per probe.")
@repo_option
@socket_option
def receipt_client(
    harness: str,
    probe: str,
    order: str | None,
    run_id: str | None,
    repo: Path,
    sock: Path | None,
) -> None:
    """Have the runner probe an installed client and sign what it saw.

    Exits non-zero when what was observed differs from what was expected, or a
    required field is missing.
    """
    if order is not None and not ORDER_ID.match(order):
        fail(f"not an order id: {order!r}")
    try:
        top = repo_top(repo)
        reply = request(
            sock or default_socket(top),
            {
                "op": "probe",
                "harness": harness,
                "probe": probe,
                "run_id": run_id or f"probe-{harness}-{probe}",
            },
        )
        signed = receipt_from(reply)
    except RunnerError as exc:
        fail(str(exc))
        return
    text = signed.to_json()
    if order is not None:
        folder = top / "work" / "orders" / order / "receipts"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{signed.receipt.receipt_id}.json").write_text(text, "utf-8")
    click.echo(text, nl=False)
    if signed.receipt.observed.get("matched") is not True:
        raise SystemExit(1)


def trusted_key(root: Path, base: str, key_env: str | None) -> Ed25519PublicKey:
    """A provisioned key if named and set, else runner.pub at the base revision."""
    if key_env and os.environ.get(key_env):
        return load_public_key(os.environ[key_env])
    return trusted_key_from_revision(root, base)


key_env_option = click.option(
    "--key-env",
    default=None,
    help="An environment variable holding a separately provisioned runner.pub.",
)


@receipt_group.command("verify")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--base", required=True, help="Trusted revision to take runner.pub from.")
@click.option("--repository", required=True, help="owner/name expected.")
@click.option("--revision", required=True, help="Revision the receipt must name.")
@click.option("--run", "run_id", required=True)
@click.option("--stage", required=True)
@key_env_option
@repo_option
def receipt_verify(
    path: Path,
    base: str,
    repository: str,
    revision: str,
    run_id: str,
    stage: str,
    key_env: str | None,
    repo: Path,
) -> None:
    """Verify one receipt against an exact binding; the policy is recomputed."""
    try:
        top = repo_top(repo)
        commit = resolve(top, revision)
        expected = Binding(
            repository=repository,
            revision=commit,
            run_id=run_id,
            stage=stage,
            policy_hash=policy_hash(top, commit),
        )
        receipt = verify(
            parse(path.read_text("utf-8")), trusted_key(top, base, key_env), expected
        )
    except (ReceiptError, RunnerError, ValueError) as exc:
        fail(str(exc))
        return
    click.echo(f"verified {receipt.kind} receipt {receipt.receipt_id}")


@receipt_group.command("verify-tree")
@click.option("--base", required=True, help="Trusted revision to take runner.pub from.")
@click.option("--head", default="HEAD", show_default=True)
@click.option(
    "--repository",
    envvar="GITHUB_REPOSITORY",
    required=True,
    help="owner/name expected; defaults to GITHUB_REPOSITORY.",
)
@key_env_option
@repo_option
def receipt_verify_tree(
    base: str, head: str, repository: str, key_env: str | None, repo: Path
) -> None:
    """Verify every committed receipt under work/orders/ from the base's key."""
    try:
        top = repo_top(repo)
    except RunnerError as exc:
        fail(str(exc))
        return
    files = sorted(top.glob(RECEIPTS_GLOB))
    if not files:
        click.echo("no committed receipts")
        return
    try:
        key: Ed25519PublicKey | None = trusted_key(top, base, key_env)
    except MissingTrustRoot as exc:
        click.echo(f"tac: {exc}", err=True)
        key = None
    try:
        results = verify_committed(top, files, key, repository, head)
    except ReceiptError as exc:
        fail(str(exc))
        return
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        click.echo(f"{mark} {result.path}: {result.detail}")
    if not all(result.ok for result in results):
        raise SystemExit(1)
