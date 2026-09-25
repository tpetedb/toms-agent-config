"""The configuration: `.agents/config.toml` and the tables it points at.

The owner edits one knob file. It names the active profile, the models policy,
the teams table and the standards registry, each a commented file under
`.agents/config/`; the floor under the registry is `.agents/standards.floor.toml`.
`load_config` reads every file through a pydantic model that refuses an unknown
key and a value of the wrong type, checks what the files say about each other,
applies the floor and the waivers, and mounts everything into one effective tree
with the file, line and comment each value came from, which `tac config show`
and `tac explain` print. Nothing here reads a secret: `[secrets] env_file` is a
path the launcher injects, never opened.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import tomllib
import types
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ValidationError

from tac import standards
from tac.config_schema import (
    GatePack,
    GitHubFile,
    HooksFile,
    Knobs,
    McpFile,
    ModelsFile,
    NetworkFile,
    PipelineFile,
    PolicyFile,
    Profile,
    Role,
    RuntimeFile,
)
from tac.draft07 import draft07
from tac.receipts import ReceiptPolicy
from tac.runner import ProbesFile
from tac.standards import FLOOR_FILE, Floor, Registry
from tac.tomldoc import KeyDoc, document, join
from tac.work import (
    KNOBS_FILE,
    TEAMS_FILE,
    Bad,
    Teams,
    TeamsFile,
    git,
    load_teams,
    validation_error,
)

CONFIG_DIR = ".agents/config"
SKILLS_DIR = ".agents/skills"
# Every file and folder .agents/config/ may hold; anything else is a typo that
# would otherwise be silently ignored. A later stage adds its file here with its model.
KNOWN_FILES = frozenset(
    {
        "models.toml",
        "teams.toml",
        "standards.toml",
        "hooks.toml",
        "policy.toml",
        "mcp.toml",
        "network.toml",
        "runtime.toml",
        "github.toml",
        "probes.toml",
        "receipts.toml",
        "runner.pub",
    }
)
KNOWN_DIRS = frozenset({"profiles", "roles", "gates", "pipelines"})


# ---------------------------------------------------------------- the effective tree


@dataclass(frozen=True, slots=True)
class Entry:
    """One effective value and where it came from."""

    path: str
    value: Any
    source: str
    line: int
    comment: str
    applies: str = "always"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.path,
            "value": plain(self.value),
            "source": f"{self.source}:{self.line}",
            "applies": self.applies,
            "comment": self.comment,
        }


def plain(value: Any) -> Any:
    """A value as JSON writes it: dates as ISO 8601 text, tuples as lists."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def show_value(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False)


def _leaves(data: Mapping[str, Any], prefix: list[str]) -> Iterator[tuple[str, Any]]:
    """Every value that is not itself a table; an empty table is a value."""
    for key, value in data.items():
        parts = [*prefix, key]
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, parts)
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(item, Mapping) for item in value)
        ):
            for i, item in enumerate(value):
                yield from _leaves(item, [*parts, f"[{i}]"])
        else:
            yield join(parts), value


@dataclass
class _Tree:
    entries: dict[str, Entry] = field(default_factory=dict)
    tables: dict[str, Entry] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    parents: set[str] = field(default_factory=set)

    def mount(
        self,
        source: str,
        text: str,
        at: tuple[str, ...],
        skip: frozenset[str],
        applies: str = "always",
    ) -> None:
        data = tomllib.loads(text)
        docs = document(text)
        kept = {k: v for k, v in data.items() if k not in skip}
        for rel, value in _leaves(kept, []):
            self._put(rel, value, source, docs, at, applies)
        for rel, doc in docs.items():
            if doc.table and rel.split(".")[0].split("[")[0] not in skip:
                path = join([*at, rel]) if at else rel
                self.tables[path] = Entry(path, None, source, doc.line, doc.comment)

    def _put(
        self,
        rel: str,
        value: Any,
        source: str,
        docs: Mapping[str, KeyDoc],
        at: tuple[str, ...],
        applies: str,
    ) -> None:
        path = join([*at, rel]) if at else rel
        doc = docs.get(rel)
        # A value and a table at one path collide as surely as two values do.
        parts = _split_path(path)
        above = [join(parts[:i]) for i in range(1, len(parts))]
        clash = next((p for p in [path, *above] if p in self.entries), None)
        if clash is None and path in self.parents:
            clash = next(
                p for p in self.entries if p.startswith((f"{path}.", f"{path}["))
            )
        if clash is not None:
            self.problems.append(
                f"{path} is set in both {self.entries[clash].source} ({clash}) "
                f"and {source}"
            )
            return
        self.parents.update(above)
        self.entries[path] = Entry(
            path,
            value,
            source,
            doc.line if doc else 0,
            doc.comment if doc else "",
            applies,
        )


