"""`tac runner`: the one host process that observes, signs and keeps the store.

The skeleton of section 10 of docs/DESIGN.md. It owns the controller store, a
per-repository folder under the user state directory that is never inside the
repository, and answers on a Unix socket there. It signs only what it observed
itself: it runs the gate or the probe and records the result, and no request can
hand it an exit code or a payload to sign (build condition C3).

It serves only from its own venv in the controller store, built non-editable
from the stamped package, so it never imports code from a checkout an agent can
write. The signing key is a file in the controller store, mode 0600, until the
keychain backend of milestone M3 (build condition C6) replaces it.

A gate runs code a builder wrote: the justfile recipe, an order's criteria as
shell strings, the tests `just verify` starts. The runner never runs that code
with access to its store, nor lets it write anywhere the owner's own processes
later load code from. Every child that touches the candidate tree (the gate and
the runner's own `git status`, which can start a configured fsmonitor or filter)
starts under a macOS Seatbelt profile from /usr/bin/sandbox-exec. Writes are an
allowlist: the checkout minus its toolchain, git and harness folders, and a
private scratch folder that is the child's HOME, TMPDIR and caches; nothing else.
The store and the key are denied to reads as well. Connects are an allowlist too:
Unix sockets only in the scratch folder, plus the DNS resolver's socket, no
IPv4 loopback and no outbound IPv6 (a v4-mapped address reaches loopback), since
a socket of a process the owner runs unsandboxed (tmux, Docker) would run any
command for the child. Apple Events, LaunchServices and
preference writes are denied, since each asks a process outside the sandbox to
act. The child gets only the variables in GATE_ENV_VARS, the runner's PATH and
the scratch locations. The gate runs in the
checkout at the committed revision it judges, and a gate that leaves HEAD moved
or the tree changed gets no receipt. Where there is no sandbox-exec (not macOS),
the runner refuses to gate and never falls back to running unsandboxed. A client
probe starts the claude or codex executable the same way, with no write to the
tree, and is refused where the sandbox is missing.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Annotated, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from tac.probes import project_trust
from tac.receipts import (
    ID_PATTERN,
    ORDER_PATTERN,
    Binding,
    ReceiptError,
    SignedReceipt,
    git,
    key_id,
    policy_hash,
    public_pem,
    resolve,
    sign,
)

SOCKET_NAME = "runner.sock"
LOCK_NAME = "runner.lock"
KEY_NAME = "signing-key.pem"
VENV_NAME = "venv"
AGENTS_PROJECT = ".agents"
PROBES = ".agents/config/probes.toml"
# AF_UNIX paths are capped at 104 bytes on macOS, 108 on Linux.
MAX_SOCKET_PATH = 103
GATE_TIMEOUT_S = 1800
PROBE_TIMEOUT_S = 60
MAX_REQUEST_BYTES = 64 * 1024
# A gate is `just <recipe> [args]` and nothing else: every other just flag
# (--command, --justfile, --shell, --set, overrides) would run code the runner
# did not choose. The table names each recipe a gate may run and how many
# arguments it takes; arguments are ids, never flags, paths or shell text.
GATE_PROGRAM = "just"
DEFAULT_GATE_RECIPES: Mapping[str, int] = MappingProxyType(
    {
        "verify": 0,
        "lint-ci": 0,
        "private-scan": 0,
        "work-validate": 0,
        "work-check": 1,
        "work-review": 1,
        "work-accept": 1,
    }
)
RECIPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
GATE_ARG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# The clients a probe may start, by harness; the executable is never read from
# a file an agent could write.
HARNESS_EXECUTABLES = {"claude": "claude", "codex": "codex"}
# The variables that locate each client's user config, the only part of the
# runner's environment a trust probe reads.
CLIENT_CONFIG_VARS = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
# Set in the shells that Claude Code and a sandboxed Codex start. A tripwire,
# not a boundary: an agent can unset them, which is why the key moves to the
# keychain in M3.
AGENT_MARKERS = ("CLAUDECODE", "CODEX_SANDBOX")
# Clients whose process, anywhere above the runner, means an agent started it.
# Harder to shed than a variable, still a tripwire: a detached start escapes it.
AGENT_PROCESSES = ("claude", "codex")
AGENT_PACKAGES = ("claude-code", "codex")
MAX_ANCESTORS = 64
# Seatbelt by absolute path: never a sandbox-exec found on a PATH.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# All a gate child inherits besides the runner's PATH and its scratch folder. No
# token, no GIT_* that could point git elsewhere, no TAC_STATE_HOME naming the
# store, and not the owner's HOME, whose config the owner's processes load.
GATE_ENV_VARS = ("USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
# Inside the checkout, what a builder's own sandbox cannot write either: the
# stamped toolchain and the judge's venv, the git directory (hooks, config) and
# the harness config whose hooks run unsandboxed.
GATE_PROTECTED = (".agents", ".git", ".claude", ".codex")
# Device files a shell or a test writes to; every other write outside the
# allowlist is refused.
GATE_DEVICES = (
    "/dev/null",
    "/dev/zero",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/stdout",
    "/dev/stderr",
)
# The one Unix socket outside its scratch folder a gate child may connect to:
# name resolution goes through it. Every other socket may belong to a process
# the owner runs unsandboxed (tmux, Docker), which would act for the child.
GATE_SOCKETS = ("/private/var/run/mDNSResponder",)
# Services that open a file or start a program outside the sandbox.
LAUNCH_SERVICES = ("com.apple.coreservices.launchservicesd",)
LAUNCH_SERVICE_PREFIXES = ("com.apple.lsd.",)
# A scratch folder lives under TMPDIR when that path is this short, else under
# /tmp: a test a gate runs may bind a Unix socket under its TMPDIR, and inside a
# gate only the gate's own short TMPDIR is writable.
SHORT_TMP = 40
GIT_STATUS_TIMEOUT_S = 120
UNIX_PERMS_STORE = 0o700
UNIX_PERMS_KEY = 0o600


class RunnerError(Exception):
    """The runner refuses: the message says why."""


# ---- where the controller store lives


def state_home(environ: Mapping[str, str]) -> Path:
    """The user state folder for tac: TAC_STATE_HOME, else XDG, else ~/.local/state."""
    if environ.get("TAC_STATE_HOME"):
        return Path(environ["TAC_STATE_HOME"]).expanduser()
    xdg = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg).expanduser() / "tac"


def git_common_dir(root: Path) -> Path:
    done = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if done.returncode != 0:
        raise RunnerError(f"not a git repository: {root}")
    return Path(done.stdout.strip()).resolve()


def repo_top(root: Path) -> Path:
    done = git(root, "rev-parse", "--show-toplevel")
    if done.returncode != 0:
        raise RunnerError(f"not a git repository: {root}")
    return Path(done.stdout.strip()).resolve()


def store_slug(root: Path) -> str:
    """One store per repository, shared by every worktree of it."""
    common = git_common_dir(root)
    name = common.parent.name if common.name == ".git" else common.name
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name).removesuffix(".git") or "repo"
    digest = hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:10]
    return f"{name}-{digest}"


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def controller_store(root: Path, environ: Mapping[str, str]) -> Path:
    store = (state_home(environ) / store_slug(root)).resolve()
    for forbidden in (repo_top(root), git_common_dir(root)):
        if _inside(store, forbidden):
            raise RunnerError(
                "the controller store must live outside the repository and its "
                f"git directory; set TAC_STATE_HOME elsewhere (got {store})"
            )
    return store


def ensure_store(store: Path) -> Path:
    store.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
    store.chmod(UNIX_PERMS_STORE)
    (store / "receipts").mkdir(exist_ok=True, mode=UNIX_PERMS_STORE)
    return store


def ancestor_commands(pid: int) -> list[str]:
    """The command lines of pid and every process above it, nearest first.

    Empty when ps cannot answer: this is a tripwire, not the boundary.
    """
    commands: list[str] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(commands) < MAX_ANCESTORS:
        seen.add(pid)
        try:
            done = subprocess.run(
                ["ps", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        parts = done.stdout.strip().split(None, 1)
        if done.returncode != 0 or not parts or not parts[0].isdigit():
            break
        commands.append(parts[1] if len(parts) > 1 else "")
        pid = int(parts[0])
    return commands


def agent_command(commands: list[str]) -> str | None:
    """The first command line that is an agent client, by executable or package."""
    for command in commands:
        for token in command.split()[:2]:
            path = Path(token)
            if path.name in AGENT_PROCESSES or any(
                part in AGENT_PACKAGES for part in path.parts
            ):
                return command
    return None


def agent_session(
    environ: Mapping[str, str], ancestors: list[str] | None = None
) -> str | None:
    """Why this process looks like part of an agent session, or None.

    A tripwire, not the boundary: an agent can unset a variable or detach.
    """
    markers = [name for name in AGENT_MARKERS if environ.get(name)]
    if markers:
        return f"{', '.join(markers)} is set"
    chain = ancestor_commands(os.getppid()) if ancestors is None else ancestors
    found = agent_command(chain)
    if found is not None:
        name = Path(found.split()[0]).name if found.split() else "?"
        return f"{name} is among its parents"
    return None


def refuse_agent_parent(
    environ: Mapping[str, str], ancestors: list[str] | None = None
) -> None:
    reason = agent_session(environ, ancestors)
    if reason is not None:
        raise RunnerError(
            "the runner is a host process and never a child of an agent session "
            f"({reason}); start it from the owner's terminal"
        )


# ---- the runner's own venv


def runner_venv(store: Path) -> Path:
    return store / VENV_NAME


def install_command(
    top: Path, store: Path, environ: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """uv sync of the agent toolchain into the store's venv, non-editable.

    The same lock and the same stamped source as .agents/.venv, installed where
    no agent can write, so the runner never imports from a worktree.
    """
    uv = shutil.which("uv", path=environ.get("PATH", ""))
    if uv is None:
        raise RunnerError("uv is not on PATH; run bootstrap.sh")
    project = top / AGENTS_PROJECT
    if not (project / "pyproject.toml").is_file():
        raise RunnerError(f"no {AGENTS_PROJECT}/pyproject.toml in {top}")
    env = {k: v for k, v in environ.items() if k != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(runner_venv(store))
    argv = [uv, "sync", "--frozen", "--no-editable", "--project", str(project)]
    return argv, env


def require_own_venv(store: Path, module_file: Path) -> None:
    """Serve only when tac was imported from the store's venv site-packages."""
    venv = runner_venv(store).resolve()
    resolved = module_file.resolve()
    if not resolved.is_relative_to(venv) or "site-packages" not in resolved.parts:
        raise RunnerError(
            f"the runner imports tac from {resolved.parent}, not from its own "
            f"venv at {venv}; run `just runner-install`, then start it with "
            "`just runner`"
        )


