"""Work orders: a task for an agent is data, and its acceptance is a command.

A prompt that says "and run the tests" is a hope. An order says which files the
work may touch, which commands have to pass and which judgements a second agent
has to make, and this module is the one place all of that is checked: from a
`just work-*` recipe, from a hook, from a pipeline gate and from CI.
`work/README.md` is the short version.

The teams, the paths each answers for and the knobs of this module live in
`.agents/config/teams.toml`; nothing here names a project's own files.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import jinja2
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
)

TEAMS_FILE = ".agents/config/teams.toml"
KNOBS_FILE = ".agents/config.toml"
ORDERS = "work/orders"
GOALS = "work/goals"
SCHEMA_VERSION = 1
# Orders, reviews, sign-offs and goals carry `v`; vibe-map's orders load as is.
VERSION = 1
ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,48}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
JUDGES = ("reviewer", "manager")
# A report's first line names its order; "order: x" further down is prose.
ORDER_LINE = re.compile(r"\A\s*order:\s*([a-z0-9-]+)\s*$", re.M)
ROLE_LINE = re.compile(r"^\s*role:\s*([a-z-]+)\s*$", re.M)
SHA = re.compile(r"^[0-9a-f]{40}$")
# What the tool writes next to an order: measurements, never committed, and not
# an agent's to edit.
MEASURED = ("result.json", "touched.json", "repair.md")
ORDER_KEYS = frozenset(
    {
        "v",
        "id",
        "title",
        "team",
        "branch",
        "goal",
        "builder",
        "provider",
        "owns",
        "cross",
        "needs",
        "criteria",
        "issue",
        "reference",
    }
)
CRITERION_KEYS = frozenset({"id", "text", "check", "judge", "timeout"})


class Bad(Exception):
    """A file that does not say what it has to. Always names the file."""


def covers(pattern: str, path: str) -> bool:
    """Whether a path falls under a pattern: `dir/` is a prefix, the rest is a glob."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return path == pattern or fnmatch.fnmatchcase(path, pattern)


def overlap(a: str, b: str) -> bool:
    """Whether two owned entries (a file, or a folder ending in /) can collide."""
    return covers(a, b) or covers(b, a)


# Every path handed to git is a file name, never pathspec magic: a file called
# `:(nope)x.js` would stop a diff, and one called `:!src/x.js` would hide x.js.
GIT = ("git", "--literal-pathspecs")


