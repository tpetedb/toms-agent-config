"""Acceptance 8's tools: the coverage manifest, the dev proof and the live probes.

`tac proof coverage` must refuse a condition without a map entry and a test id
that does not collect; `tac proof dev` must report a live probe it cannot run as
skipped with its reason, never passed without a verified runner receipt, and
`--require-live` must turn a skip into a failure. Both bind their JSON to HEAD
and refuse a dirty tree unless told otherwise. The runs here use fixture
checkouts and a stand-in for pytest, never the real suite, so nothing recurses.
"""

from __future__ import annotations

import json
import tomllib
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tac import doctor, proof
from tac.cli import cli
from tac.config import load_config
from tac.config_schema import LiveProbeKind, OfflineProbeKind
from tac.doctor import Status
from tac.probes import LIVE_OBSERVATIONS, NotYetLive, observe
from tac.receipts import Binding, sign
from tac.runner import (
    ProbesFile,
    RunnerError,
    controller_store,
    create_key,
    ensure_store,
    open_runner,
)
from tests._gitrepo import commit_all, git, make_repo, short_dir, write
from tests._syncproject import copy_project, replace_in

REPO = Path(__file__).resolve().parents[1]
PASSING = "tests/test_fixture.py::test_ok"


# ---------------------------------------------------------------- stand-ins


@dataclass
class FakePytest(proof.Pytest):
    """Collects what it is told and writes a JUnit report with the outcomes it
    is given, so the proof's own logic is what the test exercises."""

    collectable: set[str] = field(default_factory=set)
    outcomes: Mapping[str, str] = field(default_factory=dict)
    ran: list[list[str]] = field(default_factory=list)

    def collect(self, files: Sequence[str]) -> set[str]:
        return {c for c in self.collectable if c.split("::")[0] in files}

    def run(self, ids: Sequence[str], junit: Path) -> tuple[int, str]:
        self.ran.append(list(ids))
        cases = []
        for node, outcome in self.outcomes.items():
            path, _, name = node.partition("::")
            classname = path.removesuffix(".py").replace("/", ".")
            inner = {"failed": "<failure/>", "skipped": "<skipped/>"}.get(outcome, "")
            cases.append(
                f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'
            )
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(
            "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>",
            encoding="utf-8",
        )
        failed = any(o != "passed" for o in self.outcomes.values())
        return (1 if failed else 0), ""


def committed_project(root: Path) -> Path:
    """A copy of this project's configuration, committed, with the test files
    the fixture map names."""
    copy_project(root)
    write(root, "tests/test_fixture.py", "def test_ok():\n    pass\n")
    write(root, "tests/test_bypass.py", "def test_a():\n    pass\n")
    write(root, "tests/test_mutation.py", "def test_b():\n    pass\n")
    write(root, ".gitignore", "dev/out/\n")
    git(root, "init", "-q")
    commit_all(root, "base")
    return root


def full_map(root: Path, skip: str = "", extra: str = "") -> str:
    """A map with one passing test per derived condition, less `skip`."""
    lines = ["schema_version = 1", ""]
    for condition in proof.conditions(load_config(root), root):
        if condition.id == skip:
            continue
        lines += [
            "[[entries]]",
            f'condition = "{condition.id}"',
            f'tests = ["{PASSING}"]',
            "",
        ]
    return "\n".join(lines) + extra


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return committed_project(tmp_path / "project")


def put_map(root: Path, text: str) -> None:
    write(root, proof.MAP_FILE, text)
    commit_all(root, "map")


# ---------------------------------------------------------------- the conditions


def test_the_shipped_configuration_derives_every_family() -> None:
    found = [c.id for c in proof.conditions(load_config(REPO), REPO)]
    assert 60 <= len(found) <= 150
    assert len(found) == len(set(found))
    families = {c.split(".")[0] for c in found}
    assert families == {
        "hook",
        "pipeline",
        "handoff",
        "contract",
        "memory",
        "human",
        "layer",
        "doctor",
        "condition",
        "bypass",
    }
    assert "hook.secret-read.claude.deny" in found
    assert "hook.secret-read.codex.deny" not in found, "not wired on Codex"
    assert "pipeline.order.gate.work-repair" in found
    assert {f"condition.C{n}" for n in range(1, 7)} <= set(found)
    assert {f"doctor.{c.name}" for c in doctor.CHECKS} <= set(found)


