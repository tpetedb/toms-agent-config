"""`tac launch`: the command line, environment and records of each client's
session, the refusal on drift and on an API-key billing route (design 9, 13).

The clients are fakes on the launcher's search path; a real `claude` or `codex`
never runs, and nothing reaches a model.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tac import launch
from tac.cli import cli
from tac.config import Config, load_config
from tac.sync import sync
from tests import _fakeclients as fakes
from tests._gitrepo import commit_all, git
from tests._syncproject import replace_in

REPO = Path(__file__).resolve().parents[1]
BILLING = launch.BILLING_VARS


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fakes.project_repo(tmp_path)


@pytest.fixture
def config(project: Path) -> Config:
    return load_config(project)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return fakes.install(tmp_path / "bin", "claude", "codex")


def search(bin_dir: Path) -> str:
    return os.pathsep.join(
        [str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    )


def environ(bin_dir: Path, **more: str) -> dict[str, str]:
    return {"PATH": search(bin_dir), "HOME": str(bin_dir.parent), "LANG": "C", **more}


def headless(tmp_path: Path, contract: str = "order-request") -> launch.Headless:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Turn the issue into an order request.\n", encoding="utf-8")
    return launch.Headless(
        contract=contract,
        prompt_file=prompt,
        session_id=str(uuid.uuid4()),
        max_turns=12,
        max_budget_usd=2.5,
    )


def plan(
    project: Path, config: Config, bin_dir: Path, harness: launch.Harness, **more: Any
) -> launch.Plan:
    given = more.pop("environ", environ(bin_dir))
    return launch.plan(
        project,
        config,
        harness,
        environ=given,
        search_path=search(bin_dir),
        **more,
    )


# ---------------------------------------------------------------- command lines


def test_claude_interactive_is_the_agent_file_with_the_seat_s_effort(
    project: Path, config: Config, bin_dir: Path
) -> None:
    builder = plan(project, config, bin_dir, "claude", role="builder")
    assert builder.argv[1:5] == ["--agent", "builder", "--effort", "high"]
    assert builder.argv[0] == str(bin_dir / "claude")
    assert builder.argv[5:] == ["--session-id", builder.session_id]
    chief = plan(project, config, bin_dir, "claude", role="chief")
    assert chief.argv[1:5] == ["--agent", "chief", "--effort", "ultracode"]


def test_claude_headless_carries_every_flag_and_never_bare_mode(
    project: Path, config: Config, bin_dir: Path, tmp_path: Path
) -> None:
    spec = headless(tmp_path)
    found = plan(project, config, bin_dir, "claude", role="chief", headless=spec)
    argv = found.argv
    assert argv[1] == "-p"
    flag = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1)}
    assert flag["--setting-sources"] == "project"
    assert "--strict-mcp-config" in argv
    assert flag["--output-format"] == "json"
    assert flag["--max-turns"] == "12" and flag["--max-budget-usd"] == "2.5"
    assert flag["--session-id"] == spec.session_id
    overlay = json.loads(Path(flag["--settings"]).read_text())
    assert overlay == {"permissions": {"disableBypassPermissionsMode": "disable"}}
    assert json.loads(Path(flag["--mcp-config"]).read_text()) == {"mcpServers": {}}
    contract = json.loads(
        (project / "contracts/handoffs/order-request.schema.json").read_text()
    )
    assert json.loads(flag["--json-schema"]) == contract
    assert found.stdin == spec.prompt_file
    assert "--bare" not in argv and found.billing_route == "subscription"


def test_codex_headless_writes_its_result_to_a_file_not_the_event_stream(
    project: Path, config: Config, bin_dir: Path, tmp_path: Path
) -> None:
    spec = headless(tmp_path, "review")
    found = plan(project, config, bin_dir, "codex", role="reviewer", headless=spec)
    argv = found.argv
    assert argv[1] == "exec"
    flag = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1)}
    assert flag["--output-schema"] == str(
        project / "contracts/handoffs/review.schema.json"
    )
    assert Path(flag["-o"]) == found.result_file
    store = Path(flag["--add-dir"])
    assert store.is_absolute() and store == (project / ".git" / "agents").resolve()
    assert flag["-C"] == str(project)
    assert argv[-1] == "-" and "--json" not in argv


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("role", ["chief", "builder", "reviewer", "scout", None])
def test_no_command_line_ever_takes_bare_mode(
    project: Path,
    config: Config,
    bin_dir: Path,
    tmp_path: Path,
    harness: Any,
    role: Any,
) -> None:
    for spec in (None, headless(tmp_path)):
        found = plan(project, config, bin_dir, harness, role=role, headless=spec)
        assert "--bare" not in found.argv
    assert "--bare" not in (REPO / "src/tac/launch.py").read_text()


# ------------------------------------------------------------ what a session inherits


@pytest.mark.parametrize("name", BILLING)
def test_an_api_key_billing_route_is_refused(
    project: Path, config: Config, bin_dir: Path, name: str
) -> None:
    with pytest.raises(launch.LaunchError, match="subscription"):
        plan(
            project, config, bin_dir, "claude", environ=environ(bin_dir, **{name: "1"})
        )


def test_the_environment_is_an_allowlist(
    project: Path, config: Config, bin_dir: Path
) -> None:
    given = environ(
        bin_dir,
        GITHUB_TOKEN="ghp_not_real",
        GH_TOKEN="ghp_not_real",
        AWS_SECRET_ACCESS_KEY="x",
        PYTHONPATH="/planted",
        TERM="xterm",
        CLAUDE_CONFIG_DIR=str(bin_dir.parent / "claude"),
    )
    env = plan(project, config, bin_dir, "claude", environ=given).env
    assert env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert env["GH_TELEMETRY"] == "false"
    assert env["TERM"] == "xterm" and env["LANG"] == "C"
    assert env["CLAUDE_CONFIG_DIR"] == given["CLAUDE_CONFIG_DIR"]
    for secret in ("GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH"):
        assert secret not in env
    allowed = {*launch.CLIENT_VARS, *config.runtime.env.pass_}
    allowed |= {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "GH_TELEMETRY"}
    assert set(env) <= allowed


def test_a_headless_session_runs_the_fake_client_with_that_environment(
    project: Path, config: Config, bin_dir: Path, tmp_path: Path
) -> None:
    spec = headless(tmp_path)
    found = plan(
        project,
        config,
        bin_dir,
        "claude",
        role="chief",
        headless=spec,
        environ=environ(bin_dir, GITHUB_TOKEN="ghp_not_real"),
    )
    done = launch.run(found)
    assert done.returncode == 0
    result = json.loads(launch.result_text(found, done))
    assert result == json.loads((fakes.FIXTURES / "order-request.json").read_text())
    [call] = fakes.calls(bin_dir)
    assert "GITHUB_TOKEN" not in call["env"]
    assert call["prompt"] == spec.prompt_file.read_text()


def test_the_launch_record_holds_what_the_memory_reader_takes(
    project: Path, config: Config, bin_dir: Path
) -> None:
    found = plan(project, config, bin_dir, "claude", role="builder")
    launch.write_records(project, config, found)
    folder = project / ".git" / "agents" / "dispatch"
    record = json.loads((folder / f"{found.session_id}.launch.json").read_text())
    assert record == {
        "provider": "claude",
        "harness": "claude",
        "model": "claude-opus-5-5",
        "effort_requested": "high",
        "effort_actual": "high",
        "role": "builder",
    }
    # A session the owner starts is not dispatched: no token record.
    assert not (folder / f"{found.session_id}.json").exists()


# ---------------------------------------------------------------- drift


def drifted(project: Path, tmp_path: Path) -> None:
    """origin/main holds a lock for a render this checkout does not have."""
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(project), str(other))
    replace_in(
        other / ".agents/config/policy.toml",
        'deny_read = ["*.env",',
        'deny_read = ["*.key", "*.env",',
    )
    sync(other, links=False)
    commit_all(other, "Deny reading key files")
    git(
        other,
        "remote",
        "set-url",
        "origin",
        git(project, "remote", "get-url", "origin"),
    )
    git(other, "push", "-q", "origin", "HEAD:main")
    git(project, "fetch", "-q", "origin")


def test_a_worktree_that_differs_from_origin_s_lock_is_refused(
    project: Path, config: Config, bin_dir: Path, tmp_path: Path
) -> None:
    assert plan(project, config, bin_dir, "claude").drift == ()
    drifted(project, tmp_path)
    found = plan(project, config, bin_dir, "claude", role="builder")
    assert any("differs from the lock at origin/main" in p for p in found.drift)
    with pytest.raises(launch.LaunchError, match="differ from origin/main"):
        launch.refuse_drift(found, allow_drift=False)
    launch.refuse_drift(found, allow_drift=True)
    with pytest.raises(launch.LaunchError, match="interactive"):
        plan(
            project,
            config,
            bin_dir,
            "claude",
            role="builder",
            headless=headless(tmp_path),
            allow_drift=True,
        )


def test_a_planted_hook_source_is_drift(
    project: Path, config: Config, bin_dir: Path
) -> None:
    planted = project / "hooks" / "extra.py"
    planted.parent.mkdir(exist_ok=True)
    planted.write_text("print('planted')\n", encoding="utf-8")
    found = plan(project, config, bin_dir, "claude")
    assert "hooks/extra.py is not in the lock at origin/main" in found.drift


def test_launch_refuses_to_start_on_drift_from_the_command_line(
    project: Path, bin_dir: Path, tmp_path: Path
) -> None:
    drifted(project, tmp_path)
    done = CliRunner().invoke(
        cli,
        ["launch", "claude", "--role", "builder", "--root", str(project)],
        env={"PATH": search(bin_dir)},
    )
    assert done.exit_code == 1 and "differ from origin/main" in done.output
    assert fakes.calls(bin_dir) == []


# ---------------------------------------------------------------- --print


def test_print_shows_the_command_and_exits_0_without_the_client(
    project: Path, tmp_path: Path
) -> None:
    done = CliRunner().invoke(
        cli,
        ["launch", "codex", "--role", "builder", "--print", "--root", str(project)],
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.exit_code == 0, done.output
    assert done.output.startswith("codex --add-dir ")
    assert "not installed" in done.output


def test_print_shortens_the_inline_schema(
    project: Path, bin_dir: Path, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x\n", encoding="utf-8")
    done = CliRunner().invoke(
        cli,
        [
            "launch",
            "claude",
            "--role",
            "chief",
            "--headless",
            "--contract",
            "order-request",
            "--prompt-file",
            str(prompt),
            "--print",
            "--root",
            str(project),
        ],
        env={"PATH": search(bin_dir)},
    )
    assert done.exit_code == 0, done.output
    assert "<schema," in done.output and "--bare" not in done.output
