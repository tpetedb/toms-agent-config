"""Refuse private terms in this public repository (docs/DESIGN.md, section 8).

The design under docs/ was distilled from private notes. This scan reads what
git will publish: committed blobs, straight from the object store with
`git cat-file --batch`, never the checkout and never through gitattributes, so
no attribute, filter or checkout encoding the candidate carries decides what is
read. Every text blob is read as UTF-8 with replacement and as UTF-16 in both
byte orders; only a blob that is binary by content is skipped.

Two modes:
  (default)            the index, plus new and changed files in the working tree,
                       and the current branch name: what the next commit holds.
  --range BASE..HEAD   every blob added or changed by each commit of the range,
                       the whole tree at HEAD, every commit message and the
                       branch name: everything a pull request publishes.

The term list is scripts/private_terms.txt next to this file (in CI, the base
revision's copy); the one path whose content the scan skips is that list's own
path, `scripts/private_terms.txt`, exactly.

Runs on the system python3 from $RUNNER_TEMP in CI, outside any project
environment, so it uses the standard library alone.

Exit: 0 and no output when clean; 1 with each hit on its own line; 2 on error.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# The term list's path in any tree: the only content the scan does not read.
TERMS_PATH = "scripts/private_terms.txt"
# Git's own window for deciding a blob is binary by a NUL byte.
SNIFF = 8000
# The first line of a Git LFS pointer, current and pre-release spec. The real
# content lives outside the repository, where the scan cannot read it.
LFS_PREFIXES = (
    b"version https://git-lfs.github.com/spec/v1",
    b"version https://hawser.github.com/spec/v1",
)
UTF16 = ("utf-16-le", "utf-16-be")
# Mode of a submodule entry: a commit in another repository, no blob here.
GITLINK = b"160000"
KINDS = ("text", "regex")


class ScanError(Exception):
    """Anything that stops the scan from reading what it must: exit 2."""


@dataclass(frozen=True)
class Terms:
    texts: tuple[str, ...]  # lowercased: matched without regard to case
    patterns: tuple[re.Pattern[str], ...]

    def match(self, line: str) -> bool:
        folded = line.lower()
        return any(t in folded for t in self.texts) or any(
            p.search(line) for p in self.patterns
        )


def parse_terms(path: Path) -> list[tuple[str, str]]:
    """The (kind, term) pairs of a term list; a malformed line fails the scan
    rather than silently dropping a term."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ScanError(f"cannot read the term list {path}: {err}") from err
    pairs = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, encoded = line.partition(" ")
        if kind not in KINDS:
            raise ScanError(f"{path}:{number}: the kind must be one of {KINDS}")
        try:
            term = bytes.fromhex(encoded).decode("utf-8")
        except ValueError as err:
            raise ScanError(f"{path}:{number}: the term is not UTF-8 in hex") from err
        if not term:
            raise ScanError(f"{path}:{number}: an empty term matches everything")
        pairs.append((kind, term))
    if not pairs:
        # An empty list would pass every file; that is a broken list, not a clean
        # repository.
        raise ScanError(f"{path}: no terms")
    return pairs


def load_terms(path: Path) -> Terms:
    texts, patterns = [], []
    for kind, term in parse_terms(path):
        if kind == "text":
            texts.append(term.lower())
            continue
        try:
            patterns.append(re.compile(term))
        except re.error as err:
            raise ScanError(f"{path}: a regex does not compile: {err}") from err
    return Terms(tuple(texts), tuple(patterns))


def git(*args: str) -> bytes:
    done = subprocess.run(["git", *args], capture_output=True, check=False)
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip()
        raise ScanError(f"git {' '.join(args)} failed: {message}")
    return done.stdout


