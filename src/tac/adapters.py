"""What each harness's templates are given: the configuration, turned into the
values the vendor's own files take.

The templates under templates/adapters/<harness>/ only place these values; the
mapping from TAC policy to Claude Code and Codex keys lives here, where a test
can hold it to the vendor documentation. Every value handed to a template is a
plain string, number, boolean, list or dict, since templates render in a
sandbox that refuses attribute access on arbitrary objects.

Claude Code: https://code.claude.com/docs/en/settings-reference,
https://code.claude.com/docs/en/permissions, https://code.claude.com/docs/en/sub-agents.
Codex: https://developers.openai.com/codex/config-reference,
https://developers.openai.com/codex/subagents.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tac.config import CONFIG_DIR, Config
from tac.work import Bad

CONTEXT_DIR = ".agents/context"
BRIEF_FILE = f"{CONTEXT_DIR}/brief.md"
CLAUDE_EXTRAS_FILE = f"{CONTEXT_DIR}/claude.md"
# The controller store belongs to the runner alone (design section 2.1); every
# repository's store sits under this folder.
CONTROLLER_STORE = "~/.local/state/tac"
# Where a client keeps credentials an agent never needs (design section 11).
CREDENTIAL_DIRS = ("~/.ssh", "~/.aws", "~/.config/gh")

# The Claude Code telemetry opt-outs per tier (design section 12, Q15). The
# partial tier keeps Remote Control and auto mode working on the owner's machine;
# the full set also cuts those, so it is for containers and CI.
CLAUDE_TELEMETRY: dict[str, dict[str, str]] = {
    "partial": {
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_FEEDBACK_COMMAND": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "DISABLE_AUTOUPDATER": "1",
    },
    "full": {
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_FEEDBACK_COMMAND": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    },
}
# The tools that start agents natively: the subagent tool under its current and
# former names, and Workflow, which runs a dynamic workflow's script and starts
# every agent() it calls (https://code.claude.com/docs/en/workflows). Denied
# outright when the profile turns native delegation off, so refusal survives a
# hook failure (C1), and listed under disallowedTools in the agent file of a
# role that does not delegate (https://code.claude.com/docs/en/sub-agents).
CLAUDE_SPAWN_TOOLS = ("Agent", "Task", "Workflow")
# Whether the rendered Codex config carries the PreToolUse handoff guard. It does
# not yet, so a "guarded" profile cannot be honoured there and Codex native
# delegation renders off in every profile; M2 turns this on once the guard is
# installed and its failure tests pass (C1).
CODEX_HANDOFF_GUARD = False

# ---- hook wiring (design section 9, build condition C2)

# The events the guard answers per client, as each client's docs name them:
# https://code.claude.com/docs/en/hooks and https://developers.openai.com/codex/hooks.
# hooks/run.py keeps the same table, since it cannot import this package.
GUARD_EVENTS: dict[str, tuple[str, ...]] = {
    "claude": ("PreToolUse", "PostToolUse", "Stop", "SubagentStop", "SessionStart"),
    "codex": ("PreToolUse", "PostToolUse", "Stop", "SessionStart"),
}
# An event wired and judged by the checks hooks.toml writes for another: a
# subagent's stop on Claude is held to what a session's stop is held to.
CONFIGURED_AS = {"SubagentStop": "Stop"}
# Events whose matcher filters by tool name; the others take every occurrence.
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
# What a hook command hands on from the client's environment, and nothing else:
# the guard's own PASS_ENV, so PYTHON*, UV_*, GIT_* and tokens never reach it.
HOOK_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
ENV = "/usr/bin/env"
# Codex sets no project folder for a hook and starts it in the session's
# folder, so the root comes from git, run by absolute path in an empty
# environment (https://developers.openai.com/codex/hooks).
GIT = "/usr/bin/git"
# Claude Code names the project root for every hook command.
CLAUDE_ROOT = "$CLAUDE_PROJECT_DIR"
# The guard's own deny class: a hook that cannot even start the guard (no git
# work tree around a Codex session, the stamped guard gone) refuses a tool call
# there and lets every other event pass, since a stop sent back for a guard
# that is not there would come back on every stop.
DENY_CLASS = frozenset({"PreToolUse"})
# A matcher that is a plain list of tool names, which can be merged with others.
NAMES = re.compile(r"^[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*$")
# The guard's path goes inside double quotes in a shell command.
SAFE_SCRIPT = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True, slots=True)
class Seat:
    """The model and effort a role runs on in one harness; effort is None for a
    model that takes none."""

    model: str
    effort: str | None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def charter_sha256(root: Path, role: str) -> str:
    """The hash every adapter of a role carries, so a pin test can match it."""
    return sha256_bytes((root / CONFIG_DIR / "roles" / f"{role}.toml").read_bytes())


def provider_of(config: Config, harness: str) -> str | None:
    """The provider id that runs through a harness, if models.toml names one."""
    return next(
        (pid for pid, p in config.models.providers.items() if p.harness == harness),
        None,
    )


def seat(config: Config, role: str, harness: str) -> Seat | None:
    """A role's model and effort on the provider behind `harness`; None when the
    role has no seat there, and then no agent file is rendered for it."""
    provider = provider_of(config, harness)
    if provider is None:
        return None
    if role in config.models.roles:
        found = config.models.roles[role].seats().get(provider)
        return Seat(found.model, found.effort) if found else None
    if role == "director":
        # One charter covers every board seat; the harness's agent file takes the
        # lead seat on that provider when there is one, else the first in the file.
        seats = sorted(
            (d for d in config.models.directors.values() if d.provider == provider),
            key=lambda d: not d.lead,
        )
        return Seat(seats[0].model, seats[0].effort) if seats else None
    return None


def claude_session(config: Config, role: str) -> list[str] | None:
    """The command that starts `role` as the owner's own Claude Code session, as
    config/models.toml seats it; None unless the charter runs it as the host
    session on the provider that runs through Claude Code.

    `--agent` runs the session as .claude/agents/<role>.md, taking its prompt,
    tools and model; `--effort` sets the session's effort and also takes
    `ultracode` (v2.1.203 or later), which an agent file cannot say. Ultracode is
    never rendered into .claude/settings.json, where it would turn on for every
    session and headless run in the project.
    https://code.claude.com/docs/en/cli-reference,
    https://code.claude.com/docs/en/sub-agents,
    https://code.claude.com/docs/en/model-config#adjust-effort-level
    """
    charter = config.roles.get(role)
    spec = config.models.roles.get(role)
    provider = provider_of(config, "claude")
    if charter is None or spec is None or charter.runs != "host-session":
        return None
    if provider is None or spec.provider != provider:
        return None
    found = spec.seats()[provider]
    argv = ["claude", "--agent", role]
    if found.ultracode:
        argv += ["--effort", "ultracode"]
    elif found.effort is not None:
        argv += ["--effort", found.effort]
    return argv


def _read(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def _role(config: Config, root: Path, name: str, found: Seat) -> dict[str, Any]:
    charter = config.roles[name]
    return {
        "name": charter.name,
        "description": charter.description,
        "instructions": charter.instructions.strip(),
        "may": list(charter.may),
        "may_not": list(charter.may_not),
        "model": found.model,
        "effort": found.effort,
        "charter_sha256": charter_sha256(root, name),
        # Empty for a role that delegates; the handoff guard judges its calls.
        "disallowed_tools": [] if charter.delegates else list(CLAUDE_SPAWN_TOOLS),
    }


def roles_for(config: Config, root: Path, harness: str) -> list[dict[str, Any]]:
    """Every role with a seat on this harness, by name."""
    out = []
    for name in sorted(config.roles):
        found = seat(config, name, harness)
        if found is not None:
            out.append(_role(config, root, name, found))
    return out


# ---------------------------------------------------------------- deny sets


def denied_reads(config: Config) -> list[str]:
    """Every glob no agent reads, in render order: the policy's deny_read, each
    credential folder, the folder of the secrets env file and the controller
    store. The one list the rendered Claude rules (the Read denies and
    sandbox.filesystem.denyRead) and the hook guard's secret-read check both
    take, so layer 1 and layer 2 refuse the same paths. Never .agents/: a Read
    deny there would also break the skill links."""
    env_file_dir = str(Path(config.knobs.secrets.env_file).parent)
    return list(
        dict.fromkeys(
            [
                *config.policy.paths.deny_read,
                *(f"{d}/**" for d in CREDENTIAL_DIRS),
                f"{env_file_dir}/**",
                f"{CONTROLLER_STORE}/**",
            ]
        )
    )


def denied_writes(config: Config, generated: Iterable[str]) -> list[str]:
    """Every path no agent writes, in render order: the policy's deny_write
    (the authored tree), each generated path, then the controller store. The
    rendered sandbox.filesystem.denyWrite and the hook guard's generated-paths
    check both take it."""
    return list(
        dict.fromkeys(
            [
                *config.policy.paths.deny_write,
                *generated,
                f"{CONTROLLER_STORE}/**",
            ]
        )
    )


# ---------------------------------------------------------------- Claude Code


def _sandbox_path(pattern: str) -> str:
    """A policy glob as a sandbox filesystem path: `~/` stays home-relative, the
    rest anchors at the project root with `./`, and a trailing `/**` goes, since
    Claude Code strips it anyway. A bare file pattern such as `*.env` matches at
    any depth, as the permission rule with the same pattern does."""
    path = pattern.removesuffix("/**")
    if path.startswith("~/"):
        return path
    if "/" not in path and any(c in path for c in "*?["):
        return f"./**/{path}"
    return f"./{path.removeprefix('./')}"


def _edit_rule(path: str) -> str:
    # A leading `/` anchors a permission rule at the project root in project
    # settings; Edit rules cover every built-in editing tool, and a Write rule
    # with a path is never consulted, so none is rendered.
    return f"Edit(/{path.removeprefix('./')})"


def claude(config: Config, root: Path, outputs: list[str]) -> dict[str, Any]:
    """The values `.claude/settings.json` and the agent files take."""
    policy, profile, runtime = config.policy, config.profile, config.runtime
    deny_read_globs = denied_reads(config)
    write_paths = denied_writes(config, outputs)
    deny_rules = [f"Read({g})" for g in deny_read_globs]
    # Edit rules anchor at the project root, so they take the paths inside the
    # checkout; the controller store outside it is held by the sandbox's
    # denyWrite for commands and by the guard's generated-paths check for edits.
    deny_rules += [_edit_rule(p) for p in write_paths if not p.startswith("~/")]
    if profile.native_delegation == "off":
        deny_rules += list(CLAUDE_SPAWN_TOOLS)
    env = dict(CLAUDE_TELEMETRY[config.knobs.telemetry.host_tier])
    if runtime.env.claude_subprocess_env_scrub:
        env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    if not runtime.env.gh_telemetry:
        env["GH_TELEMETRY"] = "false"
    allowed = (
        list(config.network.egress.allow) if profile.network == "allowlist" else []
    )
    return {
        "default_mode": profile.claude.defaultMode,
        "disable_bypass": "bypassPermissions" in policy.host.refuse_claude_modes
        and profile.on_host,
        "deny_rules": deny_rules,
        # A disposable container is the isolation there; everywhere else the
        # client's own sandbox is on, rendered explicitly (section 3.2).
        "sandbox_enabled": profile.isolation != "disposable-container",
        "fail_if_unavailable": profile.claude.failIfUnavailable,
        "allow_unsandboxed": profile.claude.allowUnsandboxedCommands,
        "deny_read": [_sandbox_path(g) for g in deny_read_globs],
        "deny_write": [_sandbox_path(p) for p in write_paths],
        "allowed_domains": allowed,
        "env": env,
        "extras": _read(root, CLAUDE_EXTRAS_FILE),
        "roles": roles_for(config, root, "claude"),
        "hooks": hooks(config, "claude"),
    }


# ---------------------------------------------------------------- hooks


def hook_command(config: Config, client: str, event: str) -> str:
    """The shell command a client runs for one event: the stamped guard found by
    absolute path, then started from an empty environment with a fixed PATH and
    the pinned interpreter in isolated mode (build condition C2). A guard that
    cannot be found fails the way the guard itself fails: closed before a tool
    call, open with a note everywhere else."""
    guard = config.hooks.guard
    if not SAFE_SCRIPT.match(guard.script) or ".." in guard.script.split("/"):
        raise Bad(
            f"{CONFIG_DIR}/hooks.toml: guard.script {guard.script!r} must be a "
            "plain path inside the checkout"
        )
    path = ":".join(guard.path)
    env = [ENV, "-i", "PATH=" + shlex.quote(path)]
    if client == "claude":
        root = CLAUDE_ROOT
    else:
        root = "$(" + " ".join([*env, GIT, "rev-parse", "--show-toplevel"]) + ")"
    missing = shlex.quote("tac guard: no stamped guard in this checkout; run tac init")
    code = 2 if event in DENY_CLASS else 0
    find = (
        f'g="{root}/{guard.script}" && [ -f "$g" ] '
        f"|| {{ echo {missing} >&2; exit {code}; }};"
    )
    words = [
        find,
        *env,
        *(f'{name}="${name}"' for name in HOOK_ENV),
        shlex.quote(guard.python),
        "-I",
        '"$g"',
        "--client",
        client,
        "--event",
        event,
        "--deadline-s",
        str(guard.deadline_s),
        "--path",
        shlex.quote(path),
    ]
    return " ".join(words)


def _merge(matchers: list[str]) -> list[str]:
    """One matcher per group: `*` covers everything, plain tool names merge
    into one alternation, and anything else stays a group of its own."""
    if "*" in matchers:
        return ["*"]
    names: list[str] = []
    other: list[str] = []
    for matcher in matchers:
        if NAMES.match(matcher):
            names += [n for n in matcher.split("|") if n not in names]
        elif matcher not in other:
            other.append(matcher)
    return (["|".join(names)] if names else []) + other


def hooks(config: Config, client: str) -> list[dict[str, Any]]:
    """Every hook group a client's file takes, from config/hooks.toml: one per
    event and matcher, each running the guard once. A check with an empty
    matcher is not wired on that client; a record-only check changes no answer
    and waits for the worker store journal, so it renders nothing yet."""
    wanted: dict[str, list[str]] = {}
    for name, spec in config.hooks.checks.items():
        matcher = spec.claude if client == "claude" else spec.codex
        if not matcher or spec.kind == "record":
            continue
        events = [spec.event] + [
            fired
            for fired, judged in CONFIGURED_AS.items()
            if judged == spec.event and fired in GUARD_EVENTS[client]
        ]
        for event in events:
            if event not in GUARD_EVENTS[client]:
                raise Bad(
                    f"{CONFIG_DIR}/hooks.toml: checks.{name} is wired on {client} "
                    f"for {event}, which the guard does not answer there"
                )
            wanted.setdefault(event, []).append(
                matcher if event in TOOL_EVENTS else "*"
            )
    groups = []
    for event in GUARD_EVENTS[client]:
        for matcher in _merge(wanted.get(event, [])):
            groups.append(
                {
                    "event": event,
                    "matcher": "" if matcher == "*" else matcher,
                    "command": hook_command(config, client, event),
                    "timeout": config.hooks.guard.timeout_s,
                }
            )
    return groups


# ---------------------------------------------------------------- Codex


def codex(config: Config, root: Path) -> dict[str, Any]:
    """The values `.codex/config.toml` and the agent files take."""
    profile = config.profile
    return {
        "sandbox_mode": profile.codex.sandbox_mode,
        "approval_policy": profile.codex.approval_policy,
        # The allowlist needs features.network_proxy with a tested proxy; until a
        # client receipt proves one, command networking stays off (section 3.2).
        "network_access": False,
        "inherit": config.runtime.env.codex_inherit,
        "agents_enabled": profile.native_delegation == "guarded"
        and CODEX_HANDOFF_GUARD,
        "max_threads": config.knobs.teams.max_local_agents,
        "hooks": hooks(config, "codex"),
        "roles": [
            {**role, "developer_instructions": _developer_instructions(role)}
            for role in roles_for(config, root, "codex")
        ],
    }


def _developer_instructions(role: dict[str, Any]) -> str:
    may = "\n".join(f"- {item}" for item in role["may"])
    may_not = "\n".join(f"- {item}" for item in role["may_not"])
    return f"{role['instructions']}\n\nYou may:\n{may}\n\nYou may never:\n{may_not}\n"


def common(config: Config, root: Path, outputs: list[str]) -> dict[str, Any]:
    """The values every harness shares: the brief and what is generated."""
    return {
        "project": {
            "name": config.knobs.project.name,
            "kind": config.knobs.project.kind,
        },
        "profile": config.profile.name,
        "brief": _read(root, BRIEF_FILE),
        "enforced": list(config.knobs.harnesses.enforced),
        "instructions_only": list(config.knobs.harnesses.instructions_only),
        "generated": sorted(outputs),
    }
