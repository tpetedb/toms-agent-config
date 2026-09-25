"""`tac run`: a pipeline, stage by stage, as the trusted runner (design section 5).

A run is a persistent state machine at `<controller store>/runs/<run>/state.json`,
written whole through a temporary file and a rename after every transition,
which is its checkpoint: `--resume` reloads it, reconciles the effect journal
first and carries on from the first stage that is not done. The states are the
ones section 5 names: ready, running, blocked, waiting-human, failed, skipped,
cancelled, verified (a gate passed), reviewed (a review passed), complete.

`tac run` is the runner itself, in the owner's terminal: it holds the signing
key, issues the dispatch tokens in process, runs gates through the runner's own
gate, performs effects through the journal and signs every receipt. It refuses
a pipeline that is not enabled or whose gate still waits on a later milestone.

A stage by kind:

- agent: the prompt is rendered from the upstream envelopes, a token is bound
  to the rendered bytes (or to the registered Workflow script's hash), the
  client starts headless in a session id the runner chose, with the dispatch
  record beside it, and the result is judged against the contract. One repair
  pass; a failure after it becomes a human item and the run waits. A stage's
  gates run after it, rerun at most `retry.max` times after a repair, and two
  rounds without fewer failures (the profile's `loop.blocks_no_progress`) stop
  it first. Usage and the host's agent budget are checked before every dispatch.
- gate: the runner's gate op, under the Seatbelt sandbox.
- effect: through the effect journal; what leaves the machine waits for the
  owner's signed approval where the profile asks for one.
- human: an item in the owner's queue; `land` carries the one merge command,
  squash, pinned to the head commit (C6), and `tac run` prints the same line.

A `when = "cross_team"` stage is skipped when the order names no cross team, and
a join counts skipped as done.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from tac import handoff, human, launch, leases, pipelines, usage
from tac.config import Config
from tac.config_schema import Gate as GateSpec
from tac.config_schema import PipelineFile, PipelineStage
from tac.effects import EffectError, EffectRequest, Effects, action
from tac.receipts import ID_PATTERN, Binding, policy_hash, resolve
from tac.runner import (
    DEFAULT_GATE_RECIPES,
    UNIX_PERMS_STORE,
    Gate,
    Runner,
    RunnerError,
)
from tac.sync import read_lock
from tac.work import Bad, Order

RUNS = "runs"
STATE_FILE = "state.json"
DISPATCHED_DIR = "dispatched"
# Recipes the run loop runs besides the gates a pipeline names: the repair
# between gate reruns and the release notes draft.
EXTRA_RECIPES = ("work-repair", "changelog")
# A headless stage's spend cap until the configuration carries one.
DEFAULT_MAX_BUDGET_USD = 5.0
MERGE_COMMAND = (
    "gh pr review {number} --approve && "
    "gh pr merge {number} --squash --match-head-commit {sha}"
)

StageStateName = Literal[
    "ready",
    "running",
    "blocked",
    "waiting-human",
    "failed",
    "skipped",
    "cancelled",
    "verified",
    "reviewed",
    "complete",
]
SATISFIED: frozenset[str] = frozenset({"skipped", "verified", "reviewed", "complete"})
RunStatus = Literal[
    "running", "blocked", "waiting-human", "failed", "complete", "cancelled"
]


class RunError(Exception):
    """`tac run` refuses: the message says why."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StageState(_Model):
    state: StageStateName = "ready"
    reason: str = ""
    envelope: str | None = None
    sessions: tuple[str, ...] = ()
    receipts: tuple[str, ...] = ()
    gate_reruns: int = 0
    no_progress: int = 0
    human_item: str | None = None
    approval_item: str | None = None
    observed: dict[str, JsonValue] = Field(default_factory=dict)


class RunState(_Model):
    run_version: Literal[1] = 1
    run_id: str = Field(pattern=ID_PATTERN)
    pipeline: str
    order_id: str | None
    harness: launch.Harness
    status: RunStatus
    created: str
    modified: str
    # The entry payloads by contract, as files the owner named.
    entries: dict[str, str] = Field(default_factory=dict)
    stages: dict[str, StageState]


