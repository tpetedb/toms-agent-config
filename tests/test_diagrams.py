"""The diagram inventory, the house subset, render.lock and the render loop
(docs/DESIGN.md section 14, docs/diagrams/README.md).

No test needs node: the render loop runs against a fake `npx` on PATH that
records its arguments and writes the SVG it is asked for, or fails on cue.
"""

from __future__ import annotations

import json
import shutil
import stat
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac import diagrams
from tac.cli import cli
from tac.config import load_config
from tac.diagrams import (
    LOCK_FILE,
    Source,
    check,
    expected,
    house_problems,
    inventory,
    lock_text,
    pinned_version,
    render,
    sources,
)
from tac.sync import check_tree, sync
from tac.work import Bad
from tests._syncproject import REPO, copy_project, replace_in

GITHUB = ".agents/config/github.toml"
COMMITTED = sorted((REPO / diagrams.DIAGRAMS_DIR).glob("*.mmd"))
FAKE_NPX = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$(dirname "$0")/calls.txt"
if [ -f "$(dirname "$0")/fail" ]; then echo "Parse error on line 2" >&2; exit 1; fi
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
printf '<svg/>' > "$out"
"""


def project(tmp_path: Path) -> Path:
    """This repository's configuration, diagrams and mise.toml, with a lock
    written for exactly the sources the copy holds."""
    root = copy_project(tmp_path / "p")
    shutil.copytree(REPO / diagrams.DIAGRAMS_DIR, root / diagrams.DIAGRAMS_DIR)
    shutil.copy2(REPO / "mise.toml", root / "mise.toml")
    relock(root)
    return root


def relock(root: Path) -> None:
    (root / LOCK_FILE).write_text(
        lock_text(pinned_version(root), sources(root)), encoding="utf-8"
    )


def fake_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    npx = folder / "npx"
    npx.write_text(FAKE_NPX, encoding="utf-8")
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return folder


# ---------------------------------------------------------------- the repository


def test_the_repository_holds_twelve_sources_and_a_current_lock() -> None:
    assert len(COMMITTED) == 12
    assert check(REPO) == []


def test_the_inventory_is_the_seven_lifecycles_and_the_five_pipelines() -> None:
    names = expected(load_config(REPO))
    assert names[:7] == [
        f"lifecycle-{n}.mmd"
        for n in ("init", "sync", "hook", "memory", "board", "human-loop", "release")
    ]
    assert names[7:] == [
        f"pipeline-{n}.mmd" for n in ("order", "board", "review", "release", "retro")
    ]


def test_the_pin_is_mermaid_cli_12() -> None:
    assert pinned_version(REPO) == "12.0.0"


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.name)
def test_every_committed_source_keeps_the_house_subset(path: Path) -> None:
    rel = path.relative_to(REPO).as_posix()
    text = path.read_text(encoding="utf-8")
    assert house_problems(Source(rel, rel, text)) == []
    assert "%% Legend:" in text
    assert "—" not in text


@pytest.mark.parametrize(
    "path",
    [p for p in COMMITTED if p.name.startswith("pipeline-")],
    ids=lambda p: p.name,
)
def test_a_pipeline_diagram_names_every_stage_of_its_pipeline(path: Path) -> None:
    name = path.stem.removeprefix("pipeline-")
    pipeline = tomllib.loads(
        (REPO / f".agents/config/pipelines/{name}.toml").read_text(encoding="utf-8")
    )
    text = path.read_text(encoding="utf-8")
    positions = []
    for stage in pipeline["stages"]:
        marker = f'"{stage["id"]}: '
        assert marker in text, f"{path.name} does not draw stage {stage['id']}"
        positions.append(text.index(marker))
        for gate in stage.get("gates", []):
            assert " ".join(gate["argv"]) in text, (path.name, gate["argv"])
    assert positions == sorted(positions), "stages are drawn in dependency order"


# ---------------------------------------------------------------- the inventory


def test_a_clean_copy_passes(tmp_path: Path) -> None:
    assert check(project(tmp_path)) == []


def test_a_missing_and_an_extra_source_are_named(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / diagrams.DIAGRAMS_DIR / "lifecycle-hook.mmd").unlink()
    extra = root / diagrams.DIAGRAMS_DIR / "lifecycle-extra.mmd"
    shutil.copy2(root / diagrams.DIAGRAMS_DIR / "lifecycle-init.mmd", extra)
    missing, extras = inventory(load_config(root), root)
    assert missing == ["lifecycle-hook.mmd"]
    assert extras == ["lifecycle-extra.mmd"]
    problems = check(root)
    assert "docs/diagrams/lifecycle-hook.mmd: missing; github.toml [diagrams] " in (
        "\n".join(problems)
    )
    assert any("lifecycle-extra.mmd: not in the inventory" in p for p in problems)


def test_per_pipeline_false_makes_the_pipeline_diagrams_extra(tmp_path: Path) -> None:
    root = project(tmp_path)
    replace_in(root / GITHUB, "per_pipeline = true", "per_pipeline = false")
    config = load_config(root)
    assert expected(config) == [n for n in expected(config) if n.startswith("life")]
    missing, extras = inventory(config, root)
    assert missing == []
    assert sorted(extras) == sorted(
        f"pipeline-{n}.mmd" for n in ("order", "board", "review", "release", "retro")
    )


def test_a_lifecycle_dropped_from_the_config_is_extra(tmp_path: Path) -> None:
    root = project(tmp_path)
    replace_in(root / GITHUB, '"human-loop", ', "")
    assert inventory(load_config(root), root) == ([], ["lifecycle-human-loop.mmd"])


# ---------------------------------------------------------------- the lock


def test_a_changed_source_is_refused_until_rendered_again(tmp_path: Path) -> None:
    root = project(tmp_path)
    source = root / diagrams.DIAGRAMS_DIR / "pipeline-retro.mmd"
    source.write_text(
        source.read_text().replace("writes lessons", "writes lessons."),
        encoding="utf-8",
    )
    assert check(root) == [
        "docs/diagrams/pipeline-retro.mmd: changed since the last render; "
        "run just diagrams-render"
    ]
    relock(root)
    assert check(root) == []


def test_a_new_block_in_the_docs_and_a_vanished_one_are_named(tmp_path: Path) -> None:
    root = project(tmp_path)
    page = root / "docs" / "page.md"
    page.write_text("# Page\n\n```mermaid\nflowchart LR\n    a --> b\n```\n")
    assert check(root) == [
        "docs/page.md#mermaid-1: never rendered since it was added; "
        "run just diagrams-render"
    ]
    relock(root)
    page.unlink()
    assert check(root) == [
        f"{LOCK_FILE}: docs/page.md#mermaid-1 is gone since the last render; "
        "run just diagrams-render"
    ]


def test_a_missing_or_broken_lock_is_named_not_a_crash(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / LOCK_FILE).unlink()
    assert check(root) == [f"{LOCK_FILE}: missing; run just diagrams-render"]
    (root / LOCK_FILE).write_text("{not json")
    assert check(root)[0].startswith(f"{LOCK_FILE}: not valid JSON")


def test_a_lock_from_another_version_is_named(tmp_path: Path) -> None:
    root = project(tmp_path)
    data = json.loads((root / LOCK_FILE).read_text())
    data["mermaid_cli"] = "11.12.0"
    (root / LOCK_FILE).write_text(json.dumps(data))
    assert check(root) == [
        f"{LOCK_FILE}: rendered with mermaid-cli 11.12.0, mise.toml pins 12.0.0; "
        "run just diagrams-render"
    ]


def test_the_lock_is_the_version_then_a_sorted_map() -> None:
    data = json.loads((REPO / LOCK_FILE).read_text())
    assert list(data) == ["mermaid_cli", "sources"]
    assert list(data["sources"]) == sorted(data["sources"])
    assert "docs/DESIGN.md#mermaid-1" in data["sources"]


def test_an_unpinned_mise_is_refused(tmp_path: Path) -> None:
    root = project(tmp_path)
    replace_in(root / "mise.toml", '= "12.0.0"', '= "latest"')
    with pytest.raises(Bad, match=r"exact X\.Y\.Z"):
        pinned_version(root)


def test_tac_check_holds_the_inventory(tmp_path: Path) -> None:
    root = project(tmp_path)
    sync(root)
    assert check_tree(root) == []
    (root / diagrams.DIAGRAMS_DIR / "pipeline-board.mmd").unlink()
    relock(root)
    assert "docs/diagrams/pipeline-board.mmd: missing; github.toml [diagrams] " in (
        "\n".join(check_tree(root))
    )


# ---------------------------------------------------------------- sources


def test_fenced_blocks_get_stable_ids_by_position(tmp_path: Path) -> None:
    root = tmp_path
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text(
        "```toml\nx = 1\n```\n\n```mermaid\nflowchart LR\n    a\n```\n\n"
        "````mermaid\nflowchart TD\n    b\n````\n"
    )
    found = sources(root)
    assert [s.id for s in found] == ["docs/a.md#mermaid-1", "docs/a.md#mermaid-2"]
    assert found[0].text == "flowchart LR\n    a\n"
    assert found[1].block == 2


# ---------------------------------------------------------------- the house subset


GOOD = """%% Legend: green terminator, blue process.
flowchart LR
    a(["start"]) --> b["step"]
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    class a term
    class b proc
