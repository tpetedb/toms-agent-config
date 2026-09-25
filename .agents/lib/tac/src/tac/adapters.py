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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tac.config import CONFIG_DIR, Config

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
# The subagent tool under its current and former names; denied outright when the
# profile turns native delegation off, so refusal survives a hook failure (C1).
CLAUDE_SPAWN_TOOLS = ("Agent", "Task")


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
    }


def roles_for(config: Config, root: Path, harness: str) -> list[dict[str, Any]]:
    """Every role with a seat on this harness, by name."""
    out = []
    for name in sorted(config.roles):
        found = seat(config, name, harness)
        if found is not None:
            out.append(_role(config, root, name, found))
    return out


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
    env_file_dir = str(Path(config.knobs.secrets.env_file).parent)
    deny_read_globs = [
        *policy.paths.deny_read,
        *(f"{d}/**" for d in CREDENTIAL_DIRS),
        f"{env_file_dir}/**",
        f"{CONTROLLER_STORE}/**",
    ]
    deny_read_globs = list(dict.fromkeys(deny_read_globs))
    # Write denies: the authored tree and every generated path the lock lists.
    # Never a Read deny on .agents/: that would also break the skill links.
    write_paths = list(dict.fromkeys([*policy.paths.deny_write, *outputs]))
    deny_rules = [f"Read({g})" for g in deny_read_globs]
    deny_rules += [_edit_rule(p) for p in write_paths]
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
        "deny_write": list(
            dict.fromkeys(
                [
                    *(_sandbox_path(p) for p in write_paths),
                    CONTROLLER_STORE,
                ]
            )
        ),
        "allowed_domains": allowed,
        "env": env,
        "extras": _read(root, CLAUDE_EXTRAS_FILE),
        "roles": roles_for(config, root, "claude"),
    }


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
        "agents_enabled": profile.native_delegation != "off",
        "max_threads": config.knobs.teams.max_local_agents,
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
