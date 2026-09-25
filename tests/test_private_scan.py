"""The private scan finds a private term wherever the repository publishes it.

CI runs the base revision's copy of `scripts/private_scan.sh`, `private_scan.py`
and `private_terms.txt` against the candidate (docs/DESIGN.md, section 8). The
scan reads committed blobs, never the checkout and never through gitattributes,
text or binary alike, and with `--range` every commit object whole (identities,
headers and message) and the branch name. Each test runs the base's copy from a
folder outside the checkout, as CI does.
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
LFS_BODY = "oid sha256:" + "4" * 64 + "\nsize 1234\n"
LFS_POINTER = "version https://git-lfs.github.com/spec/v1\n" + LFS_BODY
# The identity every fixture commit and every local scan runs under, unless a
# test sets its own: the host's git identity must not decide a result.
FIXTURE_IDENTITY = {
    f"GIT_{role}_{part}": value
    for role in ("AUTHOR", "COMMITTER")
    for part, value in (("NAME", "fixture"), ("EMAIL", "fixture@example.invalid"))
}


def base_copy(folder: Path) -> Path:
    """The scan as CI holds it: the three files, together, outside the checkout."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in SCAN_FILES:
        shutil.copy2(REPO / "scripts" / name, folder / name)
    return folder / "private_scan.sh"


def run_scan(
    root: Path, *args: str, terms: str | None = None
) -> subprocess.CompletedProcess[str]:
    scan = base_copy(root.parent / "gates")
    if terms is not None:
        (scan.parent / "private_terms.txt").write_text(terms, encoding="utf-8")
    return subprocess.run(
        ["bash", str(scan), *args],
        cwd=root,
        env=FIXTURE_IDENTITY | dict(os.environ),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def run_range(
    root: Path, base: str, *args: str, terms: str | None = None
) -> subprocess.CompletedProcess[str]:
    return run_scan(root, "--range", f"{base}..HEAD", *args, terms=terms)


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


def png_with(text: bytes) -> bytes:
    """A PNG-shaped file carrying `text` as plain bytes, as a tEXt chunk does,
    among NUL bytes and repeated 0xD8 bytes, which make a lone UTF-16 surrogate
    in every byte order and alignment: binary by any content test."""
    body = zlib.compress(bytes(range(256)) * 8) + text + b"\xd8" * 8
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + body + b"\x00" * 4


@pytest.mark.parametrize(
    "data",
    [
        png_with(TEXT.encode()),
        # A NUL byte and a lone surrogate in every UTF-16 reading, in front of
        # plain UTF-8 anyone can read in the raw download.
        b"\x00\xdc\xdc\xdc\xdc" + TEXT.encode(),
        TEXT.encode("utf-32"),
        TEXT.encode("utf-32-le"),
        TEXT.encode("utf-32-be"),
        # One stray byte in front shifts every UTF-32 code unit.
        b"#" + TEXT.encode("utf-32-le"),
    ],
    ids=[
        "png-text-chunk",
        "nul-surrogate",
        "utf32-bom",
        "utf32-le",
        "utf32-be",
        "utf32-shifted",
    ],
)
def test_no_blob_is_skipped_as_binary(repo: Path, base: str, data: bytes) -> None:
    # A guess at binary can be forced, and would hide the whole file; so every
    # blob gets every reading.
    (repo / "docs").mkdir()
    (repo / "docs/a.bin").write_bytes(data)
    assert_found(run_scan(repo), "docs/a.bin:", f"notes on {TERM}")
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), "docs/a.bin:", f"notes on {TERM}")
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.bin:")


def test_a_hit_in_one_long_line_shows_a_window_around_the_term(
    repo: Path, base: str
) -> None:
    write(repo, "docs/a.txt", "x" * 5000 + f" {TERM} " + "y" * 5000)
    commit_all(repo, "candidate")
    lines = run_range(repo, base).stdout.splitlines()
    assert len(lines) == 1, lines
    assert TERM in lines[0] and len(lines[0]) < 300, lines


def test_a_real_binary_without_a_term_passes(repo: Path, base: str) -> None:
    (repo / "logo.png").write_bytes(png_with(b"a public logo"))
    commit_all(repo, "candidate")
    for done in (run_scan(repo), run_range(repo, base)):
        assert (done.returncode, done.stdout) == (0, ""), done.stdout


