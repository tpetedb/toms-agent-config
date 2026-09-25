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

import contextlib
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
from tac.runner import (
    GUARD_TIMEOUT_S,
    ID_PATTERN,
    SESSION_PATTERN,
    TOKEN_PATTERN,
    RunnerError,
    controller_store,
    request,
    socket_path,
)
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
# Where each allowed Workflow call is recorded, inside the worker store; the
# runner signs what these journals hold into the dispatch receipt.
WORKFLOW_JOURNAL = "journal/workflows.jsonl"
WORKFLOW_RESULTS = "journal/workflow-results.jsonl"
SPAWN_JOURNAL = "journal/spawns.jsonl"
# The launcher's record of a session it started: the worker store holds the
# record with the token (agent-writable, so read as data only), the controller
# store a marker the runner writes, which no agent can write or remove.
DISPATCH_DIR = "dispatch"
DISPATCHED_DIR = "dispatched"
DISPATCH_KEYS = frozenset({"run_id", "stage", "role", "token", "kind", "session_id"})


class TokenRefused(Exception):
    """No runner token vouches for this spawn: the message says why."""


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
    single-use token for it, and only once the call is recorded. Agent and Task
    in a session the runner dispatched need the token bound to the sha256 of the
    spawn's prompt (C1); a session nobody dispatched, the owner's own chat with
    the chief, keeps them as the one recorded exemption."""
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
        return _check_spawn(root, payload, config, tool)
    return _check_workflow(root, payload, config, role)


def _session(payload: Mapping[str, Any]) -> str:
    given = payload.get("session_id")
    if not isinstance(given, str) or not re.match(SESSION_PATTERN, given):
        raise TokenRefused("the event names no usable session id")
    return given


def _dispatched(root: Path, payload: Mapping[str, Any], config: Config) -> bool:
    """Whether the runner dispatched this session: its marker in the controller
    store, or the launcher's record in the worker store. Either one makes the
    session answer for its spawns with a token."""
    session = _session(payload)
    try:
        store = controller_store(root, os.environ)
        if (store / DISPATCHED_DIR / session).is_file():
            return True
    except RunnerError:
        pass
    try:
        return (worker_store(root, config) / DISPATCH_DIR / f"{session}.json").exists()
    except work.Bad:
        return False


def _dispatch_record(
    root: Path, payload: Mapping[str, Any], config: Config
) -> dict[str, str]:
    """The launcher's record for this session, read as data: any shape but the
    one the launcher writes is a refusal."""
    session = _session(payload)
    try:
        path = worker_store(root, config) / DISPATCH_DIR / f"{session}.json"
    except work.Bad as e:
        raise TokenRefused(
            f"no worker store to read a dispatch record from ({e})"
        ) from None
    if not path.is_file() or path.is_symlink():
        raise TokenRefused(
            "this session has no dispatch record, so it carries no runner token"
        )
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError) as e:
        raise TokenRefused(f"the dispatch record cannot be read ({e})") from None
    if (
        not isinstance(data, dict)
        or set(data) != DISPATCH_KEYS
        or not all(isinstance(data[k], str) for k in DISPATCH_KEYS)
        or not re.match(TOKEN_PATTERN, data["token"])
        or not re.match(ID_PATTERN, data["run_id"])
        or not re.match(ID_PATTERN, data["stage"])
        or data["session_id"] != session
    ):
        raise TokenRefused(
            "the dispatch record is not in the shape the launcher writes"
        )
    marked = _marker(root, session)
    if marked is not None and marked != (data["run_id"], data["stage"]):
        # The worker store is agent-writable; the marker is not, so the record
        # names the run and stage the runner started this session for or none.
        raise TokenRefused(
            "the dispatch record names another run or stage than the runner "
            "dispatched this session for"
        )
    return data


def _marker(root: Path, session: str) -> tuple[str, str] | None:
    """The run and stage the runner wrote into the controller store when it
    dispatched this session, or None when there is no marker to read."""
    try:
        path = controller_store(root, os.environ) / DISPATCHED_DIR / session
    except RunnerError:
        return None
    if not path.is_file():
        return None
    try:
        words = path.read_text(encoding="utf-8").split()
    except OSError as e:
        raise TokenRefused(
            f"the runner's dispatch marker cannot be read ({e})"
        ) from None
    if len(words) != 2:
        raise TokenRefused("the runner's dispatch marker is not a run and a stage")
    return words[0], words[1]


def _runner_token(
    root: Path,
    payload: Mapping[str, Any],
    config: Config,
    kind: Literal["workflow", "agent"],
    digest: str,
) -> str:
    """The runner's single-use token for this spawn, bound to the run, the stage
    and the sha256 of what it runs, once the runner has consumed it; a missing
    record, a missing token, a runner that does not answer or closes the
    connection, and any refusal raise TokenRefused."""
    record = _dispatch_record(root, payload, config)
    try:
        sock = socket_path(controller_store(root, os.environ))
        request(
            sock,
            {
                "op": "token.consume",
                "token": record["token"],
                "run_id": record["run_id"],
                "stage": record["stage"],
                "sha256": digest,
                "kind": kind,
                "session_id": record["session_id"],
            },
            timeout=GUARD_TIMEOUT_S,
        )
    except RunnerError as e:
        raise TokenRefused(f"the runner did not grant the token: {e}") from None
    return record["token"]


def _check_spawn(
    root: Path, payload: Mapping[str, Any], config: Config, tool: str
) -> str | None:
    try:
        if not _dispatched(root, payload, config):
            # The owner's own chat names no dispatch: the recorded exemption.
            return None
        given = payload.get("tool_input")
        prompt = given.get("prompt") if isinstance(given, Mapping) else None
        if not isinstance(prompt, str):
            raise TokenRefused("the call carries no prompt to bind a token to")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        _runner_token(root, payload, config, "agent", digest)
    except TokenRefused as e:
        return (
            f"{tool} is refused in a session the runner dispatched: {e}; a spawn "
            "there needs the single-use token bound to its prompt's sha256"
        )
    return None


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
    try:
        token = _runner_token(root, payload, config, "workflow", digest)
    except TokenRefused as e:
        # A registered script alone does not run: the runner's token must too.
        return (
            f"Workflow is refused: {script} is registered for {role}, but the call "
            "carries no single-use runner token bound to its run, stage and "
            f"script hash ({e})"
        )
    # The token was bound to the stage the runner dispatched; the record names it.
    try:
        dispatched = _dispatch_record(root, payload, config)["stage"]
    except TokenRefused:
        dispatched = stage
    chosen = [m for m in matches if m[2] == dispatched]
    if not chosen:
        return (
            f"Workflow is refused: the runner dispatched stage {dispatched}, "
            f"which does not register {script}"
        )
    script, pipeline, stage = chosen[0]
    entry = {
        "at": _now(),
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
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except (OSError, work.Bad) as e:
        return f"Workflow is refused: the call cannot be recorded ({e})"
    return None


def _append(root: Path, config: Config, rel: str, record: Mapping[str, Any]) -> None:
    """One JSON line in a worker store journal; a record never changes an answer,
    so a failure to write is dropped here and missing evidence shows downstream."""
    try:
        journal = worker_store(root, config) / rel
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, work.Bad):
        pass


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def check_spawn_record(
    root: Path, payload: Mapping[str, Any], config: Config
) -> str | None:
    """A subagent start, recorded: SubagentStart cannot block in either client,
    so it only leaves evidence in journal/spawns.jsonl in the worker store."""
    _append(
        root,
        config,
        SPAWN_JOURNAL,
        {
            "at": _now(),
            "session_id": payload.get("session_id"),
            "agent_type": payload.get("agent_type"),
            "agent_id": payload.get("agent_id"),
        },
    )
    return None


def check_workflow_record(
    root: Path, payload: Mapping[str, Any], config: Config
) -> str | None:
    """After a Workflow call: the script hash, the hash of what it returned and
    the token the guard consumed for it, one line the runner signs into the
    dispatch receipt of the session's stage."""
    if payload.get("tool_name") != WORKFLOW_TOOL:
        return None
    found = _script_bytes(root, payload)
    script = hashlib.sha256(found[0]).hexdigest() if isinstance(found, tuple) else None
    result = json.dumps(payload.get("tool_response"), sort_keys=True, default=str)
    token = None
    with contextlib.suppress(TokenRefused):
        token = _dispatch_record(root, payload, config)["token"]
    _append(
        root,
        config,
        WORKFLOW_RESULTS,
        {
            "at": _now(),
            "session_id": payload.get("session_id"),
            "script_sha256": script,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()
            if token
            else None,
            "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        },
    )
    return None


CHECKS: Mapping[str, Check] = {
    "generated-paths": check_generated_paths,
    "secret-read": check_secret_read,
    "owned-paths": check_owned_paths,
    "order-check": check_order,
    "handoff-guard": check_handoff_guard,
    "spawn-record": check_spawn_record,
    # Not wired in hooks.toml yet: a PostToolUse record renders once the
    # configuration names it (a follow-up of M3).
    "workflow-record": check_workflow_record,
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
        if spec.event != as_configured:
            continue
        matcher = spec.claude if client == "claude" else spec.codex
        if not wired(matcher, tool if event in TOOL_EVENTS else "*"):
            continue
        run = CHECKS.get(name)
        if spec.kind == "record":
            # Evidence only: a record never changes the answer.
            if run is not None:
                run(root, payload, config)
            continue
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
