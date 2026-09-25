"""The private scan finds a private term wherever a candidate puts it.

CI runs the base revision's copy of `scripts/private_scan.sh` against the
candidate's files (docs/DESIGN.md, section 8). So nothing the candidate carries,
neither a `.gitattributes` nor its own copy of the scan, may decide what the scan
skips. Each test runs the base's copy from outside the checkout, as CI does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._gitrepo import REPO, commit_all, git, write

SCAN = REPO / "scripts/private_scan.sh"
SELF = "scripts/private_scan.sh"
# Spelled in pieces so this file does not carry the terms it tests.
TERM = "e-Boek" + "houden"
KEY = "F" + "079"
# The line the base's copy carries for TERM in its fixed-term list.
TERM_LINE = f'  "{TERM}"\n'


def scan(root: Path, candidate: str | None = None) -> subprocess.CompletedProcess[str]:
    """Runs the base's copy from a folder outside the checkout, the way CI runs
    it from $RUNNER_TEMP, after writing the candidate's copy into the checkout."""
    base = SCAN.read_text(encoding="utf-8")
    gates = write(root.parent / "gates", "private_scan.sh", base)
    write(root, SELF, base if candidate is None else candidate)
    return subprocess.run(
        ["bash", str(gates)],
        cwd=root,
        capture_output=True,
        text=True,
        # A binary hit printed raw must fail the assertion, not the decoding.
        errors="replace",
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "README.md", "A public project.\n")
    return root


def test_a_clean_repository_passes(repo: Path) -> None:
    commit_all(repo, "base")
    done = scan(repo)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_term_in_a_text_file_is_found(repo: Path) -> None:
    write(repo, "docs/a.md", f"notes on {TERM}\n")
    done = scan(repo)
    assert done.returncode == 1
    assert f"docs/a.md:1:notes on {TERM}" in done.stdout


@pytest.mark.parametrize(
    ("path", "rules"),
    [
        # The reviewer's case: -diff marks the files binary for git grep -I.
        (".gitattributes", "docs/** -diff\n"),
        (".gitattributes", "docs/** binary\n"),
        (".gitattributes", "*.md -text -diff\n"),
        # A folder's own attributes file applies below it just the same.
        ("docs/.gitattributes", "* binary\n"),
    ],
)
@pytest.mark.parametrize("tracked", [True, False])
def test_the_candidates_attributes_cannot_hide_a_term(
    repo: Path, path: str, rules: str, tracked: bool
) -> None:
    write(repo, path, rules)
    write(repo, "docs/a.md", f"notes on {TERM}\nsee {KEY}\n")
    if tracked:
        commit_all(repo, "candidate")
    done = scan(repo)
    assert done.returncode == 1, done.stdout
    assert f"docs/a.md:1:notes on {TERM}" in done.stdout
    assert f"docs/a.md:2:see {KEY}" in done.stdout


def test_a_file_with_a_nul_byte_is_still_skipped_as_binary(repo: Path) -> None:
    # The content decides what is binary; an image that happens to hold the
    # bytes of a term is not text anyone reads.
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00" + TERM.encode() + b"\x00")
    commit_all(repo, "candidate")
    done = scan(repo)
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize("text", [f"# see {TERM}\n", f"# the key {KEY} applies\n"])
def test_a_term_the_candidate_adds_to_the_scan_itself_is_found(
    repo: Path, text: str
) -> None:
    candidate = SCAN.read_text(encoding="utf-8") + text
    line = candidate.count("\n")
    done = scan(repo, candidate)
    assert done.returncode == 1
    assert f"{SELF}:{line}:{text.rstrip()}" in done.stdout


def test_the_scans_own_term_list_is_skipped(repo: Path) -> None:
    base = SCAN.read_text(encoding="utf-8")
    assert TERM_LINE in base
    # A new term that holds none of the old ones is an ordinary list change.
    candidate = base.replace(TERM_LINE, TERM_LINE + '  "another-name"\n')
    done = scan(repo, candidate)
    assert done.returncode == 0, done.stdout


def test_only_the_running_copys_term_lines_are_skipped(repo: Path) -> None:
    # Spelled as a term-list line, but the base's list has no such line: the
    # candidate cannot widen what is skipped by adding to its own list.
    added = f'  "{TERM} ledger"\n'
    base = SCAN.read_text(encoding="utf-8")
    candidate = base.replace(TERM_LINE, TERM_LINE + added)
    line = candidate[: candidate.index(added)].count("\n") + 1
    done = scan(repo, candidate)
    assert done.returncode == 1
    assert f"{SELF}:{line}:{added.rstrip()}" in done.stdout
    assert f"{SELF}:{line - 1}:" not in done.stdout
