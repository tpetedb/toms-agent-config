"""The private scan finds a private term wherever the repository publishes it.

CI runs the base revision's copy of `scripts/private_scan.sh`, `private_scan.py`
and `private_terms.txt` against the candidate (docs/DESIGN.md, section 8). The
scan reads committed blobs, never the checkout and never through gitattributes,
and with `--range` every commit, message and the branch name. Each test runs the
base's copy from a folder outside the checkout, as CI does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest
import yaml

from tests._gitrepo import REPO, commit_all, git, write

SCAN_FILES = ("private_scan.sh", "private_scan.py", "private_terms.txt")
TERMS_PATH = "scripts/private_terms.txt"
# Spelled in pieces so this file does not carry the terms it tests.
TERM = "e-Boek" + "houden"
KEY = "F" + "079"
TEXT = f"notes on {TERM}\nsee {KEY}\n"
LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:" + "4" * 64 + "\nsize 1234\n"
)


def base_copy(folder: Path) -> Path:
    """The scan as CI holds it: the three files, together, outside the checkout."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in SCAN_FILES:
        shutil.copy2(REPO / "scripts" / name, folder / name)
    return folder / "private_scan.sh"


def run_scan(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    scan = base_copy(root.parent / "gates")
    return subprocess.run(
        ["bash", str(scan), *args],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def run_range(root: Path, base: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run_scan(root, "--range", f"{base}..HEAD", *args)


def short(root: Path, rev: str = "HEAD") -> str:
    return git(root, "rev-parse", rev)[:12]


def assert_found(done: subprocess.CompletedProcess[str], *hits: str) -> None:
    assert done.returncode == 1, (done.stdout, done.stderr)
    for hit in hits:
        assert hit in done.stdout, (hit, done.stdout)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "README.md", "A public project.\n")
    return root


@pytest.fixture
def base(repo: Path) -> str:
    """A clean first commit that a range starts from."""
    return commit_all(repo, "base")


def test_a_clean_repository_passes(repo: Path, base: str) -> None:
    for done in (run_scan(repo), run_range(repo, base)):
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_term_in_a_new_or_committed_file_is_found(repo: Path, base: str) -> None:
    write(repo, "docs/a.md", TEXT)
    assert_found(
        run_scan(repo), f"docs/a.md:1:notes on {TERM}", f"docs/a.md:2:see {KEY}"
    )
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), f"docs/a.md:1:notes on {TERM}")
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md:1:notes on {TERM}")


@pytest.mark.parametrize(
    ("path", "rules"),
    [
        (".gitattributes", "docs/** -diff\n"),
        (".gitattributes", "docs/** binary\n"),
        (".gitattributes", "*.md -text -diff\n"),
        ("docs/.gitattributes", "* binary\n"),
    ],
)
def test_the_candidates_attributes_cannot_hide_a_term(
    repo: Path, base: str, path: str, rules: str
) -> None:
    # -diff and binary make git grep -I skip a text file; the scan reads no
    # attribute at all.
    write(repo, path, rules)
    write(repo, "docs/a.md", TEXT)
    assert_found(run_scan(repo), f"docs/a.md:1:notes on {TERM}")
    commit_all(repo, "candidate")
    assert_found(
        run_scan(repo), f"docs/a.md:1:notes on {TERM}", f"docs/a.md:2:see {KEY}"
    )
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md:2:see {KEY}")


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
    repo: Path, base: str, encoding: str, python_codec: str
) -> None:
    # The blob stays UTF-8, so GitHub shows the term, while the checkout CI
    # holds is UTF-16, which git grep -I skips and no pattern matches.
    write(repo, ".gitattributes", f"docs/*.md working-tree-encoding={encoding}\n")
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_bytes(TEXT.encode(python_codec))
    commit_all(repo, "candidate")
    check_out_again(repo, "docs/a.md")
    assert git(repo, "show", "HEAD:docs/a.md") == TEXT.strip()
    assert b"\x00" in (repo / "docs/a.md").read_bytes()
    assert_found(
        run_scan(repo), f"docs/a.md:1:notes on {TERM}", f"docs/a.md:2:see {KEY}"
    )
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md:1:notes on {TERM}")


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xfe" + TEXT.encode("utf-16-le"),
        b"\xfe\xff" + TEXT.encode("utf-16-be"),
        TEXT.encode("utf-16-le"),
        TEXT.encode("utf-16-be"),
        # One stray byte in front shifts every code unit.
        b"#" + TEXT.encode("utf-16-le"),
    ],
    ids=["le-bom", "be-bom", "le", "be", "shifted"],
)
def test_a_utf16_blob_is_read_as_text(repo: Path, base: str, data: bytes) -> None:
    # A UTF-16 blob holds NUL bytes, so git grep -I calls it binary; GitHub still
    # shows it as text, with or without a byte order mark.
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_bytes(data)
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), f"notes on {TERM}", f"see {KEY}")
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md:")


