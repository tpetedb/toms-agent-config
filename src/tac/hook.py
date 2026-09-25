"""`tac hook run`: the full checker the stamped guard calls on each hook event.

The guard (`hooks/run.py`, stamped to `.agents/hooks/run.py`) is stdlib only and
speaks each client's hook protocol; it knows nothing about policy. This module
reads `config/hooks.toml`, runs every check wired for the client and the event,
and answers one verdict as JSON, which the guard turns into an exit code or the
client's own decision field. The answer carries where `tac` was imported from,
so the guard can refuse a checker that is not the deployed, non-editable copy.

Hooks remind and refuse early; the gate is `tac check`, the work-order checks,
CI and the ruleset (DESIGN 9).
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

import tac
from tac import pipelines, work
from tac.adapters import (
    CLAUDE_SPAWN_TOOLS,
    CONFIGURED_AS,
    GUARD_EVENTS,
    TOOL_EVENTS,
    denied_reads,
    denied_writes,
)
from tac.config import CONFIG_DIR, Config, load_config, ultracode_seats
from tac.draft07 import draft07
from tac.handoff import worker_store
from tac.sync import LOCK_FILE, generated_paths, read_lock

CONTRACT = "contracts/hook-verdict.schema.json"
Client = Literal["claude", "codex"]
# The events the guard forwards, per client, and the ones judged by another
# event's checks; the adapters render the hooks from the same tables.
EVENTS: Mapping[str, tuple[str, ...]] = GUARD_EVENTS
# Where each tool names the file it acts on: Edit, Write, Read and NotebookEdit
# use file_path or notebook_path; Grep and Glob search under path, and Glob's
# pattern may name a file outright.
EDIT_KEYS = ("file_path", "notebook_path")
READ_KEYS = ("file_path", "notebook_path", "path", "pattern")
# Codex edits files through apply_patch, whose tool_input.command holds the
# patch; each file it adds, updates, deletes or moves to is named on a header
# line (https://developers.openai.com/codex/hooks).
PATCH_TOOL = "apply_patch"
PATCH_FILE = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M
)
# Claude's tools that start agents: Agent and Task spawn one subagent, Workflow
# runs a script that spawns many. Codex's spawn_agent is judged apart.
SPAWN_TOOLS = frozenset(CLAUDE_SPAWN_TOOLS)
WORKFLOW_TOOL = "Workflow"
# A Workflow call carries its script inline or names the file it runs; the
# guard hashes whichever it is given, and refuses a call that carries both.
SCRIPT_KEYS = ("script", "scriptPath")
# Where each allowed Workflow call is recorded, inside the worker store, until
# the runner's journal and signed receipts take over in M3.
WORKFLOW_JOURNAL = "journal/workflows.jsonl"


class Verdict(BaseModel):
    """What the checker answers the guard, once per hook event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["allow", "deny", "remind"]
    # The hooks.toml check that decided, or empty when none fired.
    check: str
    reason: str
    # Where `tac` was imported from; the guard refuses anything outside .agents/.venv.
    module: str


def verdict_json_schema() -> dict[str, JsonValue]:
    """The verdict as JSON Schema draft-07, generated from the model."""
    return draft07(Verdict)


def module_origin() -> str:
    return os.path.realpath(tac.__file__ or "")


# ---------------------------------------------------------------- checks

# A check answers the reason it fires, or None when the call is fine.
Check = Callable[[Path, Mapping[str, Any], Config], "str | None"]


