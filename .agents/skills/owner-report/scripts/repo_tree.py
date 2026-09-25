#!/usr/bin/env python3
"""Print a repository's directory and file tree for an owner report.

Deterministic: the file list comes from git (tracked plus untracked files that
are not ignored), so .gitignore'd secrets, venvs and caches never appear.
Outside a git repository it walks the folder and skips hidden and heavy dirs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
from pathlib import Path

SKIP = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


def list_files(root: Path) -> tuple[list[str], str]:
    """Relative file paths and a short description of where they came from."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        sha = (
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "no commits"
        )
        branch = (
            subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "detached"
        )
        return sorted(p for p in out.splitlines() if p), f"git {branch} @ {sha}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = []
        for base, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
            rel = Path(base).relative_to(root)
            files += [str(rel / n) if str(rel) != "." else n for n in names]
        return sorted(files), "folder walk"


def repo_name(root: Path) -> str:
    """The repository's own name from its origin URL; a worktree folder name says
    nothing."""
    url = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if url else ""
    return name or root.name


def build(paths: list[str]) -> dict:
    tree: dict = {}
    for p in paths:
        node = tree
        parts = p.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None
    return tree


def count(node: dict) -> int:
    return sum(1 if v is None else count(v) for v in node.values())


def render(
    node: dict, prefix: str, depth: int, max_depth: int, dirs_only: bool
) -> list[str]:
    # Directories first, then files, each alphabetical, so two runs on the same
    # tree match.
    items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
    if dirs_only:
        items = [kv for kv in items if kv[1] is not None]
    lines = []
    for i, (name, child) in enumerate(items):
        last = i == len(items) - 1
        branch, ext = ("└── ", "    ") if last else ("├── ", "│   ")
        if child is None:
            lines.append(prefix + branch + name)
            continue
        n = count(child)
        lines.append(f"{prefix}{branch}{name} ({n} file{'' if n == 1 else 's'})")
        if max_depth == 0 or depth + 1 < max_depth:
            lines += render(child, prefix + ext, depth + 1, max_depth, dirs_only)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--depth", type=int, default=0, help="0 means the full tree")
    ap.add_argument("--dirs-only", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    paths, source = list_files(root)
    tree = build(paths)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{repo_name(root)}/  ({len(paths)} files, {source}, {stamp})")
    print("\n".join(render(tree, "", 0, args.depth, args.dirs_only)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # A reader such as head closed early; that is not an error for a tree listing.
        raise SystemExit(0) from None