def test_a_new_hook_check_is_a_new_condition(tmp_path: Path) -> None:
    """Derived from the configuration, never from a hand list."""
    root = copy_project(tmp_path / "project")
    replace_in(
        root / ".agents/config/hooks.toml",
        "# ---- git:",
        '[checks.mystery]\nevent = "PreToolUse"\nkind = "deny"\n'
        'claude = "Bash"\ncodex = ""\n\n# ---- git:',
    )
    found = {c.id for c in proof.conditions(load_config(root), root)}
    assert "hook.mystery.claude.deny" in found
    assert "hook.mystery.codex.deny" not in found


def test_the_shipped_map_covers_every_condition_with_tests_that_collect() -> None:
    """The map in the repository is held to the configuration in every run, so a
    new condition without an entry fails here before coverage-proof runs."""
    cmap = proof.load_map(REPO / proof.MAP_FILE)
    ids = sorted({t for e in cmap.entries for t in e.tests})
    collected = proof.Pytest(REPO).collect(proof.test_files(ids))
    derived = proof.conditions(load_config(REPO), REPO)
    assert proof.map_problems(derived, cmap, collected) == []


# ---------------------------------------------------------------- the map


def test_a_condition_without_an_entry_is_refused_and_named(project: Path) -> None:
    put_map(project, full_map(project, skip="memory.search"))
    result = proof.coverage(project, tests=FakePytest(project, collectable={PASSING}))
    assert result.code == 1
    assert "coverage: memory.search: no entry in dev/coverage/map.toml" in result.lines
    assert not (project / proof.COVERAGE_JSON).exists(), "refused before any run"


def test_a_test_id_that_does_not_collect_is_refused(project: Path) -> None:
    missing = "tests/test_fixture.py::test_gone"
    text = full_map(project).replace(f'["{PASSING}"]', f'["{missing}"]', 1)
    put_map(project, text)
    result = proof.coverage(project, tests=FakePytest(project, collectable={PASSING}))
    assert result.code == 1
    assert any(f"{missing} does not collect" in line for line in result.lines)


def test_an_entry_for_no_condition_and_a_duplicate_are_refused(project: Path) -> None:
    extra = (
        '[[entries]]\ncondition = "hook.nothing.claude.deny"\n'
        f'tests = ["{PASSING}"]\n\n[[entries]]\ncondition = "layer.1"\n'
        f'tests = ["{PASSING}"]\n'
    )
    put_map(project, full_map(project, extra=extra))
    result = proof.coverage(project, tests=FakePytest(project, collectable={PASSING}))
    assert result.code == 1
    assert (
        "coverage: hook.nothing.claude.deny: no such condition in the configuration"
        in result.lines
    )
    assert "coverage: layer.1: more than one entry" in result.lines


def test_a_live_entry_must_ask_for_what_its_probes_need() -> None:
    entry = proof.Entry(
        condition="condition.C6", probe="C6", requires=("runner-key",), live=True
    )
    cmap = proof.CoverageMap(schema_version=1, entries=(entry,))
    problems = proof.map_problems([proof.Condition("condition.C6", "x")], cmap, set())
    assert problems and "requires" in problems[0] and "keychain" in problems[0]
    with pytest.raises(ValueError, match="live = true goes with a probe"):
        proof.Entry(condition="x", tests=(PASSING,), live=True)
    with pytest.raises(ValueError, match="names what it requires"):
        proof.Entry(condition="x", probe="C1", live=True)


def test_a_bare_id_passes_only_when_every_parameter_did() -> None:
    outcomes: dict[str, proof.Outcome] = {
        "t.py::x[a]": "passed",
        "t.py::x[b]": "skipped",
    }
    assert proof.outcome_of("t.py::x", outcomes) == "skipped"
    assert proof.outcome_of("t.py::x[a]", outcomes) == "passed"
    assert proof.outcome_of("t.py::y", outcomes) == "missing"


# ---------------------------------------------------------------- coverage runs


