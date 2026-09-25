"""Every relative link in the operator's page and the diagrams README resolves:
the file exists, and an anchor into a Markdown file names one of its headings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PAGES = ("docs/HARNESS.md", "docs/diagrams/README.md")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(`{3,}|~{3,})")


def prose(text: str) -> str:
    """The text outside fenced code blocks, where a bracket is not a link."""
    kept: list[str] = []
    fence = ""
    for line in text.splitlines():
        opened = FENCE.match(line.strip())
        if fence:
            if opened and line.strip().startswith(fence):
                fence = ""
            continue
        if opened:
            fence = opened[1]
            continue
        kept.append(line)
    return "\n".join(kept)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces
    become hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {
        slug(line.lstrip("#"))
        for line in prose(path.read_text(encoding="utf-8")).splitlines()
        if line.startswith("#")
    }


def links(page: str) -> list[str]:
    text = prose((REPO / page).read_text(encoding="utf-8"))
    return [
        target
        for target in LINK.findall(text)
        if not re.match(r"^[a-z][a-z0-9+.-]*:", target)
    ]


@pytest.mark.parametrize("page", PAGES)
def test_every_relative_link_resolves(page: str) -> None:
    found = links(page)
    assert found, f"{page} links nowhere"
    broken = []
    for target in found:
        rel, _, anchor = target.partition("#")
        dest = (REPO / page).parent / rel if rel else REPO / page
        if not dest.exists():
            broken.append(f"{target}: no such file")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            broken.append(f"{target}: no heading with that anchor")
    assert not broken, f"{page}:\n" + "\n".join(broken)


def test_the_slug_matches_github() -> None:
    assert slug("Diagram conventions") == "diagram-conventions"
    assert slug("Checks that tell you it worked") == "checks-that-tell-you-it-worked"
    assert slug("12. Skills and telemetry") == "12-skills-and-telemetry"


def test_a_broken_link_is_caught(tmp_path: Path) -> None:
    page = tmp_path / "p.md"
    page.write_text(
        "# Title\n\n[x](missing.md) and [y](#title)\n```text\n[z](n)\n```\n"
    )
    assert [t for t in LINK.findall(prose(page.read_text()))] == [
        "missing.md",
        "#title",
    ]
    assert "title" in anchors(page)
