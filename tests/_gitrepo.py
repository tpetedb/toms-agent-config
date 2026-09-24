"""Throwaway git repositories for the runner and receipt tests."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ORIGIN = "https://github.com/example/demo.git"
REPOSITORY = "example/demo"
# The host's git config must not sign, hook or rename anything in a fixture.
GIT = (
    "git",
    "-c",
    "user.name=fixture",
    "-c",
    "user.email=fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "init.defaultBranch=main",
)
# What a base revision carries of the deployed toolchain: enough to build it.
TOOLCHAIN = (".agents/pyproject.toml", ".agents/uv.lock", ".agents/lib/tac")
PROBES = """schema_version = 1

[probes.version]
description = "Discovery: the client reports its version."
kind = "version"
expect_exit = 0

[probes.trust]
description = "Trust: the client accepts this checkout."
kind = "trust"
expect_exit = 0
"""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [*GIT, "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def make_repo(root: Path, runner_pub: str | None = None) -> str:
    """A repository with an origin, the probe table and maybe a runner.pub."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", ORIGIN)
    write(root, ".agents/config/probes.toml", PROBES)
    if runner_pub is not None:
        write(root, ".agents/config/runner.pub", runner_pub)
    return commit_all(root, "base")


@contextlib.contextmanager
def short_dir() -> Iterator[Path]:
    """A folder with a short path, since AF_UNIX socket paths are capped."""
    path = Path(tempfile.mkdtemp(prefix="tac-", dir="/tmp")).resolve()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def copy_toolchain(root: Path, extra: tuple[str, ...] = ()) -> None:
    """This checkout's stamped toolchain, and any other files named, into `root`."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv")
    for relative in (*TOOLCHAIN, *extra):
        source, target = REPO / relative, root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copy2(source, target)