def utc_text() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(pipeline: str, order_id: str | None) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    return f"{pipeline}-{order_id or 'run'}-{stamp}-{uuid.uuid4().hex[:6]}"[:64]


# ---------------------------------------------------------------- checkpoints


def run_dir(store: Path, run_id: str) -> Path:
    if not re.match(ID_PATTERN, run_id):
        raise RunError(f"not a run id: {run_id!r}")
    folder = store / RUNS / run_id
    folder.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
    return folder


def save(store: Path, state: RunState) -> RunState:
    """The checkpoint: the whole state, written aside and renamed into place."""
    state = state.model_copy(update={"modified": utc_text()})
    path = run_dir(store, state.run_id) / STATE_FILE
    temp = path.with_suffix(".json.tmp")
    temp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return state


def load(store: Path, run_id: str) -> RunState:
    path = run_dir(store, run_id) / STATE_FILE
    if not path.is_file():
        raise RunError(f"no run {run_id} in the controller store")
    try:
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise RunError(f"the checkpoint of {run_id} is unreadable: {exc}") from exc


def set_stage(run: RunState, stage_id: str, /, **changes: object) -> RunState:
    stages = dict(run.stages)
    stages[stage_id] = stages[stage_id].model_copy(update=changes)
    return run.model_copy(update={"stages": stages})


# ---------------------------------------------------------------- what may run


def gate_recipes(root: Path) -> dict[str, int]:
    """Every live gate an enabled pipeline names, with the number of values it
    passes, plus the repair and changelog recipes and the skeleton's own; the
    runner's allowlist."""
    table = dict(DEFAULT_GATE_RECIPES)
    problems: list[str] = []
    known = pipelines.known(root, problems)
    for name in pipelines.enabled(root, problems):
        try:
            pipeline = pipelines.read_pipeline(root, name)
        except Bad:
            continue
        for stage in pipeline.stages:
            for gate in _gates(stage):
                if gate.argv[0] == "just" and not gate.waits_on:
                    table[gate.argv[1]] = len(gate.argv) - 2 + len(gate.args_from)
    for name in EXTRA_RECIPES:
        recipe = known.recipes.get(name)
        if recipe is not None:
            table[name] = recipe.required
    return table


def _gates(stage: PipelineStage) -> list[GateSpec]:
    found = list(stage.gates)
    if stage.retry is not None and stage.retry.on_failure is not None:
        found.append(stage.retry.on_failure)
    return found


def runnable(root: Path, config: Config, name: str) -> PipelineFile:
    """The pipeline, or the reason it may not run now."""
    if name not in config.knobs.pipelines.enabled:
        raise RunError(f"pipeline {name} is not in [pipelines] enabled")
    problems = pipelines.check_pipelines(root, [name])
    if problems:
        raise RunError(f"pipeline {name} does not hold: " + "; ".join(problems))
    pipeline = pipelines.read_pipeline(root, name)
    waiting = [
        f"stage {s.id}: `{' '.join(g.argv)}` waits on {g.waits_on}"
        for s in pipeline.stages
        for g in _gates(s)
        if g.waits_on
    ]
    if waiting:
        raise RunError(f"pipeline {name} is not live yet: " + "; ".join(waiting))
    return pipeline


def merge_command(number: int, sha: str) -> str:
    """The owner's one merge command: approve, then squash pinned to the head."""
    if not re.match(r"^[0-9a-f]{40}$", sha):
        raise RunError(f"not a head commit: {sha!r}")
    return MERGE_COMMAND.format(number=int(number), sha=sha)


# ---------------------------------------------------------------- the loop


@dataclass
class Context:
    root: Path
    config: Config
    runner: Runner
    effects: Effects
    pipeline: PipelineFile
    order: Order | None
    environ: Mapping[str, str]
    search_path: str
    out: Callable[[str], object] = print
    # How a planned client starts; tests put a fake client on search_path.
    start: Callable[[launch.Plan, float | None], object] = field(
        default=lambda plan, timeout: launch.run(plan, timeout)
    )
    now: Callable[[], float] | None = None