@dataclass(frozen=True)
class Config:
    """Everything the knob file points at, checked, with the floor applied."""

    root: Path
    knobs: Knobs
    profile: Profile
    profiles: Mapping[str, Profile]
    models: ModelsFile
    roles: Mapping[str, Role]
    teams: Teams
    registry: Registry
    floor: Floor
    standards: Mapping[str, Any]
    gates: Mapping[str, GatePack]
    hooks: HooksFile
    policy: PolicyFile
    mcp: McpFile
    network: NetworkFile
    runtime: RuntimeFile
    github: GitHubFile
    entries: Mapping[str, Entry]
    tables: Mapping[str, Entry]
    notes: tuple[str, ...] = ()

    def explain(self, key: str) -> list[Entry]:
        """The entry for `key`, or every entry under it when it names a table."""
        if key in self.entries:
            return [self.entries[key]]
        return [
            e for p, e in self.entries.items() if p.startswith((f"{key}.", f"{key}["))
        ]

    def close_to(self, key: str) -> list[str]:
        known = {*self.entries, *self.tables}
        return difflib.get_close_matches(key, sorted(known), n=3)

    def tree(self) -> dict[str, Any]:
        """The effective values, nested the way the files nest them."""
        out: dict[str, Any] = {}
        for path, entry in self.entries.items():
            node = out
            parts = _split_path(path)
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = plain(entry.value)
        return out


def _split_path(path: str) -> list[str]:
    parts: list[str] = []
    for piece in path.split("."):
        head, _, rest = piece.partition("[")
        parts.append(head)
        if rest:
            parts.append(f"[{rest}")
    return parts


# ---------------------------------------------------------------- loading


def _read(root: Path, rel: str, problems: list[str]) -> str | None:
    path = root / rel
    if not path.is_file():
        problems.append(f"{rel}: missing")
        return None
    return path.read_text(encoding="utf-8")


def _parse[M: BaseModel](
    model: type[M], rel: str, text: str | None, problems: list[str]
) -> M | None:
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        problems.append(f"{rel}: not valid TOML: {e}")
        return None
    try:
        return model.model_validate(data)
    except ValidationError as e:
        problems.append(str(validation_error(Path(rel), e)))
        return None


def _profile_names(root: Path) -> list[str]:
    folder = root / CONFIG_DIR / "profiles"
    return sorted(p.stem for p in folder.glob("*.toml")) if folder.is_dir() else []


def _unknown_files(root: Path) -> list[str]:
    folder = root / CONFIG_DIR
    if not folder.is_dir():
        return [f"{CONFIG_DIR}: missing"]
    problems = []
    for entry in sorted(folder.iterdir()):
        known = KNOWN_DIRS if entry.is_dir() else KNOWN_FILES
        if entry.name not in known:
            problems.append(
                f"{CONFIG_DIR}/{entry.name}: not a file tac reads; "
                "a misspelt name is ignored silently otherwise"
            )
    return problems


