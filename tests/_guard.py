"""Fixture checkouts for the hook guard tests.

Each fixture is a folder with the guard where a rendered hook finds it, a lock
that records it, and a `.agents/.venv` built for the test with either a stub
checker, whose answer the test sets, or a copy of the candidate `src/tac`. The
guard always runs as a subprocess from an absolute path, the way a client runs
it, under an interpreter that is not a virtual environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tac import tomlwrite
from tac.sync import sync
from tests._syncproject import copy_project

REPO = Path(__file__).resolve().parents[1]
SOURCE_GUARD = REPO / "hooks" / "run.py"
STAMPED = ".agents/hooks/run.py"
SOURCE = "hooks/run.py"
# Generous next to what a stub needs; the timeout test passes its own.
DEADLINE_S = 30

# The stub checker: what it answers is read from stub.json at the root, so a
# test can make it answer, crash, print garbage, hang, or report what it saw.
STUB = r"""
import json, os, sys, threading

def main():
    args = sys.argv[1:]
    root = args[args.index("--root") + 1]
    raw = sys.stdin.read()
    with open(os.path.join(root, "stub.json")) as handle:
        spec = json.load(handle)
    mode = spec.get("mode", "answer")
    if mode == "crash":
        sys.stderr.write("stub checker fell over\n")
        sys.exit(3)
    if mode == "garbage":
        print("this is not a verdict")
        return
    if mode == "hang":
        threading.Event().wait()
    reason = spec.get("reason", "")
    if mode == "echo":
        reason = json.dumps(
            {"env": dict(os.environ), "path": sys.path, "argv": args,
             "stdin": json.loads(raw), "cwd": os.getcwd()}
        )
    here = os.path.realpath(os.path.dirname(__file__))
    print(json.dumps({
        "verdict": spec.get("verdict", "allow"),
        "check": spec.get("check", "stub"),
        "reason": reason,
        "module": spec.get("module", here),
    }))

main()
"""


def base_python() -> str:
    """The interpreter behind the test's virtual environment: the guard refuses
    to run inside one, as it would for a project's own venv."""
    return os.path.realpath(getattr(sys, "_base_executable", sys.executable))


def site_packages(root: Path) -> Path:
    tail = Path(sysconfig.get_path("purelib")).relative_to(sys.prefix)
    return root / ".agents" / ".venv" / tail


def lock_text(entries: Mapping[str, str]) -> str:
    return tomlwrite.dumps({"schema_version": 1, "hooks": dict(entries)})


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Fixture:
    root: Path
    guard: Path

    @property
    def rel(self) -> str:
        return self.guard.relative_to(self.root).as_posix()

    def relock(self, extra: Mapping[str, str] | None = None) -> None:
        """Write a lock that records every file in the guard's folder."""
        folder = self.guard.parent
        entries = {
            p.relative_to(self.root).as_posix(): sha256(p)
            for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }
        entries.update(extra or {})
        lock = self.root / ".agents" / "generated.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(lock_text(entries), encoding="utf-8")

    def stub(self, **spec: Any) -> None:
        (self.root / "stub.json").write_text(json.dumps(spec), encoding="utf-8")


def make_venv(root: Path) -> Path:
    target = root / ".agents" / ".venv"
    venv.EnvBuilder(symlinks=True, with_pip=False, clear=True).create(target)
    site = site_packages(root)
    site.mkdir(parents=True, exist_ok=True)
    return site


def install_stub(root: Path) -> None:
    package = make_venv(root) / "tac"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(STUB, encoding="utf-8")


def install_candidate(root: Path) -> None:
    """A copy of src/tac in the fixture venv, as `uv sync --no-editable` would
    put it, with the test environment's own site-packages behind it for pydantic,
    click and the rest."""
    site = make_venv(root)
    shutil.copytree(
        REPO / "src" / "tac", site / "tac", ignore=shutil.ignore_patterns("__pycache__")
    )
    deps = sysconfig.get_path("purelib")
    (site / "_tac_deps.pth").write_text(deps + "\n", encoding="utf-8")


def guarded(tmp: Path, layout: str = "stamped", checker: str = "stub") -> Fixture:
    """A checkout with the source guard's bytes placed as `layout` says, its lock,
    and a checker venv (`stub`, `candidate` or `none`)."""
    root = tmp / "checkout"
    rel = STAMPED if layout == "stamped" else SOURCE
    guard = root / rel
    guard.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_GUARD, guard)
    fixture = Fixture(root, guard)
    fixture.relock()
    if checker == "stub":
        install_stub(root)
        fixture.stub()
    elif checker == "candidate":
        install_candidate(root)
    return fixture


