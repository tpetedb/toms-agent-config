"""Does CODEOWNERS cover every file CI runs from (docs/DESIGN.md, section 8).

On pull_request the workflow file and the checkout are the candidate's, so the
one layer no agent can bypass is the owner's required review, which reaches
only the paths CODEOWNERS names. This module reads CODEOWNERS as GitHub does and
names each CI path it leaves without an owner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Where GitHub looks, in order; the first one found is the one it uses.
# https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners#codeowners-file-location
LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
WORKFLOWS = ".github/workflows"
SCRIPTS = "scripts"
JUSTFILE = "justfile"
# Stand for a file a pull request adds: GitHub reads CODEOWNERS from the base,
# so a rule that names today's files only would leave tomorrow's unowned.
NEW_WORKFLOW = f"{WORKFLOWS}/<new>.yml"
NEW_GATE = f"{SCRIPTS}/ci_<new>.sh"


@dataclass(frozen=True, slots=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str]


def pattern_regex(pattern: str) -> re.Pattern[str] | None:
    """A CODEOWNERS pattern as a regex over a repository path, or None when
    GitHub would skip the line: it supports neither `!` nor `[ ]`."""
    if pattern.startswith("!") or "[" in pattern or "]" in pattern:
        return None
    body = pattern.lstrip("/")
    directory = body.endswith("/")
    body = body.rstrip("/")
    if not body:
        return None
    # A leading or inner slash anchors the pattern at the root; a bare name
    # matches at any depth.
    anchored = pattern.startswith("/") or "/" in body
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    last = body.rsplit("/", 1)[-1]
    if directory:
        tail = "/.*"
    elif "*" in last or "?" in last:
        # `docs/*` owns the files directly in docs/, not the ones below.
        tail = ""
    else:
        tail = "(?:/.*)?"
    head = "" if anchored else "(?:.*/)?"
    return re.compile(head + "".join(out) + tail)


def parse(text: str) -> list[Rule]:
    rules = []
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        owners = []
        for word in words[1:]:
            if word.startswith("#"):
                break
            owners.append(word)
        regex = pattern_regex(words[0])
        if regex is not None:
            rules.append(Rule(words[0], tuple(owners), regex))
    return rules


def owners_of(rules: list[Rule], path: str) -> tuple[str, ...] | None:
    """The owners of `path`: the last matching rule wins, and a rule with no
    owners leaves the path unowned. None when no rule matches."""
    found = None
    for rule in rules:
        if rule.regex.fullmatch(path):
            found = rule.owners
    return found


def location(root: Path) -> str | None:
    return next((rel for rel in LOCATIONS if (root / rel).is_file()), None)


def ci_paths(root: Path, codeowners: str) -> list[str]:
    """Every path CI runs from or whose change steers what it runs: each
    workflow, each gate script and each script a workflow names, the justfile
    whose recipes CI mirrors, CODEOWNERS itself, and a stand-in for a new
    workflow and a new gate."""
    workflows = root / WORKFLOWS
    files = sorted(
        p for p in workflows.glob("*") if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    named = "\n".join(p.read_text(encoding="utf-8") for p in files)
    scripts = sorted(p for p in (root / SCRIPTS).glob("*") if p.is_file())
    paths = [p.relative_to(root).as_posix() for p in files]
    paths += [
        f"{SCRIPTS}/{p.name}"
        for p in scripts
        if p.name.startswith("ci_") or re.search(rf"\b{re.escape(p.name)}\b", named)
    ]
    if (root / JUSTFILE).is_file():
        paths.append(JUSTFILE)
    paths += [codeowners, NEW_WORKFLOW, NEW_GATE]
    return list(dict.fromkeys(paths))


def ci_gaps(root: Path, agent: str) -> list[str]:
    """Each CI path CODEOWNERS leaves without an owner who is not the agent
    identity, as `path: reason`; a missing CODEOWNERS is one gap."""
    found = location(root)
    if found is None:
        return [f"no CODEOWNERS in {', '.join(LOCATIONS)}"]
    rules = parse((root / found).read_text(encoding="utf-8"))
    gaps = []
    for path in ci_paths(root, found):
        owners = owners_of(rules, path)
        if owners is None:
            gaps.append(f"{path}: no rule matches")
        elif not owners:
            gaps.append(f"{path}: the last matching rule names no owner")
        elif {o.lstrip("@").lower() for o in owners} <= {agent.lower()}:
            # The agent identity approving its own change is no review at all.
            gaps.append(f"{path}: owned only by the agent identity {agent}")
    return gaps
