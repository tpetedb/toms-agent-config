"""Pipelines: the declared ways work flows from a request to landed (design section 5).

A pipeline is `.agents/config/pipelines/<name>.toml`, a list of stages. An agent
stage names the role it runs as, its skills, the contracts it reads, the
contract it writes and the template that renders its prompt: that is its
envelope, and a stage without one is refused. A gate stage runs deterministic
checks only, an effect stage is performed by the trusted runner, and a human
stage waits for the owner.

`check_pipelines` names the reason for every refusal: the shape, an unknown
role, skill, contract or template, a template whose header disagrees with the
stage, a read no upstream stage writes, a cycle, a gate that is a shell string,
names no recipe or hands it a number of values it does not take, an effect
policy.toml keeps for the owner. `plan` gives the
stage order without running anything; `tac run` lands with the runner (M3).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tac import handoff
from tac.config_schema import Gate, Knobs, PipelineFile, PipelineStage, PolicyFile
from tac.work import KNOBS_FILE, Bad, validation_error

CONFIG_DIR = ".agents/config"
PIPELINES_DIR = f"{CONFIG_DIR}/pipelines"
ROLES_DIR = f"{CONFIG_DIR}/roles"
POLICY_FILE = f"{CONFIG_DIR}/policy.toml"
SKILLS_DIR = ".agents/skills"
JUSTFILE = "justfile"
# The contracts a review writes; only a review stage may ask for the other provider.
REVIEW_CONTRACTS = frozenset({"review"})
# The keys each kind of stage may carry, besides id, kind, depends_on and when.
COMMON = frozenset({"id", "kind", "depends_on", "when", "on_fail"})
ALLOWED: Mapping[str, frozenset[str]] = {
    "agent": COMMON
    | {
        "role",
        "seats",
        "provider",
        "skills",
        "reads",
        "writes",
        "bind",
        "template",
        "budget",
        "max_turns",
        "gates",
        "retry",
    },
    "gate": COMMON | {"gates", "retry"},
    "effect": COMMON | {"effects"},
    "human": COMMON | {"reads", "bind", "asks", "owner_actions"},
}
# What each kind must carry, and what that key is for, for the refusal.
REQUIRED: Mapping[str, tuple[tuple[str, str], ...]] = {
    "agent": (
        ("role", "the role it runs as"),
        ("writes", "the contract its envelope carries"),
        ("template", "the template that renders its prompt"),
    ),
    "gate": (("gates", "the checks it runs"),),
    "effect": (("effects", "what the runner performs"),),
    "human": (("asks", "what the owner is asked"),),
}
# A line that may open a recipe; _header_words tells it from an assignment.
_RECIPE_LINE = re.compile(r"^@?[A-Za-z_][A-Za-z0-9_-]*(?=[\s:])")
_IMPORT_LINE = re.compile(r"""^import\??\s+['"]([^'"]+)['"]""")


@dataclass(frozen=True, slots=True)
class Recipe:
    """A justfile recipe's parameters, enough to know what a gate may pass it."""

    name: str
    required: int = 0
    optional: int = 0
    variadic: bool = False

    def takes(self, count: int) -> bool:
        if count < self.required:
            return False
        return self.variadic or count <= self.required + self.optional

    def wants(self) -> str:
        """The values it takes, in words, for a refusal."""
        if self.variadic:
            return f"{self.required} or more values"
        if self.optional:
            return f"{self.required} to {self.required + self.optional} values"
        return f"{self.required} value" + ("" if self.required == 1 else "s")


def _header_words(line: str) -> list[str] | None:
    """The words before a recipe header's colon, quotes and parentheses kept whole;
    None when the line's first colon is an assignment's `:=`."""
    words: list[str] = []
    word, quote, depth = "", "", 0
    for i, char in enumerate(line):
        if quote:
            word += char
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
            word += char
        elif char == "(":
            depth += 1
            word += char
        elif char == ")":
            depth -= 1
            word += char
        elif char == ":" and depth == 0:
            if line[i + 1 : i + 2] == "=":
                return None
            return [*words, word] if word else words
        elif char.isspace() and depth == 0:
            if word:
                words.append(word)
            word = ""
        else:
            word += char
    return None


