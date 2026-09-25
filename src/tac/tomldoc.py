"""The comments of a TOML file, per key.

tomllib reads values and drops comments, and the house style puts the meaning of
every key in its comment: the block directly above it, or the text after it on
the same line. This scanner finds each key and table header and the comment that
documents it, so `tac explain` can print it and a test can require it. It reads
only the shapes the shipped files use (tables, arrays of tables, dotted and
quoted keys, multi-line arrays and strings, inline tables); the test that uses it
also checks that it found exactly the keys tomllib parsed.
"""

from __future__ import annotations

from dataclasses import dataclass

BARE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


@dataclass(frozen=True, slots=True)
class KeyDoc:
    """One key or table header, where it is and what its comment says."""

    path: str
    line: int
    comment: str
    source: str
    table: bool = False


def join(parts: list[str]) -> str:
    """A path as `tac explain` spells it: dotted, with [n] for an array item."""
    out = ""
    for part in parts:
        out += part if part.startswith("[") or not out else "." + part
    return out


def split_key(text: str) -> list[str]:
    """`a."b.c".d` into ["a", "b.c", "d"]."""
    parts: list[str] = []
    i, text = 0, text.strip()
    while i < len(text):
        ch = text[i]
        if ch in " \t.":
            i += 1
        elif ch in "\"'":
            end = text.index(ch, i + 1)
            parts.append(text[i + 1 : end])
            i = end + 1
        else:
            start = i
            while i < len(text) and text[i] in BARE:
                i += 1
            if i == start:
                raise ValueError(f"cannot read the key {text!r}")
            parts.append(text[start:i])
    return parts


class _Scan:
    """Walks one value that may span lines, outside strings, noting comments."""

    def __init__(self, lines: list[str], index: int, start: int) -> None:
        self.lines = lines
        self.row = index
        self.col = start
        self.comments: list[str] = []
        self.value: list[str] = []

    def run(self) -> int:
        depth = 0
        quote = ""
        line = self.lines[self.row]
        while True:
            if self.col >= len(line):
                self.value.append("\n")
                if depth == 0 and not quote:
                    return self.row + 1
                self.row += 1
                if self.row >= len(self.lines):
                    raise ValueError("a value runs past the end of the file")
                line, self.col = self.lines[self.row], 0
                continue
            ch = line[self.col]
            if quote:
                if ch == "\\" and quote in ('"', '"""'):
                    self.value.append(line[self.col : self.col + 2])
                    self.col += 2
                    continue
                if line.startswith(quote, self.col):
                    quote_len = len(quote)
                    self.value.append(quote)
                    self.col += quote_len
                    quote = ""
                    continue
                self.value.append(ch)
                self.col += 1
                continue
            if ch == "#":
                self.comments.append(line[self.col + 1 :].strip())
                self.col = len(line)
                continue
            for opener in ('"""', "'''", '"', "'"):
                if line.startswith(opener, self.col):
                    quote = opener
                    self.value.append(opener)
                    self.col += len(opener)
                    break
            else:
                depth += ch in "[{"
                depth -= ch in "]}"
                self.value.append(ch)
                self.col += 1


def _split_assignment(line: str) -> tuple[str, int]:
    """The key text and the column after `=`, skipping `=` inside quoted keys."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "=":
            return line[:i], i + 1
    raise ValueError(f"not a key = value line: {line!r}")


def inline_keys(value: str) -> list[list[str]]:
    """The key paths inside an inline table value, relative to it."""
    found: list[list[str]] = []

    def skip_ws(i: int) -> int:
        while i < len(value) and value[i] in " \t\n":
            i += 1
        return i

    def skip_scalar(i: int) -> int:
        depth, quote = 0, ""
        while i < len(value):
            ch = value[i]
            if quote:
                if ch == "\\" and quote == '"':
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                if depth == 0:
                    return i
                depth -= 1
            elif ch == "," and depth == 0:
                return i
            i += 1
        return i

    def table(i: int, prefix: list[str]) -> int:
        i = skip_ws(i + 1)  # past "{"
        while i < len(value) and value[i] != "}":
            eq = value.index("=", i)
            key = prefix + split_key(value[i:eq])
            found.append(key)
            i = skip_ws(eq + 1)
            i = table(i, key) if value[i] == "{" else skip_scalar(i)
            i = skip_ws(i)
            if i < len(value) and value[i] == ",":
                i = skip_ws(i + 1)
        return i + 1

    start = skip_ws(0)
    if start < len(value) and value[start] == "{":
        table(start, [])
    return found


def document(text: str) -> dict[str, KeyDoc]:
    """Every key and table header in `text`, keyed by its path."""
    lines = text.splitlines()
    docs: dict[str, KeyDoc] = {}
    above: list[str] = []
    table: list[str] = []
    counts: dict[str, int] = {}
    row = 0
    while row < len(lines):
        raw = lines[row]
        stripped = raw.strip()
        if not stripped:
            above = []
            row += 1
            continue
        if stripped.startswith("#"):
            text = stripped[1:].strip()
            # A banner's ruled line is decoration, not documentation.
            if not text or set(text) - set("=-"):
                above.append(text)
            row += 1
            continue
        if stripped.startswith("["):
            array = stripped.startswith("[[")
            close = stripped.index("]]" if array else "]")
            name = split_key(stripped[2 if array else 1 : close])
            rest = stripped[close + (2 if array else 1) :].strip()
            trailing = [rest[1:].strip()] if rest.startswith("#") else []
            # A header below an array of tables belongs to its latest item.
            resolved: list[str] = []
            for i, part in enumerate(name):
                resolved.append(part)
                if i < len(name) - 1 and join(resolved) in counts:
                    resolved.append(f"[{counts[join(resolved)]}]")
            name = resolved
            if array:
                dotted = join(name)
                counts[dotted] = counts.get(dotted, -1) + 1
                name = [*name, f"[{counts[dotted]}]"]
            table = name
            path = join(table)
            docs[path] = KeyDoc(
                path, row + 1, "\n".join(above + trailing), stripped, table=True
            )
            above = []
            row += 1
            continue
        key_text, col = _split_assignment(raw)
        scan = _Scan(lines, row, col)
        after = scan.run()
        keys = [*table, *split_key(key_text)]
        comment = "\n".join(above + scan.comments)
        source = "\n".join(lines[row:after])
        path = join(keys)
        docs[path] = KeyDoc(path, row + 1, comment, source)
        for inner in inline_keys("".join(scan.value)):
            sub = join([*keys, *inner])
            docs[sub] = KeyDoc(sub, row + 1, comment, source)
        above = []
        row = after
    return docs