def load_config(
    root: Path, *, today: dt.date | None = None, floor_text: str | None = None
) -> Config:
    """Read, check and merge every configuration file; one Bad names every problem.

    `floor_text` replaces the floor file, so CI can apply the base revision's floor.
    """
    today = today or standards.utc_today()
    problems = _unknown_files(root)
    tree = _Tree()
    texts: dict[str, str] = {}

    def read(rel: str) -> str | None:
        text = _read(root, rel, problems)
        if text is not None:
            texts[rel] = text
        return text

    knobs = _parse(Knobs, KNOBS_FILE, read(KNOBS_FILE), problems)
    if knobs is None:
        raise Bad("\n".join(problems))

    profiles: dict[str, Profile] = {}
    for name in _profile_names(root):
        rel = f"{CONFIG_DIR}/profiles/{name}.toml"
        profile = _parse(Profile, rel, read(rel), problems)
        if profile is not None:
            profiles[name] = profile
            if profile.name != name:
                problems.append(f"{rel}: name = {profile.name!r}, the file says {name}")
    active = knobs.profile.active
    if active not in _profile_names(root):
        problems.append(
            f"{CONFIG_DIR}/profiles/{active}.toml: missing, and [profile] active "
            "names it"
        )

    models_rel = f"{CONFIG_DIR}/models.toml"
    models = _parse(ModelsFile, models_rel, read(models_rel), problems)

    roles: dict[str, Role] = {}
    roles_dir = root / CONFIG_DIR / "roles"
    for path in sorted(roles_dir.glob("*.toml")) if roles_dir.is_dir() else []:
        rel = f"{CONFIG_DIR}/roles/{path.name}"
        role = _parse(Role, rel, read(rel), problems)
        if role is not None:
            roles[path.stem] = role
            if role.name != path.stem:
                problems.append(
                    f"{rel}: name = {role.name!r}, the file says {path.stem}"
                )

    teams: Teams | None = None
    teams_text = read(TEAMS_FILE)
    if teams_text is not None:
        try:
            teams = load_teams(root)
        except Bad as e:
            problems.append(str(e))

    reg_rel = standards.STANDARDS_FILE
    registry = _parse(Registry, reg_rel, read(reg_rel), problems)
    floor: Floor | None = None
    floor_source = floor_text if floor_text is not None else read(FLOOR_FILE)
    if floor_source is not None:
        texts[FLOOR_FILE] = floor_source
        try:
            floor = standards.parse_floor(tomllib.loads(floor_source), FLOOR_FILE)
        except (Bad, tomllib.TOMLDecodeError) as e:
            problems.append(str(e))

    gates: dict[str, GatePack] = {}
    gates_dir = root / CONFIG_DIR / "gates"
    for path in sorted(gates_dir.glob("*.toml")) if gates_dir.is_dir() else []:
        rel = f"{CONFIG_DIR}/gates/{path.name}"
        pack = _parse(GatePack, rel, read(rel), problems)
        if pack is not None:
            gates[path.stem] = pack
            if pack.kind != path.stem:
                problems.append(
                    f"{rel}: kind = {pack.kind!r}, the file says {path.stem}"
                )

    singles: dict[str, BaseModel | None] = {}
    for stem, model in (
        ("hooks", HooksFile),
        ("policy", PolicyFile),
        ("mcp", McpFile),
        ("network", NetworkFile),
        ("runtime", RuntimeFile),
        ("github", GitHubFile),
        ("probes", ProbesFile),
        ("receipts", ReceiptPolicy),
    ):
        rel = f"{CONFIG_DIR}/{stem}.toml"
        singles[stem] = _parse(model, rel, read(rel), problems)

    if problems:
        raise Bad("\n".join(problems))
    assert models and registry and floor and teams is not None
    hooks, policy, mcp, network, runtime, github = (
        singles["hooks"],
        singles["policy"],
        singles["mcp"],
        singles["network"],
        singles["runtime"],
        singles["github"],
    )
    assert isinstance(hooks, HooksFile) and isinstance(policy, PolicyFile)
    assert isinstance(mcp, McpFile) and isinstance(network, NetworkFile)
    assert isinstance(runtime, RuntimeFile) and isinstance(github, GitHubFile)
    profile = profiles[active]

    problems += _cross_checks(
        root, knobs, profiles, models, roles, teams, texts, registry, gates, policy
    )
    problems += _delegation_checks(
        profile, f"{CONFIG_DIR}/profiles/{active}.toml", models, roles
    )
    problems += standards.waiver_problems(floor, FLOOR_FILE, today)
    applied = standards.apply_floor(registry.model_dump(by_alias=True), floor, reg_rel)

    # Mount every file into one tree; a key set by two files is a problem.
    tree.problems = problems
    tree.mount(KNOBS_FILE, texts[KNOBS_FILE], (), frozenset())
    tree.mount(
        f"{CONFIG_DIR}/profiles/{active}.toml",
        texts[f"{CONFIG_DIR}/profiles/{active}.toml"],
        ("profile",),
        frozenset({"schema_version", "name"}),
        applies=f"while profile.active = {active}",
    )
    tree.mount(
        models_rel,
        texts[models_rel],
        ("models",),
        frozenset({"schema_version", "name"}),
    )
    for name in roles:
        rel = f"{CONFIG_DIR}/roles/{name}.toml"
        tree.mount(
            rel, texts[rel], ("roles", name), frozenset({"schema_version", "name"})
        )
    tree.mount(TEAMS_FILE, texts[TEAMS_FILE], (), frozenset({"schema_version", "name"}))
    tree.mount(
        reg_rel, texts[reg_rel], ("standards",), frozenset({"schema_version", "name"})
    )
    tree.mount(FLOOR_FILE, texts[FLOOR_FILE], (), frozenset({"schema_version"}))
    kind = knobs.project.kind
    for pack in _packs_for(kind, gates):
        rel = f"{CONFIG_DIR}/gates/{pack}.toml"
        tree.mount(
            rel,
            texts[rel],
            ("gates", pack),
            frozenset({"schema_version", "kind"}),
            applies=f"while project.kind = {kind}",
        )
    for stem in ("hooks", "policy", "mcp", "network", "runtime", "github"):
        rel = f"{CONFIG_DIR}/{stem}.toml"
        tree.mount(rel, texts[rel], (stem,), frozenset({"schema_version"}))
    # probes.toml already nests everything under [probes.<name>].
    probes_rel = f"{CONFIG_DIR}/probes.toml"
    tree.mount(probes_rel, texts[probes_rel], (), frozenset({"schema_version"}))
    receipts_rel = f"{CONFIG_DIR}/receipts.toml"
    tree.mount(
        receipts_rel, texts[receipts_rel], ("receipts",), frozenset({"schema_version"})
    )
    notes = _apply_standards(tree, applied, texts[FLOOR_FILE])
    for i, waiver in enumerate(floor.waivers):
        for path in [p for p in tree.entries if p.startswith(f"waivers[{i}].")]:
            tree.entries[path] = replace(
                tree.entries[path], applies=f"until {waiver.expires.isoformat()}"
            )

    if problems:
        raise Bad("\n".join(problems))
    return Config(
        root=root,
        knobs=knobs,
        profile=profile,
        profiles=profiles,
        models=models,
        roles=roles,
        teams=teams,
        registry=registry,
        floor=floor,
        standards=applied.values,
        gates={k: gates[k] for k in _packs_for(kind, gates)},
        hooks=hooks,
        policy=policy,
        mcp=mcp,
        network=network,
        runtime=runtime,
        github=github,
        entries=tree.entries,
        tables=tree.tables,
        notes=tuple(notes),
    )


