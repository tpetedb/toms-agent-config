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
    """Writes the candidate's copy into the checkout and runs the base's copy."""
    base = SCAN.read_text(encoding="utf-8")
    write(root, SELF, base if candidate is None else candidate)
    return run_base(root)


def run_base(root: Path) -> subprocess.CompletedProcess[str]:
    """Runs the base's copy from a folder outside the checkout, the way CI runs
    it from $RUNNER_TEMP, against whatever the checkout holds."""
    gates = write(root.parent / "gates", "private_scan.sh", SCAN.read_text("utf-8"))
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


def check_out_again(root: Path, *paths: str) -> None:
    """Writes the files afresh from the index, as a clone in CI does, so the
    candidate's checkout attributes apply to them."""
    for path in paths:
        (root / path).unlink()
    git(root, "checkout", "--", *paths)


@pytest.mark.parametrize(
    ("encoding", "python_codec"),
    [("UTF-16", "utf-16"), ("UTF-16LE", "utf-16-le"), ("UTF-16BE", "utf-16-be")],
)
def test_a_checkout_encoding_cannot_hide_a_committed_term(
    repo: Path, encoding: str, python_codec: str
) -> None:
    # The blob stays UTF-8, so GitHub shows the term, while the checkout that CI
    # scans holds UTF-16, which git grep -I skips and no pattern matches.
    write(repo, ".gitattributes", f"docs/*.md working-tree-encoding={encoding}\n")
    text = f"notes on {TERM}\nsee {KEY}\n"
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_bytes(text.encode(python_codec))
    commit_all(repo, "candidate")
    check_out_again(repo, "docs/a.md")
    assert git(repo, "show", "HEAD:docs/a.md") == text.strip()
    assert b"\x00" in (repo / "docs/a.md").read_bytes()
    done = scan(repo)
    assert done.returncode == 1, done.stdout
    assert f"docs/a.md:1:notes on {TERM}" in done.stdout
    assert f"docs/a.md:2:see {KEY}" in done.stdout


def test_a_checkout_encoding_cannot_hide_a_term_in_the_scan_itself(
    repo: Path,
) -> None:
    candidate = SCAN.read_text(encoding="utf-8") + f"# see {TERM}\n"
    line = candidate.count("\n")
    write(repo, ".gitattributes", "scripts/*.sh working-tree-encoding=UTF-16\n")
    (repo / "scripts").mkdir()
    (repo / SELF).write_bytes(candidate.encode("utf-16"))
    commit_all(repo, "candidate")
    check_out_again(repo, SELF)
    done = run_base(repo)
    assert done.returncode == 1, done.stdout
    assert done.stdout.count(f"{SELF}:{line}:# see {TERM}") == 1


@pytest.mark.parametrize("bom", ["le", "be"])
@pytest.mark.parametrize("tracked", [True, False])
def test_a_utf16_text_file_is_decoded_and_scanned(
    repo: Path, bom: str, tracked: bool
) -> None:
    # A blob that is itself UTF-16 holds NUL bytes, so git grep -I calls it
    # binary; GitHub still shows it as text. A byte order mark says it is text.
    text = f"notes on {TERM}\nsee {KEY}\n"
    data = (
        b"\xff\xfe" + text.encode("utf-16-le")
        if bom == "le"
        else b"\xfe\xff" + text.encode("utf-16-be")
    )
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_bytes(data)
    if tracked:
        commit_all(repo, "candidate")
    done = scan(repo)
    assert done.returncode == 1, done.stdout
    assert f"docs/a.md:1:notes on {TERM}" in done.stdout
    assert f"docs/a.md:2:see {KEY}" in done.stdout


def test_a_committed_term_is_found_once_and_an_unstaged_one_too(repo: Path) -> None:
    write(repo, "docs/a.md", f"notes on {TERM}\nplain\n")
    commit_all(repo, "candidate")
    # Changed after it was staged: the committed line and the new one both count.
    write(repo, "docs/a.md", f"notes on {TERM}\nsee {KEY}\n")
    done = scan(repo)
    assert done.returncode == 1, done.stdout
    assert done.stdout.count(f"docs/a.md:1:notes on {TERM}") == 1
    assert f"docs/a.md:2:see {KEY}" in done.stdout


def test_a_committed_term_deleted_only_from_the_working_tree_is_found(
    repo: Path,
) -> None:
    write(repo, "docs/a.md", f"notes on {TERM}\n")
    commit_all(repo, "candidate")
    write(repo, "docs/a.md", "clean now\n")
    done = scan(repo)
    assert done.returncode == 1, done.stdout
    assert f"docs/a.md:1:notes on {TERM}" in done.stdout
