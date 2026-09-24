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
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import time
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Annotated, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from tac.receipts import (
    ID_PATTERN,
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
# The programs a gate may start, by name; a pipeline gate is `just <recipe>`.
DEFAULT_PROGRAMS = ("just",)
# The clients a probe may start, by harness; the executable is never read from
# a file an agent could write.
HARNESS_EXECUTABLES = {"claude": "claude", "codex": "codex"}
# Set in the shells that Claude Code and a sandboxed Codex start. A tripwire,
# not a boundary: an agent can unset them, which is why the key moves to the
# keychain in M3.
AGENT_MARKERS = ("CLAUDECODE", "CODEX_SANDBOX")
# Clients whose process, anywhere above the runner, means an agent started it.
# Harder to shed than a variable, still a tripwire: a detached start escapes it.
AGENT_PROCESSES = ("claude", "codex")
AGENT_PACKAGES = ("claude-code", "codex")
MAX_ANCESTORS = 64
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


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["probe"]
    harness: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    probe: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    run_id: str = Field(pattern=ID_PATTERN)


Request = Annotated[Ping | PubKey | Gate | Probe, Field(discriminator="op")]
REQUEST = TypeAdapter(Request)


class ProbeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    description: str
    kind: Literal["version"]
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
    programs: tuple[str, ...] = DEFAULT_PROGRAMS
    # The PATH the runner resolves programs and clients on: its own, not a caller's.
    search_path: str = field(default_factory=lambda: os.environ.get("PATH", ""))

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

    def clean_revision(self) -> str:
        status = git(self.root, "status", "--porcelain", "--untracked-files=normal")
        if status.returncode != 0 or status.stdout.strip():
            raise RunnerError(
                "the working tree has uncommitted changes; a receipt names a "
                "revision, so the runner observes committed trees only"
            )
        return resolve(self.root, "HEAD")

    def binding(self, run_id: str, stage: str) -> Binding:
        revision = self.clean_revision()
        return Binding(
            repository=self.repository,
            revision=revision,
            run_id=run_id,
            stage=stage,
            policy_hash=policy_hash(self.root, revision),
        )

    def which(self, name: str) -> str:
        found = shutil.which(name, path=self.search_path)
        if found is None:
            raise RunnerError(f"{name} is not installed on the runner's PATH")
        return found

    def gate(self, request: Gate) -> dict[str, JsonValue]:
        program = request.argv[0]
        if program not in self.programs:
            raise RunnerError(
                f"{program!r} is not a gate program; "
                f"allowed: {', '.join(self.programs)}"
            )
        binding = self.binding(request.run_id, request.stage)
        argv = [self.which(program), *request.argv[1:]]
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv,
                cwd=self.root,
                capture_output=True,
                timeout=GATE_TIMEOUT_S,
                check=False,
            )
            exit_code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code, out, err = -1, exc.stdout or b"", exc.stderr or b""
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
        binding = self.binding(request.run_id, f"probe.{request.probe}")
        spec = self.probe_spec(binding.revision, request.probe)
        executable = self.which(HARNESS_EXECUTABLES[request.harness])
        try:
            done = subprocess.run(
                [executable, "--version"],
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
            "matched": exit_code == spec.expect_exit and not missing,
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
    programs: tuple[str, ...] = DEFAULT_PROGRAMS,
    search_path: str | None = None,
) -> Runner:
    top = repo_top(root)
    store = ensure_store(controller_store(top, environ))
    return Runner(
        root=top,
        store=store,
        private=load_key(store),
        repository=repository or origin_repository(top),
        programs=programs,
        search_path=search_path if search_path is not None else environ.get("PATH", ""),
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