def _packs_for(kind: str, gates: Mapping[str, GatePack]) -> list[str]:
    if kind not in gates:
        return []
    return [kind, *(k for k in gates[kind].includes if k != kind)]


def _apply_standards(
    tree: _Tree, applied: standards.Applied, floor_text: str
) -> list[str]:
    """Where the floor wins, the effective entry is the floor's, and says so."""
    docs = document(floor_text)
    notes = []
    for rule in standards.FLOOR_RULES:
        if rule.where not in applied.from_floor:
            continue
        project = applied.from_floor[rule.where]
        path = f"standards.{rule.where}"
        doc = docs.get(f"floor.{rule.key}")
        was = (
            "the project does not set it"
            if project is None
            else f"the project's {show_value(project)} in {standards.STANDARDS_FILE}"
        )
        value = standards.get_path(applied.values, rule.path)
        tree.entries.pop(path, None)
        tree.entries[path] = Entry(
            path,
            value,
            FLOOR_FILE,
            doc.line if doc else 0,
            doc.comment if doc else "",
            applies=f"always: the floor wins over {was}",
        )
        notes.append(f"{path} = {show_value(value)}: the floor wins over {was}")
    return notes


# The roles the board ruled may never start subagents or workflows natively:
# the runner starts each of them, one per order, stage or question.
NEVER_DELEGATE = ("builder", "manager", "reviewer", "scout")
# The --effort value that starts a Claude Code session with ultracode.
ULTRACODE = "ultracode"