def git(root: Path, *args: str, must: bool = False) -> str:
    """One git call. `must` is for an answer the verdict depends on: an empty
    string from a failed diff would read as "nothing changed"."""
    out = subprocess.run(
        [*GIT, "-c", "core.quotepath=false", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if must and out.returncode != 0:
        raise Bad(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def names(root: Path, *args: str, must: bool = False) -> list[str]:
    """Paths from a git command that was asked for them with -z. Splitting on
    newlines hands back a quoted name for anything with a quote, a backslash or
    a newline in it, and a quoted name matches no file."""
    out = subprocess.run(
        [*GIT, "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if must and out.returncode != 0:
        raise Bad(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return [n for n in out.stdout.split("\0") if n]


def repo_root(start: Path) -> Path:
    """The top of the checkout holding `start`; a worktree is its own top."""
    top = git(start, "rev-parse", "--show-toplevel")
    if not top:
        raise Bad(f"{start}: not inside a git checkout")
    return Path(top)


def measured(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, f"{ORDERS}/*/{m}") for m in MEASURED)


def dirty(root: Path) -> list[str]:
    """Paths with uncommitted changes, every untracked file by name. The first
    column of a status line is a space for an unstaged change, so the entries
    are read unstripped."""
    fields = names(root, "status", "--porcelain", "-z", "--untracked-files=all")
    out = []
    while fields:
        entry = fields.pop(0)
        out.append(entry[3:])
        # A rename is two paths, and the one that went away matters as much.
        if entry[0] in "RC" and fields:
            out.append(fields.pop(0))
    return [p for p in out if not measured(p)]


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise Bad(f"{path}: not valid TOML: {e}") from e


def _version(data: dict[str, Any], path: Path) -> None:
    if data.get("v") != VERSION:
        raise Bad(f"{path}: v = {data.get('v')!r}, this tool reads v = {VERSION}")


def _only(data: dict[str, Any], allowed: set[str], where: str) -> None:
    """Configuration fails loudly on a key it does not know: a typo that is
    silently ignored looks fine and does nothing."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise Bad(f"{where}: unknown key {', '.join(unknown)}")


def _int(value: object, where: str, low: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise Bad(f"{where}: a whole number of at least {low}")
    return value


# ---------------------------------------------------------------- configuration


def _as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _as_date(value: object) -> object:
    """TOML has a date type, but a quoted date is the common mistake; both work."""
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as e:
            raise ValueError("an ISO 8601 date, like 2026-12-31") from e
    if isinstance(value, dt.datetime):
        return value.date()
    return value


# Strict, so a number where a name belongs, or a string where a number belongs,
# is refused instead of coerced; lists arrive from TOML and are kept as tuples.
Strs = Annotated[tuple[str, ...], BeforeValidator(_as_tuple)]
Whole = Annotated[int, Field(ge=1)]
IsoDate = Annotated[dt.date, BeforeValidator(_as_date)]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Repair(_Config):
    """One bounded builder turn. `argv` runs without a shell and reads the
    rendered handoff on stdin; `{order}` and `{max_turns}` are the only
    placeholders, so nothing a model or a check printed reaches a command line."""

    argv: Strs = ()
    timeout_s: Whole = 1800
    max_turns: Whole = 30


class WorkConfig(_Config):
    """The `[work]` table as written in teams.toml."""

    base: Annotated[str, Field(pattern=r"\S")] = "origin/main"
    shared: Strs = ()
    anyone: Strs = ("changelog.d/",)
    check_timeout_s: Whole = 1500
    stop_budget_s: Whole = 1500
    touched_keep: Whole = 32
    review_provider: Literal["other", "any"] = "other"
    repair: Repair = Repair()


class Settings(WorkConfig):
    """`[work]` plus the host budget, which is named once in the knob file."""

    max_local_agents: Whole = 4


class TeamConfig(_Config):
    """One `[teams.<id>]` table as written in teams.toml."""

    title: str = ""
    owns: Annotated[Strs, Field(min_length=1)]
    tests: Strs = ()
    manager: str = ""
    max_workers: Whole | None = None
    models: dict[str, str] = Field(default_factory=dict)
    skills: Strs = ()
    budget_usd: Annotated[float, Field(ge=0)] | None = None
    expires: IsoDate | None = None


class Team(TeamConfig):
    id: str

    def expired(self, today: dt.date | None = None) -> bool:
        return self.expires is not None and (today or dt.date.today()) > self.expires


class TeamsFile(_Config):
    schema_version: int
    work: WorkConfig = WorkConfig()
    teams: Annotated[dict[str, TeamConfig], Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class Teams:
    teams: tuple[Team, ...]
    settings: Settings = field(default_factory=Settings)

    @property
    def shared(self) -> tuple[str, ...]:
        return self.settings.shared

    def ids(self) -> set[str]:
        return {t.id for t in self.teams}

    def get(self, tid: str) -> Team | None:
        return next((t for t in self.teams if t.id == tid), None)

    def of(self, path: str) -> str | None:
        """The team a path belongs to: the first one in the file that covers it."""
        if any(covers(s, path) for s in self.shared):
            return "shared"
        for team in self.teams:
            if any(covers(p, path) for p in team.owns):
                return team.id
        return None


def _max_local_agents(root: Path) -> int:
    """The host budget named once in the knob file; 4 until that file exists."""
    path = root / KNOBS_FILE
    if not path.is_file():
        return 4
    value = _toml(path).get("teams", {}).get("max_local_agents", 4)
    return _int(value, f"{path}: teams.max_local_agents")


def _explain(path: Path, error: ValidationError) -> Bad:
    """Name the table and the key the way the file spells them, one line each."""
    lines = []
    for item in error.errors(include_url=False):
        loc = [str(part) for part in item["loc"]]
        if item["type"] == "extra_forbidden":
            table, what = loc[:-1], f"unknown key {loc[-1]}"
        elif item["type"] == "tuple_type":
            # The model keeps lists as tuples; the file only knows lists.
            table, what = loc, 'a list of strings, like ["src/"]'
        else:
            table, what = loc, item["msg"]
        where = f"{path}: [{'.'.join(table)}]" if table else str(path)
        lines.append(f"{where}: {what}")
    return Bad("\n".join(lines))


def load_teams(root: Path) -> Teams:
    path = root / TEAMS_FILE
    data = _toml(path)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise Bad(
            f"{path}: schema_version = {data.get('schema_version')!r}, "
            f"this tac reads {SCHEMA_VERSION}"
        )
    if not isinstance(data.get("teams"), dict) or not data["teams"]:
        raise Bad(f"{path}: no [teams.<id>] tables")
    try:
        parsed = TeamsFile.model_validate(data)
    except ValidationError as e:
        raise _explain(path, e) from None
    for tid in parsed.teams:
        if not ID.match(tid):
            raise Bad(f"{path}: a team needs an id like 'core', got {tid!r}")
    settings = Settings.model_validate(
        {**parsed.work.model_dump(), "max_local_agents": _max_local_agents(root)}
    )
    # TOML keeps the order the tables were written in, and the first team that
    # covers a path is its home, so the specific teams come first.
    teams = tuple(
        Team.model_validate({**cfg.model_dump(), "id": tid})
        for tid, cfg in parsed.teams.items()
    )
    return Teams(teams, settings)


# ---------------------------------------------------------------- orders


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    text: str
    check: str | None
    judge: str | None
    timeout: int


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    root: Path
    title: str
    team: str
    branch: str
    goal: str
    builder: str
    owns: tuple[str, ...]
    cross: tuple[str, ...]
    needs: tuple[str, ...]
    criteria: tuple[Criterion, ...]
    issue: int | None = None
    provider: str = ""
    anyone: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    # A worked example of the format: read and validated, never built, so it
    # never guards an edit and never needs a review.
    reference: bool = False

    @property
    def dir(self) -> Path:
        return self.root / ORDERS / self.id

    @property
    def rel(self) -> str:
        return f"{ORDERS}/{self.id}/"

    def may_touch(self, path: str) -> bool:
        return (
            path.startswith(self.rel)
            or any(covers(o, path) for o in self.owns)
            or any(covers(a, path) for a in self.anyone)
        )


def load_order(folder: Path, teams: Teams, root: Path) -> Order:
    path = folder / "order.toml"
    data = _toml(path)
    _version(data, path)
    oid = str(data.get("id", ""))
    if not ID.match(oid) or oid != folder.name:
        raise Bad(f"{path}: id = {oid!r} has to be the folder name, {folder.name!r}")
    for key in ("title", "team", "branch"):
        if not str(data.get(key, "")).strip():
            raise Bad(f"{path}: {key} is missing")
    if data["team"] not in teams.ids():
        raise Bad(f"{path}: team {data['team']!r} is not in {TEAMS_FILE}")
    cross = tuple(data.get("cross", []))
    for other in cross:
        if other not in teams.ids() or other == data["team"]:
            raise Bad(f"{path}: cross names {other!r}, which is not another team")
    reference = data.get("reference", False)
    if not isinstance(reference, bool):
        raise Bad(f"{path}: reference is true or false")
    provider = str(data.get("provider", ""))
    if provider and not PROVIDER.match(provider):
        raise Bad(f"{path}: provider = {provider!r}: a short lowercase name")
    owns = tuple(data.get("owns", []))
    if not owns:
        raise Bad(f"{path}: owns is empty, so the order may touch nothing")
    for entry in owns:
        if any(ch in entry for ch in "*?["):
            raise Bad(f"{path}: owns {entry!r}: name files or folders/, not globs")
        inside = (
            names(root, "ls-files", "-z", "--", entry) if entry.endswith("/") else []
        )
        for name in (entry, *inside):
            home = teams.of(name)
            if home is None:
                raise Bad(
                    f"{path}: owns {name!r}, which no team in {TEAMS_FILE} covers"
                )
            if home not in (data["team"], "shared", *cross):
                raise Bad(
                    f"{path}: owns {entry!r}, but {name!r} belongs to team {home!r}; "
                    f"add it to cross and get that team's sign-off, or leave it"
                )
    criteria = []
    unknown = [k for k in data if k not in ORDER_KEYS]
    for raw in data.get("criteria", []):
        cid = str(raw.get("id", ""))
        if not cid or not str(raw.get("text", "")).strip():
            raise Bad(f"{path}: a criterion needs an id and a text")
        unknown += [f"criteria.{cid}.{k}" for k in raw if k not in CRITERION_KEYS]
        has_check, has_judge = bool(raw.get("check")), bool(raw.get("judge"))
        if has_check == has_judge:
            raise Bad(f"{path}: criterion {cid} needs exactly one of check or judge")
        if has_judge and raw["judge"] not in JUDGES:
            raise Bad(f"{path}: criterion {cid}: judge is one of {', '.join(JUDGES)}")
        criteria.append(
            Criterion(
                cid,
                raw["text"],
                raw.get("check"),
                raw.get("judge"),
                int(raw.get("timeout", teams.settings.check_timeout_s)),
            )
        )
    if len({c.id for c in criteria}) != len(criteria):
        raise Bad(f"{path}: two criteria share an id")
    if not any(c.check for c in criteria):
        raise Bad(f"{path}: no criterion has a check; at least one must be a command")
    return Order(
        oid,
        root,
        data["title"],
        data["team"],
        data["branch"],
        str(data.get("goal", "")),
        str(data.get("builder", "")),
        owns,
        cross,
        tuple(data.get("needs", [])),
        tuple(criteria),
        data.get("issue"),
        provider,
        teams.settings.anyone,
        tuple(unknown),
        reference,
    )


def orders_in(root: Path, teams: Teams) -> list[Order]:
    base = root / ORDERS
    if not base.is_dir():
        return []
    return [
        load_order(d, teams, root)
        for d in sorted(base.iterdir())
        if (d / "order.toml").is_file()
    ]


def readable(root: Path, teams: Teams) -> tuple[list[Order], list[str]]:
    """The orders that load, and one sentence for each that does not. Reading
    across worktrees has to survive a draft somebody else has just started."""
    base, good, drafts = root / ORDERS, [], []
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        if (d / "order.toml").is_file():
            try:
                good.append(load_order(d, teams, root))
            except Bad as e:
                drafts.append(str(e))
    return good, drafts


def base_of(root: Path, base: str | None) -> str:
    """The branch orders are measured against: the caller's, else the config's."""
    if base:
        return base
    try:
        return load_teams(root).settings.base
    except (Bad, FileNotFoundError):
        return Settings().base


def worktrees(root: Path) -> list[tuple[Path, str]]:
    """Every checkout of this repository with the branch it is on."""
    out, path = [], None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:])
        elif line.startswith("branch ") and path:
            out.append((path, line[7:].removeprefix("refs/heads/")))
    return out


def landed(root: Path, base: str | None = None) -> set[str]:
    """Orders whose accepting review is on the base branch: it travels with the
    work."""
    ref = base_of(root, base)
    on_base = names(root, "ls-tree", "-r", "--name-only", "-z", ref, f"{ORDERS}/")
    done = set()
    for name in on_base:
        if name.endswith("/review.toml"):
            try:
                ruling = tomllib.loads(git(root, "show", f"{ref}:{name}"))
            except tomllib.TOMLDecodeError:
                continue
            if ruling.get("verdict") == "accept":
                done.add(name.split("/")[2])
    return done


def active(
    root: Path, drafts: list[str] | None = None, base: str | None = None
) -> list[Order]:
    """Orders being worked on now: their branch is checked out in some worktree.
    Unreadable ones are skipped, and named in `drafts` when the caller asks."""
    done, found = landed(root, base), {}
    for path, branch in worktrees(root):
        try:
            orders, bad = readable(path, load_teams(path))
        except (Bad, FileNotFoundError):
            continue
        if drafts is not None:
            drafts += bad
        for order in orders:
            if order.branch == branch and order.id not in done and not order.reference:
                found[order.id] = order
    return list(found.values())


def collisions(orders: list[Order], involving: set[str] | None = None) -> list[str]:
    """Two orders that cannot both be built as written: they own the same file,
    or they share a branch, where each would count the other's files as strays.
    With `involving`, only the pairs one of those orders is part of."""
    out = []
    for i, a in enumerate(orders):
        for b in orders[i + 1 :]:
            if involving is not None and not {a.id, b.id} & involving:
                continue
            if a.branch == b.branch:
                out.append(f"{a.id} and {b.id} share the branch {a.branch}")
            hit = [(x, y) for x in a.owns for y in b.owns if overlap(x, y)]
            if hit:
                x, y = hit[0]
                out.append(
                    f"{a.id} and {b.id} both own {x if x == y else x + ' / ' + y}"
                )
    return out


# A test marked integration may fetch the internet, and a publisher's outage is
# no reason for an order to fail: a criterion's command runs offline.
INTEGRATION = re.compile(
    r"^\s*(@pytest\.mark\.integration|pytestmark\b.*\bmark\.integration)\b", re.M
)


def online(order: Order) -> list[str]:
    """Each criterion that runs pytest over a file with integration tests and
    does not deselect them, as a sentence. A warning: the marker may be off."""
    out = []
    for c in order.criteria:
        for part in re.split(r"&&|\|\||[;|]", c.check or ""):
            try:
                words = shlex.split(part)
            except ValueError:
                words = part.split()
            if "pytest" not in words:
                continue
            args = words[words.index("pytest") + 1 :]
            if any("not integration" in w for w in args):
                continue
            # A path, not the value of -m or -k; a file not written yet is none.
            given = [
                w.split("::")[0]
                for w in args
                if w[:1] != "-" and ("/" in w or w.split("::")[0].endswith(".py"))
            ]
            # With no path pytest runs its testpaths, which here is tests/.
            paths = [order.root / g for g in given or ["tests"]]
            files = sorted(
                f
                for g in paths
                for f in ([g] if g.is_file() else g.rglob("*.py") if g.is_dir() else [])
            )
            hits = [
                f.relative_to(order.root).as_posix()
                for f in files
                if INTEGRATION.search(f.read_text(encoding="utf-8", errors="replace"))
            ]
            if hits:
                out.append(
                    f"{order.id} {c.id} runs pytest on {', '.join(hits)}, which has "
                    f"integration tests, without -m 'not integration'"
                )
    return out


def find(oid: str, root: Path) -> Order:
    teams = load_teams(root)
    for order in orders_in(root, teams):
        if order.id == oid:
            return order
    raise Bad(f"no order {oid!r} under {ORDERS}/")


# ---------------------------------------------------------------- checks


def changed(root: Path, base: str) -> list[str]:
    """What this branch changes against base, committed or not."""
    mine = set(diff_names(root, base))
    loose = dirty(root)
    if loose:
        # An uncommitted file is this branch's work only if it differs from
        # base: in the middle of a merge, everything arriving from main is
        # uncommitted too, and none of it is ours. The index is asked as well as
        # the working tree, or a change that is only staged reads as no change.
        differs: set[str] = set()
        for where in ((), ("--cached",)):  # the working tree, then the index
            args = ("diff", *where, "--name-only", "--no-renames", "-z", base, "--")
            differs |= set(names(root, *args, *loose, must=True))
        new = set(
            names(root, "ls-files", "--others", "--exclude-standard", "-z", must=True)
        )
        mine |= {n for n in loose if n in differs or n in new}
    return sorted(n for n in mine if n)


def diff_names(root: Path, base: str, rev: str = "HEAD") -> list[str]:
    """Paths that differ between the merge base and rev. A shallow clone may
    lack the merge base, so deepen once; after that, not knowing is an error."""
    args = ("diff", "--name-only", "--no-renames", "-z", f"{base}...{rev}")
    try:
        return names(root, *args, must=True)
    except Bad:
        git(root, "fetch", "--no-tags", "--deepen=500", "origin")
        return names(root, *args, must=True)


def strays(order: Order, base: str) -> list[str]:
    return [p for p in changed(order.root, base) if not order.may_touch(p)]


def tip(root: Path, branch: str) -> str:
    """The newest commit of a branch: in this clone, else on origin. A pull
    request's runner has only the commit it tests, so origin is asked once."""
    refs = (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
    for ref in refs:
        if sha := git(root, "rev-parse", "--verify", "-q", ref + "^{commit}"):
            return sha
    if git(root, "check-ref-format", "--normalize", refs[0]) == refs[0]:
        git(root, "fetch", "-q", "--no-tags", "origin", f"+{refs[0]}:{refs[1]}")
        if sha := git(root, "rev-parse", "--verify", "-q", refs[1] + "^{commit}"):
            return sha
    raise Bad(
        f"branch {branch} is neither in this clone nor on origin, so what it "
        f"changed cannot be told apart from the rest of this pull request"
    )


def own_files(order: Order, base: str, head: str) -> list[str]:
    """What the order's own branch changed, as this checkout carries it: all of
    it on that branch, and in a train car the part of the branch the car merged,
    so a loose change riding in the same car is nobody's stray."""
    if order.branch == head:
        return changed(order.root, base)
    merged = git(order.root, "merge-base", "HEAD", tip(order.root, order.branch))
    if not merged:
        raise Bad(f"branch {order.branch} and this pull request share no commit")
    return diff_names(order.root, base, merged)


def tree_key(root: Path) -> str:
    """The exact state of the checkout: a result is only good for the tree it saw."""
    paths = dirty(root)
    parts = [git(root, "rev-parse", "HEAD"), git(root, "diff", "HEAD")]
    for rel in paths:
        file = root / rel
        if file.is_file():
            parts.append(rel + hashlib.sha1(file.read_bytes()).hexdigest())
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def last_result(order: Order) -> dict[str, Any] | None:
    path = order.dir / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except json.JSONDecodeError:
        return None


def _spawn(
    args: str | list[str], cwd: Path, timeout: int, stdin: str | None = None
) -> tuple[int, str]:
    """A command in its own process group, so that a timeout ends the browsers
    and servers it started and not only the shell. A string runs in a shell (an
    order's criterion, the same trust as the justfile); a list never does."""
    proc = subprocess.Popen(
        args,
        shell=isinstance(args, str),
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(input=stdin, timeout=timeout)
        return proc.returncode, out[-1200:]
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return 124, f"timed out after {timeout}s"


def run_check(
    order: Order, base: str, force: bool = False, budget: int | None = None
) -> dict[str, Any]:
    """Ownership, then every command. The cache is the builder's convenience:
    it is a file the builder can write, so acceptance always passes force."""
    out_path = order.dir / "result.json"
    key = tree_key(order.root)
    old = None if force else last_result(order)
    if old and old.get("tree") == key and old.get("ok"):
        old["cached"] = True
        return old
    result: dict[str, Any] = {
        "order": order.id,
        "tree": key,
        "strays": strays(order, base),
    }
    rows, began = [], time.monotonic()
    for c in order.criteria:
        if not c.check:
            rows.append({"id": c.id, "judge": c.judge, "text": c.text})
            continue
        start = time.monotonic()
        spent = int(start - began)
        allowed = (
            c.timeout if budget is None else max(1, min(c.timeout, budget - spent))
        )
        code, tail = _spawn(c.check, order.root, allowed)
        rows.append(
            {
                "id": c.id,
                "check": c.check,
                "exit": code,
                "seconds": round(time.monotonic() - start, 1),
                "tail": tail,
                "text": c.text,
            }
        )
    result["criteria"] = rows
    result["ok"] = not result["strays"] and all(r.get("exit", 0) == 0 for r in rows)
    # A run hurried by a hook's time limit is not the measurement.
    if budget is None:
        out_path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def show(result: dict[str, Any]) -> str:
    lines = [
        f"order {result['order']}" + ("  (cached)" if result.get("cached") else "")
    ]
    for p in result["strays"]:
        lines.append(f"  STRAY  {p}  is outside what the order owns")
    for r in result["criteria"]:
        if "judge" in r:
            lines.append(f"  judge  {r['id']}  for the {r['judge']}: {r['text']}")
        else:
            mark = "pass " if r["exit"] == 0 else "FAIL "
            lines.append(f"  {mark}  {r['id']}  {r['seconds']}s  {r['check']}")
            if r["exit"] != 0:
                lines += [f"         | {x}" for x in r["tail"].splitlines()[-12:]]
    lines.append("  OK" if result["ok"] else "  NOT OK")
    return "\n".join(lines)


# ---------------------------------------------------------------- review


def contribution(order: Order, base: str, rev: str) -> str:
    """What the branch adds to the files the order owns, and to the order itself,
    as a patch id: the same after a clean merge of main, different after any
    edit, whitespace included."""
    span = f"{base}...{rev}"
    touched = names(
        order.root,
        *("diff", "--name-only", "--no-renames", "-z", span, "--"),
        *order.owns,
        order.rel + "order.toml",
        must=True,
    )
    # The rulings are about the contribution, not part of it: an order that owns
    # work/ would otherwise end its own review by committing it.
    rulings = (order.rel + "review.toml", order.rel + "signoff-*.toml")
    paths = [p for p in touched if not any(fnmatch.fnmatchcase(p, r) for r in rulings)]
    if not paths:
        return ""
    diff = subprocess.run(
        [*GIT, "-C", str(order.root), "diff", "--no-renames", span, "--", *paths],
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        raise Bad(
            f"git diff {base}...{rev}: {diff.stderr.decode(errors='replace')[:300]}"
        )
    # Bytes in and out: an owned file does not have to be UTF-8. Verbatim,
    # because indentation is meaning in Python and in YAML.
    ident = subprocess.run(
        ["git", "patch-id", "--verbatim"],
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    return ident.stdout.split(b" ")[0].strip().decode()


def _signed(order: Order, path: Path, who: str, base: str) -> dict[str, Any]:
    """A review or a sign-off: by someone else, about this order, still current."""
    data = _toml(path)
    _version(data, path)
    if data.get("order") != order.id:
        raise Bad(f"{path}: order = {data.get('order')!r}, expected {order.id!r}")
    by = str(data.get("by", "")).strip()
    if not by:
        raise Bad(f"{path}: by is missing: a {who} has a name")
    if not order.builder:
        raise Bad(
            f"{order.dir / 'order.toml'}: builder is empty, so nobody can tell "
            f"whether {by!r} is someone else"
        )
    if by == order.builder:
        raise Bad(f"{path}: by = {by!r} is the builder; nobody accepts their own work")
    if data.get("verdict") not in ("accept", "improve"):
        raise Bad(f"{path}: verdict is accept or improve")
    sha = str(data.get("reviewed", ""))
    if not SHA.match(sha):
        raise Bad(f"{path}: reviewed = {sha!r}: the full commit id, not a name for it")
    contained = subprocess.run(
        [*GIT, "-C", str(order.root), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True,
        check=False,
    )
    if contained.returncode != 0:
        raise Bad(f"{path}: reviewed = {sha!r} is not a commit this branch contains")
    if contribution(order, base, sha) != contribution(order, base, "HEAD"):
        raise Bad(
            f"{path}: the order or a file it owns changed after the {who} looked; "
            f"review again and update reviewed"
        )
    return data


def _other_provider(order: Order, data: dict[str, Any], path: Path, rule: str) -> None:
    """The reviewer is a fresh execution on the other provider: a model does
    not catch what its own family tends to miss."""
    if rule == "any":
        return
    if not order.provider:
        raise Bad(
            f"{order.dir / 'order.toml'}: provider is empty, so nobody can tell "
            f"whether the review came from the other provider"
        )
    theirs = str(data.get("provider", "")).strip()
    if not theirs:
        raise Bad(f"{path}: provider is missing: which provider ran the review")
    if theirs == order.provider:
        raise Bad(
            f"{path}: provider = {theirs!r} built it too; the review comes from "
            f"the other provider"
        )


def load_review(order: Order, base: str | None = None) -> dict[str, Any]:
    teams = load_teams(order.root)
    ref = base or teams.settings.base
    path = order.dir / "review.toml"
    if not path.is_file():
        raise Bad(f"{path}: no review yet")
    data = _signed(order, path, "reviewer", ref)
    _other_provider(order, data, path, teams.settings.review_provider)
    ruled = {str(r.get("id")): r for r in data.get("criteria", [])}
    for c in order.criteria:
        row = ruled.get(c.id)
        if not row or row.get("verdict") not in ("pass", "fail"):
            raise Bad(f"{path}: criterion {c.id} has no pass or fail")
        if not str(row.get("evidence", "")).strip():
            raise Bad(f"{path}: criterion {c.id} has a verdict and no evidence")
    return data


def review_gate(order: Order, base: str | None = None) -> list[str]:
    """The review stage's gate, as sentences; empty means it passes: a readable
    review, current for this branch, by someone other than the builder, from the
    other provider, that accepts and fails no criterion."""
    try:
        review = load_review(order, base)
    except Bad as e:
        return [str(e)]
    why = []
    if review["verdict"] != "accept":
        why.append("the reviewer asked for improvements")
    failed = [r["id"] for r in review.get("criteria", []) if r["verdict"] == "fail"]
    if failed:
        why.append(f"the reviewer failed {', '.join(failed)}")
    return why


def acceptance(order: Order, base: str, run: bool = True) -> list[str]:
    """Everything between this order and main, as sentences. Empty means land it.
    With `run` the commands are measured afresh; CI passes False because the
    battery next to it runs the same tests."""
    why, reviewer = [], ""
    teams = load_teams(order.root)
    if run and not run_check(order, base, force=True)["ok"]:
        why.append("the checks do not pass (just work-check)")
    try:
        review = load_review(order, base)
        reviewer = str(review["by"]).strip()
        if review["verdict"] != "accept":
            why.append("the reviewer asked for improvements")
        failed = [r["id"] for r in review["criteria"] if r["verdict"] == "fail"]
        if failed:
            why.append(f"the reviewer failed {', '.join(failed)}")
    except Bad as e:
        why.append(str(e))
    for team in order.cross:
        path = order.dir / f"signoff-{team}.toml"
        try:
            if not path.is_file():
                raise Bad(f"{path}: team {team} has not signed off on its files")
            signed = _signed(order, path, f"{team} manager", base)
            if signed.get("team") != team:
                raise Bad(f"{path}: team = {signed.get('team')!r}, expected {team!r}")
            by = str(signed["by"]).strip()
            if by == reviewer:
                raise Bad(
                    f"{path}: by = {reviewer!r} also wrote the review; a team's "
                    f"manager speaks for the team, not for the review"
                )
            home = teams.get(team)
            if home and home.manager and by != home.manager:
                raise Bad(
                    f"{path}: by = {by!r}, but team {team}'s manager is "
                    f"{home.manager!r} in {TEAMS_FILE}"
                )
            if signed["verdict"] != "accept":
                why.append(f"team {team} did not accept the change to its files")
        except Bad as e:
            why.append(str(e))
    return why


# ---------------------------------------------------------------- repair


REPAIR_TAG = "check-output"


def _inert(text: str) -> str:
    """Output of a check is data; it may not close the tag that fences it."""
    return re.sub(
        rf"<(/?)\s*{REPAIR_TAG}", lambda m: f"&lt;{m.group(1)}{REPAIR_TAG}", text
    )


def _templates() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("tac", "templates"),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_repair(order: Order, result: dict[str, Any]) -> str:
    """The repair handoff for the builder role: the criteria that failed and the
    strays, fenced as reference data, never as instructions."""
    failing = [
        {
            "id": r["id"],
            "text": _inert(str(r["text"])),
            "check": _inert(str(r["check"])),
            "exit": r["exit"],
            "tail": _inert(str(r["tail"])),
        }
        for r in result.get("criteria", [])
        if "exit" in r and r["exit"] != 0
    ]
    return (
        _templates()
        .get_template("handoffs/repair.md.j2")
        .render(
            order={
                "id": order.id,
                "title": order.title,
                "team": order.team,
                "branch": order.branch,
                "owns": list(order.owns),
            },
            strays=[_inert(s) for s in result.get("strays", [])],
            failing=failing,
            tag=REPAIR_TAG,
        )
    )


def repair(order: Order, base: str) -> tuple[dict[str, Any], str]:
    """One bounded builder turn on an order whose check failed, then the check
    again. Returns that check and a line on what the turn did."""
    teams = load_teams(order.root)
    result = last_result(order)
    if not result or result.get("tree") != tree_key(order.root):
        result = run_check(order, base, force=True)
    if result.get("ok"):
        return result, "the order holds; nothing to repair"
    prompt = render_repair(order, result)
    (order.dir / "repair.md").write_text(prompt, encoding="utf-8")
    rep = teams.settings.repair
    if not rep.argv:
        note = (
            f"no builder turn: [work.repair] argv in {TEAMS_FILE} is empty; "
            f"the handoff is in {order.rel}repair.md"
        )
    else:
        argv = [
            a.replace("{order}", order.id).replace("{max_turns}", str(rep.max_turns))
            for a in rep.argv
        ]
        code, _ = _spawn(argv, order.root, rep.timeout_s, stdin=prompt)
        note = f"builder turn ended with exit {code}"
    return run_check(order, base, force=True), note


# ---------------------------------------------------------------- plan


def load_goal(gid: str, root: Path) -> dict[str, Any]:
    path = root / GOALS / f"{gid}.toml"
    data = _toml(path)
    _version(data, path)
    if data.get("id") != gid or not str(data.get("statement", "")).strip():
        raise Bad(f"{path}: a goal needs id = {gid!r} and a statement")
    if not data.get("orders"):
        raise Bad(f"{path}: orders is empty")
    return data


def plan(gid: str, root: Path) -> tuple[list[list[Order]], dict[str, list[str]]]:
    """Launch groups: what an order needs has landed or ran in an earlier group,
    no two orders in a group own the same file, a group fits the budget and no
    team runs more workers than it may. Next to them, what cannot start yet and
    which orders it waits for: an order of another goal that has not landed, or
    one here that waits itself. Only orders that wait on each other are a
    mistake in the plan."""
    goal = load_goal(gid, root)
    teams = load_teams(root)
    ceiling = teams.settings.max_local_agents
    budget = min(int(goal.get("budget", ceiling)), ceiling)
    by_id = {o.id: o for o in orders_in(root, teams)}
    missing = [i for i in goal["orders"] if i not in by_id]
    if missing:
        raise Bad(f"goal {gid}: no order file for {', '.join(missing)}")
    done = landed(root, teams.settings.base)
    todo = [by_id[i] for i in goal["orders"] if i not in done]
    for o in todo:
        unknown = [n for n in o.needs if n not in by_id and n not in done]
        if unknown:
            raise Bad(f"order {o.id}: needs {', '.join(unknown)}, which does not exist")
    ours = {o.id for o in todo}
    blocked: dict[str, list[str]] = {}
    grew = True
    while grew:
        grew = False
        for o in todo:
            waits = [
                n for n in o.needs if n not in done and (n not in ours or n in blocked)
            ]
            if waits and o.id not in blocked:
                blocked[o.id], grew = waits, True
    todo = [o for o in todo if o.id not in blocked]
    groups: list[list[Order]] = []
    settled = set(done)
    while todo:
        group: list[Order] = []
        for o in todo:
            ready = all(n in settled for n in o.needs)
            free = not any(overlap(x, y) for g in group for x in g.owns for y in o.owns)
            team = teams.get(o.team)
            cap = team.max_workers if team and team.max_workers else budget
            room = sum(1 for g in group if g.team == o.team) < cap
            if ready and free and room and len(group) < budget:
                group.append(o)
        if not group:
            raise Bad(
                f"goal {gid}: {', '.join(o.id for o in todo)} wait on each other, "
                f"a cycle in needs"
            )
        groups.append(group)
        settled |= {o.id for o in group}
        todo = [o for o in todo if o not in group]
    return groups, blocked


def sweep(root: Path, base: str | None = None) -> list[str]:
    """Remove the orders that have landed: the base has their review, git the rest."""
    gone = []
    for oid in sorted(landed(root, base)):
        folder = root / ORDERS / oid
        if folder.is_dir():
            shutil.rmtree(folder)
            gone.append(oid)
    return gone


# ---------------------------------------------------------------- the issue
#
# The repository is the record: orders, reviews and sign-offs travel with the
# code. The order's GitHub issue is the conversation. An agent reads it before
# it starts and the tool keeps one status comment there current.

MARK = "<!-- work:{id} -->"
# Anyone can comment on a public issue. What an agent reads as instructions
# comes only from people who can already push here.
TRUSTED = ("OWNER", "COLLABORATOR")


def status_comment(
    order: Order, result: dict[str, Any] | None, review: dict[str, Any] | None
) -> str:
    """The order as its issue sees it. Same inputs, same text, so it is safe to
    rewrite in place."""
    lines = [
        MARK.format(id=order.id),
        f"**Order `{order.id}`** ({order.team}): {order.title}",
        "",
        f"Branch `{order.branch}`. Owns: {', '.join(f'`{o}`' for o in order.owns)}.",
        "",
        "| criterion | how | state |",
        "|---|---|---|",
    ]
    ran = {r["id"]: r for r in (result or {}).get("criteria", [])}
    ruled = {r["id"]: r for r in (review or {}).get("criteria", [])}
    for c in order.criteria:
        if c.check:
            row = ran.get(c.id)
            state = "not run" if not row else ("pass" if row["exit"] == 0 else "FAIL")
            how = f"`{c.check}`"
        else:
            state, how = "waits for the " + str(c.judge), "judged"
        if c.id in ruled:
            state += f", reviewer: {ruled[c.id]['verdict']}"
        lines.append(f"| {c.id}: {c.text} | {how} | {state} |")
    for p in (result or {}).get("strays", []):
        lines.append(f"\nOutside the order: `{p}`")
    if review:
        lines.append(f"\nReview by {review['by']}: **{review['verdict']}**.")
        for f in review.get("findings", []):
            lines.append(
                f"- {f.get('severity', '')}: `{f.get('file', '')}` {f['text']}"
            )
    lines.append("\nWritten by `tac work post`; edits here are overwritten.")
    return "\n".join(lines) + "\n"


def is_status(comment: dict[str, Any], oid: str = "") -> bool:
    """A status comment of this tool: the marker, from someone who can push. A
    stranger who pastes the marker has written an ordinary untrusted comment."""
    body = str(comment.get("body", ""))
    mark = MARK.format(id=oid) if oid else "<!-- work:"
    return body.startswith(mark) and comment.get("author_association") in TRUSTED


def trusted(comments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The comments an agent may read, and how many were held back."""
    keep = [
        c
        for c in comments
        if c.get("author_association") in TRUSTED and not is_status(c)
    ]
    ours = sum(1 for c in comments if is_status(c))
    return keep, len(comments) - len(keep) - ours


def gh(root: Path, *args: str, body: str | None = None) -> str:
    out = subprocess.run(
        ["gh", "api", *args, *(["-f", f"body={body}"] if body is not None else [])],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise Bad(f"gh api {args[0]}: {out.stderr.strip()[:300]}")
    return out.stdout


def comments(root: Path, issue: int) -> list[dict[str, Any]]:
    raw = gh(root, f"repos/{{owner}}/{{repo}}/issues/{issue}/comments", "--paginate")
    return json.loads(raw or "[]")


def issue_of(order: Order) -> int:
    if not order.issue:
        raise Bad(f"{order.dir / 'order.toml'}: no issue = <number> to talk on")
    return int(order.issue)


# ---------------------------------------------------------------- hooks
#
# Reminders for a cooperative agent, never the gate: an edit made through a
# shell is seen by no hook. What decides is check, accept and CI.


def _repo_of(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _who(event: dict[str, Any]) -> str:
    """The subagent behind a hook event, or nothing for a session: a session is
    held by what its report says, so nothing would ever read its id."""
    return str(event.get("agent_id") or "")


def _touched(order: Order) -> dict[str, str]:
    path = order.dir / "touched.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except json.JSONDecodeError:
        return {}


def _remember(order: Order, who: str, rel: str, keep: int) -> None:
    """Note which agent worked on which order, so that stopping can be judged by
    what an agent did and not by how it words its report. Someone who only ever
    wrote into the order's folder is reviewing, not building.

    Agents are kept in the order of their last edit and only the `keep` most
    recent stay. An agent that is already the most recent with the same role
    costs no write, the common case of one builder editing file after file. A
    lost entry costs an agent a reminder, never a check."""
    if not who:
        return
    seen = _touched(order)
    role = "builder" if not rel.startswith(order.rel) else seen.get(who, "reviewer")
    if list(seen.items())[-1:] == [(who, role)]:
        return
    seen.pop(who, None)
    seen[who] = role
    kept = dict(list(seen.items())[-keep:])
    (order.dir / "touched.json").write_text(json.dumps(kept), encoding="utf-8")


def hook_pre_tool(event: dict[str, Any]) -> tuple[int, str]:
    """Refuse an edit outside what the orders on this branch own. Anything this
    cannot read lets the edit through: a broken order has to stay repairable,
    and `check` refuses it later anyway."""
    given = event.get("tool_input") or {}
    raw = given.get("file_path") or given.get("notebook_path")
    if not raw or not isinstance(raw, str):
        return 0, ""
    target = Path(raw).resolve()
    root = _repo_of(target.parent)
    if not root or not (root / TEAMS_FILE).is_file():
        return 0, ""
    try:
        teams = load_teams(root)
        orders, _ = readable(root, teams)
        rel = target.relative_to(root.resolve()).as_posix()
    except (Bad, ValueError):
        return 0, ""
    branch = git(root, "branch", "--show-current")
    mine = [o for o in orders if o.branch == branch]
    if not mine:
        return 0, ""
    if measured(rel):
        return 2, f"{rel} is a measurement tac work writes, not yours to edit"
    for order in mine:
        if order.may_touch(rel):
            _remember(order, _who(event), rel, teams.settings.touched_keep)
            return 0, ""
    home = teams.of(rel) or "nobody"
    ids = ", ".join(o.id for o in mine)
    owns = ", ".join(x for o in mine for x in o.owns)
    return 2, (
        f"{rel} is outside order {ids}; it belongs to team {home}. Leave it and "
        f"report it, or have the manager change owns and cross. Owned here: {owns}"
    )


def hook_stop(
    event: dict[str, Any], base: str | None = None, root: Path | None = None
) -> tuple[int, str]:
    """Send an agent back once when the order it worked on does not hold. A
    subagent is known by the edits the other hook saw; a session only by a
    report whose first line names the order, because Stop fires at the end of
    every turn and a check can take minutes."""
    if event.get("stop_hook_active"):
        return 0, ""
    if root is None:
        found = _repo_of(Path(str(event.get("cwd") or Path.cwd())).resolve())
        if not found or not (found / TEAMS_FILE).is_file():
            return 0, ""
        root = found
    settings = load_teams(root).settings
    ref = base or settings.base
    orders = {o.id: o for o in active(root, base=ref)}
    who, text = _who(event), str(event.get("last_assistant_message", ""))
    roles = {o.id: _touched(o)[who] for o in orders.values() if who in _touched(o)}
    named = ORDER_LINE.search(text)
    if named and named.group(1) in orders and named.group(1) not in roles:
        said = ROLE_LINE.search(text)
        roles[named.group(1)] = said.group(1) if said else "builder"
    for oid, role in roles.items():
        order = orders[oid]
        try:
            if role == "reviewer":
                load_review(order, ref)
                continue
            result = run_check(order, ref, budget=settings.stop_budget_s)
        except Bad as e:
            return 2, str(e)
        if not result["ok"]:
            return 2, "The order does not hold yet, so this is not done:\n" + show(
                result
            )
    return 0, ""


# ---------------------------------------------------------------- scaffolding

ORDER_TEMPLATE = """v = 1
id = {id}
title = {title}
team = {team}
branch = {branch}
goal = {goal}
# Whoever builds this writes their name here; a review by the same name does not count.
builder = ""
# The builder's provider; the review comes from the other one.
provider = ""

# Files, or folders ending in /. No globs: two orders may never own the same file.
owns = []
# Other teams whose files this order has to touch; each one signs off.
cross = []
# Orders that have to land before this one starts.
needs = []

# At least one criterion is a command. Exit 0 is pass; anything else is not.
[[criteria]]
id = "c1"
text = ""
check = ""

# What a command cannot decide goes to a person in a role, who rules on it
# in review.toml with evidence.
# [[criteria]]
# id = "c2"
# text = ""
# judge = "reviewer"
"""


def _q(text: str) -> str:
    """A TOML basic string: JSON's escapes are a subset of TOML's."""
    return json.dumps(text, ensure_ascii=False)


def new_order(
    root: Path, oid: str, team: str, title: str, goal: str = "", branch: str = ""
) -> Path:
    if not ID.match(oid):
        raise Bad(f"{oid!r}: an id is lowercase letters, digits and dashes")
    teams = load_teams(root)
    home = teams.get(team)
    if home is None:
        raise Bad(f"{team!r} is not a team in {TEAMS_FILE}")
    if home.expired():
        raise Bad(f"team {team} expired on {home.expires}; it takes no new orders")
    folder = root / ORDERS / oid
    if folder.exists():
        raise Bad(f"{ORDERS}/{oid} exists")
    folder.mkdir(parents=True)
    (folder / "order.toml").write_text(
        ORDER_TEMPLATE.format(
            id=_q(oid),
            title=_q(title),
            team=_q(team),
            branch=_q(branch or git(root, "branch", "--show-current")),
            goal=_q(goal),
        ),
        encoding="utf-8",
    )
    return folder / "order.toml"