def run_guard(
    fixture: Fixture,
    client: str,
    event: str,
    payload: Mapping[str, Any] | str,
    *,
    python: str | None = None,
    flags: Sequence[str] = ("-I",),
    script: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    extra: Sequence[str] = (),
    deadline: int = DEADLINE_S,
) -> subprocess.CompletedProcess[str]:
    """The guard as a client starts it: absolute interpreter, absolute script,
    the event on stdin."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    argv = [python or base_python(), *flags, script or str(fixture.guard)]
    argv += ["--client", client, "--event", event, "--deadline-s", str(deadline)]
    return subprocess.run(
        [*argv, *extra],
        input=body,
        capture_output=True,
        text=True,
        cwd=cwd or fixture.root,
        env=dict(env) if env is not None else {"PATH": "/usr/bin:/bin"},
        check=False,
        timeout=deadline + 60,
    )


def event(name: str, **more: Any) -> dict[str, Any]:
    return {"session_id": "s-1", "cwd": "/", "hook_event_name": name, **more}


def tool_event(name: str, tool: str = "Edit", **given: Any) -> dict[str, Any]:
    return event(name, tool_name=tool, tool_input=given)


def patch(*headers: str) -> str:
    """A Codex apply_patch body that names each header's file."""
    body = [f"*** {h}\n@@\n+x" for h in headers]
    return "*** Begin Patch\n" + "\n".join(body) + "\n*** End Patch\n"


def rendered(root: Path, client: str, event_name: str) -> str:
    """The one command a client's rendered file runs for an event."""
    rel = ".claude/settings.json" if client == "claude" else ".codex/hooks.json"
    hooks = json.loads((root / rel).read_text("utf-8"))["hooks"]
    (group,) = hooks[event_name]
    (one,) = group["hooks"]
    return one["command"]


def real_checkout(tmp: Path) -> Fixture:
    """A synced copy of this project with the guard stamped into it and the
    candidate tac installed, not editable, in its .agents/.venv."""
    fx = guarded(tmp, checker="candidate")
    copy_project(fx.root)
    sync(fx.root, links=False)
    return fx


def emitted(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The guard's one JSON line on stdout."""
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, done.stdout + done.stderr
    return json.loads(lines[0])


# ---------------------------------------------------------------- a hostile checkout

SHIM = "#!/bin/sh\necho shim >> {marker}\nexit 0\n"
PLANT = "open({marker!r}, 'a').write('planted {name}\\n')\n"
PLANTED_MODULES = ("json", "hashlib", "subprocess", "argparse", "re", "tac")


def plant(folder: Path, marker: Path) -> None:
    """Modules that shadow what the guard and the checker import, each leaving a
    mark when imported."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in PLANTED_MODULES:
        body = PLANT.format(marker=str(marker), name=name)
        if name == "tac":
            (folder / "tac").mkdir(exist_ok=True)
            (folder / "tac" / "__init__.py").write_text(body, "utf-8")
            (folder / "tac" / "__main__.py").write_text(body, "utf-8")
        else:
            (folder / f"{name}.py").write_text(body, "utf-8")
    (folder / "sitecustomize.py").write_text(
        PLANT.format(marker=str(marker), name="sitecustomize"), "utf-8"
    )


def hostile(fx: Fixture, tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """An environment and a checkout an agent prepared: shims first on PATH,
    planted modules on PYTHONPATH, in the checkout and in the working folder,
    and project configuration pointing uv and Python elsewhere."""
    marker = tmp_path / "marker.txt"
    shims = tmp_path / "shims"
    shims.mkdir()
    for name in ("python", "python3", "uv", "tac", "git", "env", "sh"):
        path = shims / name
        path.write_text(SHIM.format(marker=marker), encoding="utf-8")
        path.chmod(0o755)
    planted = tmp_path / "planted"
    plant(planted, marker)
    plant(fx.root, marker)
    work_dir = tmp_path / "cwd"
    plant(work_dir, marker)
    (fx.root / "pyproject.toml").write_text(
        '[project]\nname = "tac"\nversion = "9"\n', "utf-8"
    )
    (fx.root / "uv.toml").write_text(f'python-install-dir = "{planted}"\n', "utf-8")
    env = {
        "PATH": f"{shims}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(planted),
        "PYTHONHOME": str(planted),
        "PYTHONSTARTUP": str(planted / "json.py"),
        "PYTHONSAFEPATH": "",
        "PYTHONUSERBASE": str(planted),
        "VIRTUAL_ENV": str(planted),
        "UV_PROJECT_ENVIRONMENT": str(planted),
        "UV_PYTHON": str(shims / "python"),
        "GIT_DIR": str(planted),
        "GITHUB_TOKEN": "not-a-real-token",
    }
    return env, marker, work_dir