def ultracode_seats(models: ModelsFile) -> dict[str, list[str]]:
    """Every role launched with ultracode, with the models.toml keys that say
    so: a role's seat with ultracode = true, or a director seat launched with
    launch_effort = "ultracode"."""
    found: dict[str, list[str]] = {}
    for name, spec in models.roles.items():
        for pid, seat in sorted(spec.seats().items()):
            if seat.ultracode:
                found.setdefault(name, []).append(f"roles.{name}.{pid}")
    for seat, director in sorted(models.directors.items()):
        if director.launch_effort == ULTRACODE:
            found.setdefault("director", []).append(f"directors.{seat}")
    return found


def _delegation_checks(
    profile: Profile, profile_rel: str, models: ModelsFile, roles: Mapping[str, Role]
) -> list[str]:
    """Ultracode plans dynamic workflows, so a role launched with it delegates
    and the active profile guards delegation instead of switching it off; the
    worker roles never delegate (design sections 4 and 5)."""
    problems: list[str] = []
    for name in NEVER_DELEGATE:
        charter = roles.get(name)
        if charter is not None and charter.delegates:
            problems.append(
                f"{CONFIG_DIR}/roles/{name}.toml: delegates = true is refused; "
                "builders, reviewers, managers and scouts never start subagents "
                "or workflows, the runner starts them"
            )
    for name, where in sorted(ultracode_seats(models).items()):
        keys = ", ".join(where)
        charter = roles.get(name)
        if charter is not None and not charter.delegates:
            problems.append(
                f"{CONFIG_DIR}/models.toml starts {name} with ultracode ({keys}), "
                f"but roles/{name}.toml says delegates = false, so the handoff "
                "guard would refuse every workflow ultracode plans"
            )
        if profile.native_delegation == "off":
            problems.append(
                f'{profile_rel}: native_delegation = "off", but {CONFIG_DIR}/'
                f"models.toml starts {name} with ultracode ({keys}), which "
                'starts agents through Workflow; set native_delegation = "guarded" '
                "or take ultracode off"
            )
    return problems


