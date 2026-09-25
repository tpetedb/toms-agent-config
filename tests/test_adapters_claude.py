"""The Claude Code files carry the fields and values Claude Code's docs require.

Sources: https://code.claude.com/docs/en/settings-reference (settings keys and
scopes), https://code.claude.com/docs/en/permissions (rule syntax),
https://code.claude.com/docs/en/sub-agents (agent frontmatter) and
https://code.claude.com/docs/en/memory (the @ import).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tac.adapters import CLAUDE_TELEMETRY, charter_sha256, seat
from tac.config import load_config
from tac.sync import render, sync
from tests._syncproject import copy_project, frontmatter, replace_in, synced

SCHEMA_URL = "https://json.schemastore.org/claude-code-settings.json"
# The settings keys this adapter writes, each documented in the settings reference.
TOP_KEYS = {"$schema", "env", "permissions", "sandbox", "hooks"}
PERMISSION_KEYS = {"defaultMode", "deny", "disableBypassPermissionsMode"}
MODES = {"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}
SANDBOX_KEYS = {
    "enabled",
    "failIfUnavailable",
    "allowUnsandboxedCommands",
    "filesystem",
    "network",
}
FILESYSTEM_KEYS = {"allowWrite", "denyWrite", "denyRead", "allowRead"}
# Variables a project settings file may not set: Claude Code drops them.
PROJECT_REFUSED_ENV = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TMPDIR", "HOME", "TMPDIR"}
# Agent frontmatter fields from the sub-agents reference.
AGENT_FIELDS = {
    "name",
    "description",
    "tools",
    "disallowedTools",
    "model",
    "permissionMode",
    "maxTurns",
    "skills",
    "mcpServers",
    "hooks",
    "memory",
    "background",
    "omitClaudeMd",
    "effort",
    "isolation",
    "color",
    "initialPrompt",
}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def settings(root: Path) -> dict[str, Any]:
    return json.loads((root / ".claude/settings.json").read_text())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return synced(tmp_path)


def test_settings_use_documented_keys_and_the_published_schema(root: Path) -> None:
    data = settings(root)
    assert data["$schema"] == SCHEMA_URL
    assert set(data) <= TOP_KEYS
    assert set(data["permissions"]) <= PERMISSION_KEYS
    assert set(data["sandbox"]) <= SANDBOX_KEYS
    assert set(data["sandbox"]["filesystem"]) <= FILESYSTEM_KEYS
    assert set(data["sandbox"]["network"]) == {"allowedDomains"}


def test_the_sandbox_is_rendered_explicitly_and_fails_closed(root: Path) -> None:
    sandbox = settings(root)["sandbox"]
    # Booleans, never strings: Claude Code reads the type.
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


def test_the_permission_mode_comes_from_the_profile(root: Path) -> None:
    permissions = settings(root)["permissions"]
    config = load_config(root)
    assert permissions["defaultMode"] == config.profile.claude.defaultMode
    assert permissions["defaultMode"] in MODES - {"bypassPermissions", "auto"}
    assert permissions["disableBypassPermissionsMode"] == "disable"


def test_writes_to_the_tree_and_every_locked_output_are_denied(root: Path) -> None:
    data = settings(root)
    deny = data["permissions"]["deny"]
    deny_write = data["sandbox"]["filesystem"]["denyWrite"]
    assert "Edit(/.agents/**)" in deny
    assert "./.agents" in deny_write
    for rel in render(root).outputs:
        assert f"Edit(/{rel})" in deny, rel
        assert f"./{rel}" in deny_write, rel
    assert "~/.local/state/tac" in deny_write


def test_no_rule_the_client_would_ignore_or_that_breaks_skill_links(
    root: Path,
) -> None:
    deny = settings(root)["permissions"]["deny"]
    for rule in deny:
        tool, _, rest = rule.partition("(")
        # Path rules for Write, MultiEdit or NotebookEdit are never consulted.
        if rest:
            assert tool in {"Read", "Edit"}, rule
    # A Read deny on .agents/ would hide the skills the links point at.
    assert not [r for r in deny if r.startswith("Read(/.agents")]
    assert not [r for r in deny if r.startswith("Read(.agents")]


def test_secrets_and_credentials_are_denied_to_tools_and_commands(root: Path) -> None:
    data = settings(root)
    deny = data["permissions"]["deny"]
    deny_read = data["sandbox"]["filesystem"]["denyRead"]
    for rule in ("Read(*.env)", "Read(~/.ssh/**)", "Read(~/.config/gh/**)"):
        assert rule in deny
    for path in (
        "./**/*.env",
        "~/.ssh",
        "~/.aws",
        "~/.config/gh",
        "~/.local/state/tac",
    ):
        assert path in deny_read
    env_dir = str(Path(load_config(root).knobs.secrets.env_file).parent)
    assert env_dir in deny_read


def test_sandbox_paths_use_the_documented_prefixes(root: Path) -> None:
    fs = settings(root)["sandbox"]["filesystem"]
    for path in [*fs["denyRead"], *fs["denyWrite"]]:
        assert path.startswith(("./", "~/", "/")), path
        assert not path.endswith("/"), path


def test_telemetry_and_secret_scrub_ride_in_env(root: Path) -> None:
    env = settings(root)["env"]
    assert all(isinstance(v, str) for v in env.values())
    for name, value in CLAUDE_TELEMETRY["partial"].items():
        assert env[name] == value
    assert env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert env["GH_TELEMETRY"] == "false"
    # The partial tier keeps Remote Control and auto mode (Q15).
    assert "DISABLE_TELEMETRY" not in env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env
    assert not set(env) & PROJECT_REFUSED_ENV
    assert not [n for n in env if n.startswith("XDG_")]


def test_the_network_allowlist_comes_from_network_toml(root: Path) -> None:
    allowed = settings(root)["sandbox"]["network"]["allowedDomains"]
    assert allowed == list(load_config(root).network.egress.allow)


def test_hooks_are_an_empty_table_until_m2(root: Path) -> None:
    assert settings(root)["hooks"] == {}


def test_claude_md_imports_agents_md(root: Path) -> None:
    lines = (root / "CLAUDE.md").read_text().splitlines()
    assert "@AGENTS.md" in lines
    assert (root / "AGENTS.md").is_file()


def test_every_agent_has_the_required_frontmatter(root: Path) -> None:
    config = load_config(root)
    files = sorted((root / ".claude/agents").glob("*.md"))
    assert {p.stem for p in files} == set(config.roles)
    for path in files:
        text = path.read_text()
        meta = frontmatter(text)
        assert set(meta) <= AGENT_FIELDS, path.name
        assert meta["name"] == path.stem and ":" not in meta["name"]
        assert meta["description"] == config.roles[path.stem].description
        found = seat(config, path.stem, "claude")
        assert found is not None, path.name
        if found.effort is None:
            # A model that takes no effort parameter gets no effort field.
            assert "effort" not in meta, path.name
        else:
            assert meta["effort"] == found.effort and found.effort in EFFORTS
        assert meta["model"].startswith("claude-") or meta["model"] in {
            "sonnet",
            "opus",
            "haiku",
            "fable",
            "inherit",
        }
        assert f"# charter-sha256: {charter_sha256(root, path.stem)}" in text


def test_directors_run_at_xhigh_on_the_lead_seat(root: Path) -> None:
    meta = frontmatter((root / ".claude/agents/director.md").read_text())
    assert meta == {
        "name": "director",
        "description": meta["description"],
        "model": "claude-fable-5-1",
        "effort": "xhigh",
    }


def test_an_agent_carries_the_models_toml_seat(root: Path) -> None:
    models = tomllib.loads((root / ".agents/config/models.toml").read_text())
    for role in ("builder", "chief", "scout"):
        meta = frontmatter((root / f".claude/agents/{role}.md").read_text())
        seat = models["roles"][role]["anthropic"]
        # A seat with no effort renders no effort field, and the other way round.
        pair = (meta["model"], meta.get("effort"))
        assert pair == (seat["model"], seat.get("effort"))


def test_native_delegation_off_denies_the_spawn_tools(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    replace_in(
        root / ".agents/config/profiles/standard.toml",
        'native_delegation = "guarded"',
        'native_delegation = "off"',
    )
    sync(root)
    deny = settings(root)["permissions"]["deny"]
    assert "Agent" in deny and "Task" in deny


def test_the_full_telemetry_tier_adds_the_kill_switches(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    replace_in(
        root / ".agents/config.toml", 'host_tier = "partial"', 'host_tier = "full"'
    )
    sync(root)
    env = settings(root)["env"]
    assert env["DISABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_nothing_renders_for_claude_when_it_is_not_enforced(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    replace_in(
        root / ".agents/config.toml",
        'enforced = ["claude", "codex"]',
        'enforced = ["codex"]',
    )
    replace_in(
        root / ".agents/config.toml",
        'instructions_only = ["copilot"',
        'instructions_only = ["claude", "copilot"',
    )
    outputs = render(root).outputs
    assert not [p for p in outputs if p.startswith(".claude/") or p == "CLAUDE.md"]
    assert "AGENTS.md" in outputs
