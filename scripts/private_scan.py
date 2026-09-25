"""Refuse private terms in this public repository (docs/DESIGN.md, section 8).

The design under docs/ was distilled from private notes. This scan reads what
git will publish: committed blobs, straight from the object store with
`git cat-file --batch`, never the checkout and never through gitattributes, so
no attribute, filter or checkout encoding the candidate carries decides what is
read. Every blob, text or binary, is read as UTF-8 with replacement and as UTF-16
and UTF-32 in both byte orders at every alignment; none is skipped. A Git LFS
pointer is refused, found the way git-lfs finds one.

Three modes:
  (default)            the index, plus new and changed files in the working tree,
                       the author and committer identity git would record, and
                       the current branch name: what the next commit holds.
  --range BASE..HEAD   every blob added or changed by each commit of the range,
                       the whole tree at HEAD, every commit object whole (author,
                       committer, other headers and message) and the branch
                       name: everything a pull request publishes.
  --message FILE       the message a commit-msg hook is handed, before git makes
                       the commit that publishes it.

The term list is scripts/private_terms.txt next to this file (in CI, the base
revision's copy). Each line of it is blank or one entry: a kind, one space, and
the term's UTF-8 bytes in lowercase hex. Hex, so the public list is not
searchable text and an older scan that greps every file does not flag it; it is
not a secret, anyone can decode it. The kinds:
  text      a fixed string, matched without regard to case
  regex     a Python regular expression, matched with case, line by line
  identity  an exact `Name <email>` pair, compared without regard to case,
            that a commit's author or committer header may carry although a
            term matches it: the owner's own commit identity, which every
            commit already publishes. It exempts that header only; the same
            words anywhere else, and any other identity, are still refused.
The list takes no comments: a free-text line could carry a term past the scan,
so any other line fails the list, and in a tree the scan reads the list at
`scripts/private_terms.txt` like any other file less its entry lines, and
reports each line there that is neither blank nor an entry.

The list covers local home and workspace paths, the owner's business name,
personal mail addresses and the owner's commit identity (Q22 in TODO.HUMAN.md
asks whether it moves to the GitHub noreply address), business services and
clients, private tools and repositories, machine names and private paths,
subscription details, private watch tools, and the citation keys of the private
design notes. A change to it needs the owner's review: CODEOWNERS names it.

Add a term:     bash scripts/private_scan.sh --encode text 'the term'
Read the list:  bash scripts/private_scan.sh --show-terms

Runs on the system python3 from $RUNNER_TEMP in CI, outside any project
environment, so it uses the standard library alone.

Exit: 0 and no output when clean; 1 with each hit on its own line; 2 on error.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# The term list's path in any tree: its entry lines are the only content the
# scan does not read, since each is a private term by design.
TERMS_PATH = "scripts/private_terms.txt"
# git-lfs reads at most this many bytes of a blob when it looks for a pointer.
LFS_WINDOW = 1024
# The spec URLs git-lfs accepts on a pointer's version line (v1Aliases in its
# lfs/pointer.go). The content a pointer stands for lives outside the repository,
# where the scan cannot read it.
LFS_SPECS = (
    "https://git-lfs.github.com/spec/v1",
    "https://hawser.github.com/spec/v1",
    "http://git-media.io/v/2",
)
# Every multi-byte reading and its code unit width: each is decoded at every
# alignment, so a stray leading byte cannot shift a text out of reach.
WIDE = (("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4))
# A hit shows at most this much of its line: a binary blob can be one long line.
SHOWN = 200
# Mode of a submodule entry: a commit in another repository, no blob here.
GITLINK = b"160000"
KINDS = ("text", "regex", "identity")
# One entry of the term list, the whole line: a kind, one space, and at least
# one byte in lowercase hex. Nothing else may follow, so no text rides along.
ENTRY = re.compile(r"(text|regex|identity) ((?:[0-9a-f]{2})+)")
ENTRY_BYTES = re.compile(rb"(?:text|regex|identity) (?:[0-9a-f]{2})+")
# The commit headers that carry an identity, `Name <email> time zone`.
IDENTITY_HEADERS = ("author", "committer")
# Git's scissors line: git drops it and everything below it from the message.
SCISSORS = b"# ------------------------ >8 ------------------------"


NOT_AN_ENTRY = (
    "neither blank nor an entry `<kind> <UTF-8 in lowercase hex>`; the list "
    f"takes no comments (kinds: {', '.join(KINDS)})"
)


class ScanError(Exception):
    """Anything that stops the scan from reading what it must: exit 2."""


@dataclass(frozen=True)
class Terms:
    texts: tuple[str, ...]  # lowercased: matched without regard to case
    patterns: tuple[re.Pattern[str], ...]
    # Lowercased `Name <email>` pairs an author or committer header may carry.
    identities: frozenset[str] = frozenset()

    def find(self, line: str) -> int | None:
        """Where in `line` the first term found starts, or None."""
        folded = line.lower()
        starts = [folded.find(t) for t in self.texts]
        starts += [m.start() for p in self.patterns if (m := p.search(line))]
        found = [start for start in starts if start >= 0]
        return min(found) if found else None

    def match(self, line: str) -> bool:
        return self.find(line) is not None


def parse_terms(path: Path) -> list[tuple[str, str]]:
    """The (kind, term) pairs of a term list; a malformed line fails the scan
    rather than silently dropping a term."""
    try:
        # Bytes, not read_text: universal newlines would hide a carriage return.
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(f"cannot read the term list {path}: {err}") from err
    pairs = []
    # Split on newlines alone, as the scan reads the list in a tree, so a stray
    # carriage return fails here too.
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        entry = ENTRY.fullmatch(line)
        if entry is None:
            raise ScanError(f"{path}:{number}: {NOT_AN_ENTRY}")
        kind, encoded = entry.groups()
        try:
            term = bytes.fromhex(encoded).decode("utf-8")
        except ValueError as err:
            raise ScanError(f"{path}:{number}: the term is not UTF-8 in hex") from err
        pairs.append((kind, term))
    if not pairs:
        # An empty list would pass every file; that is a broken list, not a clean
        # repository.
        raise ScanError(f"{path}: no terms")
    return pairs


def load_terms(path: Path) -> Terms:
    texts, patterns, identities = [], [], set()
    for kind, term in parse_terms(path):
        if kind == "text":
            texts.append(term.lower())
            continue
        if kind == "identity":
            identities.add(term.lower())
            continue
        try:
            patterns.append(re.compile(term))
        except re.error as err:
            raise ScanError(f"{path}: a regex does not compile: {err}") from err
    return Terms(tuple(texts), tuple(patterns), frozenset(identities))


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

    def read(self, oid: str, kind: bytes = b"blob") -> bytes:
        stdin, stdout = self.proc.stdin, self.proc.stdout
        assert stdin is not None and stdout is not None
        stdin.write(oid.encode("ascii") + b"\n")
        stdin.flush()
        header = stdout.readline().split()
        if len(header) != 3 or header[1] != kind:
            name = kind.decode("ascii")
            raise ScanError(f"object {oid} is not a readable {name}: {header!r}")
        data = stdout.read(int(header[2]))
        stdout.read(1)  # the newline after the content
        return data

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        self.proc.wait()


def readings(data: bytes) -> list[str]:
    """Every reading of a blob. No blob counts as binary: a guess at binary can
    be forced with a NUL byte and an invalid code unit, and would then hide the
    whole file, while GitHub's raw download still serves its bytes. UTF-8 with
    replacement keeps every ASCII byte in place, so it also reads an ASCII term
    inside a binary format or a legacy single-byte encoding."""
    out = [data.decode("utf-8", "replace")]
    for codec, width in WIDE:
        out += [data[start:].decode(codec, "replace") for start in range(width)]
    return out


def lfs_pointer(data: bytes) -> bool:
    """A blob git-lfs would take for a pointer, found as git-lfs finds one: in
    its first 1024 bytes, after surrounding whitespace is trimmed, a version
    line naming an accepted spec. A version line followed by an `oid` line also
    counts, so a spec git-lfs adds later is refused too."""
    head = data[:LFS_WINDOW].decode("utf-8", "replace").strip()
    if not head.startswith("version"):
        return False
    first, _, rest = head.partition("\n")
    return any(spec in first for spec in LFS_SPECS) or any(
        line.lstrip().startswith("oid ") for line in rest.split("\n")
    )


def printable(text: str) -> str:
    """A candidate's text made safe to print into a terminal or a CI log."""
    return "".join(c if c.isprintable() or c == "\t" else "?" for c in text)