def _worker_runs(ctx: Context) -> Path:
    return handoff.worker_store(ctx.root, ctx.config) / RUNS


def _stage_harness(
    ctx: Context, state: RunState, stage: PipelineStage
) -> launch.Harness:
    team = state.harness
    other: launch.Harness = "codex" if team == "claude" else "claude"
    spec = ctx.config.models.roles.get(stage.role or "")
    provider = spec.provider if spec is not None else "any"
    if stage.provider == "other" or provider == "other":
        # The profile decides whether a review comes from the other provider.
        return other if ctx.config.profile.review.cross_provider else team
    found = ctx.config.models.providers.get(provider)
    if found is not None:
        return found.harness
    return team


def _inputs(ctx: Context, state: RunState, stage: PipelineStage) -> list[handoff.Given]:
    ups = pipelines.ancestors(ctx.pipeline.stages)[stage.id]
    by_id = {s.id: s for s in ctx.pipeline.stages}
    given: list[handoff.Given] = []
    for contract in stage.reads:
        source = stage.bind.get(contract)
        if source is None:
            candidates = [
                s.id
                for s in pipelines.order(ctx.pipeline.stages)
                if s.id in ups and by_id[s.id].writes == contract
            ]
            source = candidates[-1] if candidates else None
        if source is not None:
            done = state.stages[source]
            if done.state == "skipped":
                continue
            if done.envelope is None:
                raise RunError(f"stage {source} left no envelope for {stage.id}")
            given.append(handoff.upstream(ctx.root, Path(done.envelope)))
            continue
        entry = state.entries.get(contract)
        if entry is None:
            raise RunError(
                f"stage {stage.id} reads {contract}, which no stage upstream writes "
                f"and no --entry {contract}=<file> gave"
            )
        given.append(handoff.entry(ctx.root, Path(entry), contract))
    return given


def _receipt_id(signed: Mapping[str, JsonValue]) -> str:
    receipt = signed["receipt"]
    assert isinstance(receipt, dict)
    inner = receipt["receipt"]
    assert isinstance(inner, dict)
    return str(inner["receipt_id"])


def _binding(ctx: Context, state: RunState, stage: str) -> Binding:
    revision = resolve(ctx.root, "HEAD")
    return Binding(
        repository=ctx.runner.repository,
        revision=revision,
        run_id=state.run_id,
        stage=stage,
        policy_hash=policy_hash(ctx.root, revision),
        order_id=state.order_id,
    )


def _workflow_results(ctx: Context, session: str) -> list[JsonValue]:
    from tac.hook import WORKFLOW_RESULTS

    path = handoff.worker_store(ctx.root, ctx.config) / WORKFLOW_RESULTS
    if not path.is_file():
        return []
    lines: list[JsonValue] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("session_id") == session:
            lines.append(record)
    return lines


