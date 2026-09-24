"""The pipeline files and their checker: all five shipped pipelines are sound, and
each way a pipeline can be unsound is refused with a reason that names it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac import pipelines
from tac.cli import cli
from tac.config_schema import PipelineFile
from tac.pipelines import PIPELINES_DIR, check_pipelines, plan_rows, read_pipeline
from tac.tomldoc import document
from tac.work import Bad

REPO = Path(__file__).resolve().parents[1]
SHIPPED = ("order", "board", "review", "release", "retro")
# What the pipeline checker reads: the knob file and its tables, the skills, the
# contracts, the handoff templates and the justfile.
INPUTS = (
    ".agents/config.toml",
    ".agents/config",
    ".agents/skills",
    "contracts",
    "templates/handoffs",
    "templates/human",
    "justfile",
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of what the checker reads, so a test may break it freely."""
    base = tmp_path / "project"
    for rel in INPUTS:
        source, target = REPO / rel, base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return base


def edit(root: Path, name: str, old: str, new: str, count: int = 1) -> None:
    path = root / PIPELINES_DIR / f"{name}.toml"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= count, f"{old!r} not in {name}.toml"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def refused(root: Path, name: str = "order") -> str:
    problems = check_pipelines(root, [name])
    assert problems, f"{name} was expected to be refused"
    return "\n".join(problems)


# ---------------------------------------------------------------- the shipped five


def test_the_five_pipelines_are_the_ones_the_knob_file_enables() -> None:
    assert tuple(sorted(pipelines.pipeline_files(REPO))) == tuple(sorted(SHIPPED))
    problems: list[str] = []
    assert set(pipelines.enabled(REPO, problems)) == set(SHIPPED)
    assert problems == []


def test_all_five_pipelines_validate() -> None:
    assert check_pipelines(REPO) == []


@pytest.mark.parametrize("name", SHIPPED)
def test_each_pipeline_validates_on_its_own(name: str) -> None:
    assert check_pipelines(REPO, [name]) == []
    assert isinstance(read_pipeline(REPO, name), PipelineFile)


def test_tac_pipeline_check_passes_on_the_checkout() -> None:
    result = CliRunner().invoke(cli, ["pipeline", "check", "--root", str(REPO)])
    assert result.exit_code == 0, result.output
    assert "pipelines sound" in result.output
    for name in SHIPPED:
        assert name in result.output


def test_the_order_pipeline_has_its_intake_and_effect_stages() -> None:
    order = read_pipeline(REPO, "order")
    intake = next(s for s in order.stages if s.id == "intake")
    assert (intake.kind, intake.reads, intake.writes) == (
        "agent",
        ("issue",),
        "order-request",
    )
    assert order.entry_contracts == ("issue",)
    effects = [s for s in order.stages if s.kind == "effect"]
    assert [s.effects for s in effects] == [("commit", "push", "open-pr")]


def test_no_shipped_effect_is_an_owner_action() -> None:
    for name in SHIPPED:
        for stage in read_pipeline(REPO, name).stages:
            assert not {"merge", "release", "approve"} & set(stage.effects)


def test_every_gate_is_an_argv_array() -> None:
    for name in SHIPPED:
        text = (REPO / PIPELINES_DIR / f"{name}.toml").read_text()
        for doc in document(text).values():
            if doc.path.endswith(".argv"):
                assert doc.source.partition("=")[2].strip().startswith("[")


# ---------------------------------------------------------------- the plan


def test_plan_prints_the_stage_order_and_runs_nothing() -> None:
    result = CliRunner().invoke(cli, ["pipeline", "plan", "order", "--root", str(REPO)])
    assert result.exit_code == 0, result.output
    ids = ["intake", "specify", "build", "review", "signoff", "gate", "publish"]
    at = [result.output.index(f". {i}  [") for i in [*ids, "recap", "land"]]
    assert at == sorted(at)
    assert "only when cross_team, else skipped" in result.output
    assert "nothing ran" in result.output


