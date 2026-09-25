"""`tac check --commit-msg`: a commit message held to `[commits]` in the effective
standards (config/standards.toml with the floor applied).

The commit-msg git hook runs this with the message file git hands it. Under the
house convention, "what-and-why", a message is one line saying what changed and
why; below it only a trailer block may follow, after one blank line. Under
"conventional-1.0.0" the subject takes a type prefix and a body is allowed. The
subject stays within `max_subject` characters and, unless the registry allows
them, holds no em dash. A commit made from an agent session carries every
trailer `trailers_required` names; a person's own commit needs none.

A waiver in the floor lifts a part while it holds: `commits` the whole check,
`commit_max_subject` the length, `trailers_required` the trailers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tac.config import Config

# Git's scissors line: everything below it is dropped from the message.
SCISSORS = "# ------------------------ >8 ------------------------"
TRAILER = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*): \S.*$")
# https://www.conventionalcommits.org/en/v1.0.0/
CONVENTIONAL = re.compile(r"^[a-z]+(\([a-z0-9._/-]+\))?!?: \S")
EM_DASH = "—"
# Variables a coding-agent client sets in the commands it runs, so a commit made
# from its session can be told from the owner's own: Claude Code sets
# CLAUDECODE, Codex sets CODEX_SANDBOX inside its sandbox and CODEX_THREAD_ID
# in every command, AI_AGENT is the cross-client marker, and TAC_AGENT is what
# the runner sets for a commit it makes for an agent.
AGENT_MARKERS = (
    "CLAUDECODE",
    "CODEX_SANDBOX",
    "CODEX_THREAD_ID",
    "AI_AGENT",
    "TAC_AGENT",
)


@dataclass(frozen=True, slots=True)
class Rules:
    """What `[commits]` asks, with the floor and the waivers applied."""

    convention: str
    max_subject: int | None
    trailers_required: tuple[str, ...]
    em_dashes: bool


def rules(config: Config) -> Rules | None:
    """The effective commit rules; None when a waiver lifts the whole check."""
    waived = {w.gate for w in config.floor.waivers}
    if "commits" in waived:
        return None
    commits: Mapping[str, Any] = config.standards["commits"]
    markdown: Mapping[str, Any] = config.standards["code"]["markdown"]
    return Rules(
        convention=commits["convention"],
        max_subject=None
        if "commit_max_subject" in waived
        else int(commits["max_subject"]),
        trailers_required=()
        if "trailers_required" in waived
        else tuple(commits["trailers_required"]),
        em_dashes=bool(markdown["em_dashes"]),
    )


def from_agent(environ: Mapping[str, str]) -> bool:
    """Whether the commit is made from a coding-agent session."""
    return any(environ.get(name) for name in AGENT_MARKERS)


def cleaned(text: str) -> list[str]:
    """The message as git stores it: no comment lines, nothing below the
    scissors, no trailing whitespace or blank lines at either end."""
    lines = []
    for line in text.splitlines():
        if line == SCISSORS:
            break
        if line.startswith("#"):
            continue
        lines.append(line.rstrip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _trailer_block(lines: Sequence[str]) -> list[str]:
    """The message's last paragraph when every line in it is a trailer."""
    if len(lines) < 3 or "" not in lines[1:]:
        return []
    start = len(lines) - lines[::-1].index("")
    block = list(lines[start:])
    return block if all(TRAILER.match(line) for line in block) else []


def check_message(text: str, rules: Rules, *, agent: bool) -> list[str]:
    """Every way the message breaks the rules; empty is clean."""
    lines = cleaned(text)
    if not lines:
        return ["the commit message is empty"]
    subject = lines[0]
    problems = []
    if subject != subject.strip():
        problems.append("the subject line starts with whitespace")
    if rules.max_subject is not None and len(subject) > rules.max_subject:
        problems.append(
            f"the subject is {len(subject)} characters, over the "
            f"{rules.max_subject} [commits] max_subject allows"
        )
    if not rules.em_dashes and any(EM_DASH in line for line in lines):
        problems.append("the message holds an em dash; use a comma or a colon")
    trailers = _trailer_block(lines)
    if len(lines) > 1 and lines[1]:
        problems.append("the subject is followed by a second line; leave one blank")
    if rules.convention == "what-and-why":
        body = lines[1 : len(lines) - len(trailers)]
        if any(body):
            problems.append(
                "the message is one line saying what changed and why; below it "
                "only trailers may follow"
            )
    elif not CONVENTIONAL.match(subject):
        problems.append("the subject is not `type(scope): description`")
    if agent:
        present = {t.split(":", 1)[0].lower() for t in trailers}
        for name in rules.trailers_required:
            if name.lower() not in present:
                problems.append(
                    f"a commit from an agent session carries a {name}: trailer"
                )
    return problems