def _targets(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Iterator[str]:
    given = payload.get("tool_input")
    if not isinstance(given, Mapping):
        return
    for key in keys:
        value = given.get(key)
        if isinstance(value, str) and value:
            yield value


def _edits(payload: Mapping[str, Any]) -> Iterator[str]:
    """Every file an editing call names, whether by a path key or, for
    apply_patch, on the patch's own header lines."""
    yield from _targets(payload, EDIT_KEYS)
    if payload.get("tool_name") != PATCH_TOOL:
        return
    for patch in _targets(payload, ("command",)):
        yield from (m.group(1) for m in PATCH_FILE.finditer(patch))


def _absolute(raw: str, payload: Mapping[str, Any], root: Path) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        base = payload.get("cwd")
        path = (Path(base) if isinstance(base, str) and base else root) / path
    return Path(os.path.realpath(path))


def _relative(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _glob_match(text: str, pattern: str) -> bool:
    """fnmatch with `**` read as `*`, since fnmatch's `*` already crosses
    folders; `dir/**` covers the folder itself too, as a search root."""
    if pattern.endswith("/**") and fnmatch.fnmatchcase(text, pattern[:-3]):
        return True
    return fnmatch.fnmatchcase(text, pattern.replace("**", "*"))


def _home(pattern: str) -> tuple[str, ...]:
    """A `~/` glob as spelled with the home folder put in, and again with its
    fixed folders resolved, since the path it is matched against has had its
    links followed (a home under /var on macOS is /private/var)."""
    expanded = os.path.expanduser(pattern)
    parts = expanded.split("/")
    fixed = next(
        (i for i, p in enumerate(parts) if any(c in p for c in "*?[")), len(parts)
    )
    real = os.path.realpath("/".join(parts[:fixed]) or "/")
    return tuple(dict.fromkeys([expanded, "/".join([real, *parts[fixed:]])]))


def _under_home(path: Path, raw: str, pattern: str) -> bool:
    return _glob_match(raw, pattern) or any(
        _glob_match(path.as_posix(), p) for p in _home(pattern)
    )


def check_generated_paths(
    root: Path, payload: Mapping[str, Any], config: Config
) -> str | None:
    """An edit to a generated file or a path no agent writes: the policy's
    deny_write and the controller store, as the rendered sandbox denies them."""
    generated = generated_paths(root)
    # The list the rendered sandbox.filesystem.denyWrite is made from.
    writes = denied_writes(config, generated)
    for raw in _edits(payload):
        path = _absolute(raw, payload, root)
        rel = _relative(path, root)
        if rel is not None and rel in generated:
            return (
                f"{rel} is generated by tac sync; change .agents/ or "
                "templates/adapters/ and run tac sync instead"
            )
        for pattern in writes:
            if pattern.startswith("~"):
                hit = _under_home(path, raw, pattern)
            else:
                hit = rel is not None and _glob_match(rel, pattern)
            if hit:
                return f"{rel or raw} is under {pattern}, which no agent writes"
    return None


def _secret(path: Path, rel: str | None, pattern: str) -> bool:
    if pattern.startswith("~"):
        return any(_glob_match(path.as_posix(), p) for p in _home(pattern))
    if "/" not in pattern:
        return _glob_match(path.name, pattern)
    candidates = [path.as_posix()] + ([rel] if rel else [])
    return any(
        _glob_match(c, pattern) or _glob_match(c, "*/" + pattern) for c in candidates
    )


def check_secret_read(
    root: Path, payload: Mapping[str, Any], config: Config
) -> str | None:
    """A read, search or glob that names a path no agent reads: the policy's
    deny_read, the credential folders, the secrets folder and the controller
    store, as the rendered client rules deny them."""
    # The list the rendered Read denies and sandbox.filesystem.denyRead are made from.
    reads = denied_reads(config)
    for raw in _targets(payload, READ_KEYS):
        path = _absolute(raw, payload, root)
        rel = _relative(path, root)
        for pattern in reads:
            if _secret(path, rel, pattern) or _glob_match(raw, pattern):
                return f"{raw} matches {pattern}, which no agent reads"
    return None


def check_owned_paths(
    root: Path, payload: Mapping[str, Any], _config: Config
) -> str | None:
    """An edit outside what the orders on this branch own (tac work), asked
    once per file the call names."""
    for raw in _edits(payload):
        one = {
            **payload,
            "tool_input": {"file_path": str(_absolute(raw, payload, root))},
        }
        code, why = work.hook_pre_tool(one)
        if code:
            return why
    return None


def check_order(_root: Path, payload: Mapping[str, Any], _config: Config) -> str | None:
    """A stop while the order this agent worked on does not hold (tac work)."""
    code, why = work.hook_stop(dict(payload))
    return why if code else None


def check_handoff_guard(
    root: Path, payload: Mapping[str, Any], config: Config
) -> str | None:
    """A native spawn the guard cannot vouch for (design sections 4 and 5).

    Every spawn tool is refused under native_delegation = "off" and to a role
    whose charter does not delegate. A Workflow call is allowed only for a role
    that delegates and launches with ultracode, running a script that a stage of
    that role registers with the hash generated.lock records, with the runner's
    single-use token for it, and only once the call is recorded; no token exists
    before M3, so until then every Workflow call is refused. Agent and Task from
    a delegating role pass until the runner's dispatch tokens (M3, C1)."""
    tool = str(payload.get("tool_name") or "")
    if tool not in SPAWN_TOOLS:
        return None
    profile = config.profile
    if profile.native_delegation == "off":
        return (
            f"{tool} is refused: profiles/{profile.name}.toml sets native_delegation "
            '= "off", so agents start only through tac run'
        )
    given = payload.get("agent_type")
    role = given if isinstance(given, str) else ""
    charter = config.roles.get(role)
    if charter is not None and not charter.delegates:
        return (
            f"{tool} is refused: {CONFIG_DIR}/roles/{role}.toml says delegates = "
            "false; the runner starts every agent this role needs"
        )
    if tool != WORKFLOW_TOOL:
        return None
    return _check_workflow(root, payload, config, role)


def _lexical(raw: str, payload: Mapping[str, Any], root: Path) -> str | None:
    """`raw` relative to the checkout as written, no link followed, or None when
    it leaves the checkout or climbs with `..`, which the OS resolves after any
    link on the way and so cannot be judged by its spelling."""
    path = Path(os.path.expanduser(raw))
    if ".." in path.parts:
        return None
    if not path.is_absolute():
        base = payload.get("cwd")
        path = (Path(base) if isinstance(base, str) and base else root) / path
    path = Path(os.path.normpath(path))
    for top in (root, root.resolve()):
        try:
            return path.relative_to(top).as_posix()
        except ValueError:
            continue
    return None


def _links_on_the_way(root: Path, rel: str) -> str | None:
    """The first component from the checkout down to the file that is a
    symlink, since a link outside `.agents/` can be retargeted by an agent
    between this check and the client reading the file."""
    here = root
    for part in Path(rel).parts:
        here = here / part
        if here.is_symlink():
            return here.relative_to(root).as_posix()
    return None


def _script_bytes(root: Path, payload: Mapping[str, Any]) -> tuple[bytes, str] | str:
    """The script a Workflow call runs and its path relative to the checkout as
    spelled (empty when inline), or the reason no single script can be read."""
    given = payload.get("tool_input")
    given = given if isinstance(given, Mapping) else {}
    named = [k for k in SCRIPT_KEYS if isinstance(given.get(k), str) and given[k]]
    if len(named) != 1:
        return (
            "a Workflow call names exactly one script, inline (script) or by "
            f"file (scriptPath); this one names {len(named)}"
        )
    if named[0] == "script":
        return str(given["script"]).encode("utf-8"), ""
    raw = str(given["scriptPath"])
    rel = _lexical(raw, payload, root)
    if rel is None:
        return f"the script {raw} is outside this checkout or climbs with .."
    link = _links_on_the_way(root, rel)
    if link is not None:
        return f"the script {raw} goes through the symlink {link}"
    path = root / rel
    if not path.is_file():
        return f"the script {raw} is not a plain file"
    try:
        return path.read_bytes(), rel
    except OSError as e:
        return f"the script {raw} cannot be read ({e.strerror})"


def _runner_token(
    _root: Path, _payload: Mapping[str, Any], _script: str, _digest: str
) -> str | None:
    """The runner's single-use token for this call, bound to the run, the stage
    and the script hash, once consumed; None when the call carries none. The
    runner issues tokens from M3, so until then no call carries one."""
    return None


def _check_workflow(
    root: Path, payload: Mapping[str, Any], config: Config, role: str
) -> str | None:
    if role not in config.roles:
        return (
            "Workflow is refused: a workflow starts only from a role session "
            "launched with `claude --agent <role>`, and this session names "
            + (f"{role!r}, not a role" if role else "no role")
        )
    if role not in ultracode_seats(config.models):
        return (
            f"Workflow is refused: {role} does not launch with ultracode "
            f"({CONFIG_DIR}/models.toml), and only an ultracode role runs workflows"
        )
    found = _script_bytes(root, payload)
    if isinstance(found, str):
        return f"Workflow is refused: {found}"
    body, rel = found
    digest = hashlib.sha256(body).hexdigest()
    try:
        locked = (read_lock(root) or {}).get("workflows", {})
    except work.Bad as e:
        return f"Workflow is refused: {e}"
    enabled = set(config.knobs.pipelines.enabled)
    matches = [
        (script, pipeline, stage)
        for script, entries in sorted(pipelines.registered_workflows(root).items())
        for pipeline, stage, owner in entries
        if owner == role
        and pipeline in enabled
        and locked.get(script) == digest
        # A file runs only from its registered path, spelled with no link on
        # the way, which no agent may write, so it cannot change between this
        # check and the client reading it.
        and rel in ("", script)
    ]
    if not matches:
        return (
            f"Workflow is refused: the script ({rel or 'inline'}, sha256 "
            f"{digest}) is not registered for {role}; a stage of that role lists "
            "its path under workflows "
            f"and {LOCK_FILE} records its hash (run tac sync after registering)"
        )
    script, pipeline, stage = matches[0]
    token = _runner_token(root, payload, script, digest)
    if token is None:
        # Fail closed until M3: a registered script alone does not run.
        return (
            f"Workflow is refused: {script} is registered for {role}, but the call "
            "carries no single-use runner token bound to its run, stage and "
            "script hash, and tac run issues those only from M3"
        )
    record = {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "session_id": payload.get("session_id"),
        "role": role,
        "pipeline": pipeline,
        "stage": stage,
        "script": script,
        "sha256": digest,
        "token": token,
    }
    try:
        journal = worker_store(root, config) / WORKFLOW_JOURNAL
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, work.Bad) as e:
        return f"Workflow is refused: the call cannot be recorded ({e})"
    return None


def check_not_yet(
    _root: Path, _payload: Mapping[str, Any], _config: Config
) -> str | None:
    """Wired, but its evidence does not exist yet: a subagent start is only
    recorded once the worker store journal exists (M3)."""
    return None


CHECKS: Mapping[str, Check] = {
    "generated-paths": check_generated_paths,
    "secret-read": check_secret_read,
    "owned-paths": check_owned_paths,
    "order-check": check_order,
    "handoff-guard": check_handoff_guard,
    "spawn-record": check_not_yet,
}


# ---------------------------------------------------------------- evaluation


def wired(matcher: str, tool: str) -> bool:
    """Whether a client matcher covers this tool: empty is not wired, `*` is
    every tool, anything else is a regular expression over the whole name, as
    Claude and Codex read their own matchers."""
    if not matcher:
        return False
    if matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def evaluate(
    root: Path,
    client: str,
    event: str,
    payload: Mapping[str, Any],
    config: Config | None = None,
) -> Verdict:
    """Run every check wired for this client and event. The first refusal
    decides; reminders are joined; a record never changes the answer."""
    if event not in EVENTS.get(client, ()):
        raise work.Bad(f"{client} fires no {event} the guard answers")
    config = config or load_config(root)
    as_configured = CONFIGURED_AS.get(event, event)
    tool = str(payload.get("tool_name") or "")
    reminders: list[tuple[str, str]] = []
    for name, spec in config.hooks.checks.items():
        if spec.event != as_configured or spec.kind == "record":
            continue
        matcher = spec.claude if client == "claude" else spec.codex
        if not wired(matcher, tool if event in TOOL_EVENTS else "*"):
            continue
        run = CHECKS.get(name)
        why = (
            run(root, payload, config)
            if run
            else f"no check named {name} in this tac; hooks.toml and tac disagree"
        )
        if why is None:
            continue
        if spec.kind == "deny":
            return Verdict(
                verdict="deny", check=name, reason=why, module=module_origin()
            )
        reminders.append((name, why))
    if reminders:
        return Verdict(
            verdict="remind",
            check=", ".join(n for n, _ in reminders),
            reason="\n".join(w for _, w in reminders),
            module=module_origin(),
        )
    return Verdict(verdict="allow", check="", reason="", module=module_origin())
