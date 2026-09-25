"""The GitHub structure of section 8: issue forms that GitHub will accept, with the
fields automation reads and only labels that exist, one labels file the
configuration agrees with, a pull request template with the tag line and the
acceptance section, and workflows actionlint passes."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tac.github_cli
from tac.cli import cli
from tac.forms import FORMS_DIR, PR_TEMPLATE, form_problems, lint, load_forms
from tac.github import REQUIRED_CHECKS
from tac.labels import (
    LABELS_FILE,
    Label,
    apply_labels,
    labels_problems,
    load_labels,
    parse_labels,
    plan_labels,
    team_ids,
)
from tac.work import ORDER_LINE, Bad
from tests._github_fixtures import REPO as SLUG
from tests._github_fixtures import FixtureTransport, Reply
from tests._syncproject import replace_in

REPO = Path(__file__).resolve().parents[1]
# The fields automation and the chief read from each form, by id; each is required.
REQUIRED_IDS = {
    "bug": {"what-happened", "expected", "reproduce", "version", "harness"},
    "feature": {"problem", "proposal"},
    "work-order": {"order-id", "team", "what-and-why", "owns", "acceptance"},
    "decision": {"question", "context", "options", "recommendation", "loosens-policy"},
    "human-question": {"item-id", "kind", "topic", "question", "recommendation"},
}
# Approvals and failed handoffs are written by tac, never asked through a form.
TOOL_ONLY_SECTIONS = {"approvals", "escalations"}


def forms() -> dict[str, Any]:
    found, problems = load_forms(REPO)
    assert problems == []
    return found


def element(form: dict[str, Any], eid: str) -> dict[str, Any]:
    return next(e for e in form["body"] if e.get("id") == eid)


def declared() -> tuple[Label, ...]:
    return load_labels(REPO)


# ---- the shipped files


def test_the_shipped_github_files_hold() -> None:
    assert lint(REPO) == []


def test_every_form_the_config_lists_is_there_and_nothing_else() -> None:
    github = tomllib.loads((REPO / ".agents/config/github.toml").read_text("utf-8"))
    assert set(forms()) == set(github["issues"]["forms"]) == set(REQUIRED_IDS)


@pytest.mark.parametrize("stem", sorted(REQUIRED_IDS))
def test_each_form_parses_as_an_issue_form(stem: str) -> None:
    problems, elements = form_problems(
        f"{FORMS_DIR}/{stem}.yml", forms()[stem], declared()
    )
    assert problems == []
    assert REQUIRED_IDS[stem] <= set(elements)


@pytest.mark.parametrize("stem", sorted(REQUIRED_IDS))
def test_the_fields_automation_reads_are_required(stem: str) -> None:
    form = forms()[stem]
    for eid in REQUIRED_IDS[stem]:
        assert element(form, eid).get("validations", {}).get("required") is True, eid


@pytest.mark.parametrize("stem", sorted(REQUIRED_IDS))
def test_each_form_sets_one_type_label_that_exists(stem: str) -> None:
    names = {label.name for label in declared()}
    wanted = forms()[stem]["labels"]
    assert set(wanted) <= names
    assert len([w for w in wanted if w.startswith("type/")]) == 1


def test_team_dropdowns_offer_exactly_the_teams() -> None:
    teams = list(team_ids(REPO))
    for stem in ("work-order", "feature"):
        assert element(forms()[stem], "team")["attributes"]["options"] == teams


def test_the_owner_question_mirrors_a_todo_human_item() -> None:
    todo = tomllib.loads((REPO / ".human/todo.toml").read_text("utf-8"))
    sections = [s["id"] for s in todo["sections"]]
    form = forms()["human-question"]
    topics = element(form, "topic")["attributes"]["options"]
    assert topics == [s for s in sections if s not in TOOL_ONLY_SECTIONS]
    # An approval is the owner's own review or a host-signed record, never a form.
    assert element(form, "kind")["attributes"]["options"] == ["question", "step"]


def test_the_bug_form_names_every_harness() -> None:
    knobs = tomllib.loads((REPO / ".agents/config.toml").read_text("utf-8"))
    harnesses = knobs["harnesses"]
    every = [*harnesses["enforced"], *harnesses["instructions_only"]]
    options = element(forms()["bug"], "harness")["attributes"]["options"]
    assert options == ["none", *every]


def test_blank_issues_are_off() -> None:
    chooser = yaml.safe_load((REPO / FORMS_DIR / "config.yml").read_text("utf-8"))
    assert chooser["blank_issues_enabled"] is False


# ---- the form checker refuses what GitHub would


def valid_form() -> dict[str, Any]:
    return copy.deepcopy(forms()["work-order"])


def problems_of(form: dict[str, Any]) -> list[str]:
    return form_problems("x.yml", form, declared())[0]


def test_a_label_missing_from_the_labels_file_is_refused() -> None:
    form = valid_form()
    form["labels"] = ["type/order", "enhancement"]
    assert problems_of(form) == [
        "x.yml: label 'enhancement' is not in .github/labels.yml"
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda f: f["body"][1].update(id="team"), "id team is used twice"),
        (lambda f: f["body"][1].update(id="has space"), "id is letters"),
        (lambda f: f["body"][0].update(id="intro"), "markdown element takes no id"),
        (lambda f: f["body"][1]["attributes"].pop("label"), "needs attributes"),
        (
            lambda f: f["body"][1]["attributes"].update(render="sh"),
            "takes no attributes",
        ),
        (lambda f: f["body"][1].update(type="select"), "is not one of"),
        (lambda f: f["body"][2]["attributes"].update(options=[]), "non-empty list"),
        (lambda f: f["body"][2]["attributes"].update(options=["a", "a"]), "distinct"),
        (lambda f: f["body"][2]["attributes"].update(default=9), "index of an option"),
        (lambda f: f["body"][1].update(validations={"min": 1}), "takes no validations"),
        (lambda f: f.pop("description"), "missing description"),
        (lambda f: f.update(template="x"), "unknown top-level key"),
        (lambda f: f.update(body=[f["body"][0]]), "not markdown"),
        (
            lambda f: f["body"][2]["attributes"].update(label="Order id"),
            "is used twice",
        ),
    ],
)
def test_a_malformed_form_is_named(change: Any, expected: str) -> None:
    form = valid_form()
    change(form)
    found = problems_of(form)
    assert any(expected in p for p in found), found


def test_lint_names_a_form_the_config_does_not_list(tmp_path: Path) -> None:
    root = github_copy(tmp_path)
    (root / FORMS_DIR / "extra.yml").write_text(
        (REPO / FORMS_DIR / "feature.yml")
        .read_text("utf-8")
        .replace("name: Feature", "name: Extra"),
        encoding="utf-8",
    )
    assert any("[issues] forms says" in p for p in lint(root))


def test_lint_names_a_chooser_that_allows_blank_issues(tmp_path: Path) -> None:
    root = github_copy(tmp_path)
    replace_in(
        root / FORMS_DIR / "config.yml",
        "blank_issues_enabled: false",
        "blank_issues_enabled: true",
    )
    assert any("blank_issues_enabled" in p for p in lint(root))


def test_the_lint_command_exits_1_naming_the_problem(tmp_path: Path) -> None:
    root = github_copy(tmp_path)
    replace_in(root / FORMS_DIR / "bug.yml", '["type/bug"]', '["bug"]')
    result = CliRunner().invoke(cli, ["github", "lint", "--root", str(root)])
    assert result.exit_code == 1
    assert "label 'bug' is not in .github/labels.yml" in result.output


# ---- the labels file


def github_copy(root: Path) -> Path:
    """The files lint reads, in a folder a test may change."""
    for rel in (
        ".github",
        ".agents/config/github.toml",
        ".agents/config/teams.toml",
    ):
        source, target = REPO / rel, root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.write_bytes(source.read_bytes())
    return root


def test_the_labels_agree_with_the_configuration() -> None:
    labels = declared()
    assert labels_problems(REPO, labels) == []
    github = tomllib.loads((REPO / ".agents/config/github.toml").read_text("utf-8"))
    families = {label.family for label in labels}
    assert families == set(github["labels"]["families"])
    states = [label.name for label in labels if label.family == "state"]
    assert states == [f"state/{s}" for s in github["labels"]["states"]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- {name: a/b, color: '000000'}\n- {name: A/B, color: '111111'}", "twice"),
        ("- {name: a/b, color: 'black'}", "six hex digits"),
        ("- {name: a/b, color: '000000', aliases: [x]}", "unknown keys"),
        (f"- {{name: a/b, color: '000000', description: {'x' * 101}}}", "at most 100"),
        ("- {name: ' a/b', color: '000000'}", "spaces at its ends"),
        ("[]", "non-empty list"),
        ("- [a]", "a mapping"),
    ],
)
def test_a_malformed_labels_file_is_refused(text: str, expected: str) -> None:
    with pytest.raises(Bad, match=expected):
        parse_labels(text)


def test_a_colour_is_kept_as_the_api_answers_it() -> None:
    (label,) = parse_labels("- {name: a/b, color: '#ABCDEF'}")
    assert label.color == "abcdef"


def test_a_missing_team_label_is_named(tmp_path: Path) -> None:
    root = github_copy(tmp_path)
    labels = [label for label in declared() if label.name != "team/docs"]
    labels.append(Label("mystery", "000000"))
    found = labels_problems(root, labels)
    assert any("team/* is ['core', 'harness']" in p for p in found), found
    assert any("mystery is not <family>/<name>" in p for p in found), found


# ---- the plan and its application


def live(*items: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"name": n, "color": c, "description": d} for n, c, d in items]


def test_the_plan_creates_updates_keeps_and_never_deletes() -> None:
    wanted = (
        Label("type/bug", "d32f2f", "broken"),
        Label("type/order", "0088cc", "work"),
        Label("state/queued", "ffd500", "waiting"),
    )
    plan = plan_labels(
        wanted,
        live(
            ("type/bug", "D32F2F", "broken"),
            ("TYPE/ORDER", "0088cc", "work"),
            ("wontfix", "ffffff", ""),
        ),
    )
    assert plan.create == (wanted[2],)
    # A change of case alone is an update, since GitHub keys names without case.
    assert plan.update == ((wanted[1], "TYPE/ORDER"),)
    assert plan.same == ("type/bug",)
    assert plan.unmanaged == ("wontfix",)
    assert not plan.settled


def labels_transport(before: list[Any], after: list[Any]) -> FixtureTransport:
    page = f"repos/{SLUG}/labels?per_page=100&page=1"
    return FixtureTransport(
        {
            ("GET", page): [Reply(200, before), Reply(200, after)],
            ("POST", f"repos/{SLUG}/labels"): [Reply(201, {})],
            ("PATCH", f"repos/{SLUG}/labels/TYPE%2FORDER"): [Reply(200, {})],
        }
    )


def test_apply_creates_updates_reads_back_and_deletes_nothing() -> None:
    wanted = (Label("type/order", "0088cc", "work"), Label("type/bug", "d32f2f", ""))
    fixture = labels_transport(
        live(("TYPE/ORDER", "000000", ""), ("wontfix", "ffffff", "")),
        live(
            ("type/order", "0088cc", "work"),
            ("type/bug", "d32f2f", ""),
            ("wontfix", "ffffff", ""),
        ),
    )
    outcome = apply_labels(fixture, SLUG, wanted)
    assert outcome.ok, outcome.lines
    writes = fixture.writes()
    assert [(m, p) for m, p, _ in writes] == [
        ("POST", f"repos/{SLUG}/labels"),
        ("PATCH", f"repos/{SLUG}/labels/TYPE%2FORDER"),
    ]
    assert writes[1][2] == {
        "new_name": "type/order",
        "color": "0088cc",
        "description": "work",
    }
    assert "left wontfix (not declared)" in outcome.lines


def test_apply_fails_when_the_read_back_still_differs() -> None:
    wanted = (Label("type/bug", "d32f2f", ""),)
    fixture = labels_transport([], [])
    outcome = apply_labels(fixture, SLUG, wanted)
    assert not outcome.ok
    assert "still differ" in outcome.lines[-1]


def test_apply_reports_a_refused_write() -> None:
    fixture = labels_transport([], [])
    fixture.replies[("POST", f"repos/{SLUG}/labels")] = [Reply(403)]
    outcome = apply_labels(fixture, SLUG, (Label("type/bug", "d32f2f", ""),))
    assert not outcome.ok
    assert outcome.lines[-1].startswith("refused:")


def test_the_dry_run_reads_without_a_credential_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = labels_transport(live(("type/bug", "d32f2f", "stale")), [])
    fixture.authenticated = False
    monkeypatch.setattr(tac.github_cli, "AnonymousTransport", lambda: fixture)
    monkeypatch.setattr(tac.github_cli, "gh_transport", pytest.fail)
    result = CliRunner().invoke(
        cli, ["github", "labels", "--dry-run", "--repo", SLUG, "--root", str(REPO)]
    )
    assert result.exit_code == 0, result.output
    assert "update type/bug #d32f2f" in result.output
    assert "create type/order #0088cc" in result.output
    assert "dry run: nothing changed" in result.output
    assert fixture.writes() == []


def test_the_dry_run_offline_prints_the_declared_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureTransport(
        {("GET", f"repos/{SLUG}/labels?per_page=100&page=1"): [Reply(503)]}
    )
    monkeypatch.setattr(tac.github_cli, "AnonymousTransport", lambda: fixture)
    result = CliRunner().invoke(
        cli, ["github", "labels", "--repo", SLUG, "--root", str(REPO)]
    )
    assert result.exit_code == 0, result.output
    assert "could not read the labels" in result.output
    assert "state/landed #00d084" in result.output


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_apply_is_the_owners_step_and_refused_in_an_agent_session(
    marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(marker, "1")
    monkeypatch.setattr(tac.github_cli, "gh_transport", pytest.fail)
    result = CliRunner().invoke(
        cli, ["github", "labels", "--apply", "--repo", SLUG, "--root", str(REPO)]
    )
    assert result.exit_code != 0
    assert "refused inside an agent session" in result.output


def test_apply_from_the_owners_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    every = [label.body() for label in declared()]
    fixture = labels_transport(every, every)
    monkeypatch.setattr(tac.github_cli, "agent_session", lambda _env: None)
    monkeypatch.setattr(tac.github_cli, "gh_transport", lambda: fixture)
    result = CliRunner().invoke(
        cli, ["github", "labels", "--apply", "--repo", SLUG, "--root", str(REPO)]
    )
    assert result.exit_code == 0, result.output
    assert f"match {LABELS_FILE}" in result.output
    assert fixture.writes() == []


# ---- the pull request template


def test_the_pull_request_template_opens_with_the_tag_line() -> None:
    text = (REPO / PR_TEMPLATE).read_text("utf-8")
    first = text.splitlines()[0]
    assert first == "order: <id>"
    # Filled in, it is the line the order hook reads in a report.
    assert ORDER_LINE.search(first.replace("<id>", "tac-github-labels"))
    assert "\n## Acceptance\n" in text
    assert "just work-check <id>" in text
    assert "gh pr merge <N> --squash --match-head-commit <sha>" in text


# ---- the workflows


def workflows() -> dict[str, Any]:
    folder = REPO / ".github/workflows"
    return {p.name: yaml.safe_load(p.read_text("utf-8")) for p in folder.glob("*.yml")}


def test_actionlint_passes_every_workflow() -> None:
    actionlint = Path(sys.executable).parent / "actionlint"
    done = subprocess.run(
        [
            str(actionlint),
            *sorted(str(p) for p in (REPO / ".github/workflows").glob("*.yml")),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("name", sorted(workflows()))
def test_every_workflow_is_read_only_on_pull_request_and_pinned(name: str) -> None:
    flow = workflows()[name]
    triggers = flow[True]
    assert set(triggers) == {"pull_request"}
    assert flow["permissions"] == {"contents": "read"}
    for job in flow["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        for step in job["steps"]:
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40} ?", step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_the_macos_job_runs_the_seatbelt_tests_and_is_not_required() -> None:
    flow = workflows()["macos.yml"]
    (job,) = flow["jobs"].values()
    assert job["runs-on"].startswith("macos-")
    assert set(flow["jobs"]).isdisjoint(REQUIRED_CHECKS)
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pytest -q -rs --color=no tests/test_runner_skeleton.py" in run
    # Any skip fails the job: without sandbox-exec or just, the confinement
    # tests skip, and a skipped test proves nothing.
    assert 'grep -q "^SKIPPED"' in run
    source = (REPO / "tests/test_runner_skeleton.py").read_text("utf-8")
    assert 'reason="needs macOS sandbox-exec"' in source
    assert 'reason="needs just"' in source
    # just comes pinned by version and checksum, never from an unpinned source.
    install = next(s for s in job["steps"] if "JUST_SHA256" in s.get("env", {}))
    assert re.fullmatch(r"[0-9a-f]{64}", install["env"]["JUST_SHA256"])
    assert "shasum -a 256 -c" in install["run"]


def test_the_work_job_judges_from_the_base_revision() -> None:
    job = workflows()["ci.yml"]["jobs"]["work"]
    # The gates run in this order, each the base revision's copy (the fetch is
    # tests/test_ci_gates.py's to prove).
    runs = [step.get("run", "") for step in job["steps"]]
    gates = re.findall(r'^bash "\$RUNNER_TEMP/gates/([\w.-]+)"', "\n".join(runs), re.M)
    assert gates == [
        "ci_work_from_base.sh",
        "ci_config_from_base.sh",
        "ci_check_from_base.sh",
        "ci_verify_receipts.sh",
    ]