@pytest.mark.parametrize(
    "pointer",
    [
        LFS_POINTER,
        # git-lfs trims surrounding whitespace before it parses a pointer.
        "\n" + LFS_POINTER,
        "   " + LFS_POINTER,
        "\t" + LFS_POINTER + "\n\n",
        "\u00a0" + LFS_POINTER,
        # The two older spec URLs git-lfs still accepts.
        "version https://hawser.github.com/spec/v1\n" + LFS_BODY,
        "version http://git-media.io/v/2\n" + LFS_BODY,
        # A spec URL git-lfs may accept later, found by its oid line.
        "version https://git-lfs.example/spec/v9\n" + LFS_BODY,
    ],
    ids=[
        "canonical",
        "newline",
        "spaces",
        "tab",
        "nbsp",
        "hawser",
        "git-media",
        "unknown-spec",
    ],
)
def test_a_git_lfs_pointer_is_refused(repo: Path, base: str, pointer: str) -> None:
    # The pointer names content stored outside the repository, which the scan
    # cannot read, and git-lfs uploads that content on push; the file itself has
    # to be committed instead.
    write(repo, ".gitattributes", "*.md filter=lfs diff=lfs merge=lfs -text\n")
    write(repo, "docs/a.md", pointer)
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), "docs/a.md: a Git LFS pointer")
    assert_found(run_range(repo, base), f"{short(repo)}:docs/a.md: a Git LFS pointer")


def test_a_file_that_only_mentions_a_version_is_not_a_pointer(
    repo: Path, base: str
) -> None:
    write(repo, "docs/a.md", "version 2 of the notes\nsize matters\n")
    commit_all(repo, "candidate")
    for done in (run_scan(repo), run_range(repo, base)):
        assert (done.returncode, done.stdout) == (0, ""), done.stdout


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


def test_a_term_in_a_message_file_is_found_before_the_commit(
    repo: Path, base: str
) -> None:
    # The commit-msg hook hands the scan git's message file by a path relative to
    # where it runs, before git cleans it: a `#` line survives `git commit -m`.
    write(repo, "sub/msg.txt", f"Add notes\n\n# from {TERM}\n")
    done = run_scan(repo / "sub", "--message", "msg.txt")
    assert_found(done, f"(message):3:# from {TERM}")
    assert "msg.txt" not in done.stdout and "(branch name)" not in done.stdout
    # Git drops the scissors line and all below it, where `commit -v` shows the diff.
    scissors = "# " + "-" * 24 + " >8 " + "-" * 24
    write(repo, "msg.txt", f"Add notes\n{scissors}\n-see {KEY}\n")
    done = run_scan(repo, "--message", "msg.txt")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert run_scan(repo, "--message", "gone.txt").returncode == 2
    both = run_scan(repo, "--message", "msg.txt", "--range", f"{base}..HEAD")
    assert both.returncode == 2 and "separate modes" in both.stderr


@pytest.mark.parametrize("role", ["AUTHOR", "COMMITTER"])
def test_a_term_in_a_commit_identity_is_found(
    repo: Path, base: str, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A commit made under a work identity publishes the employer's domain.
    monkeypatch.setenv(f"GIT_{role}_EMAIL", f"tom@{TERM}.example")
    assert_found(run_scan(repo), f"({role.lower()}):1:fixture <tom@{TERM}.example>")
    write(repo, "docs/a.md", "plain\n")
    sha = commit_all(repo, "plain")
    monkeypatch.delenv(f"GIT_{role}_EMAIL")
    assert run_scan(repo).returncode == 0
    assert_found(
        run_range(repo, base), f"{sha[:12]} ({role.lower()}):1:fixture <tom@{TERM}"
    )


def test_a_term_in_any_other_commit_header_is_found(repo: Path, base: str) -> None:
    # The whole commit object is published, so a header git does not write
    # itself is read too.
    tree = git(repo, "write-tree")
    raw = (
        f"tree {tree}\nparent {base}\n"
        "author fixture <fixture@example.invalid> 0 +0000\n"
        "committer fixture <fixture@example.invalid> 0 +0000\n"
        f"x-note see {TERM}\n\nplain\n"
    )
    sha = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--stdin",
            "--literally",
        ],
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(repo, "reset", "-q", "--soft", sha)
    assert_found(run_range(repo, base), f"{sha[:12]} (x-note):1:see {TERM}")


def test_only_a_listed_identity_is_exempt(
    repo: Path, base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner commits under a listed identity, which every commit already
    # publishes; the same words elsewhere and any other identity are refused.
    terms = (
        "text " + b"owner".hex() + "\n"
        "identity " + b"Owner <owner@home.example>".hex() + "\n"
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Owner")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "OWNER@home.example")
    write(repo, "docs/a.md", "plain\n")
    listed = commit_all(repo, "first")
    assert run_scan(repo, terms=terms).returncode == 0
    assert run_range(repo, base, terms=terms).returncode == 0
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "owner@work.example")
    write(repo, "docs/a.md", "Owner <owner@home.example>\n")
    other = commit_all(repo, "second")
    done = run_range(repo, base, terms=terms)
    assert_found(
        done,
        f"{other[:12]} (author):1:Owner <owner@work.example>",
        f"{other[:12]}:docs/a.md:1:Owner <owner@home.example>",
    )
    assert listed[:12] not in done.stdout
    assert_found(run_scan(repo, terms=terms), "(author):1:Owner <owner@work")


def test_the_shipped_list_exempts_no_identity() -> None:
    # This repository commits under the GitHub noreply address (Q22, ADR 0002),
    # so no commit identity is exempt and one under a listed address is refused.
    # Only the kinds are read: a term itself must never reach a test's output.
    lines = (REPO / TERMS_PATH).read_text(encoding="utf-8").splitlines()
    kinds = {line.split(" ", 1)[0] for line in lines if line}
    assert "identity" not in kinds
    assert kinds <= {"text", "regex"}


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
                    re.search(term, line)
                    if kind == "regex"
                    else term.lower() in line.lower()
                )
                assert not hit, (name, line)


