"""`tac launch`: start a role's session the way the runner does (design 9, 13).

The launcher builds each client's command line from the configuration, never
from a file an agent wrote: Claude Code interactively as `claude --agent <role>`
with the seat's effort (ultracode where models.toml says so), headless as
`claude -p` with the rendered settings overlay, only the project's own setting
sources, only the MCP servers config/mcp.toml names, JSON output validated
against the stage's contract, a turn and budget cap and a session id the runner
chose; and Codex as `codex exec` with its output schema, the result file, the
absolute worker store as its one extra writable folder and the worktree.

Headless never runs in Claude Code's bare mode: it skips the subscription login
and bills an API key outside the usage windows (Q16). For the same reason a launch is
refused while the environment holds a variable that routes Claude Code to an
API key or a cloud provider, and the dispatch record says the billing route is
the subscription.

The child environment is built from an allowlist, runtime.toml `env.pass`, the
scrub and telemetry switches and the variables that locate each client's own
config, and nothing else, so no token the launcher's shell holds reaches it.

Before it starts anything in a worktree, the launcher compares every generated
surface and hook source with the lock at origin/main: Codex cannot protect
`.claude/` and a worktree can be edited, so a difference is refused, and
`--allow-drift` is honoured only for an interactive session the owner watches.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tac import sync
from tac.adapters import seat
from tac.config import Config, ultracode_seats
from tac.handoff import load_contract, worker_store
from tac.runner import SESSION_PATTERN
from tac.work import Bad

Harness = Literal["claude", "codex"]
HARNESSES: tuple[Harness, ...] = ("claude", "codex")
PROVIDERS: Mapping[str, str] = {"claude": "claude", "codex": "openai"}
DISPATCH_DIR = "dispatch"
# Any of these routes Claude Code to an API key or a cloud provider instead of
# the subscription login, outside the usage windows (Q16).
BILLING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
BILLING_ROUTE = "subscription"
# What a client needs of the launcher's environment to find itself, its tools
# and its own login and config; tokens and everything else stay behind.
CLIENT_VARS = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)
# The one overlay setting a headless stage adds to the project's own: no
# session it starts can switch to bypassPermissions.
OVERLAY: Mapping[str, object] = {
    "permissions": {"disableBypassPermissionsMode": "disable"}
}
DRIFT_SECTIONS = ("outputs", "hooks")
ORIGIN_LOCK = f"origin/main:{sync.LOCK_FILE}"


class LaunchError(Exception):
    """The launcher refuses: the message says why."""


@dataclass(frozen=True, slots=True)
class Headless:
    """What a headless stage is bound to: its contract, prompt and session."""

    contract: str
    prompt_file: Path
    session_id: str
    max_turns: int
    max_budget_usd: float


@dataclass(frozen=True, slots=True)
class Plan:
    harness: Harness
    role: str | None
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    session_id: str
    installed: bool
    launch_record: dict[str, object]
    stdin: Path | None = None
    result_file: Path | None = None
    billing_route: str = BILLING_ROUTE
    drift: tuple[str, ...] = field(default_factory=tuple)


def utc_text() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def refuse_billing(environ: Mapping[str, str]) -> None:
    held = [name for name in BILLING_VARS if environ.get(name)]
    if held:
        raise LaunchError(
            f"{', '.join(held)} is set, which would bill an API key or a cloud "
            "provider instead of the subscription (Q16); unset it and launch again"
        )


def child_env(
    config: Config, environ: Mapping[str, str], harness: Harness
) -> dict[str, str]:
    """The environment a launched session starts with, from an allowlist."""
    names = [*CLIENT_VARS, *config.runtime.env.pass_]
    if config.profile.mcp_servers == "from-conf":
        for server in config.mcp.servers.values():
            names += server.env_names
    env = {name: environ[name] for name in dict.fromkeys(names) if name in environ}
    if harness == "claude" and config.runtime.env.claude_subprocess_env_scrub:
        env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    if not config.runtime.env.gh_telemetry:
        env["GH_TELEMETRY"] = "false"
    return env


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def drift(root: Path) -> list[str]:
    """Every generated surface and hook source whose bytes differ from the lock
    at origin/main, hashed as `tac sync` hashes them; empty when they match."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "show", ORIGIN_LOCK],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return [f"cannot run git to read {ORIGIN_LOCK}: {exc}"]
    if done.returncode != 0:
        return [
            f"cannot read {ORIGIN_LOCK}: {done.stderr.strip() or 'no such revision'}"
        ]
    try:
        lock = tomllib.loads(done.stdout)
    except tomllib.TOMLDecodeError as exc:
        return [f"{ORIGIN_LOCK} is not TOML: {exc}"]
    problems: list[str] = []
    for section in DRIFT_SECTIONS:
        table = lock.get(section)
        if not isinstance(table, dict):
            problems.append(f"{ORIGIN_LOCK} has no [{section}]")
            continue
        for rel, digest in sorted(table.items()):
            path = root / rel
            if not path.is_file():
                problems.append(f"{rel} is missing here")
            elif _digest(path) != digest:
                problems.append(f"{rel} differs from the lock at origin/main")
    hooks = lock.get("hooks")
    for rel in sync.hook_files(root):
        if isinstance(hooks, dict) and rel not in hooks:
            problems.append(f"{rel} is not in the lock at origin/main")
    return problems