def _cross_checks(
    root: Path,
    knobs: Knobs,
    profiles: Mapping[str, Profile],
    models: ModelsFile,
    roles: Mapping[str, Role],
    teams: Teams,
    texts: Mapping[str, str],
    registry: Registry,
    gates: Mapping[str, GatePack],
    policy: PolicyFile,
) -> list[str]:
    """What the files say about each other; each problem names both sides."""
    problems: list[str] = []
    knob = KNOBS_FILE
    if models.name != knobs.models.policy:
        problems.append(
            f"{knob}: [models] policy = {knobs.models.policy!r}, but "
            f"{CONFIG_DIR}/models.toml is named {models.name!r}"
        )
    teams_name = tomllib.loads(texts[TEAMS_FILE]).get("name", "default")
    if teams_name != knobs.teams.table:
        problems.append(
            f"{knob}: [teams] table = {knobs.teams.table!r}, but {TEAMS_FILE} "
            f"is named {teams_name!r}"
        )
    if registry.name != knobs.standards.registry:
        problems.append(
            f"{knob}: [standards] registry = {knobs.standards.registry!r}, but "
            f"{standards.STANDARDS_FILE} is named {registry.name!r}"
        )

    # Roles: a charter per role in models.toml, and the reverse; the chief exists.
    charters = set(roles)
    seated = set(models.roles) | ({"director"} if models.directors else set())
    for missing in sorted(seated - charters):
        problems.append(
            f"{CONFIG_DIR}/roles/{missing}.toml: missing, models.toml seats it"
        )
    for extra in sorted(charters - seated):
        problems.append(
            f"{CONFIG_DIR}/models.toml: no [roles.{extra}] for the charter "
            f"roles/{extra}.toml"
        )
    if knobs.governance.chief not in roles:
        problems.append(
            f"{knob}: [governance] chief = {knobs.governance.chief!r} has no charter"
        )
    # Ultracode is set when a session starts, so only a role launched as a session
    # of its own can have it; a subagent's agent file takes an effort, never
    # ultracode (https://code.claude.com/docs/en/sub-agents, frontmatter `effort`).
    for name, spec in models.roles.items():
        charter = roles.get(name)
        if charter is None or charter.runs != "subagent":
            continue
        for pid in sorted(p for p, s in spec.seats().items() if s.ultracode):
            problems.append(
                f"{CONFIG_DIR}/models.toml: roles.{name}.{pid} sets ultracode, but "
                f'roles/{name}.toml runs = "subagent": ultracode is set when a '
                "session starts, and a subagent is never launched as one"
            )
    for seat in knobs.governance.directors:
        if seat not in models.directors:
            problems.append(
                f"{knob}: director {seat!r} has no [directors.{seat}] in models.toml"
            )
    lead = next((s for s, d in models.directors.items() if d.lead), None)
    if lead is not None and knobs.governance.tally_by != lead:
        problems.append(
            f"{knob}: tally_by = {knobs.governance.tally_by!r}, but models.toml "
            f"makes {lead!r} the lead"
        )

    # Harnesses: each enforced one runs a provider that models.toml names.
    harness_of = {p.harness for p in models.providers.values()}
    for harness in knobs.harnesses.enforced:
        if harness not in harness_of:
            problems.append(
                f"{knob}: {harness} is enforced, but no provider in models.toml "
                "runs through it; it stays instructions_only until it has a receipt"
            )

    # Teams: the builder budget, model overrides and skills name real things.
    for team in teams.teams:
        if team.max_workers and team.max_workers > knobs.teams.max_local_agents:
            problems.append(
                f"{TEAMS_FILE}: [teams.{team.id}] max_workers = {team.max_workers} is "
                f"above [teams] max_local_agents = {knobs.teams.max_local_agents}"
            )
        for role in team.models:
            if role not in roles:
                problems.append(
                    f"{TEAMS_FILE}: [teams.{team.id}] models names {role!r}, not a role"
                )
        for skill in team.skills:
            if not (root / SKILLS_DIR / skill / "SKILL.md").is_file():
                problems.append(
                    f"{TEAMS_FILE}: [teams.{team.id}] skill {skill!r} is not under "
                    f"{SKILLS_DIR}/"
                )

    # Gates: the project's kind has a pack, and so does each pack it includes.
    kind = knobs.project.kind
    if kind not in gates:
        problems.append(
            f"{CONFIG_DIR}/gates/{kind}.toml: missing, [project] kind = {kind!r}"
        )
    else:
        for inc in gates[kind].includes:
            if inc not in gates:
                problems.append(
                    f"{CONFIG_DIR}/gates/{inc}.toml: missing, {kind} includes it"
                )

    # Profiles: host profiles never carry the values policy.toml refuses there.
    host = policy.host
    for name, profile in profiles.items():
        if not profile.on_host:
            continue
        rel = f"{CONFIG_DIR}/profiles/{name}.toml"
        refused = []
        if profile.claude.defaultMode in host.refuse_claude_modes:
            refused.append(f"claude.defaultMode = {profile.claude.defaultMode!r}")
        if host.refuse_unsandboxed_commands and profile.claude.allowUnsandboxedCommands:
            refused.append("claude.allowUnsandboxedCommands = true")
        if profile.codex.sandbox_mode in host.refuse_codex_sandbox:
            refused.append(f"codex.sandbox_mode = {profile.codex.sandbox_mode!r}")
        if profile.codex.approval_policy in host.refuse_codex_approval:
            refused.append(f"codex.approval_policy = {profile.codex.approval_policy!r}")
        for item in refused:
            problems.append(
                f"{rel}: {item} is refused on the host (isolation = native-sandbox); "
                "config/policy.toml [host]"
            )

    # The chief never holds an action the owner keeps for themselves.
    for power in knobs.governance.chief_may:
        if power in policy.effects.owner_only:
            problems.append(f"{knob}: chief_may {power!r} is owner_only in policy.toml")
    return problems


# ---------------------------------------------------------------- schema lookups


def _unwrap(tp: Any) -> Any:
    while True:
        origin = typing.get_origin(tp)
        if origin is Annotated:
            tp = typing.get_args(tp)[0]
        elif origin in (typing.Union, types.UnionType):
            rest = [a for a in typing.get_args(tp) if a is not type(None)]
            if len(rest) != 1:
                return tp
            tp = rest[0]
        else:
            return tp