def dispatch(
    ctx: Context,
    state: RunState,
    stage: PipelineStage,
    harness: launch.Harness,
    prompt: str,
    prompt_file: Path,
) -> tuple[str, str, str]:
    """Start one headless session for the stage; (result text, session id,
    dispatch receipt id)."""
    assert stage.writes is not None
    prompt_file.write_text(prompt, encoding="utf-8")
    if stage.workflows:
        lock = read_lock(ctx.root) or {}
        digest = lock.get("workflows", {}).get(stage.workflows[0])
        if digest is None:
            raise RunError(
                f"{stage.workflows[0]} has no hash in the lock; run tac sync"
            )
        kind: Literal["workflow", "agent"] = "workflow"
    else:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        kind = "agent"
    session = str(uuid.uuid4())
    token = ctx.runner.issue_token(state.run_id, stage.id, digest, kind, session)
    headless = launch.Headless(
        contract=stage.writes,
        prompt_file=prompt_file,
        session_id=session,
        max_turns=stage.max_turns or ctx.config.profile.loop.iterations,
        max_budget_usd=DEFAULT_MAX_BUDGET_USD,
    )
    planned = launch.plan(
        ctx.root,
        ctx.config,
        harness,
        role=stage.role,
        headless=headless,
        environ=ctx.environ,
        search_path=ctx.search_path,
        # A builder works in its order's worktree; the others in the checkout.
        cwd=ctx.effects.worktree(ctx.order.branch)
        if stage.role == "builder" and ctx.order is not None
        else None,
    )
    launch.refuse_drift(planned, False)
    launch.write_records(
        ctx.root,
        ctx.config,
        planned,
        dispatch={
            "run_id": state.run_id,
            "stage": stage.id,
            "role": stage.role or "",
            "token": token,
            "kind": kind,
            "session_id": session,
        },
    )
    # The marker no agent can remove: this session answers for its spawns.
    marker = ctx.runner.store / DISPATCHED_DIR
    marker.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
    (marker / session).write_text(f"{state.run_id} {stage.id}\n", encoding="utf-8")
    done = ctx.start(planned, ctx.config.profile.loop.wall_minutes * 60.0)
    exit_code = getattr(done, "returncode", -1)
    stdout = getattr(done, "stdout", b"") or b""
    try:
        text = launch.result_text(planned, done)  # pyright: ignore[reportArgumentType]
    except launch.LaunchError:
        text = stdout.decode("utf-8", errors="replace")
    signed = ctx.runner.issue(
        "dispatch",
        _binding(ctx, state, stage.id),
        {
            "harness": harness,
            "role": stage.role,
            "session_id": session,
            "argv": launch.shown(planned.argv),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "token_kind": kind,
            "billing_route": planned.billing_route,
            "launch": dict(planned.launch_record),  # pyright: ignore[reportArgumentType]
            "exit": exit_code,
            "result_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "workflow_results": _workflow_results(ctx, session),
        },
    )
    return text, session, _receipt_id(signed)


def _ask(ctx: Context, item: human.Item) -> None:
    paths = human.human_paths(ctx.root)
    if not paths.item_file(item.id).exists():
        human.ask(paths, item)
    human.write_page(ctx.root)


def _escalate(ctx: Context, state: RunState, stage: PipelineStage, raw: str) -> str:
    paths = human.human_paths(ctx.root)
    items = human.load_items(paths)
    for found in items:
        if found.arguments.get("run_id") == state.run_id and found.stage == stage.id:
            return found.id
    item = human.item_from_handoff(raw, "the handoff escalation", items)
    _ask(ctx, item)
    return item.id


def _question(
    ctx: Context, state: RunState, stage: PipelineStage, question: str
) -> str:
    paths = human.human_paths(ctx.root)
    items = human.load_items(paths)
    item = human.Item(
        id=f"run-{hashlib.sha256(f'{state.run_id}/{stage.id}'.encode()).hexdigest()[:12]}",
        kind="question",
        topic=human.ESCALATION_TOPIC,
        rank=human.next_rank(items, human.ESCALATION_TOPIC),
        title=f"run {stage.id}",
        question=question,
        options=("rerun", "cancel"),
        recommendation="rerun",
        recommendation_source="the run loop's default, not the board's",
        blocks=(f"stage {stage.id} of run {state.run_id}",),
        pipeline=ctx.pipeline.name,
        stage=stage.id,
        arguments={"run_id": state.run_id},
        asked_at=human.utc_text(human.utc_now()),
    )
    _ask(ctx, item)
    return item.id


def _gate_argv(ctx: Context, state: RunState, gate: GateSpec) -> list[str]:
    values = {
        "order_id": state.order_id or "",
        "run_id": state.run_id,
        "local_checks": ctx.config.profile.local_checks,
    }
    return [*gate.argv, *(values[name] for name in gate.args_from)]


