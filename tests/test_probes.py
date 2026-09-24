"""Client discovery, the minimal launch and the trust probes for Claude Code and
Codex. The launch is `--version` only, so no model session ever starts."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tac.doctor import CHECKS, Status, client_status, trust_status
from tac.probes import (
    CLIENTS,
    claude_config,
    claude_trust,
    client_version,
    codex_config,
    codex_trust,
    discover,
    launch_env,
    trust_candidates,
)
from tests._gitrepo import git


def fake_client(folder: Path, name: str, script: str) -> Path:
    """An executable that records its arguments and PATH, then runs script."""
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{folder}/{name}.args"\n'
        f'printf "%s\\n" "$PATH" > "{folder}/{name}.path"\n'
        f"{script}\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


# ---- discovery and the minimal launch


def test_discovery_finds_each_client_on_the_given_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    for name in CLIENTS.values():
        fake_client(bin_dir, name, "exit 0")
    for harness in CLIENTS:
        assert discover(harness, str(bin_dir)) == str(bin_dir / CLIENTS[harness])


def test_discovery_reports_a_missing_client(tmp_path: Path) -> None:
    assert discover("codex", str(tmp_path)) is None


def test_the_launch_is_version_only(tmp_path: Path) -> None:
    exe = fake_client(tmp_path / "bin", "claude", 'echo "2.1.0 (Claude Code)"')
    assert client_version(str(exe), {"HOME": str(tmp_path)}) == "2.1.0 (Claude Code)"
    assert (tmp_path / "bin/claude.args").read_text().split() == ["--version"]


def test_the_launch_runs_with_a_fixed_path(tmp_path: Path) -> None:
    exe = fake_client(tmp_path / "bin", "codex", 'echo "codex-cli 0.1.0"')
    client_version(str(exe), {"PATH": "/tmp/shim:/usr/bin", "HOME": str(tmp_path)})
    seen = (tmp_path / "bin/codex.path").read_text().strip()
    assert seen == os.pathsep.join([str(tmp_path / "bin"), "/usr/bin", "/bin"])


def test_the_launch_passes_no_caller_variables(tmp_path: Path) -> None:
    env = launch_env("/opt/x/bin/claude", {"HOME": "/h", "ANTHROPIC_API_KEY": "k"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == "/h" and env["DISABLE_AUTOUPDATER"] == "1"


def test_a_client_that_fails_version_has_no_version(tmp_path: Path) -> None:
    exe = fake_client(tmp_path / "bin", "codex", "echo nope; exit 3")
    assert client_version(str(exe), {}) is None


def test_client_status_reports_the_version(tmp_path: Path) -> None:
    fake_client(tmp_path / "bin", "claude", 'echo "2.1.0 (Claude Code)"')
    status, detail = client_status("claude", {"PATH": str(tmp_path / "bin")})
    assert status is Status.PASS
    assert detail == "found, version 2.1.0 (Claude Code)"


def test_client_status_fails_when_the_client_is_missing(tmp_path: Path) -> None:
    status, detail = client_status("codex", {"PATH": str(tmp_path)})
    assert status is Status.FAIL
    assert "codex not found on PATH" in detail


def test_client_status_fails_when_version_does_not_answer(tmp_path: Path) -> None:
    fake_client(tmp_path / "bin", "codex", "exit 1")
    status, _ = client_status("codex", {"PATH": str(tmp_path / "bin")})
    assert status is Status.FAIL


def test_doctor_carries_a_client_and_a_trust_check_per_harness() -> None:
    names = [c.name for c in CHECKS]
    for harness in CLIENTS:
        assert f"client-{harness}" in names and f"trust-{harness}" in names


# ---- Claude Code workspace trust


def write_claude(config: Path, projects: dict[str, object]) -> Path:
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"projects": projects, "userID": "x"}))
    return config


def test_claude_trusts_an_accepted_folder(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    entry = {"hasTrustDialogAccepted": True}
    config = write_claude(tmp_path / "c.json", {str(root.resolve()): entry})
    trust = claude_trust(root, config)
    assert trust.trusted is True and trust.detail == "trusted through this folder"


def test_claude_trust_in_a_parent_covers_the_folder(tmp_path: Path) -> None:
    root = tmp_path / "a" / "repo"
    root.mkdir(parents=True)
    entry = {"hasTrustDialogAccepted": True}
    config = write_claude(tmp_path / "c.json", {str((tmp_path / "a").resolve()): entry})
    trust = claude_trust(root, config)
    assert trust.trusted is True
    # The report may be shared, so it names no absolute path.
    assert str(tmp_path) not in trust.detail


def test_claude_without_acceptance_is_not_trusted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    entry = {"hasTrustDialogAccepted": False}
    config = write_claude(tmp_path / "c.json", {str(root.resolve()): entry})
    assert claude_trust(root, config).trusted is False


def test_claude_without_a_config_is_not_trusted(tmp_path: Path) -> None:
    assert claude_trust(tmp_path, tmp_path / "none.json").trusted is False


def test_an_unreadable_claude_config_is_unknown(tmp_path: Path) -> None:
    config = tmp_path / "c.json"
    config.write_text("{not json")
    assert claude_trust(tmp_path, config).trusted is None


def test_claude_config_dir_is_respected(tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "HOME": "/elsewhere"}
    assert claude_config(env) == tmp_path / ".claude.json"
    assert claude_config({"HOME": "/h"}) == Path("/h/.claude.json")


# ---- Codex project trust


def write_codex(config: Path, projects: dict[str, str]) -> Path:
    config.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'[projects."{p}"]\ntrust_level = "{level}"\n' for p, level in projects.items()
    ]
    config.write_text('model = "x"\n' + "".join(lines))
    return config


def test_codex_trusts_a_trusted_project(tmp_path: Path) -> None:
    config = write_codex(tmp_path / "config.toml", {str(tmp_path.resolve()): "trusted"})
    trust = codex_trust([tmp_path], config)
    assert trust.trusted is True and trust.detail == "trusted through this checkout"


def test_codex_untrusted_wins(tmp_path: Path) -> None:
    main, worktree = tmp_path / "main", tmp_path / "wt"
    main.mkdir()
    worktree.mkdir()
    projects = {str(worktree.resolve()): "trusted", str(main.resolve()): "untrusted"}
    config = write_codex(tmp_path / "config.toml", projects)
    trust = codex_trust([worktree, main], config)
    assert trust.trusted is False and "untrusted" in trust.detail


def test_codex_unknown_project_is_not_trusted(tmp_path: Path) -> None:
    config = write_codex(tmp_path / "config.toml", {"/somewhere/else": "trusted"})
    assert codex_trust([tmp_path], config).trusted is False


def test_codex_without_a_config_is_not_trusted(tmp_path: Path) -> None:
    assert codex_trust([tmp_path], tmp_path / "none.toml").trusted is False


def test_an_unreadable_codex_config_is_unknown(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[projects\n")
    assert codex_trust([tmp_path], config).trusted is None


def test_codex_home_is_respected(tmp_path: Path) -> None:
    assert codex_config({"CODEX_HOME": str(tmp_path)}) == tmp_path / "config.toml"
    assert codex_config({"HOME": "/h"}) == Path("/h/.codex/config.toml")


def test_a_linked_worktree_is_trusted_through_the_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(worktree))
    candidates = trust_candidates(worktree)
    assert candidates == [worktree.resolve(), main.resolve()]
    config = write_codex(tmp_path / "config.toml", {str(main.resolve()): "trusted"})
    trust = codex_trust(candidates, config)
    assert trust.trusted is True and trust.detail == "trusted through the main checkout"


# ---- what doctor says


@pytest.mark.parametrize("harness", sorted(CLIENTS))
def test_an_untrusted_checkout_fails_and_names_the_owner_step(
    harness: str, tmp_path: Path
) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "CODEX_HOME": str(tmp_path)}
    status, detail = trust_status(harness, tmp_path, env)
    assert status is Status.FAIL
    assert "the owner's step" in detail and harness in detail


def test_a_trusted_checkout_passes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    entry = {"hasTrustDialogAccepted": True}
    write_claude(tmp_path / ".claude.json", {str(root.resolve()): entry})
    status, _ = trust_status("claude", root, {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert status is Status.PASS