def test_coverage_binds_its_json_to_head_and_fails_a_failed_test(project: Path) -> None:
    put_map(project, full_map(project))
    ok = FakePytest(project, collectable={PASSING}, outcomes={PASSING: "passed"})
    result = proof.coverage(project, tests=ok)
    assert result.code == 0, result.lines
    report = json.loads((project / proof.COVERAGE_JSON).read_text("utf-8"))
    assert report["revision"] == git(project, "rev-parse", "HEAD")
    assert report["tree_clean"] is True
    assert report["failed"] == [] and report["passed"] == len(report["conditions"])
    bad = FakePytest(project, collectable={PASSING}, outcomes={PASSING: "skipped"})
    result = proof.coverage(project, tests=bad)
    assert result.code == 1
    assert "FAIL layer.1" in result.lines


def test_coverage_refuses_a_dirty_tree_unless_allowed(project: Path) -> None:
    put_map(project, full_map(project))
    write(project, "README-extra.md", "uncommitted\n")
    tests = FakePytest(project, collectable={PASSING}, outcomes={PASSING: "passed"})
    with pytest.raises(proof.ProofError, match="uncommitted"):
        proof.coverage(project, tests=tests)
    result = proof.coverage(project, tests=tests, allow_dirty=True)
    assert result.code == 0
    report = json.loads((project / proof.COVERAGE_JSON).read_text("utf-8"))
    assert report["tree_clean"] is False


# ---------------------------------------------------------------- live probes


def test_live_probes_cover_every_harness_place_session_and_condition() -> None:
    c1c6 = [p for p in proof.LIVE_PROBES if p.condition in ("C1", "C6")]
    assert len(c1c6) == 2 * 3 * 3 * (len(proof.C1_KINDS) + len(proof.C6_KINDS))
    assert len({p.name for p in proof.LIVE_PROBES}) == len(proof.LIVE_PROBES)
    for probe in proof.LIVE_PROBES:
        assert probe.kind in typing.get_args(LiveProbeKind)
        assert probe.receipt == "probe" and probe.requires
    github = {p.name: p.requires for p in proof.LIVE_PROBES if p.harness is None}
    assert github == {
        "bypass.ruleset-owner-review": ("github-identity",),
        "bypass.push-required-checks": ("github-identity",),
    }


NOTHING_DONE = proof.Facts(doctor={}, answered=frozenset())


def test_an_unmet_requirement_is_skipped_with_the_step_it_waits_on() -> None:
    [probe] = proof.probe_group("C6.keychain-key.claude.root.fresh")
    [result] = proof.judge_live([probe], NOTHING_DONE, None, None)
    assert result.status == "skipped"
    assert "runner-key" in result.reason and "S5 pending" in result.reason
    assert "keychain" in result.reason and "Q24 pending" in result.reason
    assert (
        "S3 pending" in result.reason
        and "doctor trust-claude: not checked" in result.reason
    )


def test_a_codex_probe_skips_on_the_pause_and_asks_nothing_else() -> None:
    [probe] = proof.probe_group("C1.missing-token.codex.worktree.resumed")
    [result] = proof.judge_live([probe], NOTHING_DONE, None, None)
    assert result.status == "skipped"
    assert result.reason == (
        f"codex-resumed (Codex is paused by the owner): answer {proof.LIVE_STEP} "
        "once Codex is resumed"
    )


def met_facts(live: bool) -> proof.Facts:
    doctor_ok = {
        n: "pass" for needs in proof.REQUIREMENTS.values() for n in needs.doctor
    }
    answered = frozenset(
        i for needs in proof.REQUIREMENTS.values() for i in needs.items
    )
    return proof.Facts(doctor_ok, answered, live=live, harness="claude")


def probe_receipt(key: Ed25519PrivateKey, probe: str, matched: bool) -> str:
    binding = Binding(
        repository="example/demo",
        revision="a" * 40,
        run_id="probe-claude",
        stage=f"probe.{probe}",
        policy_hash="sha256:" + "0" * 64,
    )
    return sign(key, "probe", binding, {"probe": probe, "matched": matched}).to_json()


