"""A transport that answers from recorded GitHub responses and nothing else."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tac.github import ApiError

FIXTURES = Path(__file__).parent / "fixtures" / "github"
REPO = "example/demo"


def load(name: str) -> Any:
    """A fresh copy of a fixture's body, safe to change in a test."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return copy.deepcopy(data["body"])


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: Any = None


@dataclass
class FixtureTransport:
    """Serves each (method, path) from its queue; any other call fails the test.

    The last reply of a queue repeats, so a read can be asked twice.
    """

    replies: dict[tuple[str, str], list[Reply]]
    authenticated: bool = True
    calls: list[tuple[str, str, Any]] = field(default_factory=list)

    def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, copy.deepcopy(body)))
        queue = self.replies.get((method, path))
        if not queue:
            raise AssertionError(f"unrecorded call {method} {path}")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if reply.status >= 400:
            raise ApiError(reply.status, f"{method} {path}: HTTP {reply.status}")
        return copy.deepcopy(reply.body)

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


def rulesets(
    listed: list[Any], full: Mapping[int, Any], repo: str = REPO
) -> dict[tuple[str, str], list[Reply]]:
    """Replies for a list, each ruleset by id and the repository itself."""
    replies = {
        ("GET", f"repos/{repo}/rulesets?per_page=100"): [Reply(200, listed)],
        ("GET", f"repos/{repo}"): [Reply(200, load("repo"))],
    }
    for rid, body in full.items():
        replies[("GET", f"repos/{repo}/rulesets/{rid}")] = [Reply(200, body)]
    return replies


def standard(**changes: Any) -> FixtureTransport:
    """The recorded holding ruleset plus a tag ruleset, with body changes applied."""
    holds = load("ruleset_holds")
    holds.update(changes)
    return FixtureTransport(
        rulesets(load("rulesets_list"), {101: holds, 102: load("ruleset_tag")})
    )
