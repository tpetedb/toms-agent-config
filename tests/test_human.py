"""The owner's queue: items in .human/approvals/, the page rendered from them,
answers recorded under them, and recaps (design section 7)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from jsonschema import Draft7Validator

from tac import human
from tac.cli import cli
from tac.tomldoc import document
from tests._gitrepo import make_repo

REPO = Path(__file__).resolve().parents[1]
AT = "2026-09-25T12:00:00Z"
SHA = "6dcb09b5b57875f334f61aebed695e2e4193db5e"


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository with the page's fixed text, the recap contract and no items."""
    root = tmp_path / "demo"
    make_repo(root)
    (root / ".human").mkdir()
    shutil.copy(REPO / human.TODO_SOURCE, root / human.TODO_SOURCE)
    contract = root / human.RECAP_CONTRACT
    contract.parent.mkdir(parents=True)
    shutil.copy(REPO / human.RECAP_CONTRACT, contract)
    return root


def ask(root: Path, *extra: str) -> tuple[int, str]:
    return run("human", "ask", "--root", str(root), "--at", AT, *extra)


def question(root: Path, *extra: str) -> tuple[int, str]:
    return ask(
        root,
        "--topic",
        "governance",
        "--title",
        "release cadence",
        "--question",
        "Cut a release every week, or when a milestone lands?",
        "--option",
        "every week",
        "--option",
        "per milestone",
        "--recommendation",
        "per milestone.",
        "--blocks",
        "the release pipeline",
        "--waits",
        "M3",
        *extra,
    )


def item_file(root: Path, item_id: str) -> dict:
    return json.loads((root / ".human/approvals" / f"{item_id}.json").read_text())


# ---------------------------------------------------------------- the shipped page


def test_the_shipped_page_is_its_own_render() -> None:
    code, out = run("human", "render", "--root", str(REPO), "--check")
    assert code == 0, out


def test_every_shipped_item_meets_the_committed_contract() -> None:
    schema = json.loads((REPO / human.ITEM_CONTRACT).read_text("utf-8"))
    Draft7Validator.check_schema(schema)
    files = sorted((REPO / ".human/approvals").glob("*.json"))
    assert files, "the queue is empty"
    for path in files:
        errors = list(Draft7Validator(schema).iter_errors(json.loads(path.read_text())))
        assert errors == [], path.name


def test_the_item_contract_matches_the_model() -> None:
    committed = (REPO / human.ITEM_CONTRACT).read_text("utf-8")
    assert committed == human.schema_text(human.item_json_schema()), (
        "run: uv run tac human schema --write"
    )


def test_every_key_of_the_page_text_file_has_a_comment() -> None:
    docs = document((REPO / human.TODO_SOURCE).read_text("utf-8"))
    assert set(docs) >= {"schema_version", "title", "intro", "sections"}
    bare = [d.path for d in docs.values() if not d.comment.strip()]
    assert bare == []


def test_the_page_keeps_the_existing_format() -> None:
    page = (REPO / "TODO.HUMAN.md").read_text("utf-8")
    assert page.startswith("# TODO.HUMAN.md: what only the owner can do\n\n")
    assert "Credential handover convention:" in page
    assert "Rendered by `tac human render` from " in page
    assert "- [ ] Q14 agent identity: create a machine account" in page
    assert "      Recommendation: a machine account with" in page
    assert page.index("## Agent identity") < page.index("## Scope and providers")
    assert "\u2014" not in page


# ---------------------------------------------------------------- render


def test_render_is_deterministic_and_ignores_file_order(project: Path) -> None:
    for n in (3, 1, 2):
        assert question(project, "--id", f"Q{n}")[0] == 0
    paths = human.human_paths(project)
    first = human.render_page(project)[1]
    items = human.load_items(paths)
    again = human.render(paths, human.load_todo(project), list(reversed(items)))
    assert first == again
    # Within a section, items keep the order they were asked in.
    assert first.index("Q3 release") < first.index("Q1 release") < first.index("Q2 ")


def test_a_hand_edit_fails_the_check(project: Path) -> None:
    assert question(project)[0] == 0
    assert run("human", "render", "--root", str(project), "--check")[0] == 0
    page = project / "TODO.HUMAN.md"
    page.write_text(page.read_text().replace("- [ ] Q1", "- [x] Q1"))
    code, out = run("human", "render", "--root", str(project), "--check")
    assert code == 1
    assert "differs from its render" in out
    assert run("human", "render", "--root", str(project))[0] == 0
    assert "- [ ] Q1" in page.read_text()


def test_an_empty_section_is_left_off_and_a_stray_topic_is_refused(
    project: Path,
) -> None:
    assert question(project)[0] == 0
    page = human.render_page(project)[1]
    assert "## Governance" in page
    assert "## Scope and providers" not in page
    data = item_file(project, "Q1") | {"topic": "nowhere"}
    (project / ".human/approvals/Q1.json").write_text(json.dumps(data))
    code, out = run("human", "render", "--root", str(project), "--check")
    assert code == 1 and "Q1 (nowhere)" in out


