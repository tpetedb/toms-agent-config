"""`tac doctor`: named checks of this checkout, each pass, fail or unknown.

Unknown counts against the exit code: a check that cannot prove its claim has
not passed. Checks read the filesystem, run local interpreters and each client's
`--version`, and read GitHub: with the owner's `gh` on the host, anonymously
inside an agent session, so an agent never carries the owner's token.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tac.codeowners import ci_gaps, ci_paths, location
from tac.config import load_config
from tac.github import (
    AnonymousTransport,
    ApiError,
    RulesetReport,
    Transport,
    admin_reasons,
    agent_identity,
    gh_auth_status,
    gh_transport,
    judge_rulesets,
    repo_owner,
)
from tac.probes import (
    CLIENTS,
    TRUST_STEPS,
    client_version,
    codex_hook_trust,
    discover,
    project_trust,
)
from tac.receipts import RUNNER_PUB, MissingTrustRoot, key_id, load_public_key
from tac.runner import RunnerError, agent_session, origin_repository
from tac.sync import check_tree
from tac.work import Bad

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


def _package_files(folder: Path) -> dict[str, bytes]:
    return {
        str(f.relative_to(folder)): f.read_bytes()
        for f in sorted(folder.rglob("*"))
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
    }


def check_agents_lib_current(root: Path) -> tuple[Status, str]:
    """The deployed checker judges with the stamped copy, so a src/tac change
    that was never stamped is judged by yesterday's rules."""
    source = root / "src" / "tac"
    if not source.is_dir():
        return Status.PASS, "no src/tac here; the stamp is the pinned release"
    stamped = root / AGENTS / LIB_SOURCE / "src" / "tac"
    if not stamped.is_dir():
        return Status.FAIL, f"no stamped copy in {AGENTS}/{LIB_SOURCE}"
    want, have = _package_files(source), _package_files(stamped)
    stale = sorted(n for n in want.keys() | have.keys() if want.get(n) != have.get(n))
    if stale:
        shown = ", ".join(stale[:3]) + (", ..." if len(stale) > 3 else "")
        return (
            Status.FAIL,
            f"{AGENTS}/{LIB_SOURCE} differs from src/tac in {len(stale)} file(s) "
            f"({shown}); run just stamp-lib on the host",
        )
    return Status.PASS, f"{AGENTS}/{LIB_SOURCE} matches src/tac"


def check_hook_guard(root: Path) -> tuple[Status, str]:
    """Every rendered hook runs the stamped guard, so a hooks/run.py change that
    was never stamped leaves sessions guarded by the old one."""
    stamped = root / AGENTS / "hooks" / "run.py"
    if not stamped.is_file():
        return Status.FAIL, f"no stamped guard at {AGENTS}/hooks/run.py"
    source = root / "hooks" / "run.py"
    if not source.is_file():
        return Status.PASS, "no hooks/run.py here; the stamp is the pinned release"
    if source.read_bytes() != stamped.read_bytes():
        return (
            Status.FAIL,
            f"{AGENTS}/hooks/run.py differs from hooks/run.py; "
            "run just stamp-lib on the host, then tac sync",
        )
    return Status.PASS, f"{AGENTS}/hooks/run.py matches hooks/run.py"


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
    """The knob file and every table it points at load and agree with each other."""
    if not (root / AGENTS / "config.toml").is_file():
        return Status.FAIL, f"{AGENTS}/config.toml missing; it arrives with tac-core"
    try:
        config = load_config(root)
    except Bad as exc:
        first = str(exc).splitlines()[0]
        return Status.FAIL, f"the configuration does not load: {first}"
    return Status.PASS, (
        f"{AGENTS}/config.toml loads: profile {config.profile.name}, "
        f"kind {config.knobs.project.kind}"
    )


def check_generated_lock(root: Path) -> tuple[Status, str]:
    if not (root / AGENTS / "generated.lock").is_file():
        return Status.FAIL, f"{AGENTS}/generated.lock missing; run tac sync"
    problems = check_tree(root)
    if problems:
        more = f" and {len(problems) - 1} more" if len(problems) > 1 else ""
        return Status.FAIL, f"{problems[0]}{more}; run tac check"
    return Status.PASS, "every generated file matches the lock"


def check_runner_pub(root: Path) -> tuple[Status, str]:
    """Receipts are judged against this key; without one every receipt is refused.

    Reads the public key only: the controller store and its private key are the
    runner's, and a doctor run inside an agent session must never open them.
    """
    path = root / RUNNER_PUB
    if not path.is_file():
        return Status.FAIL, f"{RUNNER_PUB} missing; every receipt is refused"
    try:
        public = load_public_key(path.read_text(encoding="utf-8"))
    except MissingTrustRoot as exc:
        return (
            Status.FAIL,
            f"{exc}; the owner runs `just runner-init --write-pub` on the host "
            "and lands it by pull request (TODO.HUMAN.md)",
        )
    return Status.PASS, f"{RUNNER_PUB} holds runner key {key_id(public)}"


def check_codeowners_ci(root: Path) -> tuple[Status, str]:
    """The workflow and the gate scripts are the candidate's on pull_request, so
    the owner's required review is what guards them, and it reaches only the
    paths CODEOWNERS names (section 8)."""
    gaps = ci_gaps(root, agent_identity(root))
    if gaps:
        return Status.FAIL, (
            f"CODEOWNERS leaves what CI runs open: {'; '.join(gaps)}; name the "
            "owner on each so a change to it needs the owner's review"
        )
    found = location(root) or "CODEOWNERS"
    count = len(ci_paths(root, found))
    return Status.PASS, f"{found} names an owner on the {count} paths CI runs from"