def _run_gates(
    ctx: Context, state: RunState, stage: PipelineStage, gates: list[GateSpec]
) -> tuple[list[str], list[str]]:
    """(the gates that failed, the receipts) for one round."""
    failed: list[str] = []
    receipts: list[str] = []
    # A gate judges the order's branch, so it runs in the order's worktree.
    gater = (
        dataclasses.replace(ctx.runner, root=ctx.effects.worktree(ctx.order.branch))
        if ctx.order is not None
        else ctx.runner
    )
    for gate in gates:
        argv = _gate_argv(ctx, state, gate)
        signed = gater.gate(
            Gate(
                op="gate",
                run_id=state.run_id,
                stage=stage.id,
                argv=argv,
                order_id=state.order_id,
            )
        )
        receipts.append(_receipt_id(signed))
        receipt = signed["receipt"]
        assert isinstance(receipt, dict)
        inner = receipt["receipt"]
        assert isinstance(inner, dict)
        observed = inner["observed"]
        assert isinstance(observed, dict)
        if observed.get("exit") != 0:
            failed.append(" ".join(argv))
    return failed, receipts


def _gated(
    ctx: Context, state: RunState, stage: PipelineStage
) -> tuple[RunState, bool]:
    """The stage's gates, rerun after a repair while they improve; whether they hold."""
    failed, receipts = _run_gates(ctx, state, stage, list(stage.gates))
    reruns = no_progress = 0
    limit = stage.retry.max if stage.retry else 0
    stall = ctx.config.profile.loop.blocks_no_progress
    while failed and reruns < limit and no_progress < stall:
        assert stage.retry is not None
        if stage.retry.on_failure is not None:
            _, more = _run_gates(ctx, state, stage, [stage.retry.on_failure])
            receipts += more
        reruns += 1
        again, more = _run_gates(ctx, state, stage, list(stage.gates))
        receipts += more
        no_progress = 0 if len(again) < len(failed) else no_progress + 1
        failed = again
        state = save(
            ctx.runner.store,
            set_stage(state, stage.id, gate_reruns=reruns, no_progress=no_progress),
        )
    held = state.stages[stage.id]
    state = set_stage(
        state,
        stage.id,
        receipts=(*held.receipts, *receipts),
        reason="; ".join(f"`{f}` failed" for f in failed),
    )
    return state, not failed


def _on_fail(ctx: Context, state: RunState, stage: PipelineStage, why: str) -> RunState:
    if stage.on_fail == "stop":
        return set_stage(state, stage.id, state="failed", reason=why)
    item = _question(
        ctx,
        state,
        stage,
        f"Stage {stage.id} of run {state.run_id} did not pass: {why[:300]}. "
        "Rerun it with tac run --resume, or cancel the run.",
    )
    return set_stage(
        state, stage.id, state="waiting-human", reason=why, human_item=item
    )