def test_a_synthetic_answer_never_passes_a_live_probe() -> None:
    """Every requirement answered, and still: without --live it is skipped, and
    with --live only a receipt signed by the trusted key for this very probe,
    saying it matched, passes it."""
    [probe] = proof.probe_group("C1.hook-timeout.claude.subdir.headless")
    trusted, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    [skipped] = proof.judge_live([probe], met_facts(live=False), None, None)
    assert skipped.status == "skipped" and "--live" in skipped.reason

    def judged(text: str) -> proof.LiveResult:
        [result] = proof.judge_live(
            [probe], met_facts(live=True), lambda _h, _p: text, trusted.public_key
        )
        return result

    assert judged("{}").status == "failed"
    assert judged(probe_receipt(other, probe.name, True)).status == "failed"
    assert judged(probe_receipt(trusted, "C1.other", True)).status == "failed"
    assert judged(probe_receipt(trusted, probe.name, False)).status == "failed"
    assert judged(probe_receipt(trusted, probe.name, True)).status == "passed"
    [none] = proof.judge_live([probe], met_facts(live=True), None, None)
    assert none.status == "skipped"


def test_observe_is_not_yet_live_without_live_or_with_an_unmet_requirement() -> None:
    with pytest.raises(NotYetLive, match="--live"):
        observe("p", "hook-timeout", "claude", "root", "fresh", live=False, unmet=())
    with pytest.raises(NotYetLive, match="S5 pending"):
        observe(
            "p",
            "hook-timeout",
            "claude",
            "root",
            "fresh",
            live=True,
            unmet=["S5 pending"],
        )
    seen = observe("p", "hook-timeout", "claude", "root", "fresh", live=True, unmet=())
    assert seen.expect == LIVE_OBSERVATIONS["hook-timeout"][1]
    with pytest.raises(ValueError, match="not a live probe kind"):
        observe("p", "version", "claude", "root", "fresh", live=True, unmet=())


def test_every_live_kind_is_described_and_the_runner_refuses_it(tmp_path: Path) -> None:
    assert set(LIVE_OBSERVATIONS) == set(typing.get_args(LiveProbeKind))
    root = tmp_path / "demo"
    make_repo(root)
    write(
        root,
        ".agents/config/probes.toml",
        'schema_version = 1\n\n[probes.spawn]\ndescription = "C1"\n'
        'kind = "spawn-missing-token"\nexpect_exit = 2\n',
    )
    commit_all(root, "a live probe listed")
    with short_dir() as state:
        environ = {"TAC_STATE_HOME": str(state)}
        create_key(ensure_store(controller_store(root, environ)))
        runner = open_runner(root, environ, search_path="/usr/bin:/bin")
        request = {"op": "probe", "harness": "claude", "probe": "spawn", "run_id": "p"}
        with pytest.raises(RunnerError, match="spawn is not live yet"):
            runner.handle(request)


def test_the_shipped_probes_toml_names_every_offline_kind() -> None:
    """The comment on `kind` names what probes.toml may use today; the live
    kinds join it with the follow-up config that lists them."""
    text = (REPO / ".agents/config/probes.toml").read_text("utf-8")
    kinds = [line for line in text.splitlines() if line.startswith("kind = ")]
    assert kinds
    for line in kinds:
        for kind in typing.get_args(OfflineProbeKind):
            assert kind in line
    shipped = ProbesFile.model_validate(tomllib.loads(text))
    assert {p.kind for p in shipped.probes.values()} <= set(
        typing.get_args(OfflineProbeKind)
    )


# ---------------------------------------------------------------- the dev proof


@dataclass
class Commands:
    summary: str = "default verify"
    ran: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        self.ran.append(list(argv))
        if list(argv) == ["just", "--summary"]:
            return 0, self.summary
        return 0, ""


def tools(root: Path, commands: Commands | None = None) -> proof.DevTools:
    outcomes = {
        "tests/test_bypass.py::test_a": "passed",
        "tests/test_mutation.py::test_b": "passed",
    }
    return proof.DevTools(
        run=commands or Commands(),
        tests=FakePytest(root, outcomes=outcomes),
        doctor=lambda _root, _codex: {},
    )


def test_dev_prints_a_skip_per_probe_and_binds_its_json_to_head(project: Path) -> None:
    commands = Commands()
    result = proof.dev(project, tools=tools(project, commands))
    assert result.code == 0, result.lines
    skips = [line for line in result.lines if line.startswith("SKIP live: ")]
    assert len(skips) == len(proof.LIVE_PROBES)
    assert "SKIP render: recipe lands with order 7" in result.lines
    assert [a[-1] for a in commands.ran[:3]] == [
        "dev/ledger",
        "verify",
        "gate-schema-drift",
    ]
    report = json.loads((project / proof.DEV_JSON).read_text("utf-8"))
    assert report["revision"] == git(project, "rev-parse", "HEAD")
    assert report["tree_clean"] is True
    assert report["bypass"] == {"passed": 1, "failed": 0, "skipped": 0}
    assert {r["status"] for r in report["live"]} == {"skipped"}


