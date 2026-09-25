"""A small TOML writer for the files `tac sync` generates.

The standard library reads TOML but does not write it. Generated files are
serialised here, never spelt out by a template, so a value with a quote or a
newline in it cannot break the file. The writer handles the shapes the adapters
use: tables, strings, booleans, integers and arrays of scalars. Comments come
from a map of key path to text, so a generated file stays self-explaining.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from tac.tomldoc import join

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def key(name: str) -> str:
    return name if BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _string(text: str) -> str:
    # A multi-line basic string reads better for prose; anything that would
    # need escaping inside one falls back to the escaped single-line form.
    safe = (
        "\n" in text
        and "\\" not in text
        and '"""' not in text
        and not text.endswith('"')
        and not any(ord(c) < 32 and c not in "\n\t" for c in text)
    )
    if safe:
        return '"""\n' + text + '"""'
    return json.dumps(text, ensure_ascii=False)


def value(item: Any) -> str:
    """One scalar or array of scalars as TOML writes it."""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, int):
        return str(item)
    if isinstance(item, str):
        return _string(item)
    if isinstance(item, Sequence):
        parts = [value(v) for v in item]
        if any(isinstance(v, Mapping) for v in item):
            raise TypeError("an array of tables is not a shape the adapters use")
        return "[" + ", ".join(parts) + "]"
    raise TypeError(f"cannot write {type(item).__name__} as TOML")


def _comment(text: str) -> list[str]:
    return [f"# {line}".rstrip() for line in text.splitlines()]


def dumps(
    data: Mapping[str, Any],
    header: str = "",
    comments: Mapping[str, str] | None = None,
) -> str:
    """`data` as TOML: the header first, then every key in the order given,
    scalars before the tables under them, each with its comment above it."""
    notes = comments or {}
    out: list[str] = [*_comment(header), ""] if header else []

    def table(node: Mapping[str, Any], path: list[str]) -> None:
        scalars = [(k, v) for k, v in node.items() if not isinstance(v, Mapping)]
        tables = [(k, v) for k, v in node.items() if isinstance(v, Mapping)]
        if path and (scalars or not tables):
            if out and out[-1]:
                out.append("")
            out.extend(_comment(notes.get(join(path), "")))
            out.append("[" + ".".join(key(p) for p in path) + "]")
        for name, item in scalars:
            out.extend(_comment(notes.get(join([*path, name]), "")))
            out.append(f"{key(name)} = {value(item)}")
        for name, item in tables:
            table(item, [*path, name])

    table(data, [])
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"
