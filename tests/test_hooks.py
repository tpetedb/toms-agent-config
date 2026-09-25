"""The hook guard and its checker (DESIGN 9 and 10, build condition C2).

The guard runs as a client runs it: a subprocess started from an absolute
interpreter and an absolute script, the event on stdin. Its checker is a stub
whose answer the test sets when the test is about the protocol or a refusal,
and a copy of the candidate `src/tac` when the test is about a real check.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from tac import hook, work
from tac.config import load_config
from tac.doctor import Status, check_hook_guard
from tac.sync import check_tree, read_lock, sync
from tests._guard import (
    REPO,
    SOURCE_GUARD,
    STAMPED,
    Fixture,
    emitted,
    event,
    guarded,
    hostile,
    install_candidate,
    run_guard,
    site_packages,
)
from tests._syncproject import copy_project, replace_in

CLAUDE_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SubagentStop", "SessionStart")
CODEX_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionStart")
EVERY = [("claude", e) for e in CLAUDE_EVENTS] + [("codex", e) for e in CODEX_EVENTS]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return guarded(tmp_path)


def tool_event(name: str, tool: str = "Edit", **given: Any) -> dict[str, Any]:
    return event(name, tool_name=tool, tool_input=given)


def payload_for(name: str) -> dict[str, Any]:
    return tool_event(name) if "Tool" in name else event(name)


# ---------------------------------------------------------------- the protocol


@pytest.mark.parametrize(("client", "name"), EVERY)
def test_an_allowed_event_exits_0_and_says_nothing(
    fx: Fixture, client: str, name: str
) -> None:
    done = run_guard(fx, client, name, payload_for(name))
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_a_refused_tool_call_exits_2_with_the_documented_deny(
    fx: Fixture, client: str
) -> None:
    fx.stub(verdict="deny", check="generated-paths", reason="AGENTS.md is generated")
    done = run_guard(fx, client, "PreToolUse", tool_event("PreToolUse"))
    assert done.returncode == 2
    out = emitted(done)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "[generated-paths] AGENTS.md is generated" in out["permissionDecisionReason"]
    assert "AGENTS.md is generated" in done.stderr


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_a_reminder_before_a_tool_call_lets_it_through_as_context(
    fx: Fixture, client: str
) -> None:
    """Stderr at exit 0 reaches only a debug log on both clients, so the
    reminder travels as additionalContext, which the model reads."""
    fx.stub(verdict="remind", reason="mind the order")
    done = run_guard(fx, client, "PreToolUse", tool_event("PreToolUse"))
    assert done.returncode == 0
    assert emitted(done)["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "additionalContext": "tac guard: [stub] mind the order",
    }


@pytest.mark.parametrize(
    ("client", "name"),
    [("claude", "Stop"), ("claude", "SubagentStop"), ("codex", "Stop")],
)
def test_a_stop_is_sent_back_once_and_then_ends(
    fx: Fixture, client: str, name: str
) -> None:
    fx.stub(verdict="remind", check="order-check", reason="the order does not hold")
    first = run_guard(fx, client, name, event(name))
    assert first.returncode == 2
    assert "the order does not hold" in first.stderr
    again = run_guard(fx, client, name, event(name, stop_hook_active=True))
    assert again.returncode == 0


def test_after_a_tool_claude_hears_the_reason_as_context(fx: Fixture) -> None:
    fx.stub(verdict="deny", reason="that file is generated")
    done = run_guard(fx, "claude", "PostToolUse", tool_event("PostToolUse"))
    assert done.returncode == 0
    out = emitted(done)["hookSpecificOutput"]
    assert out == {
        "hookEventName": "PostToolUse",
        "additionalContext": "tac guard: [stub] that file is generated",
    }


def test_after_a_tool_codex_gets_the_reason_in_place_of_the_result(
    fx: Fixture,
) -> None:
    fx.stub(verdict="deny", reason="that file is generated")
    done = run_guard(fx, "codex", "PostToolUse", tool_event("PostToolUse"))
    assert done.returncode == 2
    assert "that file is generated" in done.stderr


def test_after_a_tool_a_codex_reminder_keeps_the_result(fx: Fixture) -> None:
    """Only a refusal replaces the tool result; a reminder, or the guard's own
    failure, is context next to it."""
    fx.stub(verdict="remind", reason="mind the order")
    done = run_guard(fx, "codex", "PostToolUse", tool_event("PostToolUse"))
    assert done.returncode == 0
    context = emitted(done)["hookSpecificOutput"]["additionalContext"]
    assert context == "tac guard: [stub] mind the order"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_a_session_start_carries_the_checker_s_note_as_context(
    fx: Fixture, client: str
) -> None:
    fx.stub(verdict="allow", reason="order docs-intro is active on this branch")
    done = run_guard(fx, client, "SessionStart", event("SessionStart"))
    assert done.returncode == 0
    context = emitted(done)["hookSpecificOutput"]["additionalContext"]
    assert context == "order docs-intro is active on this branch"


def test_an_event_the_client_does_not_fire_is_refused(fx: Fixture) -> None:
    done = run_guard(fx, "codex", "SubagentStop", event("SubagentStop"))
    assert done.returncode == 2
    assert "fires no SubagentStop" in done.stderr


def test_an_event_wired_for_another_hook_is_refused(fx: Fixture) -> None:
    done = run_guard(fx, "claude", "PreToolUse", event("PostToolUse"))
    assert done.returncode == 2
    assert "wired for PreToolUse" in done.stderr


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_an_unreadable_event_fails_closed_before_a_tool(fx: Fixture, body: str) -> None:
    assert run_guard(fx, "claude", "PreToolUse", body).returncode == 2
    stop = run_guard(fx, "claude", "Stop", body)
    assert stop.returncode == 0
    assert "stdin" in emitted(stop)["systemMessage"]


# ---------------------------------------------------------------- tampering


def refuses(fx: Fixture, why: str, **kwargs: Any) -> None:
    """Refused before a tool, reported and let go at a stop, both naming why."""
    tool = run_guard(fx, "claude", "PreToolUse", tool_event("PreToolUse"), **kwargs)
    assert tool.returncode == 2, tool.stderr
    assert why in tool.stderr
    assert emitted(tool)["hookSpecificOutput"]["permissionDecision"] == "deny"
    stop = run_guard(fx, "codex", "Stop", event("Stop"), **kwargs)
    assert stop.returncode == 0
    assert why in stop.stderr
    assert why in emitted(stop)["systemMessage"]


def test_an_edited_guard_is_refused(fx: Fixture) -> None:
    with fx.guard.open("a", encoding="utf-8") as handle:
        handle.write("\n# one more line\n")
    refuses(fx, f"{STAMPED} does not match")


def test_a_file_planted_beside_the_guard_is_refused(fx: Fixture) -> None:
    (fx.guard.parent / "json.py").write_text("raise SystemExit(0)\n", "utf-8")
    refuses(fx, ".agents/hooks/json.py is not in")


def test_a_link_beside_the_guard_is_refused(fx: Fixture, tmp_path: Path) -> None:
    (fx.guard.parent / "extra").symlink_to(tmp_path)
    refuses(fx, "is a link")


def test_a_missing_lock_is_refused(fx: Fixture) -> None:
    (fx.root / ".agents" / "generated.lock").unlink()
    refuses(fx, "cannot be read")


def test_a_lock_without_the_guard_is_refused(fx: Fixture) -> None:
    lock = fx.root / ".agents" / "generated.lock"
    lock.write_text("schema_version = 1\n\n[hooks]\n", encoding="utf-8")
    refuses(fx, "records no hooks")


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ('extra = "value"', "not a single sha256"),
        ("import os", "not a line tac sync writes"),
        ("[hooks]", "twice"),
    ],
)
def test_a_lock_in_a_shape_tac_sync_never_writes_is_refused(
    fx: Fixture, line: str, why: str
) -> None:
    lock = fx.root / ".agents" / "generated.lock"
    lock.write_text(lock.read_text("utf-8") + line + "\n", encoding="utf-8")
    refuses(fx, why)


def test_a_lock_of_another_version_is_refused(fx: Fixture) -> None:
    replace_in(
        fx.root / ".agents" / "generated.lock",
        "schema_version = 1",
        "schema_version = 2",
    )
    refuses(fx, "schema_version 2")


def test_a_guard_reached_through_a_link_is_refused(fx: Fixture, tmp_path: Path) -> None:
    link = tmp_path / "run.py"
    link.symlink_to(fx.guard)
    refuses(fx, "is a link", script=str(link))


def test_a_guard_started_by_a_relative_path_is_refused(fx: Fixture) -> None:
    refuses(fx, "relative path", script=STAMPED)


def test_a_guard_started_without_isolation_is_refused(fx: Fixture) -> None:
    refuses(fx, "not started with -I", flags=())


def test_a_guard_started_from_a_virtual_environment_is_refused(fx: Fixture) -> None:
    refuses(fx, "inside the virtual environment", python=sys.executable)


def test_a_missing_checker_environment_says_run_tac_init(fx: Fixture) -> None:
    shutil.rmtree(fx.root / ".agents" / ".venv")
    refuses(fx, "run tac init")


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ({"mode": "crash"}, "the checker exited 3"),
        ({"mode": "garbage"}, "not JSON"),
        ({"verdict": "maybe"}, "not a verdict"),
        ({"module": "/elsewhere/tac/__init__.py"}, "imported tac from /elsewhere"),
    ],
)
def test_a_checker_that_fails_or_is_not_the_deployed_copy_is_refused(
    fx: Fixture, spec: dict[str, str], why: str
) -> None:
    fx.stub(**spec)
    refuses(fx, why)


def test_a_checker_that_does_not_answer_in_time_is_refused(fx: Fixture) -> None:
    fx.stub(mode="hang")
    done = run_guard(fx, "claude", "PreToolUse", tool_event("PreToolUse"), deadline=1)
    assert done.returncode == 2
    assert "no answer within 1s" in done.stderr


def test_an_editable_checker_pointing_at_a_source_tree_is_refused(
    fx: Fixture, tmp_path: Path
) -> None:
    """What `uv sync` without --no-editable would give: tac imported from a tree
    an agent can write, however honest its answer."""
    site = site_packages(fx.root)
    source = tmp_path / "src"
    source.mkdir()
    shutil.move(str(site / "tac"), str(source / "tac"))
    (site / "_editable_tac.pth").write_text(f"{source}\n", encoding="utf-8")
    refuses(fx, f"imported tac from {os.path.realpath(source)}")


# ---------------------------------------------------------------- isolation (C2)


def test_path_pythonpath_and_planted_modules_change_nothing_the_guard_loads(
    fx: Fixture, tmp_path: Path
) -> None:
    env, marker, work_dir = hostile(fx, tmp_path)
    fx.stub(mode="echo")
    done = run_guard(
        fx, "claude", "SessionStart", event("SessionStart"), env=env, cwd=work_dir
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text("utf-8")
    seen = json.loads(emitted(done)["hookSpecificOutput"]["additionalContext"])
    # The checker got the fixed PATH and none of the variables that steer Python,
    # uv or git, nor the token.
    assert seen["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    for name in env:
        if name not in ("PATH", "HOME"):
            assert name not in seen["env"], name
    assert seen["cwd"] == os.path.realpath(fx.root)
    for entry in seen["path"]:
        assert entry, "the working folder is on the checker's sys.path"
        for place in (fx.root, work_dir, tmp_path / "planted"):
            inside = os.path.realpath(entry).startswith(os.path.realpath(place))
            assert not inside or ".agents/.venv" in entry, entry


def test_a_real_checker_still_answers_from_the_venv_in_a_hostile_checkout(
    tmp_path: Path,
) -> None:
    fx = guarded(tmp_path, checker="candidate")
    copy_project(fx.root)
    sync(fx.root, links=False)
    env, marker, work_dir = hostile(fx, tmp_path)
    target = fx.root / "AGENTS.md"
    done = run_guard(
        fx,
        "claude",
        "PreToolUse",
        tool_event("PreToolUse", file_path=str(target)),
        env=env,
        cwd=work_dir,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert done.returncode == 2, done.stderr
    assert "[generated-paths] AGENTS.md is generated" in done.stderr


# ---------------------------------------------------------------- the real checker


@pytest.fixture
def real(tmp_path: Path) -> Fixture:
    """A synced copy of this project with the guard stamped into it and the
    candidate tac installed, not editable, in its .agents/.venv."""
    fx = guarded(tmp_path, checker="candidate")
    copy_project(fx.root)
    sync(fx.root, links=False)
    return fx


def test_the_real_checker_refuses_generated_files_and_secrets(real: Fixture) -> None:
    edit = tool_event("PreToolUse", file_path=str(real.root / ".claude/settings.json"))
    done = run_guard(real, "claude", "PreToolUse", edit)
    assert done.returncode == 2
    assert "[generated-paths]" in done.stderr
    read = tool_event("PreToolUse", tool="Read", file_path=str(real.root / "app.env"))
    done = run_guard(real, "claude", "PreToolUse", read)
    assert done.returncode == 2
    assert "[secret-read] " in done.stderr
    assert "*.env" in done.stderr


def test_the_real_checker_lets_ordinary_calls_through(real: Fixture) -> None:
    own = tool_event("PreToolUse", file_path=str(real.root / "src" / "app.py"))
    assert run_guard(real, "claude", "PreToolUse", own).returncode == 0
    patch = tool_event("PreToolUse", tool="apply_patch", command="*** Begin Patch")
    assert run_guard(real, "codex", "PreToolUse", patch).returncode == 0
    for client, name in (("claude", "Stop"), ("codex", "SessionStart")):
        done = run_guard(real, client, name, event(name, cwd=str(real.root)))
        assert done.returncode == 0, done.stderr


def rendered(root: Path, client: str, event_name: str) -> str:
    """The one command a client's rendered file runs for an event."""
    rel = ".claude/settings.json" if client == "claude" else ".codex/hooks.json"
    hooks = json.loads((root / rel).read_text("utf-8"))["hooks"]
    (group,) = hooks[event_name]
    (one,) = group["hooks"]
    return one["command"]


