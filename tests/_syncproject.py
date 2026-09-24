"""A copy of this repository's authored inputs, for the sync and adapter tests.

Every test renders into a temporary folder, never into the checkout, so a test
that edits a template or a config file cannot leave the repository dirty.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml

from tac.sync import TEMPLATES_DIR, sync

REPO = Path(__file__).resolve().parents[1]
# What `tac sync` reads: the knob file, its tables, the floor, the brief, the
# skills and the templates.
INPUTS = (
    ".agents/config.toml",
    ".agents/config",
    ".agents/standards.floor.toml",
    ".agents/context",
    ".agents/skills",
    TEMPLATES_DIR,
)


def copy_project(root: Path) -> Path:
    for rel in INPUTS:
        source, target = REPO / rel, root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return root


def synced(root: Path) -> Path:
    copy_project(root)
    sync(root)
    return root


def replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n"), "an agent file opens with its frontmatter"
    head, _, _ = text[4:].partition("\n---\n")
    return yaml.safe_load(head)


def toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))