def ruleset_transport(environ: Mapping[str, str]) -> tuple[Transport, str]:
    """The owner's gh on the host; no credential at all inside an agent session."""
    reason = agent_session(environ)
    if reason is not None:
        return AnonymousTransport(), f"read anonymously: agent session, {reason}"
    gh = gh_transport()
    if gh is None:
        return AnonymousTransport(), "read anonymously: gh not found"
    return gh, "read with the owner's gh on the host"


REPORT_STATUS = {True: Status.PASS, False: Status.FAIL, None: Status.UNKNOWN}


def ruleset_status(report: RulesetReport, how: str) -> tuple[Status, str]:
    return REPORT_STATUS[report.ok], f"{report.detail} ({how})"


def check_github_ruleset(root: Path) -> tuple[Status, str]:
    """Build condition C5: every active branch ruleset, fetched by id, holds."""
    try:
        repo = origin_repository(root)
    except RunnerError as exc:
        return Status.UNKNOWN, str(exc)
    transport, how = ruleset_transport(os.environ)
    return ruleset_status(judge_rulesets(transport, repo), how)


def session_identity_status(
    root: Path, environ: Mapping[str, str], public: Transport | None = None
) -> tuple[Status, str]:
    """Inside an agent session, gh must not reach an admin identity (section 8).

    Reads only what `gh auth status` shows and the repository's public owner;
    the session's token is never used for anything else.
    """
    reason = agent_session(environ)
    if reason is None:
        return Status.PASS, "not an agent session; the owner's gh belongs here"
    gh = shutil.which("gh", path=environ.get("PATH", ""))
    if gh is None:
        return Status.PASS, "no gh in this agent session"
    try:
        login = gh_auth_status(gh, environ)
    except ApiError as exc:
        return Status.UNKNOWN, f"cannot read gh auth status: {exc}"
    if login is None:
        return Status.PASS, "gh is not logged in inside this agent session"
    try:
        repo = origin_repository(root)
        owner, owner_type = repo_owner(public or AnonymousTransport(), repo)
    except (RunnerError, ApiError, KeyError, TypeError) as exc:
        return Status.UNKNOWN, f"gh is logged in as {login.login}; {exc}"
    reasons = admin_reasons(login, owner, owner_type)
    if reasons:
        return (
            Status.FAIL,
            f"gh inside this agent session is logged in as {login.login}, an admin "
            f"identity ({'; '.join(reasons)}); the owner's login never enters a "
            "session, which gets only the machine account's token (Q14)",
        )
    bot = agent_identity(root)
    if login.login.lower() == bot.lower():
        return Status.PASS, f"gh is logged in as the machine account {bot}"
    return (
        Status.UNKNOWN,
        f"gh is logged in as {login.login}; its role on {repo} is not visible "
        f"without using its token, and only the machine account {bot} belongs in "
        "an agent session",
    )


def hook_trust_status(root: Path) -> tuple[Status, str]:
    trust = codex_hook_trust(root)
    return REPORT_STATUS[trust.trusted], trust.detail


def client_status(harness: str, environ: Mapping[str, str]) -> tuple[Status, str]:
    """Discovery and a minimal launch: `--version`, never a model session."""
    executable = discover(harness, environ.get("PATH", ""))
    if executable is None:
        return Status.FAIL, f"{CLIENTS[harness]} not found on PATH"
    version = client_version(executable, environ)
    if version is None:
        return Status.FAIL, f"{CLIENTS[harness]} found, but --version did not answer"
    return Status.PASS, f"found, version {version}"


def trust_status(
    harness: str, root: Path, environ: Mapping[str, str]
) -> tuple[Status, str]:
    trust = project_trust(harness, root, environ)
    if trust.trusted is None:
        return Status.UNKNOWN, trust.detail
    if not trust.trusted:
        return Status.FAIL, f"{trust.detail}; the owner's step: {TRUST_STEPS[harness]}"
    return Status.PASS, trust.detail


def client_check(harness: str) -> Check:
    return Check(f"client-{harness}", lambda _root: client_status(harness, os.environ))


def trust_check(harness: str) -> Check:
    return Check(
        f"trust-{harness}", lambda root: trust_status(harness, root, os.environ)
    )


CHECKS: tuple[Check, ...] = (
    Check("uv-on-path", check_uv),
    Check("agents-project", check_agents_project),
    Check("agents-lib", check_agents_lib),
    Check("agents-lib-current", check_agents_lib_current),
    Check("hook-guard-current", check_hook_guard),
    Check("agents-venv", check_agents_venv),
    Check("tac-import", check_tac_import),
    Check("agents-config", check_agents_config),
    Check("generated-lock", check_generated_lock),
    Check("runner-pub", check_runner_pub),
    Check("codeowners-ci", check_codeowners_ci),
    Check("github-ruleset", check_github_ruleset),
    Check("session-identity", lambda root: session_identity_status(root, os.environ)),
    *(check for h in CLIENTS for check in (client_check(h), trust_check(h))),
    Check("hook-trust-codex", hook_trust_status),
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
