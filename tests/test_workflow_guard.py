"""The board ruling on Claude Code's Workflow tool (DESIGN 4, 5 and 9).

Ultracode starts agents through Workflow, which neither the handoff guard on
Agent|Task nor native_delegation = "off" covered. Workflow is now allowed only
for a role whose charter delegates and that launches with ultracode, and only
for a script a pipeline stage of that role registers, with the script's sha256
in generated.lock matching the bytes in the tool input, and only with the
runner's single-use token bound to run, stage and script hash; the call is
recorded before it is allowed. The runner issues tokens from M3, so until then
every Workflow call is denied; the tests stand a token in for the runner's to
prove the rest of the path.

From M3 the runner issues those tokens: a session it dispatched (its launcher
wrote a dispatch record) spends one on every spawn, Agent and Task included,
bound to the sha256 of the spawn's prompt or the script; a session nobody
dispatched, the owner's own chat with the chief, keeps Agent and Task.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tac import hook
from tac.config import load_config
from tac.pipelines import check_pipelines
from tac.runner import (
    Runner,
    bind,
    controller_store,
    create_key,
    ensure_store,
    open_runner,
)
from tac.sync import check_tree, read_lock, sync
from tac.work import Bad
from tests._gitrepo import short_dir
from tests._syncproject import copy_project, frontmatter, replace_in, ultracode_off

SCRIPT = ".agents/workflows/specify.js"
BODY = (
    "export const meta = { name: 'specify', description: 'Draft the order spec' }\n"
    "const spec = await agent('Draft the order spec from the request.')\n"
    "return spec\n"
)
WORKERS = ("builder", "reviewer", "manager", "scout")
SPAWN_TOOLS = {"Agent", "Task", "Workflow"}


def register(root: Path, script: str = SCRIPT, body: str = BODY) -> None:
    """Register `script` on the order pipeline's specify stage, a chief stage."""
    path = root / script
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    replace_in(
        root / ".agents/config/pipelines/order.toml",
        'template = "handoffs/specify.md.j2"',
        'template = "handoffs/specify.md.j2"\n'
        f'workflows = ["{script}"]                # the registered Workflow scripts',
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = copy_project(tmp_path / "project")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    register(root)
    sync(root, links=False)
    return root


TOKEN = "t-1"


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the runner's single-use token (M3), so a test reaches the
    part of the guard that runs after it."""
    monkeypatch.setattr(hook, "_runner_token", lambda *_: TOKEN)


def journal(root: Path) -> list[dict[str, Any]]:
    path = root / ".git" / "agents" / "journal" / "workflows.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def workflow(root: Path, role: str | None, **tool_input: Any) -> hook.Verdict:
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-1",
        "cwd": str(root),
        "tool_name": "Workflow",
        "tool_input": tool_input or {"script": BODY},
    }
    if role is not None:
        payload["agent_type"] = role
    return hook.evaluate(root, "claude", "PreToolUse", payload)


def spawn(
    root: Path, role: str | None, tool: str = "Agent", prompt: str = "look around"
) -> hook.Verdict:
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-1",
        "cwd": str(root),
        "tool_name": tool,
        "tool_input": {"prompt": prompt, "subagent_type": "scout"},
    }
    if role is not None:
        payload["agent_type"] = role
    return hook.evaluate(root, "claude", "PreToolUse", payload)


# ---------------------------------------------------------------- the guard


def test_an_unregistered_script_is_denied(project: Path) -> None:
    verdict = workflow(project, "chief", script=BODY + "// one more line\n")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "not registered" in verdict.reason
    assert journal(project) == []


def test_a_builder_s_workflow_call_is_denied(project: Path) -> None:
    verdict = workflow(project, "builder")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "roles/builder.toml" in verdict.reason
    assert journal(project) == []


@pytest.mark.parametrize("role", WORKERS)
@pytest.mark.parametrize("tool", sorted(SPAWN_TOOLS))
def test_the_guard_denies_every_spawn_tool_to_a_worker_role(
    project: Path, role: str, tool: str
) -> None:
    verdict = (
        workflow(project, role) if tool == "Workflow" else spawn(project, role, tool)
    )
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard"), role


def test_a_session_without_a_role_cannot_start_a_workflow(project: Path) -> None:
    verdict = workflow(project, None)
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "claude --agent" in verdict.reason


def test_a_registered_script_without_a_runner_token_is_denied(
    project: Path,
) -> None:
    # Fail closed until M3: the registry alone does not let a script run.
    for verdict in (
        workflow(project, "chief"),
        workflow(project, "chief", scriptPath=SCRIPT),
    ):
        assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
        assert "runner token" in verdict.reason and SCRIPT in verdict.reason
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_registered_script_by_the_chief_is_allowed_and_recorded(
    project: Path,
) -> None:
    digest = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert workflow(project, "chief").verdict == "allow"
    assert workflow(project, "chief", scriptPath=SCRIPT).verdict == "allow"
    records = journal(project)
    assert len(records) == 2
    for record in records:
        assert record["role"] == "chief"
        assert (record["pipeline"], record["stage"]) == ("order", "specify")
        assert (record["script"], record["sha256"]) == (SCRIPT, digest)
        assert record["session_id"] == "s-1"
        assert record["token"] == TOKEN


@pytest.mark.usefixtures("tokens")
def test_a_delegating_role_without_ultracode_cannot_start_a_workflow(
    project: Path,
) -> None:
    ultracode_off(project)
    verdict = workflow(project, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "does not launch with ultracode" in verdict.reason
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_script_path_through_a_symlinked_folder_is_denied(project: Path) -> None:
    # linkdir sits outside .agents/, so an agent could retarget it between the
    # guard's read and the client's.
    (project / "linkdir").symlink_to(project / ".agents/workflows")
    verdict = workflow(project, "chief", scriptPath="linkdir/specify.js")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_symlink_to_the_registered_script_is_denied(project: Path) -> None:
    link = project / "notes" / "specify.js"
    link.parent.mkdir()
    link.symlink_to(project / SCRIPT)
    verdict = workflow(project, "chief", scriptPath="notes/specify.js")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_registered_path_that_is_a_symlink_is_denied(project: Path) -> None:
    # Same bytes, but the registered path now points at a file an agent writes.
    elsewhere = project / "notes" / "specify.js"
    elsewhere.parent.mkdir()
    elsewhere.write_text(BODY, encoding="utf-8")
    (project / SCRIPT).unlink()
    (project / SCRIPT).symlink_to(elsewhere)
    verdict = workflow(project, "chief", scriptPath=SCRIPT)
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "symlink" in verdict.reason
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_script_path_that_climbs_through_a_symlink_is_denied(
    project: Path, tmp_path: Path
) -> None:
    # The spelling normalises to the registered path, but the OS resolves
    # away/.. after following away, to a tree an agent writes.
    decoy = tmp_path / "decoy"
    (decoy / "inner").mkdir(parents=True)
    (decoy / ".agents/workflows").mkdir(parents=True)
    (decoy / ".agents/workflows/specify.js").write_text(BODY, encoding="utf-8")
    (project / "away").symlink_to(decoy / "inner")
    verdict = workflow(project, "chief", scriptPath=f"away/../{SCRIPT}")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert ".." in verdict.reason
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_registered_path_with_other_bytes_is_denied(project: Path) -> None:
    (project / SCRIPT).write_text(BODY + "log('changed')\n", encoding="utf-8")
    verdict = workflow(project, "chief", scriptPath=SCRIPT)
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_copy_of_a_registered_script_at_another_path_is_denied(
    project: Path,
) -> None:
    # Only the registered path is write-protected; a copy elsewhere could be
    # swapped between the guard's read and the client's.
    copy = project / "notes" / "specify.js"
    copy.parent.mkdir()
    copy.write_text(BODY, encoding="utf-8")
    verdict = workflow(project, "chief", scriptPath="notes/specify.js")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert journal(project) == []


def test_a_workflow_call_that_names_no_script_is_denied(project: Path) -> None:
    verdict = workflow(project, "chief", args={"x": 1})
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "script" in verdict.reason


@pytest.mark.usefixtures("tokens")
def test_a_workflow_call_that_names_two_scripts_is_denied(project: Path) -> None:
    # Both spellings of the registered script: the client would run only one,
    # and the guard cannot tell which, so it judges neither.
    verdict = workflow(project, "chief", script=BODY, scriptPath=SCRIPT)
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "exactly one script" in verdict.reason
    assert journal(project) == []


def test_a_script_path_outside_the_checkout_is_denied(
    project: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.js"
    outside.write_text(BODY, encoding="utf-8")
    verdict = workflow(project, "chief", scriptPath=str(outside))
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")


@pytest.mark.usefixtures("tokens")
def test_a_registered_script_is_denied_to_a_role_it_is_not_registered_for(
    project: Path,
) -> None:
    # The director delegates and launches with ultracode, but the script is
    # registered on a chief stage.
    verdict = workflow(project, "director")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")


@pytest.mark.usefixtures("tokens")
def test_a_script_registered_in_a_pipeline_that_is_not_enabled_is_denied(
    project: Path,
) -> None:
    # The script sits on the order pipeline's specify stage; with order out of
    # [pipelines] enabled, that registration authorises nothing.
    replace_in(
        project / ".agents/config.toml",
        'enabled = ["order", "board",',
        'enabled = ["board",',
    )
    verdict = workflow(project, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "not registered" in verdict.reason
    assert journal(project) == []


@pytest.mark.usefixtures("tokens")
def test_a_call_that_cannot_be_recorded_is_denied(tmp_path: Path) -> None:
    # Not a git repository: no worker store, so no record, so no workflow.
    root = copy_project(tmp_path / "bare")
    register(root)
    sync(root, links=False)
    verdict = workflow(root, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "record" in verdict.reason


def test_the_owner_s_chat_with_the_chief_keeps_its_subagents(
    project: Path,
) -> None:
    # No dispatch record: the session is the owner's own, the recorded exemption.
    assert spawn(project, "chief").verdict == "allow"


def dispatched(root: Path, token: str, stage: str = "specify") -> None:
    """The launcher's record for session s-1, as tac run writes it."""
    folder = root / ".git" / "agents" / "dispatch"
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": "r1",
        "stage": stage,
        "role": "chief",
        "token": token,
        "kind": "agent",
        "session_id": "s-1",
    }
    (folder / "s-1.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def runner(project: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Runner]:
    """A runner serving the project's controller store, which the guard finds
    through the environment as the checker would."""
    with short_dir() as state:
        monkeypatch.setenv("TAC_STATE_HOME", str(state))
        store = ensure_store(controller_store(project, {"TAC_STATE_HOME": str(state)}))
        create_key(store)
        found = open_runner(
            project, {"TAC_STATE_HOME": str(state)}, repository="example/demo"
        )
        server = bind(found)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield found
        finally:
            server.shutdown()
            server.server_close()


def test_a_dispatched_session_spends_a_token_bound_to_the_prompt(
    project: Path, runner: Runner
) -> None:
    prompt = "Draft the order spec from the request."
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    dispatched(project, runner.issue_token("r1", "specify", digest, "agent", "s-1"))
    assert spawn(project, "chief", prompt=prompt).verdict == "allow"
    # Spent: the same record cannot start a second agent.
    again = spawn(project, "chief", prompt=prompt)
    assert (again.verdict, again.check) == ("deny", "handoff-guard")
    assert "already used" in again.reason


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_a_dispatched_session_without_a_matching_token_is_refused(
    project: Path, runner: Runner, tool: str
) -> None:
    digest = hashlib.sha256(b"the rendered prompt").hexdigest()
    dispatched(project, runner.issue_token("r1", "specify", digest, "agent", "s-1"))
    verdict = spawn(project, "chief", tool, prompt="some other prompt")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "burned" in verdict.reason


def test_a_dispatched_session_whose_runner_is_down_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with short_dir() as state:
        monkeypatch.setenv("TAC_STATE_HOME", str(state))
        dispatched(project, "a" * 64)
        verdict = spawn(project, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "did not grant" in verdict.reason


def test_a_malformed_dispatch_record_is_refused(project: Path) -> None:
    folder = project / ".git" / "agents" / "dispatch"
    folder.mkdir(parents=True)
    (folder / "s-1.json").write_text('{"token": 1}', encoding="utf-8")
    verdict = spawn(project, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert "shape" in verdict.reason


def test_a_session_without_an_id_cannot_spawn(project: Path) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(project),
        "tool_name": "Agent",
        "agent_type": "chief",
        "tool_input": {"prompt": "x"},
    }
    verdict = hook.evaluate(project, "claude", "PreToolUse", payload)
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")


def test_a_registered_script_with_the_runner_s_token_is_allowed(
    project: Path, runner: Runner
) -> None:
    digest = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    token = runner.issue_token("r1", "specify", digest, "workflow", "s-1")
    dispatched(project, token)
    record = project / ".git" / "agents" / "dispatch" / "s-1.json"
    data = json.loads(record.read_text("utf-8"))
    record.write_text(json.dumps({**data, "kind": "workflow"}), encoding="utf-8")
    assert workflow(project, "chief").verdict == "allow"
    [entry] = journal(project)
    assert (entry["stage"], entry["sha256"], entry["token"]) == (
        "specify",
        digest,
        token,
    )
    # Single use: the second call finds the token spent.
    again = workflow(project, "chief")
    assert (again.verdict, again.check) == ("deny", "handoff-guard")


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_a_session_with_no_role_keeps_agent_and_task_but_not_workflow(
    project: Path, tool: str
) -> None:
    """Decided, not an accident: the owner's own plain claude session names no
    role and keeps its subagents, as does a name no charter has; a worker seat
    is named by --agent, so its role is known and refused; Workflow stays
    fail-closed for a session with no role or an unknown one (DESIGN 4)."""
    assert spawn(project, None, tool).verdict == "allow"
    assert spawn(project, "not-a-role", tool).verdict == "allow"
    worker = spawn(project, "builder", tool)
    assert (worker.verdict, worker.check) == ("deny", "handoff-guard")
    for role in (None, "not-a-role"):
        verdict = workflow(project, role)
        assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard"), role


def test_codex_spawn_agent_is_judged_as_before(project: Path) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(project),
        "tool_name": "spawn_agent",
        "tool_input": {"message": "hi"},
    }
    assert hook.evaluate(project, "codex", "PreToolUse", payload).verdict == "allow"


# ---------------------------------------------------------------- delegation off


def delegation_off(root: Path) -> None:
    replace_in(
        root / ".agents/config/profiles/standard.toml",
        'native_delegation = "guarded"',
        'native_delegation = "off"',
    )


def test_delegation_off_denies_workflow(project: Path) -> None:
    ultracode_off(project)
    delegation_off(project)
    sync(project, links=False)
    verdict = workflow(project, "chief")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert 'native_delegation = "off"' in verdict.reason
    deny = json.loads((project / ".claude/settings.json").read_text())["permissions"][
        "deny"
    ]
    assert set(deny) >= SPAWN_TOOLS


def test_tac_check_refuses_ultracode_with_delegation_off(project: Path) -> None:
    delegation_off(project)
    with pytest.raises(Bad, match="ultracode") as refused:
        load_config(project)
    assert "roles.chief" in str(refused.value)
    assert any("ultracode" in p for p in check_tree(project))


def test_ultracode_on_a_role_that_does_not_delegate_is_refused(
    project: Path,
) -> None:
    replace_in(
        project / ".agents/config/roles/chief.toml",
        "delegates = true",
        "delegates = false",
    )
    with pytest.raises(Bad, match="delegates = false"):
        load_config(project)


@pytest.mark.parametrize("role", WORKERS)
def test_a_worker_role_may_never_delegate(project: Path, role: str) -> None:
    replace_in(
        project / f".agents/config/roles/{role}.toml",
        "delegates = false",
        "delegates = true",
    )
    with pytest.raises(Bad, match=f"roles/{role}.toml: delegates = true"):
        load_config(project)


# ---------------------------------------------------------------- the render


def test_the_rendered_agent_files_omit_the_spawn_tools_for_the_worker_roles(
    project: Path,
) -> None:
    for role in WORKERS:
        meta = frontmatter((project / f".claude/agents/{role}.md").read_text())
        denied = {t.strip() for t in meta["disallowedTools"].split(",")}
        assert denied >= SPAWN_TOOLS, role
        assert not SPAWN_TOOLS & set(meta.get("tools", "").split(", ")), role
    for role in ("chief", "director"):
        meta = frontmatter((project / f".claude/agents/{role}.md").read_text())
        assert "disallowedTools" not in meta, role


def test_the_claude_handoff_guard_matches_workflow(project: Path) -> None:
    hooks = json.loads((project / ".claude/settings.json").read_text())["hooks"]
    (pre,) = hooks["PreToolUse"]
    assert set(pre["matcher"].split("|")) >= SPAWN_TOOLS
    codex = json.loads((project / ".codex/hooks.json").read_text())["hooks"]
    assert "Workflow" not in json.dumps(codex)


# ---------------------------------------------------------------- the registry


def test_the_lock_records_every_registered_script(project: Path) -> None:
    lock = read_lock(project)
    assert lock is not None
    digest = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert lock["workflows"] == {SCRIPT: digest}
    assert check_tree(project) == []
    (project / SCRIPT).write_text(BODY + "// edited\n", encoding="utf-8")
    assert any(SCRIPT in p and "workflows" in p for p in check_tree(project))


def stage_refusals(root: Path) -> list[str]:
    return [p for p in check_pipelines(root, ["order", "board"]) if "workflows" in p]


def test_a_worker_stage_may_not_register_a_script(project: Path) -> None:
    replace_in(
        project / ".agents/config/pipelines/order.toml",
        'template = "handoffs/build.md.j2"',
        f'template = "handoffs/build.md.j2"\nworkflows = ["{SCRIPT}"]',
    )
    [problem] = stage_refusals(project)
    assert "stage build" in problem and "roles/builder.toml" in problem


def test_only_the_lead_director_s_stage_may_register_a_script(project: Path) -> None:
    replace_in(
        project / ".agents/config/pipelines/board.toml",
        'template = "handoffs/critique.md.j2"',
        f'template = "handoffs/critique.md.j2"\nworkflows = ["{SCRIPT}"]',
    )
    replace_in(
        project / ".agents/config/pipelines/board.toml",
        'template = "handoffs/design.md.j2"',
        f'template = "handoffs/design.md.j2"\nworkflows = ["{SCRIPT}"]',
    )
    [problem] = stage_refusals(project)
    assert "stage review" in problem and "lead" in problem


def test_a_design_stage_in_the_other_seats_may_not_register_a_script(
    project: Path,
) -> None:
    # The stage id alone is not enough: design must also run in the lead seat.
    replace_in(
        project / ".agents/config/pipelines/board.toml",
        'seats = "lead"                          # which director seats run it',
        'seats = "others"                        # which director seats run it',
    )
    replace_in(
        project / ".agents/config/pipelines/board.toml",
        'template = "handoffs/design.md.j2"',
        f'template = "handoffs/design.md.j2"\nworkflows = ["{SCRIPT}"]',
    )
    [problem] = stage_refusals(project)
    assert "stage design" in problem and 'seats = "others"' in problem
    assert "lead seat" in problem


def test_a_lead_director_stage_other_than_design_may_not_register_a_script(
    project: Path,
) -> None:
    # revise runs in the lead seat too, but the ruling names the design stage.
    replace_in(
        project / ".agents/config/pipelines/board.toml",
        'template = "handoffs/revise.md.j2"',
        f'template = "handoffs/revise.md.j2"\nworkflows = ["{SCRIPT}"]',
    )
    [problem] = stage_refusals(project)
    assert "stage revise" in problem and "design stage" in problem


@pytest.mark.parametrize(
    ("script", "why"),
    [
        (".agents/workflows/missing.js", "does not exist"),
        ("scripts/specify.js", ".agents/workflows/"),
    ],
)
def test_a_registered_script_lives_under_agents_workflows(
    project: Path, script: str, why: str
) -> None:
    replace_in(
        project / ".agents/config/pipelines/order.toml",
        f'workflows = ["{SCRIPT}"]',
        f'workflows = ["{script}"]',
    )
    problems = stage_refusals(project)
    assert problems and all(why in p for p in problems), problems


# ---------------------------------------------------------------- records


def test_a_subagent_start_is_recorded_in_the_worker_store(project: Path) -> None:
    payload = {
        "hook_event_name": "SubagentStart",
        "session_id": "s-1",
        "agent_type": "scout",
        "agent_id": "a-1",
    }
    assert hook.check_spawn_record(project, payload, load_config(project)) is None
    path = project / ".git" / "agents" / "journal" / "spawns.jsonl"
    [line] = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert (line["session_id"], line["agent_type"], line["agent_id"]) == (
        "s-1",
        "scout",
        "a-1",
    )


def test_a_workflow_result_is_recorded_for_the_dispatch_receipt(
    project: Path,
) -> None:
    dispatched(project, "b" * 64)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s-1",
        "cwd": str(project),
        "tool_name": "Workflow",
        "tool_input": {"script": BODY},
        "tool_response": {"result": "spec"},
    }
    assert hook.check_workflow_record(project, payload, load_config(project)) is None
    path = project / ".git" / "agents" / "journal" / "workflow-results.jsonl"
    [line] = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert line["script_sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert line["token_sha256"] == hashlib.sha256(b"b" * 64).hexdigest()
    assert len(line["result_sha256"]) == 64


def records(root: Path, rel: str) -> list[dict[str, Any]]:
    path = root / ".git" / "agents" / rel
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"agent_type": "scout", "agent_id": "a-1"}, "no usable session id"),
        (
            {"session_id": "../s-1", "agent_type": "scout", "agent_id": "a-1"},
            "no usable session id",
        ),
        ({"session_id": "s-1", "agent_type": "scout"}, "no usable agent_id"),
        ({"session_id": "s-1", "agent_type": "scout", "agent_id": ""}, "agent_id"),
        ({"session_id": "s-1", "agent_type": "scout", "agent_id": 7}, "agent_id"),
        (
            {"session_id": "s-1", "agent_type": "scout", "agent_id": "a" * 257},
            "agent_id",
        ),
        ({"session_id": "s-1", "agent_id": "a-1"}, "no usable agent_type"),
    ],
)
def test_a_malformed_subagent_start_is_not_recorded(
    project: Path, payload: dict[str, Any], why: str
) -> None:
    event = {"hook_event_name": "SubagentStart", **payload}
    refusal = hook.check_spawn_record(project, event, load_config(project))
    assert refusal is not None and why in refusal
    assert hook.evaluate(project, "claude", "SubagentStart", event).verdict == "allow"
    assert records(project, hook.SPAWN_JOURNAL) == []