# ---- the signing key


def key_path(store: Path) -> Path:
    return store / KEY_NAME


def create_key(store: Path) -> Ed25519PrivateKey:
    """Generate the signing key once; an existing key is never replaced."""
    path = key_path(store)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, UNIX_PERMS_KEY)
    except FileExistsError:
        return load_key(store)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return private


def load_key(store: Path) -> Ed25519PrivateKey:
    path = key_path(store)
    if path.is_symlink():
        # A link would put the key outside the subtree the gate sandbox denies.
        raise RunnerError(f"{KEY_NAME} must be a plain file in the store, not a link")
    if not path.is_file():
        raise RunnerError("no signing key; run `tac runner init` on the host")
    if path.stat().st_mode & 0o077:
        raise RunnerError(f"{KEY_NAME} is readable by others; it must be mode 0600")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RunnerError(f"{KEY_NAME} is not an ed25519 key")
    return key


def pub_file_text(private: Ed25519PrivateKey) -> str:
    return (
        "# The trusted runner's ed25519 public key. CI verifies receipts against\n"
        "# the copy on the base revision, never the candidate's.\n"
        f"# key id {key_id(private.public_key())}\n" + public_pem(private.public_key())
    )


# ---- the sandbox every child that runs candidate content starts in


def inside_sandbox() -> bool:
    """True when this process already runs under a Seatbelt profile.

    A profile cannot be applied inside another, so a runner started there (or
    the runner's tests when a gate runs them) has no sandbox to give a child.
    """
    if sys.platform != "darwin":
        return False
    try:
        check = ctypes.CDLL(None).sandbox_check
    except (OSError, AttributeError):
        return False
    check.restype = ctypes.c_int
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return check(os.getpid(), None, 0) != 0