def agent_stage(ctx: Context, state: RunState, stage: PipelineStage) -> RunState:
    assert stage.template is not None and stage.writes is not None
    harness = _stage_harness(ctx, state, stage)
    reading = usage.spawn_state(
        ctx.root, ctx.config, harness, ctx.now() if ctx.now else None
    )
    if reading.paused:
        return set_stage(
            state,
            stage.id,
            state="blocked",
            reason=f"usage {reading.provider} is {reading.state}: {reading.reason}",
        )
    try:
        lease = leases.acquire(
            ctx.environ,
            ctx.config.knobs.teams.max_local_agents,
            repository=ctx.runner.repository,
            run_id=state.run_id,
            stage=stage.id,
            role=stage.role or "",
        )
    except RunnerError as exc:
        return set_stage(state, stage.id, state="blocked", reason=str(exc))
    try:
        seat = (
            launch.seat(ctx.config, stage.role or "", harness) if stage.role else None
        )
        rendered = handoff.render(
            ctx.root,
            stage.template,
            handoff.Stage(
                id=stage.id,
                run_id=state.run_id,
                order_id=state.order_id,
                role=stage.role,
                provider=launch.PROVIDERS[harness],
                harness=harness,
                model=seat.model if seat else None,
                effort=seat.effort if seat else None,
            ),
            _inputs(ctx, state, stage),
            stage.skills,
        )
        path = handoff.next_envelope_path(_worker_runs(ctx), state.run_id, stage.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered.envelope.to_json(), encoding="utf-8")
        state = save(
            ctx.runner.store,
            set_stage(state, stage.id, state="running", envelope=str(path)),
        )
        text, session, receipt = dispatch(
            ctx,
            state,
            stage,
            harness,
            rendered.prompt,
            handoff.sibling(path, ".prompt.md"),
        )
        judged = handoff.validate(
            ctx.root, rendered.envelope, text, envelope_path=path.name
        )
        path.write_text(judged.envelope.to_json(), encoding="utf-8")
        sessions, receipts = [session], [receipt]
        if judged.outcome == "repair":
            assert judged.repair_prompt is not None
            text, session, receipt = dispatch(
                ctx,
                state,
                stage,
                harness,
                judged.repair_prompt,
                handoff.sibling(path, ".repair.md"),
            )
            sessions.append(session)
            receipts.append(receipt)
            judged = handoff.validate(
                ctx.root, judged.envelope, text, envelope_path=path.name
            )
            path.write_text(judged.envelope.to_json(), encoding="utf-8")
    finally:
        leases.release(lease)
    held = state.stages[stage.id]
    state = set_stage(
        state,
        stage.id,
        sessions=(*held.sessions, *sessions),
        receipts=(*held.receipts, *receipts),
    )
    if judged.outcome == "escalated":
        assert judged.human is not None
        sibling = handoff.sibling(path, ".human.json")
        sibling.write_text(judged.human.to_json(), encoding="utf-8")
        item = _escalate(ctx, state, stage, judged.human.to_json())
        return set_stage(
            state,
            stage.id,
            state="waiting-human",
            human_item=item,
            reason=f"the result failed contract {stage.writes} after the repair pass",
        )
    if stage.gates:
        state = save(ctx.runner.store, state)
        state, held_ = _gated(ctx, state, stage)
        if not held_:
            return _on_fail(ctx, state, stage, state.stages[stage.id].reason)
    done: StageStateName = "reviewed" if stage.writes == "review" else "complete"
    return set_stage(state, stage.id, state=done, reason="")


def gate_stage(ctx: Context, state: RunState, stage: PipelineStage) -> RunState:
    state, held = _gated(ctx, state, stage)
    if not held:
        return _on_fail(ctx, state, stage, state.stages[stage.id].reason)
    return set_stage(state, stage.id, state="verified", reason="")


def _order_branch(ctx: Context, stage: PipelineStage) -> tuple[Order, str]:
    if ctx.order is None:
        raise RunError(f"effect stage {stage.id} needs an order with a branch")
    return ctx.order, ctx.order.branch


def effect_stage(ctx: Context, state: RunState, stage: PipelineStage) -> RunState:
    order, branch = _order_branch(ctx, stage)
    held = state.stages[stage.id]
    observed = dict(held.observed)
    try:
        if "commit" in stage.effects and "commit" not in observed:
            request = EffectRequest(
                effect="commit",
                arguments={
                    "branch": branch,
                    "message": f"{order.id}: {order.title}"[:72],
                },
                run_id=state.run_id,
                stage=stage.id,
                order_id=state.order_id,
            )
            ctx.effects.authorize([request], None, None)
            observed["commit"] = ctx.effects.perform(request)
            state = save(
                ctx.runner.store, set_stage(state, stage.id, observed=observed)
            )
        commit = observed.get("commit")
        sha = (
            str(commit["sha"])
            if isinstance(commit, dict) and isinstance(commit.get("sha"), str)
            else resolve(ctx.root, f"refs/heads/{branch}")
        )
        rest = [e for e in stage.effects if e != "commit"]
        if all(e in observed for e in rest):
            return set_stage(
                state, stage.id, state="complete", observed=observed, reason=""
            )
        # Every effect of the stage each time, the done ones too: the approval
        # binds them all, and the journal answers a done one without acting.
        requests = [
            EffectRequest(
                effect=effect,
                arguments=_effect_arguments(ctx, order, branch, sha, effect),
                run_id=state.run_id,
                stage=stage.id,
                order_id=state.order_id,
            )
            for effect in rest
        ]
        approval = held.approval_item
        if ctx.effects.needs_approval(requests):
            if approval is None:
                approval = _ask_approval(ctx, state, stage, requests, sha)
                return set_stage(
                    state,
                    stage.id,
                    state="waiting-human",
                    approval_item=approval,
                    observed=observed,
                    reason=f"waiting for the owner's `tac approve {approval}`",
                )
            item = human.load_item(human.human_paths(ctx.root), approval)
            if item.open:
                return set_stage(
                    state,
                    stage.id,
                    state="waiting-human",
                    reason=f"waiting for the owner's `tac approve {approval}`",
                )
        ctx.effects.authorize(requests, approval, sha)
        for request in requests:
            observed[request.effect] = ctx.effects.perform(request)
            state = save(
                ctx.runner.store, set_stage(state, stage.id, observed=observed)
            )
    except (EffectError, RunnerError, human.HumanError) as exc:
        return set_stage(
            state, stage.id, state="failed", reason=str(exc), observed=observed
        )
    return set_stage(state, stage.id, state="complete", observed=observed, reason="")


