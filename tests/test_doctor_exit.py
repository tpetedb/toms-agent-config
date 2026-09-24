from __future__ import annotations

import os
import stat
from pathlib import Path

from tac.doctor import (
    Check,
    CheckResult,
    Status,
    check_agents_lib_current,
    check_agents_project,
    check_tac_import,
    classify_tac_origin,
    exit_code,
    find_root,
    run_checks,
)


def passing(_root: Path) -> tuple[Status, str]:
    return Status.PASS, "fine"


def failing(_root: Path) -> tuple[Status, str]:
    return Status.FAIL, "seeded failure"


def unknown(_root: Path) -> tuple[Status, str]:
    return Status.UNKNOWN, "cannot tell"


def crashing(_root: Path) -> tuple[Status, str]:
    raise RuntimeError("boom")


def test_all_passing_checks_exit_zero(tmp_path: Path) -> None:
    results = run_checks(tmp_path, [Check("a", passing), Check("b", passing)])
    assert exit_code(results) == 0


def test_one_seeded_failure_exits_non_zero(tmp_path: Path) -> None:
    results = run_checks(tmp_path, [Check("a", passing), Check("b", failing)])
    assert exit_code(results) == 1


def test_unknown_counts_as_not_passed(tmp_path: Path) -> None:
    results = run_checks(tmp_path, [Check("a", passing), Check("b", unknown)])
    assert exit_code(results) == 1


def test_a_crashing_check_is_reported_as_failed(tmp_path: Path) -> None:
    [result] = run_checks(tmp_path, [Check("c", crashing)])
    assert result == CheckResult("c", Status.FAIL, "check raised RuntimeError")


def test_no_checks_is_not_a_pass() -> None:
    assert exit_code([]) == 1


def test_find_root_accepts_a_worktree_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_root(nested) == tmp_path.resolve()


def write_agents_project(root: Path, source: str) -> None:
    agents = root / ".agents"
    agents.mkdir()
    (agents / "uv.lock").write_text("version = 1\n")
    (agents / "pyproject.toml").write_text(
        f'[project]\nname = "x"\n[tool.uv.sources]\ntac = {source}\n'
    )


def test_agents_project_requires_the_named_path_source(tmp_path: Path) -> None:
    write_agents_project(tmp_path, '{ path = "lib/tac" }')
    assert check_agents_project(tmp_path)[0] is Status.PASS


def test_agents_project_refuses_an_editable_source(tmp_path: Path) -> None:
    write_agents_project(tmp_path, '{ path = "lib/tac", editable = true }')
    assert check_agents_project(tmp_path)[0] is Status.FAIL


def test_agents_project_refuses_the_source_tree(tmp_path: Path) -> None:
    write_agents_project(tmp_path, '{ path = "../src/tac" }')
    assert check_agents_project(tmp_path)[0] is Status.FAIL


def test_origin_in_site_packages_passes(tmp_path: Path) -> None:
    module = tmp_path / ".agents/.venv/lib/python3.12/site-packages/tac/__init__.py"
    assert classify_tac_origin(module, tmp_path)[0] is Status.PASS


def test_origin_in_the_source_tree_fails(tmp_path: Path) -> None:
    status, detail = classify_tac_origin(tmp_path / "src/tac/__init__.py", tmp_path)
    assert status is Status.FAIL
    assert "src/tac/__init__.py" in detail


def test_origin_in_the_stamped_copy_fails(tmp_path: Path) -> None:
    module = tmp_path / ".agents/lib/tac/src/tac/__init__.py"
    assert classify_tac_origin(module, tmp_path)[0] is Status.FAIL


def fake_venv_python(root: Path, prints: str) -> None:
    python = root / ".agents/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\necho '{prints}'\n")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)


def test_tac_import_asks_the_deployed_interpreter(tmp_path: Path) -> None:
    site = tmp_path / ".agents/.venv/lib/python3.12/site-packages/tac/__init__.py"
    fake_venv_python(tmp_path, str(site))
    assert check_tac_import(tmp_path)[0] is Status.PASS


def test_tac_import_fails_on_an_editable_install(tmp_path: Path) -> None:
    fake_venv_python(tmp_path, str(tmp_path / "src/tac/__init__.py"))
    assert check_tac_import(tmp_path)[0] is Status.FAIL


def test_tac_import_is_unknown_without_a_venv(tmp_path: Path) -> None:
    assert check_tac_import(tmp_path)[0] is Status.UNKNOWN


def test_tac_import_runs_with_a_fixed_path(tmp_path: Path, monkeypatch) -> None:
    site = tmp_path / ".agents/.venv/lib/python3.12/site-packages/tac/__init__.py"
    src = tmp_path / "src/tac/__init__.py"
    python = tmp_path / ".agents/.venv/bin/python"
    python.parent.mkdir(parents=True)
    # Answers with the good origin only when the probe's PATH is the fixed one.
    python.write_text(
        '#!/bin/sh\nif [ "$PATH" = "/usr/bin:/bin" ]; '
        f"then echo '{site}'; else echo '{src}'; fi\n"
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    assert check_tac_import(tmp_path)[0] is Status.PASS


def write_package(folder: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)


def test_a_stamp_that_matches_src_passes(tmp_path: Path) -> None:
    files = {"__init__.py": "", "work.py": "x = 1\n"}
    write_package(tmp_path / "src/tac", files)
    write_package(tmp_path / ".agents/lib/tac/src/tac", files)
    # Bytecode caches are the interpreter's, not the stamp's.
    write_package(tmp_path / "src/tac/__pycache__", {"work.cpython-312.pyc": "?"})
    assert check_agents_lib_current(tmp_path)[0] is Status.PASS


def test_a_stamp_behind_src_fails_and_names_the_file(tmp_path: Path) -> None:
    write_package(tmp_path / "src/tac", {"__init__.py": "", "work.py": "x = 2\n"})
    write_package(tmp_path / ".agents/lib/tac/src/tac", {"__init__.py": ""})
    status, detail = check_agents_lib_current(tmp_path)
    assert status is Status.FAIL
    assert "work.py" in detail and "just stamp-lib" in detail


def test_a_project_without_src_tac_judges_by_its_pinned_stamp(tmp_path: Path) -> None:
    assert check_agents_lib_current(tmp_path)[0] is Status.PASS
