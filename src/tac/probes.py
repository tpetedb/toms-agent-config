"""Client probes for `tac doctor`: discovery, a minimal launch, and project trust.

The launch is `<client> --version` and nothing else, so no model session starts
and no token is spent. Trust is read from each client's own user config, taking
only the one key that says whether this checkout is trusted.

The live probes of acceptance 8 are defined here as observations the runner will
make of a real client session; until it makes them, each is refused by name with
NotYetLive, so nothing reports a live probe passed without a runner receipt.
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
from typing import get_args

from tac.config_schema import LiveProbeKind

# The harnesses release 1 enforces, by the executable each installs.
CLIENTS = {"claude": "claude", "codex": "codex"}
VERSION_TIMEOUT_S = 20
# Where Codex finds a project's hooks; each is trusted by the hash of its
# definition, separately from the project (design section 9).
CODEX_HOOK_SOURCES = (".codex/hooks.json", ".codex/config.toml")
CODEX_HOOK_STEP = "run `codex`, then `/hooks`, and approve the run.py entries once"
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


def codex_project_hooks(root: Path) -> list[str]:
    """The project files that define Codex hooks, relative to the checkout."""
    found: list[str] = []
    hooks_json = root / CODEX_HOOK_SOURCES[0]
    if hooks_json.is_file():
        found.append(CODEX_HOOK_SOURCES[0])
    config = root / CODEX_HOOK_SOURCES[1]
    if config.is_file():
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {"hooks": "unreadable"}
        if data.get("hooks"):
            found.append(CODEX_HOOK_SOURCES[1])
    return found


def codex_hook_trust(root: Path) -> Trust:
    """Hook trust, reported apart from project trust.

    Codex keeps the approved hashes in its own state, whose layout it does not
    document, so a checkout with hooks stays undecided until a signed probe of
    a hook firing (design section 13) can witness it.
    """
    sources = codex_project_hooks(root)
    if not sources:
        return Trust(True, "no project hooks to trust yet")
    return Trust(
        None,
        f"hooks in {', '.join(sources)} run only once approved by hash; "
        f"the owner's step: {CODEX_HOOK_STEP}",
    )


# ---------------------------------------------------------------- live probes

# Where a live probe starts its session, and which kind of session it is.
PLACES = ("root", "subdir", "worktree")
SESSIONS = ("fresh", "resumed", "headless")
# What each live kind has the client do, and what the receipt must show. The
# receipt records what the client actually did as well (design section 18, C1).
LIVE_OBSERVATIONS: Mapping[str, tuple[str, str]] = {
    "spawn-missing-token": (
        "a native spawn from a session the runner dispatched, whose record holds "
        "no token",
        "the guard refuses the spawn: exit 2 and a deny",
    ),
    "spawn-altered-token": (
        "a native spawn presenting its dispatch token with one character changed",
        "the guard refuses the spawn and the runner burns nothing else",
    ),
    "spawn-reused-token": (
        "two spawns presenting the same single-use dispatch token",
        "exactly one spawn passes and the other is refused",
    ),
    "spawn-tool-absent": (
        "a session under the delegation-off profile asking for its spawn tool",
        "the spawn tool is absent from the session",
    ),
    "runner-lost": (
        "a spawn whose token check loses the runner before the reply",
        "the guard refuses the spawn",
    ),
    "hook-timeout": (
        "a spawn whose checker does not answer within the guard's deadline",
        "the guard answers deny before the client's hook timeout; the receipt "
        "records what the client then did",
    ),
    "keychain-token-denied": (
        "`security find-generic-password` for the tac-bot token entry inside the "
        "sandboxed session",
        "the read fails",
    ),
    "keychain-key-denied": (
        "`security find-generic-password` for the runner signing key entry inside "
        "the sandboxed session",
        "the read fails",
    ),
    "ruleset-owner-review": (
        "a pull request by the machine account that edits src/tac without the "
        "owner's review",
        "the ruleset refuses the merge",
    ),
    "push-required-checks": (
        "a push to the default branch without the required checks",
        "the ruleset refuses the push",
    ),
    "classifier-script-computed": (
        "a registered Workflow run in auto mode",
        "the classifier marks the workflow's prompts as script-computed",
    ),
}


class NotYetLive(Exception):
    """A live probe that does not run here, and why: not asked for with --live,
    a requirement not met, or a kind the runner does not observe yet."""

    def __init__(self, probe: str, reason: str) -> None:
        super().__init__(f"{probe} is not live yet: {reason}")
        self.probe = probe
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Observation:
    """What the runner starts for a live probe and what it holds the client to."""

    probe: str
    kind: str
    harness: str | None
    place: str
    session: str
    does: str
    expect: str


def live_kind(kind: str) -> bool:
    return kind in get_args(LiveProbeKind)


def observe(
    probe: str,
    kind: str,
    harness: str | None,
    place: str,
    session: str,
    *,
    live: bool,
    unmet: Sequence[str],
) -> Observation:
    """The observation a live probe asks of the runner, or NotYetLive: while any
    requirement is unmet, or without --live, nothing is started."""
    if not live_kind(kind):
        raise ValueError(f"{kind!r} is not a live probe kind")
    if unmet:
        raise NotYetLive(probe, "; ".join(unmet))
    if not live:
        raise NotYetLive(probe, "not asked for; run with --live on the host")
    does, expect = LIVE_OBSERVATIONS[kind]
    return Observation(probe, kind, harness, place, session, does, expect)