def real_binary() -> bytes:
    """A PNG-shaped file holding a term's bytes: binary by content, since it has
    NUL bytes and repeated 0xD8 bytes make a lone UTF-16 surrogate in every byte
    order and alignment, so no UTF-16 reading of it exists."""
    body = zlib.compress(bytes(range(256)) * 8) + TERM.encode() + b"\xd8" * 8
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + body


def test_a_real_binary_file_is_skipped(repo: Path, base: str) -> None:
    (repo / "logo.png").write_bytes(real_binary())
    assert run_scan(repo).returncode == 0
    commit_all(repo, "candidate")
    for done in (run_scan(repo), run_range(repo, base)):
        assert (done.returncode, done.stdout) == (0, ""), done.stdout


def test_a_git_lfs_pointer_is_refused(repo: Path, base: str) -> None:
    # The pointer names content stored outside the repository, which the scan
    # cannot read; the file itself has to be committed instead.
    write(repo, ".gitattributes", "*.md filter=lfs diff=lfs merge=lfs -text\n")
    write(repo, "docs/a.md", LFS_POINTER)
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), "docs/a.md: a Git LFS pointer")
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md: a Git LFS pointer")


def test_a_term_added_then_removed_is_found_in_the_history(
    repo: Path, base: str
) -> None:
    write(repo, "docs/a.md", TEXT)
    added = commit_all(repo, "add notes")
    write(repo, "docs/a.md", "clean now\n")
    commit_all(repo, "clean the notes")
    # The final tree is clean, so a scan of it passes; the history is not.
    assert run_scan(repo).returncode == 0
    assert_found(run_range(repo, base), f"{added[:12]}:docs/a.md:1:notes on {TERM}")


def test_a_term_in_a_commit_message_is_found(repo: Path, base: str) -> None:
    write(repo, "docs/a.md", "plain\n")
    sha = commit_all(repo, f"Add notes\n\nTaken from {TERM}.")
    assert_found(run_range(repo, base), f"{sha[:12]} (message):3:Taken from {TERM}.")


def test_a_term_in_the_branch_name_is_found(repo: Path, base: str) -> None:
    git(repo, "checkout", "-q", "-b", f"feat/{TERM}")
    assert_found(run_scan(repo), f"(branch name):1:feat/{TERM}")
    assert_found(run_range(repo, base), f"(branch name):1:feat/{TERM}")
    # CI names the branch, since its checkout is detached.
    git(repo, "checkout", "-q", "--detach")
    assert_found(run_range(repo, base, "--branch", f"x-{TERM}"), f"x-{TERM}")


def test_a_term_in_a_merge_resolution_is_found(repo: Path, base: str) -> None:
    # A merge's own resolution is content neither parent had.
    git(repo, "checkout", "-q", "-b", "side")
    write(repo, "side.md", "side\n")
    commit_all(repo, "side")
    git(repo, "checkout", "-q", "main")
    write(repo, "main.md", "main\n")
    commit_all(repo, "main")
    git(repo, "merge", "-q", "--no-commit", "side")
    write(repo, "side.md", f"side {TERM}\n")
    git(repo, "add", "side.md")
    git(repo, "commit", "-q", "-m", "merge side")
    merge = short(repo)
    # Removed again after the merge, so only the merge commit itself holds it.
    write(repo, "side.md", "side\n")
    commit_all(repo, "clean side")
    assert_found(run_range(repo, base), f"{merge}:side.md:1:side {TERM}")


