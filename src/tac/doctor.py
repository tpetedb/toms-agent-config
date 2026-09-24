"""`tac doctor`: named checks of this checkout, each pass, fail or unknown.

Unknown counts against the exit code: a check that cannot prove its claim has
not passed. Checks read the filesystem and run local interpreters only; the
ruleset check that needs the owner's token arrives with `tac github`.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

AGENTS = ".agents"
LIB_SOURCE = "lib/tac"
# A fixed environment so a shim on the caller's PATH or a PYTHON* variable
# cannot change which tac the deployed interpreter imports.
PROBE_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
PROBE_TIMEOUT_S = 30


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": str(self.status), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    run: Callable[[Path], tuple[Status, str]]


def find_root(start: Path) -> Path:
    """The nearest ancestor holding .git (a folder, or a file in a worktree)."""
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def venv_python(root: Path) -> Path:
    return root / AGENTS / ".venv" / "bin" / "python"


def check_uv(_root: Path) -> tuple[Status, str]:
    if shutil.which("uv"):
        return Status.PASS, "uv is on PATH"
    return Status.FAIL, "uv not found; run bootstrap.sh"


def check_agents_project(root: Path) -> tuple[Status, str]:
    project = root / AGENTS / "pyproject.toml"
    lock = root / AGENTS / "uv.lock"
    missing = [p.name for p in (project, lock) if not p.is_file()]
    if missing:
        return Status.FAIL, f"missing in {AGENTS}/: {', '.join(missing)}"
    try:
        data = tomllib.loads(project.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return Status.FAIL, f"{AGENTS}/pyproject.toml does not parse: {exc}"
    source = data.get("tool", {}).get("uv", {}).get("sources", {}).get("tac", {})
    if source.get("path") != LIB_SOURCE or source.get("editable", False):
        return Status.FAIL, f"tac must come from the path source {LIB_SOURCE}"
    return Status.PASS, f"tac pinned to the named source {AGENTS}/{LIB_SOURCE}"


def check_agents_lib(root: Path) -> tuple[Status, str]:
    lib = root / AGENTS / LIB_SOURCE
    needed = [lib / "pyproject.toml", lib / "src" / "tac" / "__init__.py"]
    missing = [str(p.relative_to(root)) for p in needed if not p.is_file()]
    if missing:
        return Status.FAIL, f"stamped copy incomplete: {', '.join(missing)}"
    return Status.PASS, f"stamped copy present in {AGENTS}/{LIB_SOURCE}"


def check_agents_venv(root: Path) -> tuple[Status, str]:
    if venv_python(root).is_file():
        return Status.PASS, f"{AGENTS}/.venv has an interpreter"
    return Status.FAIL, f"{AGENTS}/.venv missing; run bootstrap.sh"


def classify_tac_origin(module_file: Path, root: Path) -> tuple[Status, str]:
    """Pass only when tac was imported from the venv's site-packages."""
    root = root.resolve()
    venv = (root / AGENTS / ".venv").resolve()
    resolved = module_file.resolve()
    if not resolved.is_relative_to(venv) or "site-packages" not in resolved.parts:
        where = resolved.relative_to(root) if resolved.is_relative_to(root) else "?"
        return Status.FAIL, f"tac imports from {where}, not site-packages"
    return Status.PASS, "tac imports from site-packages, non-editable"


def check_tac_import(root: Path) -> tuple[Status, str]:
    python = venv_python(root)
    if not python.is_file():
        return Status.UNKNOWN, f"no {AGENTS}/.venv interpreter to ask"
    probe = "import tac; print(tac.__file__)"
    try:
        done = subprocess.run(
            [str(python), "-I", "-c", probe],
            capture_output=True,
            text=True,
            env=PROBE_ENV,
            cwd=root / AGENTS,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Status.FAIL, f"probe did not run: {exc.__class__.__name__}"
    if done.returncode != 0 or not done.stdout.strip():
        return Status.FAIL, "tac does not import from the deployed venv"
    return classify_tac_origin(Path(done.stdout.strip().splitlines()[-1]), root)


def check_agents_config(root: Path) -> tuple[Status, str]:
    if (root / AGENTS / "config.toml").is_file():
        return Status.PASS, f"{AGENTS}/config.toml present"
    return Status.FAIL, f"{AGENTS}/config.toml missing; it arrives with tac-core"


def check_generated_lock(root: Path) -> tuple[Status, str]:
    if (root / AGENTS / "generated.lock").is_file():
        return Status.PASS, f"{AGENTS}/generated.lock present"
    return Status.FAIL, f"{AGENTS}/generated.lock missing; run tac sync"


def check_github_ruleset(_root: Path) -> tuple[Status, str]:
    return (
        Status.UNKNOWN,
        "not verified: needs tac github apply and the owner's token on the host",
    )


CHECKS: tuple[Check, ...] = (
    Check("uv-on-path", check_uv),
    Check("agents-project", check_agents_project),
    Check("agents-lib", check_agents_lib),
    Check("agents-venv", check_agents_venv),
    Check("tac-import", check_tac_import),
    Check("agents-config", check_agents_config),
    Check("generated-lock", check_generated_lock),
    Check("github-ruleset", check_github_ruleset),
)


def run_checks(root: Path, checks: Sequence[Check]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        try:
            status, detail = check.run(root)
        except Exception as exc:  # a crashing check is a failed check, not a crash
            status, detail = Status.FAIL, f"check raised {exc.__class__.__name__}"
        results.append(CheckResult(check.name, status, detail))
    return results


def exit_code(results: Sequence[CheckResult]) -> int:
    return 0 if results and all(r.status is Status.PASS for r in results) else 1


def render_text(results: Sequence[CheckResult]) -> str:
    width = max((len(r.name) for r in results), default=0)
    lines = [f"{r.status.upper():<7} {r.name:<{width}}  {r.detail}" for r in results]
    passed = sum(r.status is Status.PASS for r in results)
    lines.append(f"{passed} of {len(results)} checks pass")
    return "\n".join(lines)