@pytest.mark.parametrize(
    ("more", "why"),
    [
        ({"session_id": 3}, "no usable session id"),
        ({"tool_input": "not a table"}, "names 0"),
        ({"tool_input": {"script": BODY, "scriptPath": SCRIPT}}, "names 2"),
        ({"tool_input": {"scriptPath": "../elsewhere.js"}}, "outside this checkout"),
    ],
)
def test_a_malformed_workflow_result_is_not_recorded(
    project: Path, more: dict[str, Any], why: str
) -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "session_id": "s-1",
        "cwd": str(project),
        "tool_name": "Workflow",
        "tool_input": {"script": BODY},
        "tool_response": {"result": "spec"},
        **more,
    }
    refusal = hook.check_workflow_record(project, event, load_config(project))
    assert refusal is not None and why in refusal
    assert hook.evaluate(project, "claude", "PostToolUse", event).verdict == "allow"
    assert records(project, hook.WORKFLOW_RESULTS) == []


def test_a_workflow_result_without_a_response_is_not_recorded(project: Path) -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "session_id": "s-1",
        "tool_name": "Workflow",
        "tool_input": {"script": BODY},
    }
    refusal = hook.check_workflow_record(project, event, load_config(project))
    assert refusal is not None and "no tool_response" in refusal
    assert records(project, hook.WORKFLOW_RESULTS) == []


