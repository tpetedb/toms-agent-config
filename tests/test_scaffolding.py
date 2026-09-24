"""Static promises of the scaffolding: CI trust, ownership, the toolchain source."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from scripts.stamp_lib import stamp

FRAGMENT = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.(added|changed|deprecated|removed|fixed|security)\.md$"
)


def load_ci(repo: Path) -> dict:
    return yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())


def test_ci_runs_on_pull_request_with_a_read_only_token(repo: Path) -> None:
    ci = load_ci(repo)
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = ci[True]
    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}


def test_ci_runs_every_gate(repo: Path) -> None:
    steps = [
        step.get("run", "")
        for job in load_ci(repo)["jobs"].values()
        for step in job["steps"]
    ]
    script = "\n".join(steps)
    for gate in (
        "ruff check",
        "ruff format --check",
        "basedpyright",
        "pytest",
        "scripts/private_scan.sh",
        "--no-editable --project .agents",
    ):
        assert gate in script, gate


def test_ci_pins_actions_to_a_commit(repo: Path) -> None:
    for job in load_ci(repo)["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["uses"]


def test_codeowners_names_the_owner_on_the_guarded_paths(repo: Path) -> None:
    rules = {}
    for line in (repo / ".github/CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            path, *owners = line.split()
            rules[path] = owners
    for path in (
        "/.agents/",
        "/src/tac/",
        "/hooks/",
        "/templates/",
        "/contracts/",
        "/.github/",
    ):
        assert rules.get(path) == ["@tpetedb"], path


def test_agents_toolchain_installs_tac_from_the_stamped_copy(repo: Path) -> None:
    project = tomllib.loads((repo / ".agents/pyproject.toml").read_text())
    assert project["tool"]["uv"]["sources"]["tac"] == {"path": "lib/tac"}
    lock = (repo / ".agents/uv.lock").read_text()
    assert 'source = { directory = "lib/tac" }' in lock


def test_gitignore_keeps_venvs_and_runtime_out(repo: Path) -> None:
    ignored = (repo / ".gitignore").read_text().splitlines()
    for entry in (".venv/", "runtime/", "agents.env"):
        assert entry in ignored, entry


def test_changelog_fragments_are_named_slug_dot_type(repo: Path) -> None:
    fragments = [
        p.name for p in (repo / "changelog.d").iterdir() if p.name != "README.md"
    ]
    assert fragments
    for name in fragments:
        assert FRAGMENT.match(name), name


def test_stamp_copies_the_package_and_its_entry_point(
    tmp_path: Path, repo: Path
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text((repo / "pyproject.toml").read_text())
    pkg = tmp_path / "src/tac"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__/junk.pyc").write_bytes(b"\0")
    target = stamp(tmp_path)
    assert (target / "src/tac/__init__.py").read_text() == "X = 1\n"
    assert not (target / "src/tac/__pycache__").exists()
    stamped = tomllib.loads((target / "pyproject.toml").read_text())
    root = tomllib.loads((repo / "pyproject.toml").read_text())
    assert stamped["project"]["version"] == root["project"]["version"]
    assert stamped["project"]["scripts"] == {"tac": "tac.cli:main"}
    # A second stamp replaces the copy rather than merging into it.
    (pkg / "__init__.py").unlink()
    (pkg / "cli.py").write_text("")
    stamp(tmp_path)
    assert not (target / "src/tac/__init__.py").exists()