def _effect_arguments(
    ctx: Context, order: Order, branch: str, sha: str, effect: str
) -> dict[str, JsonValue]:
    if effect == "push":
        return {"branch": branch, "sha": sha}
    if effect == "open-pr":
        return {
            "branch": branch,
            "base": ctx.config.github.repository.default_branch,
            "title": f"{order.id}: {order.title}"[:72],
            "body": f"Order {order.id}, built by the {ctx.pipeline.name} pipeline.",
            "sha": sha,
        }
    return {"branch": branch, "sha": sha}


def _ask_approval(
    ctx: Context,
    state: RunState,
    stage: PipelineStage,
    requests: list[EffectRequest],
    sha: str,
) -> str:
    tool, arguments = action(requests)
    paths = human.human_paths(ctx.root)
    items = human.load_items(paths)
    item = human.Item(
        id=f"A-{hashlib.sha256(f'{state.run_id}/{stage.id}/{sha}'.encode()).hexdigest()[:12]}",
        kind="approval",
        topic="approvals",
        rank=human.next_rank(items, "approvals"),
        title=f"{tool} for order {state.order_id}",
        question=f"Approve {tool} of {sha[:12]} for run {state.run_id}?",
        options=("approve", "reject"),
        blocks=(f"stage {stage.id} of run {state.run_id}",),
        pipeline=ctx.pipeline.name,
        stage=stage.id,
        agent="runner",
        tool=tool,
        arguments=arguments,
        revision=sha,
        asked_at=human.utc_text(human.utc_now()),
    )
    _ask(ctx, item)
    return item.id


def _pull_request(state: RunState) -> tuple[int, str] | None:
    """The pull request an effect stage opened and the head it pushed."""
    for held in state.stages.values():
        pr = held.observed.get("open-pr")
        push = held.observed.get("push")
        if isinstance(pr, dict) and isinstance(pr.get("number"), int):
            sha = push.get("sha") if isinstance(push, dict) else None
            if isinstance(sha, str):
                number = pr["number"]
                assert isinstance(number, int)
                return number, sha
    return None


def human_stage(ctx: Context, state: RunState, stage: PipelineStage) -> RunState:
    held = state.stages[stage.id]
    paths = human.human_paths(ctx.root)
    if held.human_item is not None:
        item = human.load_item(paths, held.human_item)
        if item.open:
            return set_stage(state, stage.id, state="waiting-human")
        return set_stage(state, stage.id, state="complete", reason="")
    details: tuple[str, ...] = ()
    if "merge" in stage.owner_actions:
        found = _pull_request(state)
        if found is None:
            return set_stage(
                state,
                stage.id,
                state="failed",
                reason="no pull request and head commit upstream to merge",
            )
        command = merge_command(*found)
        ctx.out(command)
        details = (f"`{command}`",)
    items = human.load_items(paths)
    item = human.Item(
        id=f"L-{hashlib.sha256(f'{state.run_id}/{stage.id}'.encode()).hexdigest()[:12]}",
        kind="step",
        topic="steps",
        rank=human.next_rank(items, "steps"),
        title=f"{stage.id} for order {state.order_id}",
        question=stage.asks or f"Finish stage {stage.id} of run {state.run_id}.",
        details=details,
        blocks=(f"stage {stage.id} of run {state.run_id}",),
        pipeline=ctx.pipeline.name,
        stage=stage.id,
        arguments={"run_id": state.run_id},
        asked_at=human.utc_text(human.utc_now()),
    )
    _ask(ctx, item)
    return set_stage(
        state,
        stage.id,
        state="waiting-human",
        human_item=item.id,
        reason="the owner's step",
    )