NOT_AN_ENTRY = "neither blank nor an entry"
# A term spelled only in hex digits, and an entry whose hex spells it: the entry
# line is the one thing the scan does not read, and only at the list's path.
HEX_TERM = "a6b"
HEX_TERMS = f"text {HEX_TERM.encode().hex()}\n"
HEX_ENTRY = f"text {b'jk'.hex()}"


def test_only_the_term_lists_entry_lines_at_its_exact_path_are_not_read(
    repo: Path,
) -> None:
    assert HEX_TERM in HEX_ENTRY
    others = (f"{TERMS_PATH}.orig", "docs/private_terms.txt", "private_terms.txt")
    # The list and the copy beside it are one blob, read once each way: the
    # scan reads it at the list's path first, then again, whole, at the other.
    write(repo, TERMS_PATH, f"\n{HEX_ENTRY}\n")
    write(repo, others[0], f"\n{HEX_ENTRY}\n")
    for other in others[1:]:
        write(repo, other, f"{other}\n{HEX_ENTRY}\n")
    commit_all(repo, "candidate")
    done = run_scan(repo, terms=HEX_TERMS)
    lines = done.stdout.splitlines()
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert not [line for line in lines if f"{TERMS_PATH}:" in line], lines
    for other in others:
        assert f"{other}:2:{HEX_ENTRY}" in lines, (other, lines)


def test_a_term_in_a_comment_in_the_term_list_is_found(repo: Path, base: str) -> None:
    # A comment is no entry: it is read like any other line, and refused as well.
    listed = f"text {KEY.encode().hex()}\n# notes on {TERM}\n"
    write(repo, TERMS_PATH, listed)
    hits = (f"{TERMS_PATH}:2:# notes on {TERM}", f"{TERMS_PATH}:2: {NOT_AN_ENTRY}")
    assert_found(run_scan(repo), *hits)
    commit_all(repo, "candidate")
    assert_found(run_scan(repo), *hits)
    assert_found(run_range(repo, base), *(f"{short(repo)}:{hit}" for hit in hits))


@pytest.mark.parametrize(
    "line",
    [
        "a plain sentence",
        "# a comment",
        f"text {KEY.encode().hex()} trailing words",
        HEX_ENTRY.upper(),
        HEX_ENTRY.replace("text", "TEXT"),
        f" {HEX_ENTRY}",
        f"{HEX_ENTRY} ",
        "text",
        "text 6",
    ],
)
def test_an_unknown_line_in_the_term_list_is_refused(
    repo: Path, base: str, line: str
) -> None:
    write(repo, TERMS_PATH, f"text {KEY.encode().hex()}\n{line}\n")
    commit_all(repo, "candidate")
    hit = f"{TERMS_PATH}:2: {NOT_AN_ENTRY}"
    assert_found(run_scan(repo), hit)
    assert_found(run_range(repo, base), f"{short(repo)}:{hit}")


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
        ("\n \n", "no terms"),
        ("# a comment\ntext 6162\n", f"{NOT_AN_ENTRY}"),
        ("text 6162\nfree text\n", ":2: neither blank nor an entry"),
        ("text\n", NOT_AN_ENTRY),
        ("word 6162\n", NOT_AN_ENTRY),
        ("text zz\n", NOT_AN_ENTRY),
        ("text 6162 # a note\n", NOT_AN_ENTRY),
        ("text 6162\r\n", NOT_AN_ENTRY),
        ("text ff\n", "not UTF-8 in hex"),
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
    """The gates job's step that takes the scan from the base, then the scan."""
    steps = yaml.safe_load(CI.read_text("utf-8"))["jobs"]["gates"]["steps"]
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
    # The base and the head by commit id, as the event payload names them.
    base = git(root, "rev-parse", "main")
    environ |= {"RUNNER_TEMP": str(runner_temp), "BASE_SHA": base}
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
    # The other gates the same step takes from the base, as stand-ins.
    for gate in (REPO / "scripts").glob("ci_*.sh"):
        write(root, f"scripts/{gate.name}", "exit 0\n")
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