def test_require_live_exits_1_while_a_probe_is_skipped(project: Path) -> None:
    result = proof.dev(project, require_live=True, tools=tools(project))
    assert result.code == 1
    assert result.lines[-2].startswith("--require-live: ")


def test_dev_refuses_a_dirty_tree_unless_allowed(project: Path) -> None:
    write(project, "README-extra.md", "uncommitted\n")
    with pytest.raises(proof.ProofError, match="uncommitted"):
        proof.dev(project, tools=tools(project))
    result = proof.dev(project, allow_dirty=True, tools=tools(project))
    report = json.loads((project / proof.DEV_JSON).read_text("utf-8"))
    assert result.code == 0 and report["tree_clean"] is False


def test_dev_renders_the_diagram_once_the_recipe_exists(project: Path) -> None:
    commands = Commands(summary="default diagrams-render verify")
    result = proof.dev(project, tools=tools(project, commands))
    assert ["just", "diagrams-render"] in commands.ran
    assert "render: just diagrams-render exit 0" in result.lines


def test_a_failing_ledger_gate_fails_the_dev_proof(project: Path) -> None:
    def failing(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        return (3, "schema drift") if argv[-1] == "gate-schema-drift" else (0, "")

    result = proof.dev(
        project,
        tools=proof.DevTools(
            run=failing, tests=tools(project).tests, doctor=lambda _r, _c: {}
        ),
    )
    assert result.code == 1
    assert "ledger gate-schema-drift: exit 3" in result.lines


def test_live_needs_a_harness(project: Path) -> None:
    with pytest.raises(proof.ProofError, match="--harness"):
        proof.dev(project, live=True, tools=tools(project))


def test_the_cli_lists_the_conditions() -> None:
    done = CliRunner().invoke(cli, ["proof", "coverage", "--list", "--root", str(REPO)])
    assert done.exit_code == 0, done.output
    assert "condition.C1" in done.output.splitlines()


# ---------------------------------------------------------------- doctor gaps
# Three doctor checks no other test held; coverage-proof found them.


def test_doctor_uv_on_path_passes_with_uv_and_fails_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/opt/uv")
    assert doctor.check_uv(REPO)[0] is Status.PASS
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    status, detail = doctor.check_uv(REPO)
    assert status is Status.FAIL and "bootstrap.sh" in detail


def test_doctor_agents_lib_names_a_missing_stamped_file(tmp_path: Path) -> None:
    lib = tmp_path / ".agents" / "lib" / "tac"
    write(tmp_path, ".agents/lib/tac/pyproject.toml", "[project]\n")
    status, detail = doctor.check_agents_lib(tmp_path)
    assert status is Status.FAIL and "src/tac/__init__.py" in detail
    write(tmp_path, ".agents/lib/tac/src/tac/__init__.py", "")
    assert doctor.check_agents_lib(tmp_path)[0] is Status.PASS
    assert lib.is_dir()


def test_doctor_agents_venv_needs_an_interpreter(tmp_path: Path) -> None:
    status, detail = doctor.check_agents_venv(tmp_path)
    assert status is Status.FAIL and "bootstrap.sh" in detail
    write(tmp_path, ".agents/.venv/bin/python", "")
    assert doctor.check_agents_venv(tmp_path)[0] is Status.PASS


def test_a_live_probe_named_by_several_conditions_is_reported_once() -> None:
    entries = [
        proof.Entry(
            condition=c,
            tests=(PASSING,),
            probe="bypass.ruleset-owner-review",
            requires=("github-identity",),
            live=True,
        )
        for c in ("layer.3b", "bypass.ruleset-owner-review")
    ]
    [skip] = proof.live_skips(entries)
    assert skip["conditions"] == ["layer.3b", "bypass.ruleset-owner-review"]
    assert "github-identity" in skip["reason"]