class Blobs:
    """Raw blob content from one `git cat-file --batch`: no smudge filter, no
    textconv, no working-tree-encoding, whatever the attributes say."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def read(self, oid: str) -> bytes:
        stdin, stdout = self.proc.stdin, self.proc.stdout
        assert stdin is not None and stdout is not None
        stdin.write(oid.encode("ascii") + b"\n")
        stdin.flush()
        header = stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise ScanError(f"object {oid} is not a readable blob: {header!r}")
        data = stdout.read(int(header[2]))
        stdout.read(1)  # the newline after the content
        return data

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        self.proc.wait()


def utf16_readings(data: bytes, errors: str) -> list[str]:
    """The UTF-16 readings of `data` in both byte orders and at both alignments,
    so one stray leading byte cannot shift a UTF-16 text out of reach."""
    out = []
    for start in (0, 1):
        for codec in UTF16:
            with contextlib.suppress(UnicodeDecodeError):
                out.append(data[start:].decode(codec, errors))
    return out


def readings(data: bytes) -> list[str] | None:
    """Every text reading of a blob, or None when it is binary by content: a
    NUL byte in the first 8000 bytes and no valid UTF-16 reading. git grep -I
    would call a UTF-16 text binary for its NUL bytes, while GitHub shows it."""
    if b"\0" in data[:SNIFF] and not utf16_readings(data, "strict"):
        return None
    return [data.decode("utf-8", "replace"), *utf16_readings(data, "replace")]


def printable(text: str) -> str:
    """A candidate's text made safe to print into a terminal or a CI log."""
    return "".join(c if c.isprintable() or c == "\t" else "?" for c in text)


def content_hits(where: str, data: bytes, terms: Terms) -> list[str]:
    if data.startswith(LFS_PREFIXES):
        return [
            f"{where}: a Git LFS pointer; the scan cannot read the content it "
            "stands for, so commit the file itself"
        ]
    texts = readings(data)
    if texts is None:
        return []
    hits = []
    for text in texts:
        for number, line in enumerate(text.split("\n"), 1):
            if terms.match(line):
                hits.append(f"{where}:{number}:{printable(line.rstrip(chr(13)))}")
    return hits


@dataclass(frozen=True)
class Entry:
    """One path of a tree, index or diff: its mode, blob id and path."""

    mode: bytes
    oid: str
    path: str


class Scan:
    def __init__(self, terms: Terms, blobs: Blobs) -> None:
        self.terms = terms
        self.blobs = blobs
        self.seen: set[str] = set()
        self.hits: list[str] = []

    def text(self, where: str, text: str) -> None:
        """A name or a message: one line each, as git and GitHub show it."""
        self.hits += content_hits(where, text.encode("utf-8"), self.terms)

    def entry(self, entry: Entry, where: str) -> None:
        if self.terms.match(entry.path):
            self.hits.append(f"{where}: the path names a private term")
        if entry.mode == GITLINK or entry.path == TERMS_PATH:
            return
        # One blob is read once, under the first path that names it.
        if entry.oid in self.seen:
            return
        self.seen.add(entry.oid)
        self.hits += content_hits(where, self.blobs.read(entry.oid), self.terms)

    def branch(self, name: str | None) -> None:
        if name:
            self.text("(branch name)", name)


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def index_entries() -> Iterator[Entry]:
    """`git ls-files --stage -z`: mode, blob id, stage, a tab and the path."""
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        info, _, path = record.partition(b"\t")
        mode, oid, _stage = info.split()
        yield Entry(mode, oid.decode("ascii"), decode_path(path))