def parse_recipe(line: str) -> Recipe | None:
    """A recipe header like `work-say id role text:` or `sync *args:`, else None."""
    if not _RECIPE_LINE.match(line) or line.startswith(("set ", "export ", "alias ")):
        return None
    words = _header_words(line.removeprefix("@"))
    if not words:
        return None
    required = optional = 0
    variadic = False
    for param in (w.removeprefix("$") for w in words[1:]):
        if param[0] in "*+":
            variadic = True
            required += param[0] == "+" and "=" not in param
        elif "=" in param:
            optional += 1
        else:
            required += 1
    return Recipe(words[0], required, optional, variadic)


@dataclass(frozen=True, slots=True)
class Known:
    """What a pipeline may name, read once from the checkout."""

    roles: frozenset[str]
    skills: frozenset[str]
    recipes: Mapping[str, Recipe]
    runner_only: frozenset[str]
    owner_only: frozenset[str]


def recipes(root: Path) -> dict[str, Recipe]:
    """The recipes of the justfile and the files it imports, by name."""
    found: dict[str, Recipe] = {}
    seen: set[Path] = set()
    todo = [root / JUSTFILE]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            imported = _IMPORT_LINE.match(line)
            if imported:
                todo.append(path.parent / imported.group(1))
                continue
            recipe = parse_recipe(line)
            if recipe is not None:
                found.setdefault(recipe.name, recipe)
    return found


def known(root: Path, problems: list[str]) -> Known:
    roles_dir = root / ROLES_DIR
    skills_dir = root / SKILLS_DIR
    runner_only: frozenset[str] = frozenset()
    owner_only: frozenset[str] = frozenset()
    try:
        policy = PolicyFile.model_validate(
            tomllib.loads((root / POLICY_FILE).read_text(encoding="utf-8"))
        )
        runner_only = frozenset(policy.effects.runner_only)
        owner_only = frozenset(policy.effects.owner_only)
    except FileNotFoundError:
        problems.append(f"{POLICY_FILE}: missing; effect stages are judged by it")
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        problems.append(f"{POLICY_FILE}: unreadable, so no effect is allowed: {e}")
    return Known(
        roles=frozenset(p.stem for p in roles_dir.glob("*.toml"))
        if roles_dir.is_dir()
        else frozenset(),
        skills=frozenset(
            p.parent.name for p in skills_dir.glob("*/SKILL.md") if p.is_file()
        )
        if skills_dir.is_dir()
        else frozenset(),
        recipes=recipes(root),
        runner_only=runner_only,
        owner_only=owner_only,
    )


# ---------------------------------------------------------------- reading


def pipeline_files(root: Path) -> list[str]:
    folder = root / PIPELINES_DIR
    return sorted(p.stem for p in folder.glob("*.toml")) if folder.is_dir() else []


def _stage_label(data: Any, loc: Sequence[Any]) -> str:
    """`stages.2` as `stages.2 (id build)`, so a refusal names the stage."""
    if len(loc) < 2 or loc[0] != "stages" or not isinstance(loc[1], int):
        return ""
    stages = data.get("stages") if isinstance(data, dict) else None
    if isinstance(stages, list) and loc[1] < len(stages):
        stage = stages[loc[1]]
        if isinstance(stage, dict) and isinstance(stage.get("id"), str):
            return f"stage {stage['id']}: "
    return ""


def read_pipeline(root: Path, name: str) -> PipelineFile:
    """One pipeline file through its model; a Bad names every shape problem."""
    rel = f"{PIPELINES_DIR}/{name}.toml"
    path = root / rel
    if not path.is_file():
        raise Bad(f"{rel}: missing")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise Bad(f"{rel}: not valid TOML: {e}") from None
    try:
        pipeline = PipelineFile.model_validate(data)
    except ValidationError as e:
        lines = []
        for item, line in zip(
            e.errors(include_url=False),
            str(validation_error(Path(rel), e)).splitlines(),
            strict=True,
        ):
            label = _stage_label(data, item["loc"])
            lines.append(line.replace(f"{rel}: ", f"{rel}: {label}", 1))
        raise Bad("\n".join(lines)) from None
    if pipeline.name != name:
        raise Bad(f"{rel}: name = {pipeline.name!r}, the file says {name}")
    return pipeline


# ---------------------------------------------------------------- the graph


def find_cycle(stages: Sequence[PipelineStage]) -> list[str] | None:
    """The first cycle through depends_on, as the ids around it, or None."""
    after = {s.id: [d for d in s.depends_on if d != s.id] for s in stages}
    colour: dict[str, int] = {}
    trail: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = 1
        trail.append(node)
        for dep in after.get(node, []):
            if dep not in after:
                continue
            if colour.get(dep) == 1:
                return [*trail[trail.index(dep) :], dep]
            if dep not in colour:
                found = visit(dep)
                if found:
                    return found
        trail.pop()
        colour[node] = 2
        return None

    for stage in stages:
        if stage.id not in colour:
            found = visit(stage.id)
            if found:
                return found
    return None