def test_a_file_that_is_not_an_item_is_named(project: Path) -> None:
    assert question(project)[0] == 0
    folder = project / ".human/approvals"
    (folder / "Q9.json").write_text(json.dumps(item_file(project, "Q1")))
    (folder / "notes.txt").write_text("x")
    code, out = run("human", "render", "--root", str(project))
    assert code == 1
    assert "Q9.json: holds item Q1" in out
    assert "notes.txt: only <id>.json files belong here" in out


# ---------------------------------------------------------------- ask


def test_ask_adds_an_item_with_its_recommendation_blocks_and_milestone(
    project: Path,
) -> None:
    code, out = question(project)
    assert code == 0, out
    assert "asked Q1" in out and "rendered TODO.HUMAN.md" in out
    data = item_file(project, "Q1")
    assert data["status"] == "pending" and data["asked_at"] == AT
    assert data["blocks"] == ["the release pipeline"] and data["waits"] == "M3"
    schema = json.loads((REPO / human.ITEM_CONTRACT).read_text("utf-8"))
    assert list(Draft7Validator(schema).iter_errors(data)) == []
    page = (project / "TODO.HUMAN.md").read_text()
    assert (
        "- [ ] Q1 release cadence: Cut a release every week, or when a milestone "
        "lands?\n      Options: every week; per milestone.\n      Recommendation: "
        "per milestone. Blocks: the release pipeline. Waits: M3.\n"
    ) in page


def test_ask_numbers_the_next_question_after_the_highest(project: Path) -> None:
    assert question(project, "--id", "Q7")[0] == 0
    assert question(project)[0] == 0
    assert (project / ".human/approvals/Q8.json").is_file()


@pytest.mark.parametrize(
    "missing", ["--recommendation", "--blocks", "--waits"], ids=lambda m: m[2:]
)
def test_a_question_needs_its_recommendation_blocks_and_milestone(
    project: Path, missing: str
) -> None:
    args = {
        "--recommendation": "yes.",
        "--blocks": "M3",
        "--waits": "M3",
    }
    flat = [x for k, v in args.items() if k != missing for x in (k, v)]
    code, out = ask(project, "--topic", "governance", "--question", "Why?", *flat)
    assert code == 1 and missing in out
    assert not (project / ".human/approvals").exists()


def test_a_step_needs_no_recommendation(project: Path) -> None:
    code, _ = ask(
        project, "--kind", "step", "--id", "S1", "--topic", "steps",
        "--question", "Run `just doctor` once.",
    )  # fmt: skip
    assert code == 0
    assert (
        "- [ ] S1 Run `just doctor` once.\n" in (project / "TODO.HUMAN.md").read_text()
    )


def test_an_approval_binds_an_action_and_a_revision(project: Path) -> None:
    base = [
        "--kind", "approval", "--topic", "approvals", "--question", "Push?",
        "--recommendation", "yes.", "--blocks", "the push", "--waits", "M3",
    ]  # fmt: skip
    code, out = ask(project, *base)
    assert code == 1 and "--tool and --revision" in out
    code, out = ask(
        project, *base, "--tool", "git push", "--revision", SHA,
        "--arguments", '{"branch": "tac/x"}', "--pr", "1347", "--expires-in", "24",
    )  # fmt: skip
    assert code == 0, out
    data = item_file(project, "Q1")
    assert data["arguments"] == {"branch": "tac/x"}
    assert data["expires_at"] == "2026-09-26T12:00:00Z"


def test_ask_never_overwrites_an_item(project: Path) -> None:
    assert question(project, "--id", "Q3")[0] == 0
    code, out = question(project, "--id", "Q3", "--waits", "M4")
    assert code == 1 and "item Q3 exists" in out
    assert item_file(project, "Q3")["waits"] == "M3"


@pytest.mark.parametrize(
    ("flag", "value", "why"),
    [
        ("--topic", "nowhere", "no section 'nowhere'"),
        ("--question", "one\ntwo", "line break"),
        ("--question", "a \u2014 b", "em dash"),
        ("--id", "../escape", "id"),
    ],
)
def test_ask_refuses_what_would_break_the_page(
    project: Path, flag: str, value: str, why: str
) -> None:
    code, out = question(project, flag, value)
    assert code == 1 and why in out
    assert not list((project / ".human").rglob("*.json"))


# ---------------------------------------------------------------- answer


def answer(root: Path, item_id: str, *extra: str) -> tuple[int, str]:
    return run(
        "human", "answer", item_id, "--root", str(root), "--at", "2026-09-26T08:30:00Z",
        *extra,
    )  # fmt: skip