def dispatch_dir(root: Path, config: Config) -> Path:
    folder = worker_store(root, config) / DISPATCH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write_json(path: Path, body: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", "utf-8")
    return path


def mcp_config(config: Config) -> dict[str, object]:
    """The MCP servers a headless stage may load: config/mcp.toml's, or none.
    Variable names only; their values come through the child environment."""
    if config.profile.mcp_servers != "from-conf":
        return {"mcpServers": {}}
    return {
        "mcpServers": {
            name: {"command": server.command, "args": list(server.args)}
            for name, server in sorted(config.mcp.servers.items())
        }
    }


def worktree_for(root: Path, order_id: str | None) -> Path:
    """The checkout an order's branch has, else this one."""
    if order_id is None:
        return root
    from tac import work

    try:
        order = work.find(order_id, root)
    except Bad as exc:
        raise LaunchError(str(exc)) from exc
    done = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    path: str | None = None
    for line in done.stdout.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{order.branch}" and path is not None:
            return Path(path)
    raise LaunchError(
        f"no worktree has {order.branch} checked out for order {order_id}"
    )


def _effort(config: Config, role: str | None, harness: Harness) -> str | None:
    if role is None:
        return None
    if harness == "claude" and role in ultracode_seats(config.models):
        return "ultracode"
    found = seat(config, role, harness)
    return found.effort if found else None


def plan(
    root: Path,
    config: Config,
    harness: Harness,
    *,
    role: str | None = None,
    order_id: str | None = None,
    headless: Headless | None = None,
    environ: Mapping[str, str],
    search_path: str,
    allow_drift: bool = False,
    cwd: Path | None = None,
) -> Plan:
    """The command, environment and records of one launch; nothing starts."""
    if harness not in HARNESSES:
        raise LaunchError(f"unknown harness {harness!r}; known: {', '.join(HARNESSES)}")
    refuse_billing(environ)
    if role is not None and role not in config.roles:
        raise LaunchError(f"no role {role!r} in .agents/config/roles/")
    if headless is not None and not re.match(SESSION_PATTERN, headless.session_id):
        raise LaunchError(f"not a session id: {headless.session_id!r}")
    if allow_drift and headless is not None:
        raise LaunchError(
            "--allow-drift is for an interactive session the owner watches, never "
            "for a headless one"
        )
    cwd = cwd or worktree_for(root, order_id)
    found_drift = tuple(drift(cwd))
    executable = shutil.which(harness, path=search_path)
    program = executable or harness
    session_id = headless.session_id if headless else str(uuid.uuid4())
    store = worker_store(root, config)
    folder = store / DISPATCH_DIR
    effort = _effort(config, role, harness)
    found_seat = seat(config, role, harness) if role else None
    argv: list[str]
    stdin: Path | None = None
    result: Path | None = None
    if harness == "claude":
        argv = [program]
        if headless is not None:
            argv.append("-p")
        if role is not None:
            argv += ["--agent", role]
        if effort is not None:
            argv += ["--effort", effort]
        if headless is not None:
            contract = load_contract(root, headless.contract)
            overlay = _write_json(folder / f"{session_id}.settings.json", OVERLAY)
            mcp = _write_json(folder / f"{session_id}.mcp.json", mcp_config(config))
            argv += [
                "--settings",
                str(overlay),
                "--setting-sources",
                "project",
                "--strict-mcp-config",
                "--mcp-config",
                str(mcp),
                "--output-format",
                "json",
                # Claude Code takes the schema itself as JSON text, not a path
                # (https://code.claude.com/docs/en/cli-reference).
                "--json-schema",
                json.dumps(contract.schema, separators=(",", ":")),
                "--max-turns",
                str(headless.max_turns),
                "--max-budget-usd",
                f"{headless.max_budget_usd:g}",
            ]
            stdin = headless.prompt_file
        argv += ["--session-id", session_id]
    else:
        argv = [program]
        if headless is not None:
            contract = load_contract(root, headless.contract)
            result = folder / f"{session_id}.result.json"
            argv += [
                "exec",
                "--output-schema",
                str((root / contract.rel).resolve()),
                "-o",
                str(result),
            ]
        argv += ["--add-dir", str(store.resolve()), "-C", str(cwd)]
        if found_seat is not None:
            argv += ["-m", found_seat.model]
        if effort is not None:
            argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        if headless is not None:
            # The prompt comes on stdin; the --json event stream is never the result.
            argv.append("-")
            stdin = headless.prompt_file
    record: dict[str, object] = {
        "provider": PROVIDERS[harness],
        "harness": harness,
        "model": found_seat.model if found_seat else None,
        "effort_requested": effort,
        "effort_actual": effort,
        "role": role,
    }
    return Plan(
        harness=harness,
        role=role,
        argv=argv,
        env=child_env(config, environ, harness),
        cwd=cwd,
        session_id=session_id,
        installed=executable is not None,
        launch_record=record,
        stdin=stdin,
        result_file=result,
        drift=found_drift,
    )


def refuse_drift(launch: Plan, allow_drift: bool) -> None:
    if launch.drift and not allow_drift:
        raise LaunchError(
            "the generated surfaces or hook sources differ from origin/main: "
            + "; ".join(launch.drift)
        )


def write_records(
    root: Path,
    config: Config,
    launch: Plan,
    dispatch: Mapping[str, str] | None = None,
) -> None:
    """The launch record for the memory reader, and, for a session the runner
    dispatched, the dispatch record the guard reads its token from."""
    folder = dispatch_dir(root, config)
    _write_json(folder / f"{launch.session_id}.launch.json", launch.launch_record)
    if dispatch is not None:
        _write_json(folder / f"{launch.session_id}.json", dispatch)


def run(
    launch: Plan, timeout: float | None = None, capture: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Start the planned client and wait for it."""
    if not launch.installed:
        raise LaunchError(f"{launch.harness} is not installed on the launcher's PATH")
    stdin = launch.stdin.open("rb") if launch.stdin else None
    try:
        return subprocess.run(
            launch.argv,
            env=launch.env,
            cwd=launch.cwd,
            stdin=stdin,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    finally:
        if stdin is not None:
            stdin.close()


def result_text(launch: Plan, done: subprocess.CompletedProcess[bytes]) -> str:
    """The stage's result: Claude's structured_output from its JSON reply,
    Codex's last message from the result file."""
    if launch.harness == "codex":
        if launch.result_file is None or not launch.result_file.is_file():
            raise LaunchError("codex exec wrote no result file")
        return launch.result_file.read_text(encoding="utf-8", errors="replace")
    try:
        reply = json.loads(done.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise LaunchError("claude -p answered something that is not JSON") from exc
    if not isinstance(reply, dict):
        raise LaunchError("claude -p answered something that is not an object")
    structured = reply.get("structured_output")
    if structured is not None:
        return json.dumps(structured)
    text = reply.get("result")
    return text if isinstance(text, str) else json.dumps(text)


def shown(argv: Sequence[str]) -> list[str]:
    """argv as printed: the inline schema is long, so it is shortened."""
    out = list(argv)
    for i, part in enumerate(out[:-1]):
        if part == "--json-schema":
            out[i + 1] = f"<schema, {len(out[i + 1])} chars>"
    return out