def test_a_term_in_a_path_is_found(repo: Path, base: str) -> None:
    write(repo, f"docs/{TERM}.md", "plain\n")
    assert_found(run_scan(repo), f"docs/{TERM}.md: the path names a private term")
    commit_all(repo, "candidate")
    assert_found(run_range(repo, base), f":docs/{TERM}.md: the path names")


@pytest.mark.parametrize("name", ["private_scan.sh", "private_scan.py"])
def test_a_term_in_the_scan_itself_is_found(repo: Path, base: str, name: str) -> None:
    # The scan carries no terms, so it is read like any other file.
    source = (REPO / "scripts" / name).read_text(encoding="utf-8")
    candidate = source + f"# see {TERM}\n"
    line = candidate.count("\n")
    write(repo, f"scripts/{name}", candidate)
    commit_all(repo, "candidate")
    hit = f"scripts/{name}:{line}:# see {TERM}"
    assert_found(run_scan(repo), hit)
    assert_found(run_range(repo, base), f"{short(repo)}:{hit}")


def test_the_scan_and_its_term_list_carry_no_term() -> None:
    # The list is hex, so an older scan that greps every file does not flag it,
    # and the scan itself needs no exception to pass its own terms.
    shown = subprocess.run(
        ["bash", str(REPO / "scripts/private_scan.sh"), "--show-terms"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    terms = [line.split("\t", 1) for line in shown.splitlines()]
    assert ["text", TERM] in terms
    for name in SCAN_FILES:
        for line in (REPO / "scripts" / name).read_text(encoding="utf-8").splitlines():
            for kind, term in terms:
                hit = (
                    term.lower() in line.lower()
                    if kind == "text"
                    else re.search(term, line)
                )
                assert not hit, (name, line)


def test_only_the_term_lists_exact_path_is_skipped(repo: Path, base: str) -> None:
    # The same content elsewhere is read, even though the list's blob is not.
    others = (f"{TERMS_PATH}.orig", "docs/private_terms.txt", "private_terms.txt")
    write(repo, TERMS_PATH, f"text {TERM}\n")
    write(repo, others[0], f"text {TERM}\n")
    for other in others[1:]:
        write(repo, other, f"{other} {TERM}\n")
    commit_all(repo, "candidate")
    for done in (run_scan(repo), run_range(repo, base)):
        lines = done.stdout.splitlines()
        assert done.returncode == 1
        assert not [line for line in lines if f"{TERMS_PATH}:" in line], lines
        for other in others:
            assert [line for line in lines if f"{other}:1:" in line], (other, lines)


def test_an_unstaged_change_and_a_working_tree_deletion_are_both_read(
    repo: Path,
) -> None:
    write(repo, "docs/a.md", f"notes on {TERM}\nplain\n")
    write(repo, "docs/b.md", f"notes on {TERM}\n")
    commit_all(repo, "candidate")
    write(repo, "docs/a.md", f"notes on {TERM}\nsee {KEY}\n")
    write(repo, "docs/b.md", "clean now\n")
    done = run_scan(repo)
    assert_found(done, f"docs/a.md:2:see {KEY}", f"docs/b.md:1:notes on {TERM}")
    assert done.stdout.count(f"docs/a.md:1:notes on {TERM}") == 1


@pytest.mark.parametrize(
    ("terms", "says"),
    [
        ("# nothing but comments\n", "no terms"),
        ("text\n", "an empty term"),
        ("word 6162\n", "the kind must be one of"),
        ("text zz\n", "not UTF-8 in hex"),
        ("regex 28\n", "does not compile"),
    ],
)
def test_a_broken_term_list_fails_the_scan(
    repo: Path, base: str, terms: str, says: str
) -> None:
    scan = base_copy(repo.parent / "gates")
    (scan.parent / "private_terms.txt").write_text(terms, encoding="utf-8")
    done = subprocess.run(
        ["bash", str(scan)], cwd=repo, capture_output=True, text=True, check=False
    )
    assert done.returncode == 2, done.stdout
    assert says in done.stderr


def test_an_encoded_term_reads_back() -> None:
    scan = str(REPO / "scripts/private_scan.sh")
    line = subprocess.run(
        ["bash", scan, "--encode", "text", TERM],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert line == f"text {TERM.encode().hex()}\n"
    shown = subprocess.run(
        ["bash", scan, "--show-terms"], capture_output=True, text=True, check=True
    ).stdout
    assert f"text\t{TERM}\n" in shown
    assert line.strip() in (REPO / TERMS_PATH).read_text(encoding="utf-8")


# The CI steps, run as the workflow spells them against a pull request fixture.
CI = REPO / ".github/workflows/ci.yml"


def ci_steps() -> list[dict]:
    """The verify job's step that takes the scan from the base, then the scan."""
    steps = yaml.safe_load(CI.read_text("utf-8"))["jobs"]["verify"]["steps"]
    fetch = next(
        s for s in steps if "private_scan.sh" in s.get("env", {}).get("GATES", "")
    )
    scan = next(s for s in steps if "gates/private_scan.sh" in s.get("run", ""))
    return [fetch, scan]


def run_ci(
    root: Path, head: str, tmp_path: Path
) -> list[subprocess.CompletedProcess[str]]:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environ |= {"RUNNER_TEMP": str(runner_temp), "BASE_REF": "main"}
    environ |= {"HEAD_REF": "candidate", "HEAD_SHA": head}
    done = []
    for step in ci_steps():
        gates = (
            {"GATES": step["env"]["GATES"]} if "GATES" in step.get("env", {}) else {}
        )
        done.append(
            subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                cwd=root,
                env=environ | gates,
                capture_output=True,
                text=True,
                check=False,
            )
        )
    return done


def pull_request(
    tmp_path: Path, base_files: dict[str, str], notes: str
) -> tuple[Path, str]:
    """A base on origin/main holding `base_files`, and a candidate branch that
    brings this checkout's scan, writes `notes` in one commit and removes them in
    the next, rewrites its own scan to pass and replaces its own term list."""
    root = tmp_path / "pr"
    root.mkdir()
    git(root, "init", "-q")
    for path, text in base_files.items():
        write(root, path, text)
    base = commit_all(root, "base")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    git(root, "checkout", "-q", "-b", "candidate")
    write(root, "docs/a.md", notes)
    commit_all(root, "add notes")
    write(root, "docs/a.md", "clean now\n")
    write(root, "scripts/private_scan.sh", "exit 0\n")
    write(root, TERMS_PATH, "text 7a7a7a\n")
    return root, commit_all(root, "clean the notes, and a scan that passes")


THIS_SCAN = {
    f"scripts/{n}": (REPO / "scripts" / n).read_text("utf-8") for n in SCAN_FILES
}


def test_ci_scans_every_commit_with_the_bases_scan_and_list(tmp_path: Path) -> None:
    root, head = pull_request(tmp_path, THIS_SCAN, TEXT)
    fetch, scan = run_ci(root, head, tmp_path)
    assert fetch.returncode == 0, fetch.stdout + fetch.stderr
    assert "::warning::" not in fetch.stdout
    assert_found(scan, f":docs/a.md:1:notes on {TERM}", f":docs/a.md:2:see {KEY}")


def test_ci_passes_a_clean_pull_request(tmp_path: Path) -> None:
    root, head = pull_request(tmp_path, THIS_SCAN, "plain\n")
    _fetch, scan = run_ci(root, head, tmp_path)
    assert (scan.returncode, scan.stdout) == (0, ""), scan.stderr


def test_ci_runs_an_older_base_scan_over_the_final_tree_loudly(tmp_path: Path) -> None:
    # The older scan has no term list beside it and takes no --range.
    older = {"scripts/private_scan.sh": 'echo "older scan, $# arguments"\n'}
    root, head = pull_request(tmp_path, older, TEXT)
    _fetch, scan = run_ci(root, head, tmp_path)
    assert scan.returncode == 0, scan.stderr
    assert "has the older private scan" in scan.stdout
    assert "older scan, 0 arguments" in scan.stdout
