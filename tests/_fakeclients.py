"""Fake executables for the runner, launch and keychain tests.

Each fake is a small Python script under the test's temporary folder that acts
as the real program would for what the tests need, and logs every call beside
itself: `claude` and `codex` print a fixture result for the contract they were
asked to meet, `security` keeps a keychain in a JSON file, and `gh` keeps its
pull requests in one. Nothing here reaches a model, the network or the owner's
real keychain; the runner and the launcher find these through `search_path` or
an absolute path, the injection points they already have.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "handoffs"
CALLS = "calls.jsonl"
CONTROL = "control.json"

_PRELUDE = f"""#!{sys.executable}
import json, os, sys
HERE = os.path.dirname(os.path.realpath(__file__))
def log(entry):
    with open(os.path.join(HERE, {CALLS!r}), "a") as handle:
        handle.write(json.dumps(entry) + "\\n")
def control():
    try:
        with open(os.path.join(HERE, {CONTROL!r})) as handle:
            return json.load(handle)
    except OSError:
        return {{}}
FIXTURES = {str(FIXTURES)!r}
def payload(contract, spec):
    bad = spec.get("invalid", {{}})
    count = spec.get("seen", {{}}).get(contract, 0)
    spec.setdefault("seen", {{}})[contract] = count + 1
    with open(os.path.join(HERE, {CONTROL!r}), "w") as handle:
        json.dump(spec, handle)
    if contract in bad and count < bad[contract]:
        return {{"not": "what the contract asks"}}
    with open(os.path.join(FIXTURES, contract + ".json")) as handle:
        return json.load(handle)
"""

CLAUDE = (
    _PRELUDE
    + """
args = sys.argv[1:]
prompt = sys.stdin.read() if "-p" in args else ""
log({"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "prompt": prompt})
if "-p" not in args:
    sys.exit(0)
schema = json.loads(args[args.index("--json-schema") + 1])
result = payload(schema["title"], control())
session = args[args.index("--session-id") + 1]
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "structured_output": result,
                  "result": ""}))
"""
)

CODEX = (
    _PRELUDE
    + """