def test_plan_as_json_puts_every_stage_after_what_it_depends_on() -> None:
    result = CliRunner().invoke(
        cli, ["pipeline", "plan", "board", "--json", "--root", str(REPO)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    seen: set[str] = set()
    for row in data["stages"]:
        assert set(row["after"]) <= seen, row["id"]
        seen.add(row["id"])
    decide = next(r for r in data["stages"] if r["id"] == "decide")
    assert decide["gates"] == ["just board-tally <run_id> (waits on M8)"]


def test_plan_keeps_the_file_order_between_independent_stages() -> None:
    rows = plan_rows(read_pipeline(REPO, "order"))
    assert [r["id"] for r in rows][:3] == ["intake", "specify", "build"]


def test_plan_refuses_an_unsound_pipeline_with_the_reason(root: Path) -> None:
    edit(root, "order", 'role = "builder"', 'role = "wizard"')
    result = CliRunner().invoke(cli, ["pipeline", "plan", "order", "--root", str(root)])
    assert result.exit_code != 0
    assert "role 'wizard' is unknown" in result.output


# ---------------------------------------------------------------- the refusals


def test_a_stage_without_an_envelope_is_refused(root: Path) -> None:
    edit(root, "order", 'writes = "order-request"', "")
    text = refused(root)
    assert "stage intake: no envelope" in text
    assert "no writes" in text


def test_a_stage_without_a_template_is_refused(root: Path) -> None:
    edit(root, "order", 'template = "handoffs/specify.md.j2"', "")
    assert "stage specify: no envelope" in refused(root)


def test_a_shell_string_gate_is_refused(root: Path) -> None:
    edit(root, "order", 'argv = ["just", "work-check"]', 'argv = "just work-check x"')
    text = refused(root)
    assert "stage build" in text
    assert "never a shell string" in text


def test_a_templated_gate_argument_is_refused(root: Path) -> None:
    edit(root, "order", '"work-review"]', '"work-review", "{{ order_id }}"]')
    assert "a gate is never a template" in refused(root)


def test_a_gate_that_is_not_just_or_our_script_is_refused(root: Path) -> None:
    edit(root, "order", 'argv = ["just", "work-accept"]', 'argv = ["bash", "-c"]')
    assert "argv[0] must be just or a script under src/tac/" in refused(root)


def test_a_gate_recipe_the_justfile_lacks_is_refused(root: Path) -> None:
    edit(root, "order", '"work-accept"]', '"work-acept"]')
    assert "the justfile has no recipe work-acept" in refused(root)


def test_a_waiting_gate_whose_recipe_exists_is_refused(root: Path) -> None:
    edit(root, "board", '"board-tally"]', '"work-check"]')
    assert "drop waits_on" in refused(root, "board")


def test_an_unknown_skill_is_refused(root: Path) -> None:
    edit(root, "order", 'skills = ["work-order"]', 'skills = ["no-such-skill"]')
    text = refused(root)
    assert "stage specify: skill 'no-such-skill' is unknown" in text
    assert ".agents/skills/no-such-skill/SKILL.md" in text


def test_an_unknown_role_is_refused(root: Path) -> None:
    edit(root, "order", 'role = "builder"', 'role = "wizard"')
    text = refused(root)
    assert "stage build: role 'wizard' is unknown" in text
    assert ".agents/config/roles/wizard.toml" in text


def test_a_cycle_is_refused_and_named(root: Path) -> None:
    edit(root, "order", 'role = "chief"', 'role = "chief"\ndepends_on = ["land"]')
    text = refused(root)
    assert "depends_on has a cycle: intake -> land -> recap -> publish" in text
    assert text.rstrip().endswith("-> intake")


def test_a_stage_depending_on_itself_is_refused(root: Path) -> None:
    edit(root, "order", 'depends_on = ["intake"]', 'depends_on = ["specify"]')
    assert "stage specify depends on itself" in refused(root)


def test_an_unknown_dependency_is_refused(root: Path) -> None:
    edit(root, "order", 'depends_on = ["intake"]', 'depends_on = ["intak"]')
    assert "depends on 'intak', not a stage here" in refused(root)


def test_a_duplicate_stage_id_is_refused(root: Path) -> None:
    edit(root, "order", 'id = "specify"', 'id = "intake"')
    assert "stage id 'intake' is used twice" in refused(root)


def test_a_read_no_upstream_stage_writes_is_refused(root: Path) -> None:
    edit(root, "order", 'reads = ["order-spec"]', 'reads = ["review"]')
    text = refused(root)
    assert "stage build: reads review, which no upstream stage writes" in text


def test_a_template_that_disagrees_with_the_stage_is_refused(root: Path) -> None:
    edit(root, "order", '"handoffs/build.md.j2"', '"handoffs/review.md.j2"')
    text = refused(root)
    assert "writes build-report, but handoffs/review.md.j2 writes review" in text
    assert "reads [order-spec], but handoffs/review.md.j2 reads [build-report]" in text


def test_the_repair_template_is_never_a_stage_template(root: Path) -> None:
    edit(root, "order", '"handoffs/build.md.j2"', '"handoffs/contract-repair.md.j2"')
    assert "is the repair pass" in refused(root)


def test_an_unknown_contract_is_refused(root: Path) -> None:
    edit(root, "order", 'entry_contracts = ["issue"]', 'entry_contracts = ["isue"]')
    assert "entry contract isue: no contracts/handoffs/isue.schema.json" in refused(
        root
    )


def test_an_ambiguous_read_must_be_bound(root: Path) -> None:
    edit(root, "board", 'bind = { position = "revise" }', "")
    text = refused(root, "board")
    assert "reads position, written by design, revise" in text
    assert "bind" in text


def test_a_bind_to_a_stage_that_does_not_write_it_is_refused(root: Path) -> None:
    edit(
        root,
        "board",
        'bind = { position = "revise" }',
        'bind = { position = "review" }',
    )
    assert "is not an upstream stage that writes position" in refused(root, "board")


def test_an_owner_action_as_an_effect_is_refused(root: Path) -> None:
    edit(root, "order", '"open-pr"]', '"open-pr", "merge"]')
    text = refused(root)
    assert "effect 'merge' is the owner's own action" in text


def test_an_effect_policy_does_not_name_is_refused(root: Path) -> None:
    edit(root, "order", '"open-pr"]', '"open-pr", "deploy"]')
    assert "effect 'deploy' is not one" in refused(root)


def test_an_effect_without_a_gate_upstream_is_refused(root: Path) -> None:
    edit(root, "order", 'depends_on = ["gate"]', 'depends_on = ["review"]')
    assert "an effect runs only after a gate stage passed" in refused(root)


def test_the_other_provider_only_on_a_review_stage(root: Path) -> None:
    edit(root, "order", 'role = "builder"', 'role = "builder"\nprovider = "other"')
    assert 'provider = "other" belongs only on a review stage' in refused(root)


def test_a_key_that_does_not_belong_to_the_kind_is_refused(root: Path) -> None:
    edit(root, "order", 'effects = ["commit"', 'role = "chief"\neffects = ["commit"')
    assert "stage publish: an effect stage has no role" in refused(root)


def test_a_director_stage_names_its_seats(root: Path) -> None:
    edit(root, "board", 'seats = "lead"', "")
    assert "a director stage names seats" in refused(root, "board")


def test_repair_belongs_to_an_agent_stage_only(root: Path) -> None:
    gate_stage = 'on_fail = "human"' + " " * 23 + "# repair-once-then-human | human"
    edit(root, "order", gate_stage + " | stop; a gate", 'on_fail = "stop" # a gate')
    edit(root, "order", 'on_fail = "stop"', 'on_fail = "repair-once-then-human"')
    assert "only an agent stage has one" in refused(root)


def test_an_unknown_key_is_refused(root: Path) -> None:
    edit(root, "order", "max_turns = 50", "max_turns = 50\nshell = true")
    text = refused(root)
    assert "stage build" in text and "unknown key shell" in text


def test_a_name_that_is_not_the_file_is_refused(root: Path) -> None:
    edit(root, "retro", 'name = "retro"', 'name = "review"')
    assert "name = 'review', the file says retro" in refused(root, "retro")


def test_an_enabled_pipeline_without_a_file_is_refused(root: Path) -> None:
    (root / PIPELINES_DIR / "retro.toml").unlink()
    text = "\n".join(check_pipelines(root))
    assert "retro.toml: missing, and [pipelines] enabled names it" in text


def test_the_cli_names_every_reason_and_exits_non_zero(root: Path) -> None:
    edit(root, "order", 'role = "builder"', 'role = "wizard"')
    edit(root, "order", 'skills = ["work-order"]', 'skills = ["nope"]')
    result = CliRunner().invoke(cli, ["pipeline", "check", "--root", str(root)])
    assert result.exit_code == 1
    assert "role 'wizard' is unknown" in result.output
    assert "skill 'nope' is unknown" in result.output


def test_read_pipeline_refuses_a_missing_file(root: Path) -> None:
    with pytest.raises(Bad, match="missing"):
        read_pipeline(root, "nothing")


# ---------------------------------------------------------------- the scanner


def test_a_nested_array_of_tables_belongs_to_its_latest_item() -> None:
    docs = document(
        "[[s]]\nid = 1  # a\n\n[[s.g]]\nx = 1  # b\n\n[[s.g]]\nx = 2  # c\n\n"
        "[s.r]\ny = 1  # d\n\n[[s]]\nid = 2  # e\n\n[[s.g]]\nx = 3  # f\n"
    )
    assert docs["s[0].g[1].x"].comment == "c"
    assert docs["s[0].r.y"].comment == "d"
    assert docs["s[1].g[0].x"].comment == "f"