def test_answer_records_the_owner_and_closes_a_question(project: Path) -> None:
    assert question(project)[0] == 0
    code, out = answer(project, "Q1", "--text", "Per milestone.", "--by", "example")
    assert code == 0, out
    data = item_file(project, "Q1")
    assert data["status"] == "answered"
    assert data["decided_by"] == "example"
    assert data["decided_at"] == "2026-09-26T08:30:00Z"
    assert data["answers"] == [
        {
            "at": "2026-09-26T08:30:00Z",
            "basis": "owner-via-chief",
            "by": "example",
            "link": None,
            "text": "Per milestone.",
        }
    ]
    page = (project / "TODO.HUMAN.md").read_text()
    assert "- [x] Q1 release cadence" in page
    assert (
        "      Answered 2026-09-26 by example, owner-via-chief: Per milestone.\n"
        in page
    )
    assert (
        "1 item in `.human/approvals/` and `.human/todo.toml`, 0 open, as of " in page
    )


def test_the_answer_defaults_to_the_repository_owner_and_can_stay_open(
    project: Path,
) -> None:
    assert question(project)[0] == 0
    code, _ = answer(project, "Q1", "--text", "Half of it.", "--keep-open")
    assert code == 0
    data = item_file(project, "Q1")
    assert data["status"] == "pending" and data["answers"][0]["by"] == "example"
    assert "- [ ] Q1" in (project / "TODO.HUMAN.md").read_text()


def test_an_answer_never_approves_an_approval(project: Path) -> None:
    code, _ = ask(
        project, "--kind", "approval", "--topic", "approvals", "--question", "Push?",
        "--recommendation", "yes.", "--blocks", "the push", "--waits", "M3",
        "--tool", "git push", "--revision", SHA,
    )  # fmt: skip
    assert code == 0
    code, out = answer(project, "Q1", "--text", "Yes, go.", "--by", "example")
    assert code == 0 and "stays pending" in out
    data = item_file(project, "Q1")
    assert data["status"] == "pending" and data["approval"] is None


def test_the_agent_identity_cannot_answer_for_the_owner(project: Path) -> None:
    assert question(project)[0] == 0
    code, out = answer(project, "Q1", "--text", "Approved.", "--by", "tac-bot")
    assert code == 1 and "agent output" in out
    assert item_file(project, "Q1")["answers"] == []


def test_answering_an_unknown_item_is_refused(project: Path) -> None:
    code, out = answer(project, "Q404", "--text", "x", "--by", "example")
    assert code == 1 and "no item Q404" in out


# ---------------------------------------------------------------- recap

RECAP = {
    "title": "Human loop landed",
    "shipped": ["tac human ask, answer, render and recap"],
    "in_flight": ["the receipt check in CI"],
    "decisions": ["the owner is the repository's owning account (chief)"],
    "owner_actions": ["Answer Q1 in TODO.HUMAN.md."],
}


def recap(root: Path, payload: object, *extra: str) -> tuple[int, str]:
    source = root.parent / "recap.json"
    source.write_text(json.dumps(payload))
    return run(
        "human", "recap", "--root", str(root), "--at", AT, "--from", str(source),
        *extra,
    )  # fmt: skip


def test_recap_writes_a_dated_file_in_the_order_the_owner_reads(
    project: Path,
) -> None:
    assert question(project)[0] == 0
    code, out = recap(project, RECAP)
    assert code == 0, out
    path = project / ".human/recap/20260925T120000Z-human-loop-landed.md"
    assert path.is_file()
    text = path.read_text()
    order = ["# Recap: Human loop landed", "## Shipped", "## In flight"]
    order += ["## Decisions", "## For the owner"]
    assert [text.index(h) for h in order] == sorted(text.index(h) for h in order)
    assert "- Open in TODO.HUMAN.md: Q1.\n" in text
    assert human.word_count(text) < human.MAX_RECAP_WORDS


def test_a_recap_is_never_replaced_and_takes_a_slug(project: Path) -> None:
    assert recap(project, RECAP, "--slug", "m2")[0] == 0
    code, out = recap(project, RECAP, "--slug", "m2")
    assert code == 1 and "never replaced" in out
    code, out = recap(project, RECAP, "--slug", "Not A Slug")
    assert code == 1 and "not a slug" in out


def test_a_recap_over_four_hundred_words_is_refused(project: Path) -> None:
    long = RECAP | {"shipped": ["word " * 60] * 7}
    code, out = recap(project, long)
    assert code == 1 and "keep it under 400" in out
    assert not (project / ".human/recap").exists()


def test_a_recap_that_breaks_its_contract_is_refused(project: Path) -> None:
    code, out = recap(project, {k: v for k, v in RECAP.items() if k != "decisions"})
    assert code == 1 and "breaks its contract" in out
    code, out = recap(project, RECAP | {"extra": 1})
    assert code == 1 and "breaks its contract" in out


def test_the_recap_name_is_utc_basic_time() -> None:
    at = human.parse_utc("2026-01-02T03:04:05Z")
    assert human.recap_file_name(at, "x") == "20260102T030405Z-x.md"
    assert human.slugify("  M2: the human loop!  ") == "m2-the-human-loop"