def shown(line: str, start: int) -> str:
    """The part of a hit's line printed: all of it when short, else a window
    around the term."""
    line = line.rstrip("\r")
    if len(line) <= SHOWN:
        return printable(line)
    left = max(0, start - SHOWN // 4)
    return f"...{printable(line[left : left + SHOWN])}..."


def content_hits(where: str, data: bytes, terms: Terms) -> list[str]:
    if lfs_pointer(data):
        return [
            f"{where}: a Git LFS pointer; the scan cannot read the content it "
            "stands for, so commit the file itself"
        ]
    hits = []
    for text in readings(data):
        for number, line in enumerate(text.split("\n"), 1):
            start = terms.find(line)
            if start is not None:
                hits.append(f"{where}:{number}:{shown(line, start)}")
    return hits


def term_list_hits(where: str, data: bytes, terms: Terms) -> list[str]:
    """The term list as a tree carries it: every line that is not an entry is
    read like any other file's, and reported, so a comment cannot carry a term
    past the scan. An entry line is blanked in place, keeping line numbers."""
    lines = data.split(b"\n")
    hits = [
        f"{where}:{number}: {NOT_AN_ENTRY}"
        for number, line in enumerate(lines, 1)
        if line.strip() and not ENTRY_BYTES.fullmatch(line)
    ]
    kept = [b"" if ENTRY_BYTES.fullmatch(line) else line for line in lines]
    return hits + content_hits(where, b"\n".join(kept), terms)


def blob_hits(path: str, where: str, data: bytes, terms: Terms) -> list[str]:
    if path == TERMS_PATH:
        return term_list_hits(where, data, terms)
    return content_hits(where, data, terms)


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
        self.seen: set[tuple[str, bool]] = set()
        self.hits: list[str] = []

    def text(self, where: str, text: str) -> None:
        """A name or a message: one line each, as git and GitHub show it."""
        self.hits += content_hits(where, text.encode("utf-8"), self.terms)

    def entry(self, entry: Entry, where: str) -> None:
        if self.terms.match(entry.path):
            self.hits.append(f"{where}: the path names a private term")
        if entry.mode == GITLINK:
            return
        # One blob is read once per way of reading it, under the first path that
        # names it: at the term list's path its entry lines are not read, so the
        # same blob anywhere else is read again, whole.
        key = (entry.oid, entry.path == TERMS_PATH)
        if key in self.seen:
            return
        self.seen.add(key)
        data = self.blobs.read(entry.oid)
        self.hits += blob_hits(entry.path, where, data, self.terms)

    def branch(self, name: str | None) -> None:
        if name:
            self.text("(branch name)", name)

    def commit(self, sha: str) -> None:
        """The whole raw commit object: every header (author, committer, and any
        other, such as encoding or an embedded tag) under its own name, and
        the message, since GitHub publishes all of it."""
        raw = self.blobs.read(sha, b"commit")
        headers, _, body = raw.partition(b"\n\n")
        short = sha[:12]
        for name, value in commit_headers(headers):
            if name in IDENTITY_HEADERS:
                value = self.unlisted(value)
            self.hits += content_hits(f"{short} ({name})", value, self.terms)
        self.hits += content_hits(f"{short} (message)", body, self.terms)

    def identity(self) -> None:
        """The author and committer identity the next commit would record,
        which the commit publishes. A repository with no identity set has none
        to publish yet, so a failure here is not an error."""
        for name in ("author", "committer"):
            done = subprocess.run(
                ["git", "var", f"GIT_{name.upper()}_IDENT"],
                capture_output=True,
                check=False,
            )
            if done.returncode == 0:
                value = self.unlisted(done.stdout.rstrip(b"\n"))
                self.hits += content_hits(f"({name})", value, self.terms)

    def unlisted(self, value: bytes) -> bytes:
        """An identity header's value, less its `Name <email>` when the term
        list names that exact pair as one the owner commits under. Any other
        identity is matched whole, so a work address is still found."""
        pair, mark, when = value.rpartition(b"> ")
        if not mark:
            return value
        text = (pair + b">").decode("utf-8", "replace").lower()
        return when if text in self.terms.identities else value


def commit_headers(headers: bytes) -> list[tuple[str, bytes]]:
    """A commit's headers as (name, value) pairs, a line starting with a space
    continuing the header above it, as git writes a signature or a tag."""
    pairs: list[tuple[str, bytes]] = []
    for line in headers.split(b"\n"):
        if line.startswith(b" ") and pairs:
            name, value = pairs[-1]
            pairs[-1] = (name, value + b"\n" + line[1:])
            continue
        name, _, value = line.partition(b" ")
        pairs.append((printable(name.decode("utf-8", "replace")), value))
    return pairs


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
        if not raw:
            continue
        if scan.terms.match(path):
            scan.hits.append(f"{path}: the path names a private term")
        full = Path(os.fsdecode(raw))
        if full.is_symlink() or not full.is_file():
            continue
        scan.hits += blob_hits(path, path, full.read_bytes(), scan.terms)


def scan_index(scan: Scan) -> None:
    for entry in index_entries():
        scan.entry(entry, entry.path)
    scan_worktree(scan)
    scan.identity()


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
        scan.commit(sha)
    for entry in tree_entries(head):
        scan.entry(entry, f"{head[:12]}:{entry.path}")


def scan_message(scan: Scan, path: Path) -> None:
    """A commit message as the commit-msg hook gets it, before git cleans it.
    Comment lines are read too, since `git commit -m` keeps them; only what
    follows the scissors line is left out, since git drops it in every mode that
    writes that line."""
    try:
        data = path.read_bytes()
    except OSError as err:
        raise ScanError(f"cannot read the message {path}: {err}") from err
    kept = []
    for line in data.split(b"\n"):
        if line.rstrip(b"\r") == SCISSORS:
            break
        kept.append(line)
    scan.hits += content_hits("(message)", b"\n".join(kept), scan.terms)


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
        "--message",
        type=Path,
        metavar="FILE",
        help="scan only this commit message file, as the commit-msg hook hands it",
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
        if args.message and args.range:
            raise ScanError("--message and --range are separate modes")
        terms = load_terms(args.terms)
        # Resolved before the scan moves to the top of the work tree, since git
        # hands the hook a path relative to where it runs.
        message = args.message.resolve() if args.message else None
        os.chdir(git("rev-parse", "--show-toplevel").decode().strip())
        blobs = Blobs()
        try:
            scan = Scan(terms, blobs)
            if message:
                scan_message(scan, message)
            elif args.range:
                scan_range(scan, args.range)
            else:
                scan_index(scan)
            if not message or args.branch:
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