def order(stages: Sequence[PipelineStage]) -> list[PipelineStage]:
    """A topological order of an acyclic pipeline; ties keep the file's order."""
    done: set[str] = set()
    left = list(stages)
    out: list[PipelineStage] = []
    while left:
        ready = next(s for s in left if set(s.depends_on) <= done)
        out.append(ready)
        done.add(ready.id)
        left.remove(ready)
    return out


def ancestors(stages: Sequence[PipelineStage]) -> dict[str, set[str]]:
    """Every stage each stage transitively depends on, for an acyclic pipeline."""
    by_id = {s.id: s for s in stages}
    found: dict[str, set[str]] = {}
    for stage in order(stages):
        up: set[str] = set()
        for dep in stage.depends_on:
            up |= {dep, *found.get(dep, set())}
        found[stage.id] = up & set(by_id)
    return found


# ---------------------------------------------------------------- the checks


def _gate_problems(where: str, gate: Gate, known_: Known) -> list[str]:
    shown = " ".join(gate.argv)
    if gate.argv[0] != "just":
        return []
    if gate.waits_on:
        if gate.argv[1] in known_.recipes:
            return [
                f"{where}: gate `{shown}` waits on {gate.waits_on}, but the "
                f"justfile has recipe {gate.argv[1]}; drop waits_on"
            ]
        return []
    recipe = known_.recipes.get(gate.argv[1])
    if recipe is None:
        return [
            f"{where}: gate `{shown}`: the justfile has no recipe {gate.argv[1]}; "
            "name one, or set waits_on to the milestone that adds it"
        ]
    # args_from values are appended after any literal argv, one value each.
    given = len(gate.argv) - 2 + len(gate.args_from)
    if not recipe.takes(given):
        passed = ", ".join([*gate.argv[2:], *gate.args_from]) or "nothing"
        return [
            f"{where}: gate `{shown}` passes {given} value(s) ({passed}), but "
            f"recipe {recipe.name} takes {recipe.wants()}"
        ]
    return []


def _script_problems(root: Path, where: str, gate: Gate) -> list[str]:
    head = gate.argv[0]
    if head != "just" and not (root / head).is_file():
        return [f"{where}: gate script {head} does not exist"]
    return []


