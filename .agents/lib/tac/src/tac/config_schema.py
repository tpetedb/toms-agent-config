"""The shape of every configuration file, one pydantic model per file.

Each model refuses an unknown key and a value of the wrong type ("8" is not 8),
and checks what one file can say about itself; what the files say about each
other is checked by `tac.config`. Literal types double as the allowed values a
key's comment has to name, which tests/test_toml_docs.py holds every shipped
file to.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from tac.work import Strs, Whole, as_tuple

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
Text = Annotated[str, Field(min_length=1)]
Share = Annotated[float, Field(ge=0, le=1)]
Kind = Literal["app", "tool", "library", "data-pipeline", "document", "mixed"]
PackKind = Literal["app", "tool", "library", "data-pipeline", "document"]
Harness = Literal["claude", "codex", "copilot", "opencode", "pi", "gemini"]
ProfileName = Literal["enterprise", "standard", "yolo"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _relative(value: str, where: str) -> str:
    if value.startswith(("/", "~")) or ".." in Path(value).parts:
        raise ValueError(f"{where}: a path inside the repository, got {value!r}")
    return value


# ---------------------------------------------------------------- the knob file


class Project(_Model):
    name: Text
    kind: Kind


class ProfileRef(_Model):
    active: ProfileName


BoardTrigger = Literal[
    "policy-loosening",
    "checker-change",
    "security",
    "cross-team-dispute",
    "unresolved-finding",
]
ChiefPower = Literal["open-issue", "comment", "push-branch", "open-pr"]


class Governance(_Model):
    chief: Name
    directors: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)]
    quorum: Whole
    tally_by: Name
    board_triggers: Annotated[tuple[BoardTrigger, ...], BeforeValidator(as_tuple)]
    # A GitHub login: letters, digits and single hyphens, at most 39 characters.
    agent_identity: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$")
    ]
    chief_may: Annotated[tuple[ChiefPower, ...], BeforeValidator(as_tuple)]

    @model_validator(mode="after")
    def _board(self) -> Self:
        if len(set(self.directors)) != len(self.directors):
            raise ValueError("directors: a seat is named twice")
        if self.tally_by not in self.directors:
            raise ValueError(f"tally_by: {self.tally_by!r} is not one of directors")
        if self.quorum > len(self.directors):
            raise ValueError(
                f"quorum: {self.quorum} is more than the "
                f"{len(self.directors)} directors"
            )
        return self


class ModelsRef(_Model):
    policy: Name


class TeamsRef(_Model):
    table: Name
    max_local_agents: Whole


class Threshold(_Model):
    slow_at: Share
    stop_at: Share

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.slow_at >= self.stop_at:
            raise ValueError("slow_at must be below stop_at")
        return self


class Usage(_Model):
    claude: Threshold
    openai: Threshold
    unavailable: Literal["pause"]
    max_age_s: Whole


class StandardsRef(_Model):
    registry: Name


PipelineName = Literal["order", "board", "review", "release", "retro"]


class Pipelines(_Model):
    enabled: Annotated[tuple[PipelineName, ...], BeforeValidator(as_tuple)]


class Memory(_Model):
    runtime_store: Text
    select_cap_chars: Whole
    retention_days: Whole

    @model_validator(mode="after")
    def _store(self) -> Self:
        if (
            self.runtime_store != "git-common-dir"
            and not Path(self.runtime_store).is_absolute()
        ):
            raise ValueError(
                'runtime_store: "git-common-dir" or an absolute path, '
                f"got {self.runtime_store!r}"
            )
        return self


Verifier = Literal["github-review-by-owner", "host-signed"]


class Human(_Model):
    todo: Text
    recap_dir: Text
    approvals_dir: Text
    # An elapsed timeout never approves anything, so true is not a value.
    timeout_is_consent: Literal[False]
    verify: Annotated[
        tuple[Verifier, ...], BeforeValidator(as_tuple), Field(min_length=1)
    ]

    @model_validator(mode="after")
    def _inside(self) -> Self:
        for key in ("todo", "recap_dir", "approvals_dir"):
            _relative(getattr(self, key), key)
        return self


class Harnesses(_Model):
    enforced: Annotated[
        tuple[Harness, ...], BeforeValidator(as_tuple), Field(min_length=1)
    ]
    instructions_only: Annotated[tuple[Harness, ...], BeforeValidator(as_tuple)]
    copilot_surfaces: Annotated[
        tuple[Literal["cli", "ide", "cloud"], ...], BeforeValidator(as_tuple)
    ]

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        both = sorted(set(self.enforced) & set(self.instructions_only))
        if both:
            raise ValueError(f"{', '.join(both)}: both enforced and instructions_only")
        return self


class Secrets(_Model):
    env_file: Text


class Telemetry(_Model):
    host_tier: Literal["partial", "full"]


class Knobs(_Model):
    """`.agents/config.toml` as written."""

    schema_version: Literal[1]
    project: Project
    profile: ProfileRef
    governance: Governance
    models: ModelsRef
    teams: TeamsRef
    usage: Usage
    standards: StandardsRef
    pipelines: Pipelines
    memory: Memory
    human: Human
    harnesses: Harnesses
    secrets: Secrets
    telemetry: Telemetry


# ---------------------------------------------------------------- profiles


class Review(_Model):
    independent: Whole
    second: Literal["deterministic"]
    cross_provider: bool


class Loop(_Model):
    blocks_no_progress: Whole
    blocks_max: Whole
    iterations: Whole
    wall_minutes: Whole


class Context(_Model):
    digest_max_chars: Whole
    tool_output_max_chars: Whole
    read_max_kb: Whole
    # Codex stops reading the AGENTS.md chain at 32 KiB; tac checks well under it.
    agents_md_chain_max_kb: Annotated[int, Field(ge=1, le=32)]


ClaudeMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]
# The values Codex's config reference lists: "untrusted" is unsupported there
# and "on-failure" deprecated, so neither is accepted here.
CodexApproval = Literal["on-request", "never"]


class ClaudeProfile(_Model):
    # The keys keep Claude Code's own spelling, since they are rendered as is.
    failIfUnavailable: bool
    allowUnsandboxedCommands: bool
    defaultMode: ClaudeMode


class CodexProfile(_Model):
    sandbox_mode: CodexSandbox
    approval_policy: CodexApproval


class Profile(_Model):
    """A complete profile. It has no gates key, so no profile can drop a gate."""

    schema_version: Literal[1]
    name: ProfileName
    approvals: Literal["external-effects", "none"]
    isolation: Literal["native-sandbox", "container", "disposable-container"]
    network: Literal["allowlist", "off"]
    native_delegation: Literal["off", "guarded"]
    review: Review
    effort: Literal["models"]
    local_checks: Literal["affected-plus-security", "affected", "nearest"]
    loop: Loop
    context: Context
    mcp_servers: Literal["none", "from-conf"]
    claude: ClaudeProfile
    codex: CodexProfile

    @property
    def on_host(self) -> bool:
        return self.isolation == "native-sandbox"

    @model_validator(mode="after")
    def _prompts(self) -> Self:
        if self.approvals == "none" and self.isolation != "disposable-container":
            raise ValueError(
                'approvals = "none" needs isolation = "disposable-container"'
            )
        return self


# ---------------------------------------------------------------- models


class Provider(_Model):
    harness: Literal["claude", "codex"]
    build: Text
    efforts: Annotated[tuple[Text, ...], BeforeValidator(as_tuple), Field(min_length=1)]


class Seat(_Model):
    model: Text
    # Left out for a model that takes no effort parameter, so a receipt never
    # records a requested effort the model could not have honoured.
    effort: Text | None = None


class RoleModels(BaseModel):
    """`provider` plus one seat per provider id, keyed by that id."""

    model_config = ConfigDict(extra="allow", frozen=True, strict=True)
    __pydantic_extra__: dict[str, Seat] = Field(init=False)  # pyright: ignore[reportIncompatibleVariableOverride]

    provider: Name

    def seats(self) -> dict[str, Seat]:
        return dict(self.__pydantic_extra__ or {})


class Director(_Model):
    provider: Name
    model: Text
    effort: Text
    launch_effort: str
    lead: bool


class ModelsFile(_Model):
    schema_version: Literal[1]
    name: Name
    providers: Annotated[dict[str, Provider], Field(min_length=1)]
    roles: dict[str, RoleModels]
    directors: dict[str, Director]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        leads = [seat for seat, d in self.directors.items() if d.lead]
        if self.directors and len(leads) != 1:
            raise ValueError(f"exactly one director has lead = true, got {leads}")
        for role, spec in self.roles.items():
            seats = spec.seats()
            unknown = sorted(set(seats) - set(self.providers))
            if unknown:
                raise ValueError(f"roles.{role}: unknown provider {unknown}")
            if spec.provider in ("any", "other"):
                missing = sorted(set(self.providers) - set(seats))
                if missing:
                    raise ValueError(
                        f"roles.{role}: provider = {spec.provider!r} needs a seat "
                        f"on every provider, missing {missing}"
                    )
            elif spec.provider not in self.providers:
                raise ValueError(f"roles.{role}: unknown provider {spec.provider!r}")
            elif spec.provider not in seats:
                raise ValueError(f"roles.{role}: no seat on {spec.provider}")
            for pid, seat in seats.items():
                if seat.effort is None:
                    continue
                if seat.effort not in self.providers[pid].efforts:
                    raise ValueError(
                        f"roles.{role}.{pid}: effort {seat.effort!r} is not one of "
                        f"{list(self.providers[pid].efforts)}"
                    )
        for seat, d in self.directors.items():
            if d.provider not in self.providers:
                raise ValueError(f"directors.{seat}: unknown provider {d.provider!r}")
            if d.effort not in self.providers[d.provider].efforts:
                raise ValueError(
                    f"directors.{seat}: effort {d.effort!r} is not one of "
                    f"{list(self.providers[d.provider].efforts)}"
                )
        return self


# ---------------------------------------------------------------- roles


class Role(_Model):
    """One role's charter, rendered into every harness's agent file."""

    schema_version: Literal[1]
    name: Name
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    identity: Literal["tac-bot", "none"]
    runs: Literal[
        "host-session", "worktree", "headless", "headless-read-only", "fresh-session"
    ]
    writes: Annotated[
        tuple[Literal["worktree", "worker-store"], ...], BeforeValidator(as_tuple)
    ]
    may: Strs
    may_not: Strs
    instructions: Text


# ---------------------------------------------------------------- gate packs


class GateCheck(_Model):
    argv: Annotated[tuple[Text, ...], BeforeValidator(as_tuple), Field(min_length=2)]
    why: Text
    enabled: bool
    waits_on: Annotated[str, Field(pattern=r"^(Q[0-9]+)?$")]

    @model_validator(mode="after")
    def _runnable(self) -> Self:
        # Gates run without a shell, only as just recipes (the runner's own rule).
        if self.argv[0] != "just":
            raise ValueError(f"argv[0] must be just, got {self.argv[0]!r}")
        if self.enabled == bool(self.waits_on):
            raise ValueError("a disabled check names waits_on; an enabled one does not")
        return self


class GatePack(_Model):
    schema_version: Literal[1]
    kind: Kind
    description: Text
    includes: Annotated[tuple[PackKind, ...], BeforeValidator(as_tuple)]
    checks: dict[str, GateCheck]


# ---------------------------------------------------------------- the other tables


def _absolute(value: str, where: str) -> str:
    if not Path(value).is_absolute():
        raise ValueError(f"{where}: an absolute path, got {value!r}")
    return value


class Guard(_Model):
    script: Text
    python: Text
    # C2: the guard always runs isolated, so false is not a value.
    isolated: Literal[True]
    path: Annotated[tuple[Text, ...], BeforeValidator(as_tuple), Field(min_length=1)]
    deadline_s: Whole
    timeout_s: Whole

    @model_validator(mode="after")
    def _trusted(self) -> Self:
        _relative(self.script, "script")
        _absolute(self.python, "python")
        for folder in self.path:
            _absolute(folder, "path")
        if self.deadline_s >= self.timeout_s:
            raise ValueError("deadline_s must be below timeout_s")
        return self


HookEvent = Literal[
    "PreToolUse", "PostToolUse", "SessionStart", "SubagentStart", "Stop"
]


class HookCheck(_Model):
    event: HookEvent
    kind: Literal["deny", "reminder", "record"]
    claude: str
    codex: str


class GitHooks(_Model):
    pre_commit: Strs
    commit_msg: Strs
    pre_push: Strs


class HooksFile(_Model):
    schema_version: Literal[1]
    guard: Guard
    checks: dict[str, HookCheck]
    git: GitHooks


class DenyPaths(_Model):
    deny_read: Strs
    deny_write: Strs

    @model_validator(mode="after")
    def _tree(self) -> Self:
        if ".agents/**" not in self.deny_write:
            raise ValueError(
                "deny_write must keep .agents/**: no agent edits its policy"
            )
        return self


class Effects(_Model):
    runner_only: Strs
    owner_only: Strs

    @model_validator(mode="after")
    def _owner(self) -> Self:
        missing = sorted({"merge", "release"} - set(self.owner_only))
        if missing:
            raise ValueError(f"owner_only must keep {missing}")
        both = sorted(set(self.runner_only) & set(self.owner_only))
        if both:
            raise ValueError(f"{both}: both runner_only and owner_only")
        return self


class HostRefusals(_Model):
    refuse_claude_modes: Annotated[tuple[ClaudeMode, ...], BeforeValidator(as_tuple)]
    refuse_unsandboxed_commands: bool
    refuse_codex_sandbox: Annotated[tuple[CodexSandbox, ...], BeforeValidator(as_tuple)]
    refuse_codex_approval: Annotated[
        tuple[CodexApproval, ...], BeforeValidator(as_tuple)
    ]


class PolicyFile(_Model):
    schema_version: Literal[1]
    paths: DenyPaths
    effects: Effects
    host: HostRefusals


class McpServer(_Model):
    command: Text
    args: Strs = ()
    env_names: Strs = ()

    @model_validator(mode="after")
    def _command(self) -> Self:
        _absolute(self.command, "command")
        return self


class McpFile(_Model):
    schema_version: Literal[1]
    strict: bool
    servers: dict[str, McpServer]


class Egress(_Model):
    allow: Annotated[
        tuple[Annotated[str, Field(pattern=r"^(\*\.)?[a-z0-9.-]+$")], ...],
        BeforeValidator(as_tuple),
    ]
    ipv6: bool


class NetworkFile(_Model):
    schema_version: Literal[1]
    egress: Egress


class RuntimePaths(_Model):
    worktrees: Text
    worker_store: Name


class RuntimeEnv(_Model):
    claude_subprocess_env_scrub: bool
    codex_inherit: Literal["core", "all", "none"]
    gh_telemetry: bool
    pass_: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")], ...],
        BeforeValidator(as_tuple),
    ] = Field(alias="pass")


class RuntimeFile(_Model):
    schema_version: Literal[1]
    paths: RuntimePaths
    env: RuntimeEnv


class Repository(_Model):
    slug: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    default_branch: Text


class Merge(_Model):
    # C6: squash, pinned to the reviewed head, is the one merge the owner is handed.
    method: Literal["squash"]
    match_head_commit: Literal[True]


class Labels(_Model):
    families: Annotated[
        tuple[Literal["type", "team", "state", "priority"], ...],
        BeforeValidator(as_tuple),
    ]
    states: Annotated[
        tuple[Literal["queued", "building", "review", "blocked", "landed"], ...],
        BeforeValidator(as_tuple),
    ]


class Issues(_Model):
    forms: Annotated[
        tuple[
            Literal["bug", "feature", "work-order", "decision", "human-question"], ...
        ],
        BeforeValidator(as_tuple),
    ]
    blank_issues: bool
    comments_from: Literal["pushers"]


Lifecycle = Literal["init", "sync", "hook", "memory", "board", "human-loop", "release"]


class DiagramInventory(_Model):
    lifecycles: Annotated[tuple[Lifecycle, ...], BeforeValidator(as_tuple)]
    per_pipeline: bool


class GitHubFile(_Model):
    schema_version: Literal[1]
    repository: Repository
    merge: Merge
    labels: Labels
    issues: Issues
    diagrams: DiagramInventory


# ---------------------------------------------------------------- pipelines

StageKind = Literal["agent", "gate", "effect", "human"]
OnFail = Literal["repair-once-then-human", "human", "stop"]
# What the runner may pass a gate; each is a value it wrote itself, never text
# a model, an issue or a tool produced.
ArgSource = Literal["order_id", "run_id", "local_checks"]
# A gate script other than a just recipe lives in the product's own source.
GATE_SCRIPT = r"^src/tac/[a-z0-9_/]+\.py$"
RECIPE = r"^[a-z][a-z0-9_-]*$"


def _no_shell(value: object) -> object:
    if isinstance(value, str):
        raise ValueError(
            "a gate is an argv array run without a shell, never a shell string; "
            f'write ["just", "<recipe>"], got {value!r}'
        )
    return as_tuple(value)


class Gate(_Model):
    """One deterministic check: argv run without a shell, argv[0] `just` or a
    script under src/tac/, never text that could reach a shell or a template."""

    argv: Annotated[tuple[Text, ...], BeforeValidator(_no_shell), Field(min_length=1)]
    args_from: Annotated[tuple[ArgSource, ...], BeforeValidator(as_tuple)] = ()
    # The milestone or open question that adds the recipe, like M8 or Q13; empty
    # when the gate runs today. A run refuses a pipeline while one waits.
    waits_on: Annotated[str, Field(pattern=r"^((M|Q)[0-9]+)?$")] = ""

    @model_validator(mode="after")
    def _runnable(self) -> Self:
        for arg in self.argv:
            if "{{" in arg or "{%" in arg:
                raise ValueError(f"argv {arg!r}: a gate is never a template")
        head = self.argv[0]
        if head == "just":
            if len(self.argv) < 2:
                raise ValueError('argv ["just"] names no recipe')
            if not re.match(RECIPE, self.argv[1]):
                raise ValueError(
                    f"argv[1] {self.argv[1]!r} is not a recipe name; a gate "
                    "passes values through args_from, never in argv"
                )
        else:
            if not re.match(GATE_SCRIPT, head):
                raise ValueError(
                    f"argv[0] must be just or a script under src/tac/, got {head!r}"
                )
        return self


class Budget(_Model):
    max_items: Whole
    max_chars: Whole


class Retry(_Model):
    max: Annotated[int, Field(ge=1, le=3)]
    on_failure: Gate | None = None


class PipelineStage(_Model):
    """One stage. Which keys a stage may carry depends on its kind; the pipeline
    checker names each one that does not belong."""

    id: Name
    kind: StageKind = "agent"
    role: Name | None = None
    seats: Literal["lead", "others"] | None = None
    provider: Literal["other"] | None = None
    depends_on: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)] = ()
    when: Literal["cross_team"] | None = None
    skills: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)] = ()
    reads: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)] = ()
    writes: Name | None = None
    bind: dict[str, Name] = Field(default_factory=dict)
    template: Text | None = None
    budget: Budget | None = None
    max_turns: Whole | None = None
    gates: Annotated[tuple[Gate, ...], BeforeValidator(as_tuple)] = ()
    retry: Retry | None = None
    on_fail: OnFail | None = None
    effects: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)] = ()
    asks: Text | None = None
    owner_actions: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)] = ()


class PipelineFile(_Model):
    """`.agents/config/pipelines/<name>.toml` as written."""

    schema_version: Literal[1]
    name: PipelineName
    description: Text
    entry_contracts: Annotated[tuple[Name, ...], BeforeValidator(as_tuple)]
    stages: Annotated[
        tuple[PipelineStage, ...], BeforeValidator(as_tuple), Field(min_length=1)
    ]