def literal_choices(model: type[BaseModel], parts: list[str]) -> tuple[str, ...]:
    """The allowed text values of the key at `parts` in `model`, if it is an enum."""
    tp: Any = model
    for part in parts:
        tp = _unwrap(tp)
        if part.startswith("["):
            if typing.get_origin(tp) is tuple:
                tp = typing.get_args(tp)[0]
                continue
            return ()
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            fields = {f.alias or n: f for n, f in tp.model_fields.items()}
            if part in fields:
                tp = fields[part].annotation
                continue
            extra = typing.get_type_hints(tp).get("__pydantic_extra__")
            if extra is None:
                return ()
            tp = typing.get_args(_unwrap(extra))[1]
        elif typing.get_origin(tp) is dict:
            tp = typing.get_args(tp)[1]
        else:
            return ()
    tp = _unwrap(tp)
    if typing.get_origin(tp) is tuple:
        tp = _unwrap(typing.get_args(tp)[0])
    if typing.get_origin(tp) is Literal:
        return tuple(a for a in typing.get_args(tp) if isinstance(a, str))
    return ()


# The model each kind of shipped file is read through, by the name of its JSON
# Schema draft-07 export in contracts/config/<name>.schema.json.
CONFIG_SCHEMAS: dict[str, type[BaseModel]] = {
    "config": Knobs,
    "profile": Profile,
    "role": Role,
    "gate-pack": GatePack,
    "models": ModelsFile,
    "teams": TeamsFile,
    "standards": Registry,
    "standards-floor": standards.FloorFile,
    "hooks": HooksFile,
    "policy": PolicyFile,
    "mcp": McpFile,
    "network": NetworkFile,
    "runtime": RuntimeFile,
    "github": GitHubFile,
    "probes": ProbesFile,
    "receipts": ReceiptPolicy,
    "pipeline": PipelineFile,
}
CONTRACTS_DIR = "contracts/config"


def schema_name(rel: str) -> str | None:
    """The name of the schema a shipped file is read through, by its path."""
    if rel == KNOBS_FILE:
        return "config"
    if rel == FLOOR_FILE:
        return "standards-floor"
    if rel == TEAMS_FILE:
        return "teams"
    folder, _, name = rel.removeprefix(f"{CONFIG_DIR}/").rpartition("/")
    if folder:
        return {
            "profiles": "profile",
            "roles": "role",
            "gates": "gate-pack",
            "pipelines": "pipeline",
        }.get(folder)
    stem = name.removesuffix(".toml")
    if name.endswith(".toml") and stem in CONFIG_SCHEMAS and name in KNOWN_FILES:
        return stem
    return None


def schema_for(rel: str) -> type[BaseModel] | None:
    """The model a shipped file is read through, by its path from the root."""
    name = schema_name(rel)
    return CONFIG_SCHEMAS[name] if name else None


def config_json_schemas() -> dict[str, dict[str, Any]]:
    """Every config file's JSON Schema draft-07, by name. Config schemas are not
    model-facing, so they keep optional keys and defaults (build condition C4)."""
    return {name: draft07(model) for name, model in CONFIG_SCHEMAS.items()}


def floor_check(root: Path, base: str) -> tuple[list[str], list[str]]:
    """Judge the floor as CI does: the base revision's floor applies, and the
    candidate's floor may only tighten it. Returns the problems and the notes."""
    git(root, "rev-parse", "--verify", f"{base}^{{commit}}", must=True)
    head_path = root / FLOOR_FILE
    if not head_path.is_file():
        return [f"{FLOOR_FILE}: missing; the floor may not be removed"], []
    if git(root, "cat-file", "-t", f"{base}:{FLOOR_FILE}") != "blob":
        # The change that brings the floor has nothing older to tighten.
        return [], [f"{base} has no {FLOOR_FILE}; the candidate's floor applies"]
    base_text = git(root, "show", f"{base}:{FLOOR_FILE}", must=True)
    try:
        base_floor = standards.parse_floor(
            tomllib.loads(base_text), f"{base}:{FLOOR_FILE}"
        )
        head_floor = standards.parse_floor(
            tomllib.loads(head_path.read_text(encoding="utf-8")), FLOOR_FILE
        )
    except (Bad, tomllib.TOMLDecodeError) as e:
        return [str(e)], []
    problems = [
        f"{FLOOR_FILE}: {p}" for p in standards.loosened(base_floor, head_floor)
    ]
    try:
        load_config(root, floor_text=base_text)
    except Bad as e:
        problems.append(str(e))
    return problems, []
