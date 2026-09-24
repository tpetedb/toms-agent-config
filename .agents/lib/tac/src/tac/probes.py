"""Client probes for `tac doctor`: discovery, a minimal launch, and project trust.

The launch is `<client> --version` and nothing else, so no model session starts
and no token is spent. Trust is read from each client's own user config, taking
only the one key that says whether this checkout is trusted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# The harnesses release 1 enforces, by the executable each installs.
CLIENTS = {"claude": "claude", "codex": "codex"}
VERSION_TIMEOUT_S = 20
# Where each client asks its owner to trust a folder (TODO.HUMAN.md).
TRUST_STEPS = {
    "claude": "open `claude` once in this folder and accept workspace trust",
    "codex": "start `codex` here once and trust the project, or run `tac init --user`",
}


@dataclass(frozen=True, slots=True)
class Trust:
    """trusted is None when the client's config could not be read."""

    trusted: bool | None
    detail: str


def discover(harness: str, search_path: str) -> str | None:
    return shutil.which(CLIENTS[harness], path=search_path)


def launch_env(executable: str, environ: Mapping[str, str]) -> dict[str, str]:
    """A fixed environment: the client's own folder first (a node client needs
    the node beside it), then the system folders, and no caller variables."""
    env = {
        "PATH": os.pathsep.join([str(Path(executable).parent), "/usr/bin", "/bin"]),
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if environ.get("HOME"):
        env["HOME"] = environ["HOME"]
    return env


def client_version(
    executable: str, environ: Mapping[str, str], timeout: float = VERSION_TIMEOUT_S
) -> str | None:
    """The first line `--version` prints, or None when it did not answer."""
    try:
        done = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=launch_env(executable, environ),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = done.stdout.strip().splitlines()
    return lines[0].strip() if done.returncode == 0 and lines else None


def claude_config(environ: Mapping[str, str]) -> Path:
    if environ.get("CLAUDE_CONFIG_DIR"):
        return Path(environ["CLAUDE_CONFIG_DIR"]).expanduser() / ".claude.json"
    return Path(environ.get("HOME", str(Path.home()))) / ".claude.json"


def codex_config(environ: Mapping[str, str]) -> Path:
    if environ.get("CODEX_HOME"):
        return Path(environ["CODEX_HOME"]).expanduser() / "config.toml"
    return Path(environ.get("HOME", str(Path.home()))) / ".codex" / "config.toml"


def claude_trust(root: Path, config: Path) -> Trust:
    """Accepting the trust dialog in a folder also covers the folders below it."""
    if not config.is_file():
        return Trust(False, "claude has no user config yet")
    try:
        projects = json.loads(config.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError, AttributeError):
        return Trust(None, f"cannot read {config.name}")
    if not isinstance(projects, Mapping):
        return Trust(None, f"{config.name} has no projects table")
    here = root.resolve()
    for folder in (here, *here.parents):
        entry = projects.get(str(folder))
        if isinstance(entry, Mapping) and entry.get("hasTrustDialogAccepted") is True:
            # A shareable report names no absolute path.
            where = "this folder" if folder == here else "a parent folder"
            return Trust(True, f"trusted through {where}")
    return Trust(False, "this checkout is not trusted")


def codex_trust(candidates: Sequence[Path], config: Path) -> Trust:
    """Codex keys trust by the project or worktree path; untrusted wins.

    candidates[0] is the checkout itself, the rest the main checkout above it.
    """
    if not config.is_file():
        return Trust(False, "codex has no user config yet")
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return Trust(None, f"cannot read {config.name}")
    projects = data.get("projects", {})
    if not isinstance(projects, Mapping):
        return Trust(None, f"{config.name} has no projects table")
    levels: list[tuple[int, object]] = []
    for index, path in enumerate(candidates):
        entry = projects.get(str(path.resolve()))
        if isinstance(entry, Mapping) and "trust_level" in entry:
            levels.append((index, entry["trust_level"]))
    if any(level == "untrusted" for _, level in levels):
        return Trust(False, "this project is marked untrusted")
    for index, level in levels:
        if level == "trusted":
            # A shareable report names no absolute path.
            where = "this checkout" if index == 0 else "the main checkout"
            return Trust(True, f"trusted through {where}")
    return Trust(False, "this checkout is not trusted")


def trust_candidates(root: Path) -> list[Path]:
    """The checkout itself and, for a linked worktree, the main checkout."""
    found = [root.resolve()]
    try:
        done = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    common = Path(done.stdout.strip()) if done.returncode == 0 else None
    if common is not None and common.name == ".git":
        main = common.parent.resolve()
        if main not in found:
            found.append(main)
    return found


def project_trust(harness: str, root: Path, environ: Mapping[str, str]) -> Trust:
    if harness == "claude":
        return claude_trust(root, claude_config(environ))
    return codex_trust(trust_candidates(root), codex_config(environ))