args = sys.argv[1:]
prompt = sys.stdin.read() if args[:1] == ["exec"] else ""
log({"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "prompt": prompt})
if args[:1] != ["exec"]:
    sys.exit(0)
with open(args[args.index("--output-schema") + 1]) as handle:
    schema = json.load(handle)
result = payload(schema["title"], control())
with open(args[args.index("-o") + 1], "w") as handle:
    json.dump(result, handle)
print(json.dumps({"type": "turn.completed"}))
"""
)

SECURITY = (
    _PRELUDE
    + """
store = os.path.join(HERE, "keychain.json")
def load():
    try:
        with open(store) as handle:
            return json.load(handle)
    except OSError:
        return {}
def save(items):
    with open(store, "w") as handle:
        json.dump(items, handle)
def value(args, flag):
    return args[args.index(flag) + 1] if flag in args else None
args = sys.argv[1:]
if args == ["-i"]:
    lines = sys.stdin.read().splitlines()
    log({"argv": args, "stdin_lines": len(lines)})
    items = load()
    for line in lines:
        words = line.split()
        if words[:1] == ["add-generic-password"]:
            items[value(words, "-s") + "/" + value(words, "-a")] = value(words, "-w")
    save(items)
    sys.exit(0)
log({"argv": args})
if args[:1] == ["find-generic-password"]:
    key = value(args, "-s") + "/" + value(args, "-a")
    items = load()
    if key not in items:
        sys.stderr.write("The specified item could not be found in the keychain.\\n")
        sys.exit(44)
    if "-w" in args:
        print(items[key])
    sys.exit(0)
sys.exit(2)
"""
)

GH = (
    _PRELUDE
    + """
state_file = os.path.join(HERE, "gh.json")
try:
    with open(state_file) as handle:
        state = json.load(handle)
except OSError:
    state = {"prs": []}
args = sys.argv[1:]
log({"argv": args, "token": bool(os.environ.get("GH_TOKEN")),
     "env_keys": sorted(os.environ)})
def value(flag):
    return args[args.index(flag) + 1]
if args[:2] == ["pr", "list"]:
    print(json.dumps([p for p in state["prs"] if p["head"] == value("--head")]))
    sys.exit(0)
if args[:2] == ["pr", "create"]:
    # A flake the test asks for: fail this many creates before one succeeds.
    if state.get("fail_create"):
        state["fail_create"] -= 1
        with open(state_file, "w") as handle:
            json.dump(state, handle)
        print("HTTP 502: flake", file=sys.stderr)
        sys.exit(1)
    number = len(state["prs"]) + 1
    url = "https://github.com/" + value("--repo") + "/pull/" + str(number)
    state["prs"].append({"number": number, "url": url, "head": value("--head")})
    with open(state_file, "w") as handle:
        json.dump(state, handle)
    print(url)
    sys.exit(0)
sys.exit(1)
"""
)

# A program that must never run: calling it marks the folder, which the test
# asserts is absent.
NEVER = """#!/bin/sh
touch "$(dirname "$0")/called"
exit 99
"""

BODIES = {"claude": CLAUDE, "codex": CODEX, "security": SECURITY, "gh": GH}


def install(folder: Path, *names: str) -> Path:
    """Write the named fakes into `folder`, executable, and return it."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = folder / name
        path.write_text(BODIES[name], encoding="utf-8")
        path.chmod(0o755)
    return folder


def never(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(NEVER, encoding="utf-8")
    path.chmod(0o755)
    return path


def calls(folder: Path) -> list[dict[str, Any]]:
    path = folder / CALLS
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def control(folder: Path, **spec: Any) -> None:
    (folder / CONTROL).write_text(json.dumps(spec), encoding="utf-8")


# ------------------------------------------------------------ a project, an origin

# The local checks gate still waits on a recipe, so a run refuses the shipped
# order pipeline; the fixture drops that one gate to run the rest.
WAITING_GATE = re.compile(
    r"# The profile's local build checks.*?waits_on = \"M2\"[^\n]*\n", re.S
)
ORDER_BRANCH = "demo-order"


def project_repo(tmp: Path) -> Path:
    """A synced copy of this project's configuration, committed, with the owner's
    queue, pushed to a local bare origin whose path names example/demo, so
    origin/main holds the lock and the runner can name the repository."""
    from tac.sync import sync
    from tests._gitrepo import commit_all, git
    from tests._syncproject import copy_project

    root = copy_project(tmp / "work" / "demo")
    (root / ".human" / "approvals").mkdir(parents=True)
    shutil.copy2(REPO / ".human" / "todo.toml", root / ".human" / "todo.toml")
    order = root / ".agents/config/pipelines/order.toml"
    order.write_text(WAITING_GATE.sub("", order.read_text("utf-8")), "utf-8")
    git(root.parent, "init", "-q", root.name)
    sync(root, links=False)
    commit_all(root, "base")
    bare = tmp / "remotes" / "example" / "demo.git"
    bare.parent.mkdir(parents=True)
    git(bare.parent, "init", "-q", "--bare", bare.name)
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "origin", "main")
    git(root, "fetch", "-q", "origin")
    return root


def order_worktree(root: Path, tmp: Path) -> Path:
    """The order's own worktree on its branch, as the chief's script makes it."""
    from tests._gitrepo import git

    where = tmp / "worktrees" / ORDER_BRANCH
    git(root, "worktree", "add", "-q", "-b", ORDER_BRANCH, str(where), "main")
    return where


def demo_order(root: Path, cross: tuple[str, ...] = ()) -> Any:
    from tac.work import Order

    return Order(
        id=ORDER_BRANCH,
        root=root,
        title="Add an intro page",
        team="core",
        branch=ORDER_BRANCH,
        goal="",
        builder="claude",
        owns=("docs/**",),
        cross=cross,
        needs=(),
        criteria=(),
    )