def stage_problems(
    root: Path,
    pipeline: PipelineFile,
    stage: PipelineStage,
    up: set[str],
    known_: Known,
) -> list[str]:
    """What is wrong with one stage, each line naming the stage and the reason."""
    where = f"{PIPELINES_DIR}/{pipeline.name}.toml: stage {stage.id}"
    problems: list[str] = []
    kind = stage.kind
    present = {k for k in stage.model_fields_set if getattr(stage, k) not in ((), {})}
    a_kind = f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"
    for key in sorted(present - ALLOWED[kind]):
        problems.append(f"{where}: {a_kind} stage has no {key}")
    for key, why in REQUIRED[kind]:
        if key not in present:
            if kind == "agent" and key in ("writes", "template"):
                problems.append(
                    f"{where}: no envelope; an agent stage names writes and "
                    f"template, and this one has no {key} ({why})"
                )
            else:
                problems.append(f"{where}: {a_kind} stage names {key}, {why}")

    if stage.role is not None and stage.role not in known_.roles:
        problems.append(
            f"{where}: role {stage.role!r} is unknown; no {ROLES_DIR}/{stage.role}.toml"
        )
    if stage.role == "director" and stage.seats is None:
        problems.append(f"{where}: a director stage names seats, lead or others")
    if stage.seats is not None and stage.role != "director":
        problems.append(f"{where}: seats applies only to the director role")
    if stage.provider == "other" and stage.writes not in REVIEW_CONTRACTS:
        problems.append(
            f'{where}: provider = "other" belongs only on a review stage, one '
            f"that writes {', '.join(sorted(REVIEW_CONTRACTS))}"
        )
    for skill in stage.skills:
        if skill not in known_.skills:
            problems.append(
                f"{where}: skill {skill!r} is unknown; no {SKILLS_DIR}/{skill}/SKILL.md"
            )
    if stage.on_fail == "repair-once-then-human" and kind != "agent":
        problems.append(
            f"{where}: repair-once-then-human repairs a contract; only an agent "
            "stage has one"
        )
    if stage.retry is not None and not stage.gates:
        problems.append(f"{where}: retry reruns gates, and the stage has none")

    # Contracts: each one exists, and the template's header agrees with the stage.
    for name in (*stage.reads, *([stage.writes] if stage.writes else [])):
        try:
            handoff.load_contract(root, name)
        except Bad as e:
            problems.append(f"{where}: {e}")
    if stage.template is not None:
        problems += _template_problems(root, where, stage)

    # Every read is written upstream or entered with, and never ambiguously.
    writers_of = {s.id: s.writes for s in pipeline.stages}
    for contract, bound in stage.bind.items():
        if contract not in stage.reads:
            problems.append(f"{where}: bind names {contract!r}, which it does not read")
        elif bound not in up or writers_of.get(bound) != contract:
            problems.append(
                f"{where}: bind {contract} = {bound!r}, but {bound!r} is not an "
                f"upstream stage that writes {contract}"
            )
    for contract in stage.reads:
        if contract in stage.bind:
            continue
        writers = sorted(s for s in up if writers_of.get(s) == contract)
        sources = len(writers) + (contract in pipeline.entry_contracts)
        if sources == 0:
            problems.append(
                f"{where}: reads {contract}, which no upstream stage writes and "
                "the pipeline does not enter with"
            )
        elif sources > 1:
            named = [
                *writers,
                *(["the entry"] if contract in pipeline.entry_contracts else []),
            ]
            problems.append(
                f"{where}: reads {contract}, written by {', '.join(named)}; "
                f'say which with bind = {{ {contract} = "<stage>" }}'
            )

    # Gates run without a shell, and each names a recipe the justfile has.
    gates = [
        *stage.gates,
        *([stage.retry.on_failure] if stage.retry and stage.retry.on_failure else []),
    ]
    for gate in gates:
        problems += _gate_problems(where, gate, known_)
        problems += _script_problems(root, where, gate)

    for effect in stage.effects:
        if effect in known_.owner_only:
            problems.append(
                f"{where}: effect {effect!r} is the owner's own action "
                f"({POLICY_FILE} owner_only); a human stage asks for it"
            )
        elif effect not in known_.runner_only:
            problems.append(
                f"{where}: effect {effect!r} is not one {POLICY_FILE} lets the "
                "runner perform (runner_only)"
            )
    for action in stage.owner_actions:
        if action not in known_.owner_only:
            problems.append(
                f"{where}: owner action {action!r} is not in {POLICY_FILE} owner_only"
            )
    return problems


def _template_problems(root: Path, where: str, stage: PipelineStage) -> list[str]:
    assert stage.template is not None
    if stage.template == handoff.REPAIR_TEMPLATE:
        return [
            f"{where}: {stage.template} is the repair pass, rendered by tac handoff "
            "validate, never a stage's own template"
        ]
    try:
        template = handoff.read_handoff_template(root, stage.template)
    except Bad as e:
        return [f"{where}: {e}"]
    problems = []
    if stage.writes is not None and template.writes != stage.writes:
        problems.append(
            f"{where}: writes {stage.writes}, but {stage.template} writes "
            f"{template.writes}"
        )
    if set(template.reads) != set(stage.reads):
        problems.append(
            f"{where}: reads [{', '.join(stage.reads)}], but {stage.template} reads "
            f"[{', '.join(template.reads)}]"
        )
    return problems


def pipeline_problems(root: Path, pipeline: PipelineFile, known_: Known) -> list[str]:
    """Every reason one well-formed pipeline is refused; empty when it is sound."""
    rel = f"{PIPELINES_DIR}/{pipeline.name}.toml"
    problems: list[str] = []
    ids = [s.id for s in pipeline.stages]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"{rel}: stage id {dup!r} is used twice")
    for name in pipeline.entry_contracts:
        try:
            handoff.load_contract(root, name)
        except Bad as e:
            problems.append(f"{rel}: entry {e}")
    for stage in pipeline.stages:
        for dep in stage.depends_on:
            if dep == stage.id:
                problems.append(f"{rel}: stage {stage.id} depends on itself")
            elif dep not in ids:
                problems.append(
                    f"{rel}: stage {stage.id} depends on {dep!r}, not a stage here"
                )
    if problems:
        return problems
    cycle = find_cycle(pipeline.stages)
    if cycle is not None:
        return [f"{rel}: depends_on has a cycle: {' -> '.join(cycle)}"]
    up = ancestors(pipeline.stages)
    by_id = {s.id: s for s in pipeline.stages}
    for stage in pipeline.stages:
        problems += stage_problems(root, pipeline, stage, up[stage.id], known_)
        if stage.kind == "effect" and not any(
            by_id[a].kind == "gate" for a in up[stage.id]
        ):
            problems.append(
                f"{rel}: stage {stage.id}: an effect runs only after a gate stage "
                "passed, and none is upstream"
            )
    return problems