def _applies(ctx: Context, stage: PipelineStage) -> bool:
    if stage.when == "cross_team":
        return ctx.order is not None and bool(ctx.order.cross)
    return True


def advance(ctx: Context, state: RunState) -> RunState:
    """Run every stage that can run, in order, checkpointing each transition;
    stop at the first stage that blocks, waits, or fails."""
    state = save(ctx.runner.store, state.model_copy(update={"status": "running"}))
    for stage in pipelines.order(ctx.pipeline.stages):
        held = state.stages[stage.id]
        if held.state in SATISFIED:
            continue
        if held.state == "cancelled":
            return save(
                ctx.runner.store, state.model_copy(update={"status": "cancelled"})
            )
        waiting = [
            d for d in stage.depends_on if state.stages[d].state not in SATISFIED
        ]
        if waiting:
            return save(
                ctx.runner.store,
                state.model_copy(update={"status": "blocked"}),
            )
        if not _applies(ctx, stage):
            state = save(
                ctx.runner.store,
                set_stage(state, stage.id, state="skipped", reason="no cross team"),
            )
            continue
        ctx.out(f"stage {stage.id} ({stage.kind})")
        try:
            if stage.kind == "agent":
                state = agent_stage(ctx, state, stage)
            elif stage.kind == "gate":
                state = gate_stage(ctx, state, stage)
            elif stage.kind == "effect":
                state = effect_stage(ctx, state, stage)
            else:
                state = human_stage(ctx, state, stage)
        except (
            Bad,
            RunError,
            RunnerError,
            EffectError,
            launch.LaunchError,
            human.HumanError,
        ) as exc:
            state = set_stage(state, stage.id, state="failed", reason=str(exc))
        state = save(ctx.runner.store, state)
        after = state.stages[stage.id]
        if after.state not in SATISFIED:
            status: RunStatus = (
                "waiting-human"
                if after.state == "waiting-human"
                else "blocked"
                if after.state == "blocked"
                else "failed"
            )
            ctx.out(f"stage {stage.id}: {after.state}: {after.reason}".rstrip(": "))
            return save(ctx.runner.store, state.model_copy(update={"status": status}))
    return save(ctx.runner.store, state.model_copy(update={"status": "complete"}))


def fresh(
    ctx: Context,
    harness: launch.Harness,
    entries: Mapping[str, str],
    order_id: str | None,
) -> RunState:
    now = utc_text()
    return RunState(
        run_id=new_run_id(ctx.pipeline.name, order_id),
        pipeline=ctx.pipeline.name,
        order_id=order_id,
        harness=harness,
        status="running",
        created=now,
        modified=now,
        entries=dict(entries),
        stages={s.id: StageState() for s in ctx.pipeline.stages},
    )


def resume(ctx: Context, run_id: str) -> RunState:
    """The checkpoint, with the effect journal reconciled first; a stage left
    running, blocked or failed is tried again, one waiting on the owner is
    looked at again."""
    state = load(ctx.runner.store, run_id)
    if state.pipeline != ctx.pipeline.name:
        raise RunError(
            f"run {run_id} is a {state.pipeline} run, not {ctx.pipeline.name}"
        )
    ctx.effects.reconcile()
    stages = {
        k: (
            v.model_copy(update={"state": "ready"})
            if v.state in {"running", "blocked", "failed"}
            else v
        )
        for k, v in state.stages.items()
    }
    return state.model_copy(update={"stages": stages})


def plan_lines(pipeline: PipelineFile) -> list[str]:
    return [
        f"{s.id} ({s.kind})"
        + (f" after {', '.join(s.depends_on)}" if s.depends_on else "")
        for s in pipelines.order(pipeline.stages)
    ]