@pytest.mark.skipif(
    not os.access("/usr/bin/python3", os.X_OK), reason="no system interpreter"
)
@pytest.mark.parametrize("client", ["claude", "codex"])
def test_the_rendered_command_runs_the_stamped_guard_in_a_hostile_session(
    real: Fixture, tmp_path: Path, client: str
) -> None:
    """The exact command tac sync renders, run by a shell as the client runs it,
    from an environment and a checkout an agent prepared: it reaches the real
    checker, refuses an edit of a generated file, and loads nothing planted."""
    done = subprocess.run(
        ["/usr/bin/git", "init", "-q", str(real.root)], capture_output=True, check=False
    )
    assert done.returncode == 0, done.stderr
    env, marker, work_dir = hostile(real, tmp_path)
    target = real.root / ".claude" / "settings.json"
    if client == "claude":
        env["CLAUDE_PROJECT_DIR"] = str(real.root)
        cwd = work_dir
        call = tool_event("PreToolUse", file_path=str(target))
    else:
        # Codex names no project folder, so the command asks git from the
        # session's folder, anywhere inside the checkout.
        cwd = real.root / "src"
        cwd.mkdir(exist_ok=True)
        call = tool_event(
            "PreToolUse",
            tool="apply_patch",
            command=patch("Update File: ../.claude/settings.json"),
        )
    call["cwd"] = str(cwd)
    command = rendered(real.root, client, "PreToolUse")
    ran = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(call),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=120,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert ran.returncode == 2, ran.stderr
    assert "[generated-paths] .claude/settings.json is generated" in ran.stderr
    stop = subprocess.run(
        ["/bin/sh", "-c", rendered(real.root, client, "Stop")],
        input=json.dumps(event("Stop", cwd=str(cwd))),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=120,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert stop.returncode == 0, stop.stderr
    assert "tac guard" not in stop.stdout + stop.stderr


def test_the_lock_records_the_guard_and_tac_check_sees_it_change(
    real: Fixture,
) -> None:
    lock = read_lock(real.root)
    assert lock is not None
    assert set(lock["hooks"]) == {STAMPED, "hooks/git/prek.toml"}
    assert check_tree(real.root) == []
    with real.guard.open("a", encoding="utf-8") as handle:
        handle.write("# edited\n")
    assert any(f"hooks {STAMPED} changed" in p for p in check_tree(real.root))


def test_this_checkout_locks_the_source_guard_and_its_stamp() -> None:
    lock = read_lock(REPO)
    assert lock is not None
    assert {STAMPED, "hooks/run.py"} <= set(lock["hooks"])


def test_doctor_names_a_guard_that_was_never_stamped(tmp_path: Path) -> None:
    fx = guarded(tmp_path, layout="stamped", checker="none")
    source = fx.root / "hooks" / "run.py"
    assert check_hook_guard(fx.root)[0] is Status.PASS
    source.parent.mkdir()
    shutil.copyfile(SOURCE_GUARD, source)
    assert check_hook_guard(fx.root)[0] is Status.PASS
    with source.open("a", encoding="utf-8") as handle:
        handle.write("# a candidate change\n")
    status, detail = check_hook_guard(fx.root)
    assert status is Status.FAIL
    assert "just stamp-lib" in detail
    fx.guard.unlink()
    assert check_hook_guard(fx.root)[0] is Status.FAIL


# ---------------------------------------------------------------- evaluate()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = copy_project(tmp_path / "project")
    sync(root, links=False)
    return root


def judge(root: Path, client: str, name: str, **payload: Any) -> hook.Verdict:
    return hook.evaluate(root, client, name, {"hook_event_name": name, **payload})


def test_every_verdict_matches_its_contract(project: Path) -> None:
    contract = json.loads((REPO / hook.CONTRACT).read_text("utf-8"))
    assert contract == hook.verdict_json_schema(), (
        f"run: tac hook schema > {hook.CONTRACT}"
    )
    validator = Draft7Validator(contract)
    verdicts = [
        judge(project, "claude", "SessionStart"),
        judge(
            project,
            "claude",
            "PreToolUse",
            tool_name="Write",
            tool_input={"file_path": str(project / "CLAUDE.md")},
        ),
    ]
    assert [v.verdict for v in verdicts] == ["allow", "deny"]
    for verdict in verdicts:
        validator.validate(json.loads(verdict.model_dump_json()))


@pytest.mark.parametrize(
    "path",
    ["~/.ssh/id_ed25519", "~/.aws/credentials", "deploy/secrets/token", "key.pem"],
)
def test_a_read_of_a_secret_is_refused(project: Path, path: str) -> None:
    verdict = judge(
        project,
        "claude",
        "PreToolUse",
        tool_name="Read",
        tool_input={"file_path": path},
    )
    assert (verdict.verdict, verdict.check) == ("deny", "secret-read")


def test_a_glob_or_grep_for_secrets_is_refused(project: Path) -> None:
    glob = judge(
        project,
        "claude",
        "PreToolUse",
        tool_name="Glob",
        tool_input={"pattern": "*.env"},
    )
    grep = judge(
        project,
        "claude",
        "PreToolUse",
        tool_name="Grep",
        tool_input={"path": str(Path.home() / ".config/gh")},
    )
    assert glob.check == grep.check == "secret-read"


def test_an_edit_under_the_policy_s_deny_write_is_refused(project: Path) -> None:
    verdict = judge(
        project,
        "claude",
        "PreToolUse",
        tool_name="Edit",
        tool_input={"file_path": str(project / ".agents/config/policy.toml")},
    )
    assert verdict.verdict == "deny"
    assert ".agents/**" in verdict.reason


def test_a_check_left_unwired_on_a_client_does_not_run(project: Path) -> None:
    """secret-read names no Codex tool, since Codex reads through its shell,
    and Codex reports every edit as apply_patch, never as Edit."""
    read = judge(
        project,
        "codex",
        "PreToolUse",
        tool_name="Read",
        tool_input={"file_path": "~/.ssh/id_ed25519"},
    )
    edit = judge(
        project,
        "codex",
        "PreToolUse",
        tool_name="Edit",
        tool_input={"file_path": str(project / "AGENTS.md")},
    )
    assert read.verdict == edit.verdict == "allow"


def patch(*headers: str) -> str:
    body = [f"*** {h}\n@@\n+x" for h in headers]
    return "*** Begin Patch\n" + "\n".join(body) + "\n*** End Patch\n"


@pytest.mark.parametrize(
    "header",
    ["Update File: AGENTS.md", "Add File: .codex/extra.toml", "Delete File: CLAUDE.md"],
)
def test_a_codex_patch_on_a_generated_path_is_refused(
    project: Path, header: str
) -> None:
    verdict = judge(
        project,
        "codex",
        "PreToolUse",
        tool_name="apply_patch",
        tool_input={"command": patch("Add File: src/new.py", header)},
        cwd=str(project),
    )
    assert (verdict.verdict, verdict.check) == ("deny", "generated-paths")


def test_a_codex_patch_that_moves_a_file_onto_a_generated_path_is_refused(
    project: Path,
) -> None:
    text = patch("Update File: src/app.py").replace(
        "@@", "*** Move to: .claude/settings.json\n@@"
    )
    verdict = judge(
        project,
        "codex",
        "PreToolUse",
        tool_name="apply_patch",
        tool_input={"command": text},
        cwd=str(project),
    )
    assert (verdict.verdict, verdict.check) == ("deny", "generated-paths")
    assert ".claude/settings.json" in verdict.reason


def test_owned_paths_asks_once_for_every_file_a_patch_names(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def record(given: dict[str, Any]) -> tuple[int, str]:
        asked.append(given["tool_input"]["file_path"])
        return 0, ""

    monkeypatch.setattr(work, "hook_pre_tool", record)
    verdict = judge(
        project,
        "codex",
        "PreToolUse",
        tool_name="apply_patch",
        tool_input={"command": patch("Add File: src/a.py", "Update File: docs/b.md")},
        cwd=str(project),
    )
    assert verdict.verdict == "allow"
    real = project.resolve()
    assert asked == [str(real / "src/a.py"), str(real / "docs/b.md")]


def test_owned_paths_and_order_check_speak_through_tac_work(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(work, "hook_pre_tool", lambda _e: (2, "outside order one"))
    monkeypatch.setattr(work, "hook_stop", lambda _e: (2, "order one fails c1"))
    edit = judge(
        project,
        "claude",
        "PreToolUse",
        tool_name="Edit",
        tool_input={"file_path": str(project / "src/app.py")},
    )
    assert (edit.verdict, edit.check, edit.reason) == (
        "deny",
        "owned-paths",
        "outside order one",
    )
    for client, name in (
        ("claude", "Stop"),
        ("claude", "SubagentStop"),
        ("codex", "Stop"),
    ):
        stop = judge(project, client, name)
        assert (stop.verdict, stop.check) == ("remind", "order-check"), name


def test_a_check_tac_does_not_know_refuses(project: Path) -> None:
    hooks = project / ".agents/config/hooks.toml"
    text = hooks.read_text("utf-8")
    extra = (
        "\n[checks.mystery]\n"
        'event = "PreToolUse"\nkind = "deny"\nclaude = "Bash"\ncodex = ""\n'
    )
    hooks.write_text(text.replace("# ---- git:", extra + "\n# ---- git:"), "utf-8")
    verdict = hook.evaluate(
        project,
        "claude",
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        load_config(project),
    )
    assert (verdict.verdict, verdict.check) == ("deny", "mystery")


@pytest.mark.parametrize(
    ("matcher", "tool", "wired"),
    [
        ("", "Edit", False),
        ("*", "anything", True),
        ("Edit|Write", "Write", True),
        ("Edit|Write", "WriteMore", False),
        ("(", "Edit", False),
    ],
)
def test_matchers_read_as_the_clients_read_them(
    matcher: str, tool: str, wired: bool
) -> None:
    assert hook.wired(matcher, tool) is wired


def test_the_candidate_install_is_not_the_source_tree(tmp_path: Path) -> None:
    """The fixture venv holds a copy, as --no-editable gives, so a test of the
    module check proves the check and not a lucky path."""
    root = tmp_path / "r"
    install_candidate(root)
    installed = site_packages(root) / "tac" / "__init__.py"
    assert installed.is_file()
    assert not str(installed.resolve()).startswith(str(REPO / "src"))