def default_sandbox() -> str | None:
    """sandbox-exec on macOS outside a sandbox, else None: a gate is then refused."""
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        return None
    if inside_sandbox():
        return None
    return SANDBOX_EXEC


def _sbpl(value: str) -> str:
    return json.dumps(value)


def seatbelt_profile(
    store: Path,
    writable: Sequence[Path],
    protected: Sequence[Path] = (),
    sockets: Sequence[Path] = (),
) -> tuple[str, dict[str, str]]:
    """The profile and its parameters: writes only where allowed, nothing in the store.

    Writes are denied everywhere, then allowed under `writable` and the device
    files, then denied again under `protected`; Seatbelt applies the last rule
    that matches. Connects to Unix sockets are denied too, then allowed under
    `sockets` and to GATE_SOCKETS, and IPv4 loopback and all outbound IPv6 are
    denied: a socket or port
    outside the child's own folder may belong to a process the owner runs
    unsandboxed, which would run a command for it. Seatbelt matches paths at the
    moment of access, so a child that renamed a folder above the store would
    reach the key under a new path; every ancestor is therefore denied writes,
    which blocks the rename.
    """
    store = store.resolve()
    params = {"STORE": str(store), "KEY": str(key_path(store))}
    ancestors: list[str] = []
    for index, folder in enumerate(store.parents):
        params[f"UP{index}"] = str(folder)
        ancestors.append(f'(literal (param "UP{index}"))')
    allowed = [f"(literal {_sbpl(device)})" for device in GATE_DEVICES]
    allowed.append('(regex #"^/dev/fd/[0-9]+$")')
    for index, folder in enumerate(writable):
        params[f"W{index}"] = str(folder.resolve())
        allowed.append(f'(subpath (param "W{index}"))')
    denied: list[str] = []
    for index, folder in enumerate(protected):
        params[f"P{index}"] = str(folder.resolve())
        denied.append(f'(subpath (param "P{index}"))')
    reachable = [f"(literal {_sbpl(name)})" for name in GATE_SOCKETS]
    for index, folder in enumerate(sockets):
        params[f"S{index}"] = str(folder.resolve())
        reachable.append(f'(subpath (param "S{index}"))')
    services = [f"(global-name {_sbpl(name)})" for name in LAUNCH_SERVICES] + [
        f"(global-name-prefix {_sbpl(prefix)})" for prefix in LAUNCH_SERVICE_PREFIXES
    ]
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* {' '.join(allowed)})",
    ]
    if denied:
        rules.append(f"(deny file-write* {' '.join(denied)})")
    rules += [
        "(deny network-outbound (remote unix-socket))",
        # One rule per place: filters listed inside one `remote` must all match.
        *(
            f"(allow network-outbound (remote unix-socket {each}))"
            for each in reachable
        ),
        # IPv4 loopback and all outbound IPv6: a v4-mapped address such as
        # ::ffff:127.0.0.1 reaches an IPv4 loopback service past a localhost rule,
        # and SBPL cannot name the mapped range alone. `ip4`, not `ip`: next to an
        # ip6 rule, `(remote ip "localhost:*")` stops matching 127.0.0.1.
        '(deny network-outbound (remote ip4 "localhost:*"))',
        '(deny network-outbound (remote ip6 "*:*"))',
        '(deny file-read* file-write* (subpath (param "STORE")))',
        '(deny file-read* file-write* (literal (param "KEY")))',
        '(deny network-outbound (remote unix-socket (subpath (param "STORE"))))',
        f"(deny file-write* {' '.join(ancestors)})",
        "(deny appleevent-send)",
        "(deny user-preference-write)",
        f"(deny mach-lookup {' '.join(services)})",
    ]
    return "\n".join(rules), params