def tree_entries(rev: str) -> Iterator[Entry]:
    """`git ls-tree -r -z`: mode, type, blob id, a tab and the path."""
    for record in git("ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not record:
            continue
        info, _, path = record.partition(b"\t")
        mode, _kind, oid = info.split()
        yield Entry(mode, oid.decode("ascii"), decode_path(path))


def changed_entries(parent: str, commit: str) -> Iterator[Entry]:
    """Every path `commit` adds or changes against `parent`, from the raw diff:
    `:old-mode new-mode old-id new-id status`, then the path."""
    raw = git("diff-tree", "-r", "-z", "--raw", "--no-renames", parent, commit)
    fields = raw.split(b"\0")
    for i in range(0, len(fields) - 1, 2):
        info, path = fields[i].split(), fields[i + 1]
        if not info:
            continue
        new_mode, new_oid, status = info[1], info[3], info[4]
        if status.startswith(b"D"):
            continue
        yield Entry(new_mode, new_oid.decode("ascii"), decode_path(path))


def empty_tree() -> str:
    return git("hash-object", "-t", "tree", "/dev/null").decode().strip()


def current_branch() -> str | None:
    done = subprocess.run(
        ["git", "symbolic-ref", "-q", "--short", "HEAD"],
        capture_output=True,
        check=False,
    )
    name = done.stdout.decode("utf-8", "replace").strip()
    return name or None


def scan_worktree(scan: Scan) -> None:
    """New files git does not ignore and tracked files changed since staging:
    not blobs yet, so their bytes are read as they stand. They have not been
    through a checkout, so no attribute has converted them."""
    listed = git("ls-files", "-z", "--others", "--exclude-standard", "--modified")
    for raw in dict.fromkeys(listed.split(b"\0")):
        path = decode_path(raw)
        if not raw or path == TERMS_PATH:
            continue
        if scan.terms.match(path):
            scan.hits.append(f"{path}: the path names a private term")
        full = Path(os.fsdecode(raw))
        if full.is_symlink() or not full.is_file():
            continue
        scan.hits += content_hits(path, full.read_bytes(), scan.terms)


def scan_index(scan: Scan) -> None:
    for entry in index_entries():
        scan.entry(entry, entry.path)
    scan_worktree(scan)


def commit(rev: str) -> str:
    return (
        git("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
        .decode("ascii")
        .strip()
    )


def scan_range(scan: Scan, spec: str) -> None:
    base, dots, head = spec.partition("..")
    if not dots or not base or not head or head.startswith("."):
        raise ScanError(f"--range takes BASE..HEAD, not {spec!r}")
    base, head = commit(base), commit(head)
    empty = empty_tree()
    listed = git("rev-list", "--reverse", "--parents", f"{base}..{head}").decode()
    for line in listed.splitlines():
        sha, *parents = line.split()
        short = sha[:12]
        # A commit's blobs are its first parent's, which the walk has read or
        # the base already published, plus what it adds or changes; so diffing
        # each commit against its first parent reaches every blob in the range,
        # a merge's own resolution included.
        for entry in changed_entries(parents[0] if parents else empty, sha):
            scan.entry(entry, f"{short}:{entry.path}")
        body = git("cat-file", "commit", sha).partition(b"\n\n")[2]
        scan.text(f"{short} (message)", body.decode("utf-8", "replace"))
    for entry in tree_entries(head):
        scan.entry(entry, f"{head[:12]}:{entry.path}")


def arguments(argv: list[str]) -> argparse.Namespace:
    # argparse, not click: CI runs this with the system python3, no packages.
    parser = argparse.ArgumentParser(
        prog="scripts/private_scan.sh",
        description=(
            "Refuse private terms in what this repository publishes. Prints each "
            "hit on its own line and exits 1; exits 0 with no output when clean, "
            "2 on error."
        ),
    )
    parser.add_argument(
        "--range",
        metavar="BASE..HEAD",
        help="scan every commit of the range, its messages and the tip's tree",
    )
    parser.add_argument(
        "--branch",
        metavar="NAME",
        help="the branch name to scan (default: the current branch)",
    )
    parser.add_argument(
        "--terms",
        type=Path,
        default=Path(__file__).resolve().with_name("private_terms.txt"),
        help="the term list (default: private_terms.txt next to this script)",
    )
    parser.add_argument(
        "--encode",
        nargs=2,
        metavar=("KIND", "TERM"),
        help="print the term-list line for a term of kind text or regex, and exit",
    )
    parser.add_argument(
        "--show-terms",
        action="store_true",
        help="print the term list decoded, and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = arguments(argv)
    try:
        if args.encode:
            kind, term = args.encode
            if kind not in KINDS:
                raise ScanError(f"the kind must be one of {KINDS}")
            print(f"{kind} {term.encode('utf-8').hex()}")
            return 0
        if args.show_terms:
            for kind, term in parse_terms(args.terms):
                print(f"{kind}\t{term}")
            return 0
        terms = load_terms(args.terms)
        os.chdir(git("rev-parse", "--show-toplevel").decode().strip())
        blobs = Blobs()
        try:
            scan = Scan(terms, blobs)
            if args.range:
                scan_range(scan, args.range)
            else:
                scan_index(scan)
            scan.branch(args.branch or current_branch())
        finally:
            blobs.close()
    except ScanError as err:
        print(f"private_scan: {err}", file=sys.stderr)
        return 2
    if scan.hits:
        print("\n".join(dict.fromkeys(scan.hits)))
        print("private_scan: private terms found; remove them", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
