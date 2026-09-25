"""Every required hook is load-bearing: one mutation run per hook check and client.

Design section 13 asks for "one mutation run per required hook", and section 9
says removing each required hook in a mutation run makes the suite fail. Three
mutants per check, each against one synced fixture checkout with the candidate
checker in its venv, never the repository:

- the matcher: hooks.toml rendered with that check's matcher emptied on one
  client; the rendered client file changes, and the stamped guard now lets
  through the event the unmutated render refused;
- the checker: the check function in the fixture's installed tac answers allow;
- the guard: a copy of hooks/run.py that answers allow for that check's
  verdict, run under Python 3.9 as test_guard_py39.py runs the source guard, so
  the case test_hooks.py (or the test named below) refuses gets through.

A check that refuses nothing yet is named in NOT_LOAD_BEARING with its reason,
and a test holds that list to what the guard does, so it cannot grow unnoticed.
No test sleeps: every run waits on the guard's own exit.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tac.config import load_config
from tac.sync import sync
from tests._gitrepo import GIT
from tests._guard import (
    SOURCE_GUARD,
    Fixture,
    event,
    patch,
    real_checkout,
    run_guard,
    site_packages,
    tool_event,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = load_config(REPO)
HOOKS = ".agents/config/hooks.toml"
ORDER = "work/orders/proof-order/order.toml"
BRANCH = "feat/proof"
# The mutation runs render and start the guard dozens of times.
pytestmark = pytest.mark.slow

# Wired, but refusing nothing a mutation could take away, and why.
NOT_LOAD_BEARING: dict[tuple[str, str], str] = {
    ("handoff-guard", "codex"): "Codex's spawn_agent is judged allow until the "
    "Codex dispatch tokens; native Codex subagents render off "
    "([agents] enabled = false) in every profile",
    ("spawn-record", "claude"): "a record check renders no hook until a "
    "SubagentStart record is configured (follow-up config)",
    ("spawn-record", "codex"): "a record check renders no hook until a "
    "SubagentStart record is configured (follow-up config)",
}


def wired() -> list[tuple[str, str]]:
    """Every (check, client) hooks.toml wires, as tac proof coverage derives it."""
    return [
        (name, client)
        for name, spec in CONFIG.hooks.checks.items()
        for client in ("claude", "codex")
        if (spec.claude if client == "claude" else spec.codex)
    ]


LOAD_BEARING = [pair for pair in wired() if pair not in NOT_LOAD_BEARING]


def _id(pair: tuple[str, str]) -> str:
    return f"{pair[0]}-{pair[1]}"


# ---------------------------------------------------------------- the checkout


@dataclass
class Checkout:
    fx: Fixture
    order_text: str

    @property
    def root(self) -> Path:
        return self.fx.root

    @contextlib.contextmanager
    def order(self) -> Iterator[None]:
        """An active order on this branch that owns src/app.py and whose one
        criterion fails, for the checks that speak through tac work."""
        path = self.root / ORDER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.order_text, encoding="utf-8")
        try:
            yield
        finally:
            for leftover in sorted(path.parent.iterdir()):
                leftover.unlink()
            path.parent.rmdir()


ORDER_TEXT = """v = 1
id = "proof-order"
title = "proof order"
team = "core"
branch = "feat/proof"
builder = "builder-a"
provider = "claude"
owns = ["src/app.py"]
cross = []
needs = []
[[criteria]]
id = "c1"
text = "never holds"
check = "false"
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Checkout:
    """One synced checkout with the candidate checker, on a feature branch whose
    base is origin/main; each mutant is undone before the next."""
    fx = real_checkout(tmp_path_factory.mktemp("mutation"))
    (fx.root / ".gitignore").write_text(
        ".agents/.venv/\nwork/orders/*/result.json\nwork/orders/*/touched.json\n",
        encoding="utf-8",
    )
    (fx.root / "src").mkdir(exist_ok=True)
    (fx.root / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    _git(fx.root, "init", "-q")
    _git(fx.root, "add", "-A")
    _git(fx.root, "commit", "-q", "-m", "base")
    _git(fx.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(fx.root, "checkout", "-q", "-b", BRANCH)
    return Checkout(fx, ORDER_TEXT)


# ---------------------------------------------------------------- the cases


@dataclass(frozen=True)
class Case:
    event: str
    payload: Callable[[Path], dict[str, Any]]
    # The test that holds this refusal; the mutant makes its case get through.
    proves: str
    needs_order: bool = False


def _edit(root: Path) -> dict[str, Any]:
    return tool_event("PreToolUse", file_path=str(root / "src" / "elsewhere.py"))


def _patch_outside(root: Path) -> dict[str, Any]:
    return tool_event(
        "PreToolUse",
        tool="apply_patch",
        command=patch("Add File: src/elsewhere.py"),
    ) | {"cwd": str(root)}


def _read_secret(root: Path) -> dict[str, Any]:
    return tool_event("PreToolUse", tool="Read", file_path=str(root / "app.env"))


def _edit_generated(root: Path) -> dict[str, Any]:
    return tool_event("PreToolUse", file_path=str(root / ".claude/settings.json"))


def _patch_generated(root: Path) -> dict[str, Any]:
    return tool_event(
        "PreToolUse", tool="apply_patch", command=patch("Update File: AGENTS.md")
    ) | {"cwd": str(root)}


def _worker_spawn(_root: Path) -> dict[str, Any]:
    return event(
        "PreToolUse",
        tool_name="Agent",
        agent_type="builder",
        tool_input={"prompt": "help me", "subagent_type": "scout"},
    )


def _stop(root: Path) -> dict[str, Any]:
    return event(
        "Stop",
        cwd=str(root),
        stop_hook_active=False,
        last_assistant_message="order: proof-order\n\nDone.",
    )


CASES: dict[tuple[str, str], Case] = {
    ("owned-paths", "claude"): Case(
        "PreToolUse",
        _edit,
        "tests/test_hooks.py::test_owned_paths_and_order_check_speak_through_tac_work",
        needs_order=True,
    ),
    ("owned-paths", "codex"): Case(
        "PreToolUse",
        _patch_outside,
        "tests/test_hooks.py::test_owned_paths_asks_once_for_every_file_a_patch_names",
        needs_order=True,
    ),
    ("secret-read", "claude"): Case(
        "PreToolUse",
        _read_secret,
        "tests/test_hooks.py::test_the_real_checker_refuses_generated_files_and_secrets",
    ),
    ("generated-paths", "claude"): Case(
        "PreToolUse",
        _edit_generated,
        "tests/test_hooks.py::test_the_real_checker_refuses_generated_files_and_secrets",
    ),
    ("generated-paths", "codex"): Case(
        "PreToolUse",
        _patch_generated,
        "tests/test_hooks.py::test_a_codex_patch_on_a_generated_path_is_refused",
    ),
    ("handoff-guard", "claude"): Case(
        "PreToolUse",
        _worker_spawn,
        "tests/test_workflow_guard.py::test_the_guard_denies_every_spawn_tool_to_a_worker_role",
    ),
    ("order-check", "claude"): Case(
        "Stop",
        _stop,
        "tests/test_hooks.py::test_owned_paths_and_order_check_speak_through_tac_work",
        needs_order=True,
    ),
    ("order-check", "codex"): Case(
        "Stop",
        _stop,
        "tests/test_hooks.py::test_owned_paths_and_order_check_speak_through_tac_work",
        needs_order=True,
    ),
}


def fire(
    co: Checkout, check: str, client: str, python: str | None = None
) -> subprocess.CompletedProcess[str]:
    case = CASES[(check, client)]
    context = co.order() if case.needs_order else contextlib.nullcontext()
    with context:
        return run_guard(
            co.fx, client, case.event, case.payload(co.root), python=python
        )


def refused(done: subprocess.CompletedProcess[str], check: str) -> bool:
    return done.returncode == 2 and f"[{check}]" in done.stderr


def rendered_file(root: Path, client: str) -> str:
    rel = ".claude/settings.json" if client == "claude" else ".codex/hooks.json"
    return (root / rel).read_text(encoding="utf-8")


@contextlib.contextmanager
def restored(root: Path, *paths: str) -> Iterator[None]:
    """Put each file back and render again, whatever the mutant did."""
    saved = {p: (root / p).read_bytes() for p in paths}
    try:
        yield
    finally:
        for rel, data in saved.items():
            (root / rel).write_bytes(data)
        sync(root, links=False)


def shares_matcher(check: str, client: str) -> bool:
    def matcher(name: str) -> str:
        spec = CONFIG.hooks.checks[name]
        return spec.claude if client == "claude" else spec.codex

    event_of = CONFIG.hooks.checks[check].event
    return any(
        name != check
        and spec.event == event_of
        and spec.kind != "record"
        and set(matcher(name).split("|")) & set(matcher(check).split("|"))
        for name, spec in CONFIG.hooks.checks.items()
    )


def empty_matcher(text: str, check: str, client: str) -> str:
    block = re.compile(
        rf"(\[checks\.{re.escape(check)}\]\n(?:(?!\[).*\n)*?){client} = \"[^\"]*\""
    )
    mutated, count = block.subn(rf'\1{client} = ""', text, count=1)
    assert count == 1, f"no {client} matcher under [checks.{check}]"
    return mutated


# ---------------------------------------------------------------- the tests


def test_every_wired_check_is_a_case_or_named_as_not_load_bearing() -> None:
    assert set(wired()) == set(CASES) | set(NOT_LOAD_BEARING)
    assert not set(CASES) & set(NOT_LOAD_BEARING)


@pytest.mark.parametrize("pair", LOAD_BEARING, ids=_id)
def test_emptying_the_matcher_lets_the_refused_event_through(
    checkout: Checkout, pair: tuple[str, str]
) -> None:
    check, client = pair
    assert refused(fire(checkout, check, client), check), "the unmutated render"
    before = rendered_file(checkout.root, client)
    hooks = checkout.root / HOOKS
    with restored(checkout.root, HOOKS):
        hooks.write_text(
            empty_matcher(hooks.read_text("utf-8"), check, client), encoding="utf-8"
        )
        sync(checkout.root, links=False)
        # Another check wiring the same tool keeps the hook itself; then only
        # the checker's own matching drops the check.
        if not shares_matcher(check, client):
            assert rendered_file(checkout.root, client) != before
        done = fire(checkout, check, client)
        assert done.returncode == 0, done.stdout + done.stderr
        assert f"[{check}]" not in done.stderr


CHECK_FUNCTIONS = {
    "owned-paths": "check_owned_paths",
    "secret-read": "check_secret_read",
    "generated-paths": "check_generated_paths",
    "handoff-guard": "check_handoff_guard",
    "order-check": "check_order",
}


@pytest.mark.parametrize("pair", LOAD_BEARING, ids=_id)
def test_a_check_function_that_answers_allow_lets_the_event_through(
    checkout: Checkout, pair: tuple[str, str]
) -> None:
    check, client = pair
    installed = site_packages(checkout.root) / "tac" / "hook.py"
    text = installed.read_text("utf-8")
    entry = f'"{check}": {CHECK_FUNCTIONS[check]},'
    assert entry in text, f"tac.hook.CHECKS has no {entry}"
    try:
        installed.write_text(
            text.replace(entry, f'"{check}": lambda *_: None,'), encoding="utf-8"
        )
        done = fire(checkout, check, client)
    finally:
        installed.write_text(text, encoding="utf-8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert refused(fire(checkout, check, client), check), "restored"


def _find_py39() -> str:
    done = subprocess.run(
        ["uv", "python", "find", "--no-project", "3.9"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


@pytest.fixture(scope="module")
def py39() -> str:
    found = _find_py39()
    if not found:
        pytest.fail("uv finds no Python 3.9; run `uv python install 3.9` once")
    return found


GUARD_LINE = '    reason = verdict["reason"]\n'


def guard_mutant(check: str) -> str:
    """hooks/run.py answering allow whenever the checker's verdict names `check`."""
    source = SOURCE_GUARD.read_text("utf-8")
    assert source.count(GUARD_LINE) == 1, "the guard's answer line moved"
    return source.replace(
        GUARD_LINE,
        f'    if verdict["check"] == {check!r}:\n        return 0\n' + GUARD_LINE,
    )


@pytest.mark.parametrize("pair", LOAD_BEARING, ids=_id)
def test_a_guard_that_answers_allow_for_the_check_lets_its_case_through(
    checkout: Checkout, py39: str, pair: tuple[str, str]
) -> None:
    check, client = pair
    assert refused(fire(checkout, check, client, py39), check), CASES[pair].proves
    guard = checkout.fx.guard
    with restored(checkout.root, guard.relative_to(checkout.root).as_posix()):
        guard.write_text(guard_mutant(check), encoding="utf-8")
        # The lock records the guard's bytes; a re-render records the mutant's.
        sync(checkout.root, links=False)
        done = fire(checkout, check, client, py39)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_checks_named_not_load_bearing_refuse_nothing_yet(
    checkout: Checkout,
) -> None:
    """Held to the guard, so the day one of them refuses something it moves to
    CASES and gets its mutation runs."""
    spawn = tool_event("PreToolUse", tool="spawn_agent", message="hi") | {
        "agent_type": "builder"
    }
    done = run_guard(checkout.fx, "codex", "PreToolUse", spawn)
    assert done.returncode == 0, done.stdout + done.stderr
    for client in ("claude", "codex"):
        groups = json.loads(rendered_file(checkout.root, client))["hooks"]
        assert "SubagentStart" not in groups
    assert CONFIG.hooks.checks["spawn-record"].kind == "record"
