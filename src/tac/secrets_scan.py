"""The secrets scan every memory and session write passes (design section 6).

One function, `findings(text)`, used by `tac memory add`, `promote` and `lint`
and by `tac session log`. Each pattern is anchored on a known prefix or an
explicit `key=value` shape, so a sha256, a commit id or a record id never trips
it. A finding names the pattern and the line, never the matched text: the scan
exists so a secret is not written anywhere, its own report included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Name to pattern. Names are what a refusal prints; keep them free of any value.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pos]_[A-Za-z0-9]{30,}")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-project-key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}")),
    # The generic form last, and never the two prefixes above a second time.
    ("openai-key", re.compile(r"\bsk-(?!ant-|proj-)[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # Only an explicit assignment: prose that says "token" or "password" passes.
    (
        "assigned-secret",
        re.compile(r"(?i)(?:password|passwd|token|secret)=[^\s]{8,}"),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A secret-like value: which pattern, on which line, and nothing of it."""

    pattern: str
    line: int

    def __str__(self) -> str:
        return f"line {self.line}: looks like a {self.pattern}"


def findings(text: str) -> list[Finding]:
    """Every pattern that matches `text`, one finding per pattern and line."""
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines() or [text], start=1):
        for name, pattern in PATTERNS:
            if pattern.search(line):
                found.append(Finding(name, number))
    return found


def names(text: str) -> list[str]:
    """The pattern names `text` trips, each once, in pattern order."""
    hit = {f.pattern for f in findings(text)}
    return [name for name, _ in PATTERNS if name in hit]