"""


# The last line of GOOD, after which a mutant appends a line of its own.
TAIL = "    class b proc\n"


def lint(text: str) -> list[str]:
    return house_problems(Source("x.mmd", "x.mmd", text))


def test_a_clean_source_passes() -> None:
    assert lint(GOOD) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ('b["step"]', 'b(("step"))', "a shape outside the house subset"),
        ('b["step"]', 'b{{"step"}}', "a shape outside the house subset"),
        ("    class b proc\n", "", "node b has no class"),
        ("class b proc", "class b dec", "is a rectangle with class dec"),
        ("fill:#0067A5", "fill:#123456", "classDef proc is not the house palette's"),
        ("%% Legend: green terminator, blue process.\n", "", "no %% Legend:"),
        (", blue process", "", "the legend does not say 'blue process'"),
        ("step", "a — step", "an em dash"),
        ("class a term", "class a,ghost term", "class names ghost"),
        (TAIL, TAIL + "    Z(rounded box) --> Y>asym]\n", "node Z uses '('"),
        (TAIL, TAIL + "    Z(rounded box) --> Y>asym]\n", "node Y uses '>'"),
        (TAIL, TAIL + "    q[plain rect]\n", "node q has an unquoted label"),
        (TAIL, TAIL + "    q[plain rect]\n", "node q has no class"),
        (TAIL, TAIL + "    new --> other\n", "node new is never drawn"),
        (TAIL, TAIL + "    new --> other\n", "node other has no class"),
        (TAIL, TAIL + '    s@{ shape: doc, label: "d" }\n', "node s uses '@{'"),
        (TAIL, TAIL + "    style b fill:#FFFFFF\n", "an inline style"),
        (TAIL, TAIL + "    b --> ?\n", "cannot read"),
        (TAIL, TAIL + "    b -->\n", "no node after it"),
    ],
)
def test_the_house_lint_refuses(old: str, new: str, expected: str) -> None:
    assert old in GOOD
    problems = lint(GOOD.replace(old, new))
    assert any(expected in p for p in problems), problems


# ---------------------------------------------------------------- render


def test_render_runs_the_pinned_cli_per_source_and_writes_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    (root / LOCK_FILE).unlink()
    (root / "docs" / "page.md").write_text("```mermaid\nflowchart LR\n    a\n```\n")
    folder = fake_npx(tmp_path, monkeypatch)
    lines: list[str] = []
    out = tmp_path / "out"
    written = render(root, out, echo=lines.append)
    calls = (folder / "calls.txt").read_text().splitlines()
    assert len(calls) == len(written) == 14
    assert all(c.startswith("--yes @mermaid-js/mermaid-cli@12.0.0 -i ") for c in calls)
    assert (out / "docs_page.md_mermaid-1.mmd").read_text() == "flowchart LR\n    a\n"
    assert all(p.read_text() == "<svg/>" for p in written)
    assert lines[-1] == f"wrote {LOCK_FILE}: mermaid-cli 12.0.0, 14 sources"
    assert sum(line.startswith("rendered ") for line in lines) == 14
    assert check(root) == []


def test_render_stops_at_the_first_failure_and_keeps_the_old_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    before = (root / LOCK_FILE).read_text()
    folder = fake_npx(tmp_path, monkeypatch)
    (folder / "fail").write_text("")
    with pytest.raises(Bad, match=r"mermaid-cli 12\.0\.0 failed \(exit 1\).*Parse"):
        render(root, tmp_path / "out", echo=lambda _line: None)
    assert len((folder / "calls.txt").read_text().splitlines()) == 1
    assert (root / LOCK_FILE).read_text() == before


def test_render_without_npx_says_what_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(Bad, match="npx not found"):
        render(root, tmp_path / "out")


def test_the_cli_renders_into_a_scratch_folder_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    fake_npx(tmp_path, monkeypatch)
    runner = CliRunner()
    done = runner.invoke(cli, ["diagrams", "render", "--root", str(root)])
    assert done.exit_code == 0, done.output
    assert "wrote docs/diagrams/render.lock" in done.output
    assert not list(root.rglob("*.svg")), "no SVG lands in the repository"
    done = runner.invoke(cli, ["diagrams", "check", "--root", str(root)])
    assert done.exit_code == 0, done.output
    (root / diagrams.DIAGRAMS_DIR / "pipeline-review.mmd").write_text("flowchart\n")
    done = runner.invoke(cli, ["diagrams", "check", "--root", str(root)])
    assert done.exit_code == 1
    assert "pipeline-review.mmd: changed since the last render" in done.output