def gate_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: environ[k] for k in GATE_ENV_VARS if k in environ}


# ---- what a request may say: never an outcome, only what to observe


class Ping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["ping"]


class PubKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["pubkey"]


class Gate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["gate"]
    run_id: str = Field(pattern=ID_PATTERN)
    stage: str = Field(pattern=ID_PATTERN)
    argv: list[str] = Field(min_length=1)
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["probe"]
    harness: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    probe: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    run_id: str = Field(pattern=ID_PATTERN)
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)


Request = Annotated[Ping | PubKey | Gate | Probe, Field(discriminator="op")]
REQUEST = TypeAdapter(Request)


class ProbeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    description: str
    kind: Literal["version", "trust"]
    expect_exit: int = 0


class ProbesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    probes: dict[str, ProbeSpec]


def origin_repository(root: Path) -> str:
    """owner/name of the origin remote; the runner refuses without one."""
    done = git(root, "remote", "get-url", "origin")
    url = done.stdout.strip()
    match = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    if done.returncode != 0 or match is None:
        raise RunnerError("cannot name the repository: no usable origin remote")
    return f"{match.group(1)}/{match.group(2)}"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class Runner:
    root: Path
    store: Path
    private: Ed25519PrivateKey
    repository: str
    recipes: Mapping[str, int] = DEFAULT_GATE_RECIPES
    # The PATH the runner resolves programs and clients on: its own, not a caller's.
    search_path: str = field(default_factory=lambda: os.environ.get("PATH", ""))
    client_env: Mapping[str, str] = field(
        default_factory=lambda: {
            k: os.environ[k] for k in CLIENT_CONFIG_VARS if k in os.environ
        }
    )
    # None where no sandbox exists: every gate is then refused.
    sandbox: str | None = field(default_factory=default_sandbox)
    gate_env: Mapping[str, str] = field(
        default_factory=lambda: gate_environment(os.environ)
    )

    def handle(self, raw: object) -> dict[str, JsonValue]:
        request = REQUEST.validate_python(raw)
        if isinstance(request, Gate):
            return self.gate(request)
        if isinstance(request, Probe):
            return self.probe(request)
        if isinstance(request, PubKey):
            return {"pem": public_pem(self.private.public_key())}
        return {"pong": True, "key_id": key_id(self.private.public_key())}

    # -- observation

    # -- isolation

    def confined(
        self, argv: list[str], scratch: Path, *, write_tree: bool
    ) -> list[str]:
        """argv under the Seatbelt profile, or a refusal; never argv bare.

        The child writes its scratch folder and, when `write_tree`, the checkout
        minus GATE_PROTECTED and the git common dir; nothing else. It connects
        only to Unix sockets in its scratch folder and to GATE_SOCKETS.
        """
        if self.sandbox is None:
            raise RunnerError(
                "the runner runs candidate code only inside a macOS Seatbelt "
                f"sandbox and cannot apply one here (platform {sys.platform}, "
                f"{SANDBOX_EXEC} missing, or the runner itself already sandboxed); "
                "it refuses to gate or probe rather than run unsandboxed"
            )
        writable = [scratch, self.root] if write_tree else [scratch]
        protected = [self.root / name for name in GATE_PROTECTED]
        protected.append(git_common_dir(self.root))
        profile, params = seatbelt_profile(
            self.store, writable, protected, sockets=[scratch]
        )
        defines = [part for k, v in params.items() for part in ("-D", f"{k}={v}")]
        return [self.sandbox, "-p", profile, *defines, *argv]

    @contextlib.contextmanager
    def scratch(self) -> Iterator[Path]:
        """A private folder per child, removed after it: its HOME, TMPDIR, caches."""
        base = Path(tempfile.gettempdir()).resolve()
        parent = base if len(str(base)) <= SHORT_TMP else Path("/tmp")
        folder = Path(tempfile.mkdtemp(prefix="tac-gate-", dir=parent)).resolve()
        try:
            for name in ("home", "tmp", "cache"):
                (folder / name).mkdir(mode=0o700)
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def child_env(self, scratch: Path) -> dict[str, str]:
        env = gate_environment(self.gate_env)
        env["PATH"] = self.search_path
        env["HOME"] = str(scratch / "home")
        env["TMPDIR"] = str(scratch / "tmp")
        env["XDG_CACHE_HOME"] = str(scratch / "cache")
        env["UV_CACHE_DIR"] = str(scratch / "cache" / "uv")
        return env

    def clean_revision(self) -> str:
        # Sandboxed, and with no write to the tree: a repository's config can
        # make `git status` start an fsmonitor or a filter, which is candidate
        # code.
        program = self.which("git")
        with self.scratch() as scratch:
            argv = self.confined(
                [
                    program,
                    *("-C", str(self.root), "-c", "core.fsmonitor=false", "status"),
                    *("--porcelain", "--untracked-files=normal"),
                ],
                scratch,
                write_tree=False,
            )
            try:
                status = subprocess.run(
                    argv,
                    env=self.child_env(scratch),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=GIT_STATUS_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError("git status did not finish") from exc
        if status.returncode != 0:
            raise RunnerError(f"cannot read the working tree: {status.stderr.strip()}")
        if status.stdout.strip():
            raise RunnerError(
                "the working tree has uncommitted changes; a receipt names a "
                "revision, so the runner observes committed trees only"
            )
        return resolve(self.root, "HEAD")

    def binding(self, run_id: str, stage: str, order_id: str | None) -> Binding:
        revision = self.clean_revision()
        return Binding(
            repository=self.repository,
            revision=revision,
            run_id=run_id,
            stage=stage,
            policy_hash=policy_hash(self.root, revision),
            order_id=order_id,
        )

    def which(self, name: str) -> str:
        found = shutil.which(name, path=self.search_path)
        if found is None:
            raise RunnerError(f"{name} is not installed on the runner's PATH")
        return found

    def gate_argv(self, requested: list[str]) -> list[str]:
        """The command line for `just <recipe> [args]`, or a refusal."""
        if requested[0] != GATE_PROGRAM or len(requested) < 2:
            raise RunnerError(
                f"{' '.join(requested[:2])!r} is not a gate; a gate is "
                f"`{GATE_PROGRAM} <recipe> [args]`"
            )
        recipe, args = requested[1], requested[2:]
        if not RECIPE_PATTERN.match(recipe) or recipe not in self.recipes:
            raise RunnerError(
                f"{recipe!r} is not a gate recipe; allowed: "
                + ", ".join(sorted(self.recipes))
            )
        if len(args) != self.recipes[recipe]:
            raise RunnerError(
                f"gate recipe {recipe} takes {self.recipes[recipe]} argument(s), "
                f"not {len(args)}"
            )
        for arg in args:
            if not GATE_ARG_PATTERN.match(arg):
                raise RunnerError(f"gate argument {arg!r} is not an id")
        justfile = self.root / "justfile"
        # Named explicitly so no search, no JUST_JUSTFILE and no parent justfile
        # can choose which file the recipe comes from.
        return [
            self.which(GATE_PROGRAM),
            "--justfile",
            str(justfile),
            "--working-directory",
            str(self.root),
            recipe,
            *args,
        ]

    def gate(self, request: Gate) -> dict[str, JsonValue]:
        command = self.gate_argv(list(request.argv))
        # Refuses where there is no sandbox, before anything runs.
        binding = self.binding(request.run_id, request.stage, request.order_id)
        started = time.monotonic()
        with self.scratch() as scratch:
            argv = self.confined(command, scratch, write_tree=True)
            try:
                done = subprocess.run(
                    argv,
                    cwd=self.root,
                    env=self.child_env(scratch),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=GATE_TIMEOUT_S,
                    check=False,
                )
                exit_code, out, err = done.returncode, done.stdout, done.stderr
            except subprocess.TimeoutExpired as exc:
                exit_code, out, err = -1, exc.stdout or b"", exc.stderr or b""
        # The receipt names a revision: the gate must have judged exactly that.
        try:
            after = self.clean_revision()
        except RunnerError as exc:
            raise RunnerError(
                f"the gate changed the checkout, so no receipt: {exc}"
            ) from exc
        if after != binding.revision:
            raise RunnerError("the gate moved HEAD, so no receipt")
        observed: dict[str, JsonValue] = {
            "argv": list(request.argv),
            "exit": exit_code,
            "duration_s": round(time.monotonic() - started, 3),
            "stdout_sha256": _sha(out),
            "stderr_sha256": _sha(err),
        }
        return self.issue("gate", binding, observed)

    def probe_spec(self, revision: str, name: str) -> ProbeSpec:
        # The committed file at the observed revision, the one the policy hash covers.
        done = git(self.root, "show", f"{revision}:{PROBES}")
        if done.returncode != 0:
            raise RunnerError(f"{PROBES} is missing at {revision[:12]}")
        try:
            probes = ProbesFile.model_validate(tomllib.loads(done.stdout))
        except (tomllib.TOMLDecodeError, PydanticValidationError) as exc:
            raise RunnerError(f"{PROBES} is invalid: {exc}") from exc
        if name not in probes.probes:
            raise RunnerError(f"no probe named {name!r} in {PROBES}")
        return probes.probes[name]

    def probe(self, request: Probe) -> dict[str, JsonValue]:
        if request.harness not in HARNESS_EXECUTABLES:
            raise RunnerError(
                f"unknown harness {request.harness!r}; known: "
                + ", ".join(sorted(HARNESS_EXECUTABLES))
            )
        binding = self.binding(
            request.run_id, f"probe.{request.probe}", request.order_id
        )
        spec = self.probe_spec(binding.revision, request.probe)
        executable = self.which(HARNESS_EXECUTABLES[request.harness])
        # A client's startup code is not the runner's: it gets the gate's
        # confinement, no write to the tree, the scrubbed environment and a
        # scratch HOME, so it cannot reach the key, the store, a socket or a token.
        with self.scratch() as scratch:
            argv = self.confined([executable, "--version"], scratch, write_tree=False)
            try:
                done = subprocess.run(
                    argv,
                    cwd=scratch,
                    env=self.child_env(scratch),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=PROBE_TIMEOUT_S,
                    check=False,
                )
                exit_code, out = done.returncode, done.stdout
            except subprocess.TimeoutExpired:
                exit_code, out = -1, ""
        lines = out.strip().splitlines()
        version = lines[0].strip() if lines and exit_code == 0 else None
        expected: dict[str, JsonValue] = {"exit": spec.expect_exit}
        observed_exit: dict[str, JsonValue] = {"exit": exit_code}
        missing = [] if version else ["client_version"]
        matched = exit_code == spec.expect_exit and not missing
        if spec.kind == "trust":
            # Read by the runner from the client's own user config, never taken
            # from the request.
            trust = project_trust(request.harness, self.root, self.client_env)
            expected["trusted"] = True
            observed_exit["trusted"] = trust.trusted
            observed_exit["detail"] = trust.detail
            matched = matched and trust.trusted is True
        observed: dict[str, JsonValue] = {
            "harness": request.harness,
            "client_version": version,
            "probe": request.probe,
            "expected": expected,
            "observed": observed_exit,
            "effective_settings": None,
            "requested_model": None,
            "actual_model": None,
            "requested_effort": None,
            "actual_effort": None,
            "billing_route": None,
            "exit": exit_code,
            "missing": list(missing),
            "matched": matched,
        }
        return self.issue("probe", binding, observed)

    def issue(
        self, kind: Literal["gate", "probe"], binding: Binding, observed: dict
    ) -> dict[str, JsonValue]:
        signed = sign(self.private, kind, binding, observed)
        folder = self.store / "receipts" / binding.run_id
        folder.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
        (folder / f"{signed.receipt.receipt_id}.json").write_text(
            signed.to_json(), encoding="utf-8"
        )
        return {"receipt": signed.model_dump(mode="json")}


def open_runner(
    root: Path,
    environ: Mapping[str, str],
    *,
    repository: str | None = None,
    recipes: Mapping[str, int] = DEFAULT_GATE_RECIPES,
    search_path: str | None = None,
) -> Runner:
    top = repo_top(root)
    store = ensure_store(controller_store(top, environ))
    return Runner(
        root=top,
        store=store,
        private=load_key(store),
        repository=repository or origin_repository(top),
        recipes=recipes,
        search_path=search_path if search_path is not None else environ.get("PATH", ""),
        client_env={k: environ[k] for k in CLIENT_CONFIG_VARS if k in environ},
        sandbox=default_sandbox(),
        gate_env=gate_environment(environ),
    )


# ---- the socket


def socket_path(store: Path) -> Path:
    path = store / SOCKET_NAME
    if len(str(path).encode("utf-8")) > MAX_SOCKET_PATH:
        raise RunnerError(f"socket path is too long for AF_UNIX: {path}")
    return path


def answer(runner: Runner, line: bytes) -> dict[str, JsonValue]:
    try:
        raw = json.loads(line)
        return {"ok": True, "result": runner.handle(raw)}
    except json.JSONDecodeError:
        return {"ok": False, "error": "request is not JSON"}
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "request"
        return {"ok": False, "error": f"refused request at {where}: {first['msg']}"}
    except (RunnerError, ReceiptError) as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        # A program that cannot start is refused, never reported as observed.
        return {"ok": False, "error": f"could not run: {exc}"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(line) > MAX_REQUEST_BYTES:
            reply: dict[str, JsonValue] = {"ok": False, "error": "request too large"}
        else:
            server = self.server
            assert isinstance(server, RunnerServer)
            reply = answer(server.runner, line)
        self.wfile.write(json.dumps(reply).encode("utf-8") + b"\n")


class RunnerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, runner: Runner, lock: IO[str]) -> None:
        self.runner = runner
        self._lock = lock
        path = socket_path(runner.store)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        old = os.umask(0o077)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old)

    def server_close(self) -> None:
        super().server_close()
        with contextlib.suppress(FileNotFoundError):
            socket_path(self.runner.store).unlink()
        self._lock.close()


def bind(runner: Runner) -> RunnerServer:
    """Bind the socket, holding the store's lock so only one runner serves it."""
    lock = (runner.store / LOCK_NAME).open("a", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RunnerError(
            "another runner already serves this controller store"
        ) from exc
    try:
        return RunnerServer(runner, lock)
    except OSError:
        lock.close()
        raise


@contextlib.contextmanager
def connect(
    path: Path, timeout: float = GATE_TIMEOUT_S + 30
) -> Iterator[socket.socket]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as exc:
        sock.close()
        raise RunnerError(f"no runner is listening at {path}: {exc}") from exc
    try:
        yield sock
    finally:
        sock.close()


def request(path: Path, payload: Mapping[str, object]) -> dict[str, JsonValue]:
    """One request, one reply; a refusal is raised as RunnerError."""
    with connect(path) as sock:
        sock.sendall(json.dumps(dict(payload)).encode("utf-8") + b"\n")
        with sock.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise RunnerError("the runner closed the connection without a reply")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise RunnerError(str(reply.get("error", "refused")))
    result = reply["result"]
    assert isinstance(result, dict)
    return result


def receipt_from(result: Mapping[str, JsonValue]) -> SignedReceipt:
    return SignedReceipt.model_validate(result["receipt"])