def enabled(root: Path, problems: list[str]) -> tuple[str, ...]:
    """The pipelines the knob file enables, or none when it cannot be read."""
    try:
        data = tomllib.loads((root / KNOBS_FILE).read_text(encoding="utf-8"))
        return Knobs.model_validate(data).pipelines.enabled
    except FileNotFoundError:
        problems.append(f"{KNOBS_FILE}: missing")
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        problems.append(f"{KNOBS_FILE}: unreadable, so no pipeline is enabled: {e}")
    return ()


def check_pipelines(root: Path, names: Sequence[str] = ()) -> list[str]:
    """Every pipeline file, or the named ones, and every one the knob file enables."""
    problems: list[str] = []
    known_ = known(root, problems)
    on = enabled(root, problems)
    present = pipeline_files(root)
    for name in on:
        if name not in present and (not names or name in names):
            problems.append(
                f"{PIPELINES_DIR}/{name}.toml: missing, and [pipelines] enabled "
                "names it"
            )
    for name in names or present:
        try:
            pipeline = read_pipeline(root, name)
        except Bad as e:
            problems += str(e).splitlines()
            continue
        problems += pipeline_problems(root, pipeline, known_)
    return problems


# ---------------------------------------------------------------- the plan


def _gate_text(gate: Gate) -> str:
    text = " ".join([*gate.argv, *(f"<{a}>" for a in gate.args_from)])
    return f"{text} (waits on {gate.waits_on})" if gate.waits_on else text


def plan_rows(pipeline: PipelineFile) -> list[dict[str, Any]]:
    """The stages in the order a run takes them, as data; nothing runs."""
    rows = []
    for i, stage in enumerate(order(pipeline.stages), start=1):
        who = {
            "agent": stage.role or "",
            "gate": "deterministic, no model",
            "effect": "the trusted runner",
            "human": "the owner",
        }[stage.kind]
        if stage.seats:
            who += f" ({stage.seats})"
        if stage.provider:
            who += ", other provider"
        rows.append(
            {
                "step": i,
                "id": stage.id,
                "kind": stage.kind,
                "who": who,
                "after": list(stage.depends_on),
                "when": stage.when,
                "reads": list(stage.reads),
                "writes": stage.writes,
                "template": stage.template,
                "skills": list(stage.skills),
                "gates": [_gate_text(g) for g in stage.gates],
                "effects": list(stage.effects),
                "asks": stage.asks,
                "on_fail": stage.on_fail,
            }
        )
    return rows


def plan_text(pipeline: PipelineFile) -> str:
    entry = ", ".join(pipeline.entry_contracts) or "nothing"
    lines = [f"{pipeline.name}: {pipeline.description}", f"enters with: {entry}"]
    for row in plan_rows(pipeline):
        head = f"{row['step']:>2}. {row['id']}  [{row['kind']}: {row['who']}]"
        if row["when"]:
            head += f"  only when {row['when']}, else skipped"
        lines.append(head)
        if row["after"]:
            lines.append(f"      after   {', '.join(row['after'])}")
        if row["writes"] or row["reads"]:
            reads = ", ".join(row["reads"]) or "nothing"
            lines.append(f"      reads   {reads} -> writes {row['writes'] or '-'}")
        if row["template"]:
            lines.append(f"      prompt  templates/{row['template']}")
        if row["skills"]:
            lines.append(f"      skills  {', '.join(row['skills'])}")
        for gate in row["gates"]:
            lines.append(f"      gate    {gate}")
        if row["effects"]:
            lines.append(f"      effects {', '.join(row['effects'])}")
        if row["asks"]:
            lines.append(f"      asks    {row['asks']}")
        if row["on_fail"]:
            lines.append(f"      on fail {row['on_fail']}")
    lines.append("nothing ran: tac run lands with the runner (M3)")
    return "\n".join(lines) + "\n"


def load_plan(root: Path, name: str) -> PipelineFile:
    """A pipeline for planning, refused with every reason unless it checks clean."""
    problems = check_pipelines(root, [name])
    if problems:
        raise Bad("\n".join(problems))
    return read_pipeline(root, name)
