"""Stamp src/tac into .agents/lib/tac, the named source .agents/.venv installs,
and hooks/run.py into .agents/hooks/run.py, the guard every rendered hook runs.

A stand-in for `tac stamp` until that command exists: it writes a minimal
package project (name, version, dependencies, entry point) and a copy of the
package, so the deployed checker never imports from the source tree. Run it on
the host, then `uv sync --frozen --no-editable --project .agents`, and `tac
sync` so the lock records the stamped guard.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from pathlib import Path

DESCRIPTION = "Stamp src/tac into .agents/lib/tac for the deployed toolchain."
HEADER = (
    "# Stamped from the root pyproject.toml by scripts/stamp_lib.py; do not edit.\n"
)


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def render_pyproject(root_project: dict) -> str:
    project = root_project["project"]
    scripts = project.get("scripts", {})
    build = root_project["build-system"]
    lines = [
        HEADER,
        "[project]",
        f'name = "{project["name"]}"',
        f'version = "{project["version"]}"',
        f'requires-python = "{project["requires-python"]}"',
        f"dependencies = {toml_list(project.get('dependencies', []))}",
        "",
        "[project.scripts]",
        *(f'{name} = "{target}"' for name, target in scripts.items()),
        "",
        "[build-system]",
        f"requires = {toml_list(build['requires'])}",
        f'build-backend = "{build["build-backend"]}"',
        "",
        "[tool.uv]",
        "# Rebuild the wheel when a module changes, not only when this file does.",
        'cache-keys = [{ file = "pyproject.toml" }, { file = "src/**/*.py" }]',
        "",
    ]
    return "\n".join(lines)


def stamp(root: Path) -> Path:
    source = root / "src" / "tac"
    target = root / ".agents" / "lib" / "tac"
    root_project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target / "src" / "tac",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (target / "pyproject.toml").write_text(
        render_pyproject(root_project), encoding="utf-8"
    )
    return target


def stamp_guard(root: Path) -> Path:
    """Only the guard itself: hooks/git/ is installed by prek from the source."""
    target = root / ".agents" / "hooks" / "run.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "hooks" / "run.py", target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    for target in (stamp(root), stamp_guard(root)):
        print(f"stamped {target.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
