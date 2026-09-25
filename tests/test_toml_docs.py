"""Every key in every shipped TOML file says what it does and what it may be."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tac.config import CONFIG_DIR, literal_choices, schema_for
from tac.standards import FLOOR_FILE
from tac.tomldoc import document, inline_keys, join
from tac.work import KNOBS_FILE

REPO = Path(__file__).resolve().parents[1]
SHIPPED = sorted(
    [
        KNOBS_FILE,
        FLOOR_FILE,
        *(p.relative_to(REPO).as_posix() for p in (REPO / CONFIG_DIR).rglob("*.toml")),
    ]
)


def split(path: str) -> list[str]:
    parts: list[str] = []
    for piece in path.split("."):
        head, _, rest = piece.partition("[")
        parts.append(head)
        if rest:
            parts.append(f"[{rest}")
    return parts


def walk(
    data: Mapping[str, Any], prefix: list[str], every: set[str], leaves: set[str]
) -> None:
    for key, value in data.items():
        parts = [*prefix, key]
        every.add(join(parts))
        if isinstance(value, Mapping):
            if value:
                walk(value, parts, every, leaves)
            else:
                leaves.add(join(parts))
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(v, Mapping) for v in value)
        ):
            for i, item in enumerate(value):
                every.add(join([*parts, f"[{i}]"]))
                walk(item, [*parts, f"[{i}]"], every, leaves)
        else:
            leaves.add(join(parts))


def test_the_shipped_set_is_the_whole_configuration() -> None:
    assert KNOBS_FILE in SHIPPED
    assert FLOOR_FILE in SHIPPED
    assert f"{CONFIG_DIR}/profiles/enterprise.toml" in SHIPPED
    assert len(SHIPPED) >= 25


@pytest.mark.parametrize("rel", SHIPPED)
def test_every_shipped_file_is_read_through_a_schema(rel: str) -> None:
    assert schema_for(rel) is not None, f"{rel} has no model, so nothing checks it"


@pytest.mark.parametrize("rel", SHIPPED)
def test_the_scanner_finds_exactly_the_keys_tomllib_reads(rel: str) -> None:
    text = (REPO / rel).read_text()
    every: set[str] = set()
    leaves: set[str] = set()
    walk(tomllib.loads(text), [], every, leaves)
    docs = document(text)
    assert leaves <= set(docs), sorted(leaves - set(docs))
    assert set(docs) <= every, sorted(set(docs) - every)


@pytest.mark.parametrize("rel", SHIPPED)
def test_every_key_has_a_comment(rel: str) -> None:
    docs = document((REPO / rel).read_text())
    bare = [f"{rel}:{d.line} {d.path}" for d in docs.values() if not d.comment.strip()]
    assert not bare, "keys without a comment:\n" + "\n".join(bare)


@pytest.mark.parametrize("rel", SHIPPED)
def test_an_inline_table_comment_names_each_inner_key(rel: str) -> None:
    docs = document((REPO / rel).read_text())
    missing = []
    for doc in docs.values():
        if doc.table or "{" not in doc.source.partition("=")[2]:
            continue
        value = doc.source.partition("=")[2].split("#")[0]
        for inner in inline_keys(value):
            if inner[-1] not in doc.comment:
                missing.append(f"{rel}:{doc.line} {doc.path}.{'.'.join(inner)}")
    assert not missing, "inner keys the comment does not name:\n" + "\n".join(missing)


@pytest.mark.parametrize("rel", SHIPPED)
def test_an_enum_key_says_its_allowed_values(rel: str) -> None:
    model = schema_for(rel)
    assert model is not None
    docs = document((REPO / rel).read_text())
    missing = []
    for doc in docs.values():
        choices = literal_choices(model, split(doc.path))
        told = f"{doc.comment}\n{doc.source}"
        absent = [c for c in choices if c not in told]
        if absent:
            missing.append(f"{rel}:{doc.line} {doc.path} does not name {absent}")
    assert not missing, "\n".join(missing)


def test_the_choices_lookup_reaches_nested_and_keyed_tables() -> None:
    from tac.config_schema import Knobs, ModelsFile, Profile

    assert literal_choices(Knobs, ["profile", "active"]) == (
        "enterprise",
        "standard",
        "yolo",
    )
    assert "bypassPermissions" in literal_choices(Profile, ["claude", "defaultMode"])
    assert literal_choices(ModelsFile, ["providers", "openai", "harness"]) == (
        "claude",
        "codex",
    )
    assert literal_choices(Knobs, ["harnesses", "copilot_surfaces"]) == (
        "cli",
        "ide",
        "cloud",
    )
    assert literal_choices(Knobs, ["project", "name"]) == ()


# ---------------------------------------------------------------- the scanner


def test_a_comment_above_or_after_documents_the_key() -> None:
    docs = document(
        "# what a does\na = 1\nb = 2  # what b does\n\n# detached\n\nc = 3\n"
    )
    assert docs["a"].comment == "what a does"
    assert docs["b"].comment == "what b does"
    assert docs["c"].comment == ""


def test_tables_arrays_of_tables_and_quoted_keys() -> None:
    text = (
        '# the table\n[t.u]\n# k\n"x.y" = 1\n'
        "[[w]]\ng = 1  # first\n[[w]]\ng = 2  # second\n"
    )
    docs = document(text)
    assert docs["t.u"].table and docs["t.u"].comment == "the table"
    assert docs["t.u.x.y"].comment == "k"
    assert docs["w[0].g"].comment == "first"
    assert docs["w[1].g"].comment == "second"


def test_multi_line_values_and_hashes_inside_strings() -> None:
    text = (
        '# list\nxs = [\n  "a#b",  # not the end\n  "c",\n]\n'
        's = """\nline # inside\n"""  # after\n'
        'n = { a = 1, b = { c = "}" } }  # a b c\n'
    )
    docs = document(text)
    assert docs["xs"].comment == "list\nnot the end"
    assert docs["s"].comment == "after"
    assert {"n.a", "n.b", "n.b.c"} <= set(docs)
    assert inline_keys('{ a = 1, b = { c = "}" } }') == [["a"], ["b"], ["b", "c"]]