EARLY_RECORD = (
    "[checks.early-record]\n"
    'event = "PreToolUse"\n'
    'kind = "record"\n'
    'claude = "Workflow"\n'
    'codex = ""\n\n'
)


def _raises(*_: Any) -> str | None:
    raise RuntimeError("a record that breaks")


@pytest.mark.parametrize("behaviour", ["records", "raises"])
def test_a_record_never_lets_through_a_call_the_guard_denies(
    project: Path, monkeypatch: pytest.MonkeyPatch, behaviour: str
) -> None:
    """A record check wired on the same event and tool as the handoff guard, and
    evaluated before it, changes nothing: the worker's Workflow call is still
    refused whether the record writes or breaks."""
    replace_in(
        project / ".agents/config/hooks.toml",
        "[checks.handoff-guard]",
        EARLY_RECORD + "[checks.handoff-guard]",
    )
    names = list(load_config(project).hooks.checks)
    assert names.index("early-record") < names.index("handoff-guard")
    seen: list[str] = []

    def records_it(_root: Path, payload: Any, _config: Any) -> str | None:
        seen.append(str(payload.get("tool_name")))
        return None

    monkeypatch.setitem(
        hook.CHECKS,  # pyright: ignore[reportArgumentType]
        "early-record",
        records_it if behaviour == "records" else _raises,
    )
    verdict = workflow(project, "builder")
    assert (verdict.verdict, verdict.check) == ("deny", "handoff-guard")
    assert seen == (["Workflow"] if behaviour == "records" else [])
    assert records(project, hook.WORKFLOW_RESULTS) == []


def test_a_record_check_never_changes_the_answer(project: Path) -> None:
    # The record-kind check hooks.toml wires runs, and the verdict stays allow.
    assert load_config(project).hooks.checks["workflow-record"].kind == "record"
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s-1",
        "cwd": str(project),
        "tool_name": "Workflow",
        "tool_input": {"script": BODY},
        "tool_response": {},
    }
    verdict = hook.evaluate(project, "claude", "PostToolUse", payload)
    assert verdict.verdict == "allow"
    assert (
        project / ".git" / "agents" / "journal" / "workflow-results.jsonl"
    ).is_file()
