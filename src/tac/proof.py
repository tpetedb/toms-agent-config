"""Acceptance 8, the offline part: the coverage manifest and the dev proof.

`tac proof coverage` derives every condition the harness claims from the
configuration and the source (design sections 9 and 13), holds
dev/coverage/map.toml to that set, runs the mapped tests and binds the result to
the tested revision. `tac proof dev` runs the ledger's gates, the bypass attempts
and the mutation runs, then evaluates every live probe; a probe it cannot run is
reported skipped with the step it waits on, and a live probe is reported passed
only on a runner receipt that verifies against runner.pub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import typing
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from tac import pipelines
from tac.config import Config, load_config
from tac.doctor import CHECKS, run_checks
from tac.human import HumanError, Item, human_paths, load_items
from tac.human_cli import approve_command, human_group
from tac.memory_cli import memory_group
from tac.probes import PLACES, SESSIONS, NotYetLive, observe
from tac.receipts import ReceiptError, check_signature, parse, trusted_key_from_revision
from tac.work import as_tuple

MAP_FILE = "dev/coverage/map.toml"
OUT_DIR = "dev/out"
COVERAGE_JSON = f"{OUT_DIR}/coverage.json"
DEV_JSON = f"{OUT_DIR}/dev-proof.json"
LEDGER = "dev/ledger"
HANDOFF_TEMPLATES = ("templates/handoffs", "src/tac/templates/handoffs")
HANDOFF_CONTRACTS = "contracts/handoffs"
CLIENTS = ("claude", "codex")
PROOF_TESTS = ("tests/test_bypass.py", "tests/test_mutation.py")
RENDER_RECIPE = "diagrams-render"
RENDER_SKIP = "recipe lands with order 7"
# The one owner step that says Codex is resumed and the live run may start.
LIVE_STEP = "S10"


class ProofError(Exception):
    """A proof that cannot run as asked; the message names what to do."""


# ---------------------------------------------------------------- what is claimed

# The four layers of design section 9, strongest last.
LAYERS: Mapping[str, str] = {
    "1": "rendered entry points: the brief, the skills, the roles, the wiring",
    "2": "harness hooks: the stamped guard refuses what the session may not do",
    "3a": "git hooks: prek refuses a bad commit or push before it leaves the host",
    "3b": "the repository boundary: ruleset, CODEOWNERS, CI from the base revision",
}

# The six build conditions of design section 18.
BUILD_CONDITIONS: Mapping[str, str] = {
    "C1": "guard failure is explicit: missing, altered and reused tokens, runner "
    "loss and hook timeout; delegation off takes the spawn tools away",
    "C2": "hook execution matches the claimed isolation",
    "C3": "receipt trust is pinned to the base revision's runner.pub",
    "C4": "test the new code, judge with the deployed copy",
    "C5": "M0 and M5 check what they claim",
    "C6": "the owner's token never reaches the runner, and inside a sandboxed "
    "session both keychain entries are out of reach",
}

# "What dev/ proves about the layers, including each bypass attempt" (design
# section 9), one slug per attempt, with the sentence it stands for.
BYPASSES: Mapping[str, str] = {
    "ruleset-owner-review": "a PR that edits src/tac without owner review is "
    "refused by the ruleset",
    "push-required-checks": "a push without the required checks is refused",
    "forged-approval": "a forged approvals/*.json is ignored by the runner",
    "altered-receipt": "an altered receipt is recomputed and rejected by CI",
    "policy-tamper-guard": "a policy tamper in .agents/ from a worker is denied "
    "in the session",
    "policy-tamper-commit": "a policy tamper written through a shell is refused "
    "at commit by prek",
    "policy-tamper-boundary": "a policy tamper is refused at the boundary by "
    "CODEOWNERS",
    "hook-removed": "removing each required hook in a mutation run makes the "
    "suite fail",
    "spawn-without-token": "a native subagent spawn without a valid dispatch "
    "token is denied",
    "workflow-unregistered": "a Workflow call with an unregistered script is denied",
    "workflow-without-token": "a Workflow call without a token is denied",
    "workflow-reused-token": "a Workflow call with a reused token is denied",
    "workflow-from-builder": "a Workflow call from a builder is denied",
    "registered-run": "a registered run has every prompt hash matching a handoff "
    "template and every result valid against its contract, with the signed receipt",
    "classifier": "the auto-mode classifier marks the workflow's prompts as "
    "script-computed",
}

# A human transition with no command of its own: an item that runs out of time
# is still waiting, since a timeout is never consent (design section 7).
TIMEOUT = "timeout-without-consent"
# Commands of the human group that change or judge nothing.
HUMAN_READ_ONLY = frozenset({"schema"})


@dataclass(frozen=True, slots=True)
class Condition:
    id: str
    source: str


def _hook_conditions(config: Config) -> list[Condition]:
    found = []
    for name, spec in config.hooks.checks.items():
        for client in CLIENTS:
            matcher = spec.claude if client == "claude" else spec.codex
            if matcher:
                found.append(
                    Condition(f"hook.{name}.{client}.{spec.kind}", "hooks.toml")
                )
    return found


def _pipeline_conditions(root: Path) -> list[Condition]:
    problems: list[str] = []
    names = pipelines.enabled(root, problems)
    if problems:
        raise ProofError("; ".join(problems))
    found: list[Condition] = []
    for name in names:
        source = f"pipelines/{name}.toml"
        gates: list[str] = []
        for stage in pipelines.read_pipeline(root, name).stages:
            found.append(Condition(f"pipeline.{name}.stage.{stage.id}", source))
            chain = list(stage.gates)
            if stage.retry is not None and stage.retry.on_failure is not None:
                chain.append(stage.retry.on_failure)
            for gate in chain:
                # A gate's argv is `just <recipe> ...` or a script under src/tac.
                argv = [str(word) for word in gate.argv]
                label = argv[1] if len(argv) > 1 else argv[0]
                if label not in gates:
                    gates.append(label)
        found += [Condition(f"pipeline.{name}.gate.{g}", source) for g in gates]
    return found


def _handoff_conditions(root: Path) -> list[Condition]:
    found: list[Condition] = []
    for folder in HANDOFF_TEMPLATES:
        for path in sorted((root / folder).glob("*.j2")):
            stem = path.name.removesuffix(".j2").removesuffix(".md")
            found.append(Condition(f"handoff.{stem}", folder))
    for path in sorted((root / HANDOFF_CONTRACTS).glob("*.schema.json")):
        name = path.name.removesuffix(".schema.json")
        found.append(Condition(f"contract.{name}", HANDOFF_CONTRACTS))
    return found


def _human_conditions() -> list[Condition]:
    kinds = typing.get_args(Item.model_fields["kind"].annotation)
    found = [Condition(f"human.{kind}", "tac.human.Item") for kind in kinds]
    commands = sorted(set(human_group.commands) - HUMAN_READ_ONLY)
    found += [Condition(f"human.{c}", "tac human") for c in commands]
    found.append(Condition(f"human.{approve_command.name}", "tac approve"))
    found.append(Condition(f"human.{TIMEOUT}", "design section 7"))
    return found


def conditions(config: Config, root: Path) -> list[Condition]:
    """Every condition the harness claims, derived from the configuration and
    the source: a new check, stage, gate, template, contract, command or doctor
    check is a new condition, and the map must name a test for it."""
    found = _hook_conditions(config)
    found += _pipeline_conditions(root)
    found += _handoff_conditions(root)
    found += [
        Condition(f"memory.{c}", "tac memory") for c in sorted(memory_group.commands)
    ]
    found += _human_conditions()
    found += [Condition(f"layer.{layer}", "design section 9") for layer in LAYERS]
    found += [Condition(f"doctor.{c.name}", "tac.doctor.CHECKS") for c in CHECKS]
    found += [
        Condition(f"condition.{c}", "design section 18") for c in BUILD_CONDITIONS
    ]
    found += [Condition(f"bypass.{b}", "design section 9") for b in BYPASSES]
    seen: set[str] = set()
    unique = []
    for condition in found:
        if condition.id not in seen:
            seen.add(condition.id)
            unique.append(condition)
    return unique


# ---------------------------------------------------------------- live probes

Requirement = Literal[
    "runner-key",
    "keychain",
    "trust-claude",
    "trust-codex",
    "client-claude",
    "client-codex",
    "codex-resumed",
    "github-identity",
    "live-claude-session",
]


@dataclass(frozen=True, slots=True)
class Needs:
    """What makes a requirement hold: doctor checks that pass and owner items
    that are answered."""

    doctor: tuple[str, ...]
    items: tuple[str, ...]
    text: str


REQUIREMENTS: Mapping[str, Needs] = {
    "runner-key": Needs(("runner-pub",), ("S5",), "the runner's key, provisioned"),
    "keychain": Needs((), ("Q24",), "the runner's secrets in the login keychain"),
    "trust-claude": Needs(("trust-claude",), ("S3",), "Claude trusts this checkout"),
    "trust-codex": Needs(
        ("trust-codex", "hook-trust-codex"),
        ("S1", "S2"),
        "Codex trusts this checkout and its hooks",
    ),
    "client-claude": Needs(("client-claude",), (), "claude is installed"),
    "client-codex": Needs(("client-codex",), (), "codex is installed"),
    "codex-resumed": Needs((), (LIVE_STEP,), "Codex is paused by the owner"),
    "github-identity": Needs((), ("Q14",), "the machine account and the ruleset"),
    "live-claude-session": Needs((), (), "a live Claude session on the host"),
}


@dataclass(frozen=True, slots=True)
class LiveProbe:
    # The dotted name is the map's key and what a SKIP line prints; the slug is
    # what the runner is asked for and what probes.toml keys the probe by.
    name: str
    slug: str
    condition: str
    kind: str
    harness: str | None
    place: str
    session: str
    requires: tuple[str, ...]
    # The receipt it writes: kind probe, under the stage probe.<slug>.
    receipt: str = "probe"


# The C1 and C6 observations, by the slug their probe names carry.
C1_KINDS: Mapping[str, str] = {
    "missing-token": "spawn-missing-token",
    "altered-token": "spawn-altered-token",
    "reused-token": "spawn-reused-token",
    "delegation-off": "spawn-tool-absent",
    "runner-lost": "runner-lost",
    "hook-timeout": "hook-timeout",
}
C6_KINDS: Mapping[str, str] = {
    "keychain-token": "keychain-token-denied",
    "keychain-key": "keychain-key-denied",
}


# The runner's probe names are lowercase, hyphenated and at most 32 characters
# (tac.runner.Probe), so each slug abbreviates the parts of the dotted name.
SLUG_PARTS: Mapping[str, str] = {
    "missing-token": "tok-missing",
    "altered-token": "tok-altered",
    "reused-token": "tok-reused",
    "delegation-off": "deleg-off",
    "hook-timeout": "hook-tmout",
    "keychain-token": "kc-token",
    "keychain-key": "kc-key",
    "subdir": "sub",
    "worktree": "wt",
    "resumed": "resum",
    "headless": "hless",
}


def probe_slug(name: str) -> str:
    """The runner-facing name of a dotted probe name."""
    return "-".join(SLUG_PARTS.get(part, part) for part in name.lower().split("."))


def _client_requires(harness: str, *more: str) -> tuple[str, ...]:
    # Codex first: while it is paused nothing else of it is asked.
    first = ("codex-resumed",) if harness == "codex" else ()
    return (*first, "runner-key", *more, f"client-{harness}", f"trust-{harness}")


def _live_probes() -> tuple[LiveProbe, ...]:
    found: list[LiveProbe] = []
    for condition, kinds, more in (
        ("C1", C1_KINDS, ()),
        ("C6", C6_KINDS, ("keychain",)),
    ):
        for slug, kind in kinds.items():
            for harness in CLIENTS:
                for place in PLACES:
                    for session in SESSIONS:
                        name = f"{condition}.{slug}.{harness}.{place}.{session}"
                        found.append(
                            LiveProbe(
                                name,
                                probe_slug(name),
                                condition,
                                kind,
                                harness,
                                place,
                                session,
                                _client_requires(harness, *more),
                            )
                        )
    found += [
        LiveProbe(
            "bypass.ruleset-owner-review",
            probe_slug("bypass.ruleset-owner-review"),
            "bypass",
            "ruleset-owner-review",
            None,
            "root",
            "headless",
            ("github-identity",),
        ),
        LiveProbe(
            "bypass.push-required-checks",
            probe_slug("bypass.push-required-checks"),
            "bypass",
            "push-required-checks",
            None,
            "root",
            "headless",
            ("github-identity",),
        ),
        LiveProbe(
            "bypass.classifier.claude",
            probe_slug("bypass.classifier.claude"),
            "bypass",
            "classifier-script-computed",
            "claude",
            "root",
            "fresh",
            ("live-claude-session",),
        ),
    ]
    return tuple(found)


LIVE_PROBES: tuple[LiveProbe, ...] = _live_probes()


def probe_group(name: str) -> list[LiveProbe]:
    """The probes a map entry's `probe` names: one probe, or every probe under
    a dotted prefix such as C1 or C1.missing-token."""
    return [p for p in LIVE_PROBES if p.name == name or p.name.startswith(name + ".")]


@dataclass(frozen=True, slots=True)
class Facts:
    """What the requirements are judged on: doctor statuses by check name (a
    check not run is absent), the owner items answered, and how it was asked."""

    doctor: Mapping[str, str]
    answered: frozenset[str]
    live: bool = False
    harness: str | None = None


def unmet(requirement: str, facts: Facts) -> str | None:
    """Why a requirement does not hold, naming the step it waits on; None when
    it holds. Never read from the keychain or a token: only doctor's statuses
    and the owner's answered items."""
    needs = REQUIREMENTS[requirement]
    if requirement == "live-claude-session":
        if facts.live and facts.harness == "claude":
            return None
        return (
            f"{requirement} ({needs.text}): run `just dev-proof --live --harness "
            "claude` on the host"
        )
    why = [f"{item} pending" for item in needs.items if item not in facts.answered]
    if requirement == "codex-resumed" and why:
        why = [f"answer {LIVE_STEP} once Codex is resumed"]
    for check in needs.doctor:
        status = facts.doctor.get(check)
        if status != "pass":
            why.append(f"doctor {check}: {status or 'not checked'}")
    return f"{requirement} ({needs.text}): {', '.join(why)}" if why else None


@dataclass(frozen=True, slots=True)
class LiveResult:
    probe: str
    status: Literal["passed", "failed", "skipped"]
    reason: str
    slug: str

    def as_dict(self) -> dict[str, str]:
        return {
            "probe": self.probe,
            "slug": self.slug,
            "status": self.status,
            "reason": self.reason,
        }


ReceiptClient = Callable[[str, str], str]


def judge_live(
    probes: Sequence[LiveProbe],
    facts: Facts,
    receipt: ReceiptClient | None,
    trusted: Callable[[], Any] | None,
) -> list[LiveResult]:
    """Each probe skipped with its reason, or, with --live and every
    requirement met, run through `tac receipt client` and passed only when the
    signed receipt verifies and says it matched."""
    results: list[LiveResult] = []
    for probe in probes:
        reasons = []
        for requirement in probe.requires:
            why = unmet(requirement, facts)
            if why is not None:
                reasons.append(why)
                if requirement == "codex-resumed":
                    break
        harness = probe.harness or facts.harness or ""
        if facts.live and facts.harness and probe.harness not in (None, facts.harness):
            reasons.append(f"another harness: run with --harness {probe.harness}")
        try:
            observe(
                probe.name,
                probe.kind,
                probe.harness,
                probe.place,
                probe.session,
                live=facts.live,
                unmet=reasons,
            )
        except NotYetLive as exc:
            results.append(LiveResult(probe.name, "skipped", exc.reason, probe.slug))
            continue
        if receipt is None or trusted is None:
            results.append(
                LiveResult(probe.name, "skipped", "no receipt client", probe.slug)
            )
            continue
        results.append(_receipt_result(probe, harness, receipt, trusted))
    return results


def _receipt_result(
    probe: LiveProbe,
    harness: str,
    receipt: ReceiptClient,
    trusted: Callable[[], Any],
) -> LiveResult:
    def result(status: Literal["passed", "failed"], reason: str) -> LiveResult:
        return LiveResult(probe.name, status, reason, probe.slug)

    try:
        text = receipt(harness, probe.slug)
        signed = check_signature(parse(text), trusted())
    except (ReceiptError, ProofError, ValidationError, ValueError) as exc:
        return result("failed", f"no verified receipt: {exc}")
    if signed.kind != "probe" or signed.observed.get("probe") != probe.slug:
        return result("failed", "the receipt is for another probe")
    if signed.observed.get("matched") is not True:
        return result("failed", "the receipt says it did not match")
    return result("passed", f"receipt {signed.receipt_id}")


# ---------------------------------------------------------------- the map


class Entry(BaseModel):
    """One condition and how it is proved: tests that run offline, a live probe
    with what it requires, or both."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    condition: str = Field(min_length=1)
    why: str | None = None
    tests: Annotated[tuple[str, ...], BeforeValidator(as_tuple)] = ()
    probe: str | None = None
    requires: Annotated[tuple[Requirement, ...], BeforeValidator(as_tuple)] = ()
    live: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Entry:
        if not self.tests and self.probe is None:
            raise ValueError(f"{self.condition}: name tests, a probe, or both")
        if (self.probe is not None) != self.live:
            raise ValueError(f"{self.condition}: live = true goes with a probe")
        if self.probe is not None and not self.requires:
            raise ValueError(f"{self.condition}: a probe names what it requires")
        if self.probe is None and self.requires:
            raise ValueError(f"{self.condition}: requires belongs to a probe")
        return self


class CoverageMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1]
    entries: Annotated[tuple[Entry, ...], BeforeValidator(as_tuple)]


def load_map(path: Path) -> CoverageMap:
    if not path.is_file():
        raise ProofError(f"{path.name} is missing; every condition needs an entry")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return CoverageMap.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ProofError(f"{path.name} is not a coverage map: {exc}") from None


def test_files(ids: Iterable[str]) -> list[str]:
    return sorted({i.split("::", 1)[0] for i in ids})


def covers(entry_id: str, node: str) -> bool:
    """Whether a map test id takes a collected node: itself, one of its
    parameters, or, for a file, every test in it."""
    return node == entry_id or node.startswith((entry_id + "[", entry_id + "::"))


def matches(entry_id: str, collected: set[str]) -> bool:
    """A test id collects when pytest lists it, its parameters or its tests."""
    return any(covers(entry_id, c) for c in collected)


def map_problems(
    derived: Sequence[Condition], cmap: CoverageMap, collected: set[str]
) -> list[str]:
    """What makes the map unfit before a test runs: a condition without an
    entry, an entry for no condition, a duplicate, a test id that does not
    collect, a probe that is not defined or that asks for less than it needs."""
    problems: list[str] = []
    known = {c.id for c in derived}
    seen: set[str] = set()
    for entry in cmap.entries:
        if entry.condition in seen:
            problems.append(f"{entry.condition}: more than one entry")
        seen.add(entry.condition)
        if entry.condition not in known:
            problems.append(
                f"{entry.condition}: no such condition in the configuration"
            )
        for test in entry.tests:
            if not matches(test, collected):
                problems.append(f"{entry.condition}: {test} does not collect")
        if entry.probe is not None:
            group = probe_group(entry.probe)
            if not group:
                problems.append(f"{entry.condition}: no live probe named {entry.probe}")
            needed = {r for p in group for r in p.requires}
            if needed - set(entry.requires):
                problems.append(
                    f"{entry.condition}: {entry.probe} requires "
                    f"{', '.join(sorted(needed - set(entry.requires)))} as well"
                )
    problems += [f"{c.id}: no entry in {MAP_FILE}" for c in derived if c.id not in seen]
    return problems


# ---------------------------------------------------------------- running tests

Outcome = Literal["passed", "failed", "skipped", "missing"]


def node_id(root: Path, classname: str, name: str) -> str:
    """The pytest node id of a JUnit test case."""
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        path = "/".join(parts[:cut]) + ".py"
        if (root / path).is_file():
            return "::".join([path, *parts[cut:], name])
    return f"{classname}::{name}"


def junit_outcomes(root: Path, report: Path) -> dict[str, Outcome]:
    outcomes: dict[str, Outcome] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        node = node_id(root, case.get("classname", ""), case.get("name", ""))
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            outcomes[node] = "failed"
        elif "skipped" in tags:
            outcomes[node] = "skipped"
        else:
            outcomes[node] = "passed"
    return outcomes


def outcome_of(entry_id: str, outcomes: Mapping[str, Outcome]) -> Outcome:
    """A bare id of a parametrized test passes only when every parameter did.
    A skip proves nothing, so it counts against the condition."""
    found = [o for n, o in outcomes.items() if covers(entry_id, n)]
    if not found:
        return "missing"
    for worst in ("failed", "skipped"):
        if worst in found:
            return worst
    return "passed"


@dataclass
class Pytest:
    """pytest in this checkout's own environment, the candidate's."""

    root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "PY_COLORS": "0"}
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def collect(self, files: Sequence[str]) -> set[str]:
        if not files:
            return set()
        done = self._run(["--collect-only", "-q", *files])
        return {line.strip() for line in done.stdout.splitlines() if "::" in line}

    def run(self, ids: Sequence[str], junit: Path) -> tuple[int, str]:
        junit.parent.mkdir(parents=True, exist_ok=True)
        done = self._run(["-q", f"--junitxml={junit}", *ids])
        return done.returncode, done.stdout + done.stderr


# ---------------------------------------------------------------- the revision


def git_text(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise ProofError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def revision_of(root: Path, allow_dirty: bool) -> tuple[str, bool]:
    """HEAD and whether the tree is clean. A proof binds to a commit, so a dirty
    tree is refused unless the caller says the result binds to nothing."""
    revision = git_text(root, "rev-parse", "HEAD").strip()
    clean = git_text(root, "status", "--porcelain").strip() == ""
    if not clean and not allow_dirty:
        raise ProofError(
            "the tree has uncommitted changes, so the result would not name what "
            "was tested; commit first, or pass --allow-dirty"
        )
    return revision, clean


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


# ---------------------------------------------------------------- coverage


@dataclass(frozen=True, slots=True)
class CoverageResult:
    code: int
    lines: list[str]
    report: dict[str, Any] | None


def live_skips(entries: Iterable[Entry]) -> list[dict[str, Any]]:
    """One line per live probe the map names, with the conditions waiting on it."""
    found: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.probe is None:
            continue
        item = found.setdefault(
            entry.probe,
            {
                "probe": entry.probe,
                "conditions": [],
                "reason": f"live; requires {', '.join(entry.requires)}; runs "
                "through `just dev-proof --require-live` on the host",
            },
        )
        item["conditions"].append(entry.condition)
    return list(found.values())


def coverage(
    root: Path,
    *,
    allow_dirty: bool = False,
    map_path: Path | None = None,
    tests: Pytest | None = None,
    config: Config | None = None,
) -> CoverageResult:
    revision, clean = revision_of(root, allow_dirty)
    derived = conditions(config or load_config(root), root)
    cmap = load_map(map_path or root / MAP_FILE)
    runner = tests or Pytest(root)
    ids = sorted({t for e in cmap.entries for t in e.tests})
    collected = runner.collect(test_files(ids))
    problems = map_problems(derived, cmap, collected)
    if problems:
        return CoverageResult(1, [f"coverage: {p}" for p in problems], None)
    junit = root / OUT_DIR / "coverage-junit.xml"
    # A whole file takes its tests; naming one of them as well would run it twice.
    files = {i for i in ids if "::" not in i}
    code, output = runner.run(
        [i for i in ids if i.split("::")[0] not in files or i in files], junit
    )
    outcomes = junit_outcomes(root, junit) if junit.is_file() else {}
    records, failed = [], []
    for entry in cmap.entries:
        by_test = {t: outcome_of(t, outcomes) for t in entry.tests}
        ok = all(o == "passed" for o in by_test.values())
        status = "failed" if not ok else ("passed" if entry.tests else "live")
        if not ok:
            failed.append(entry.condition)
        records.append(
            {"condition": entry.condition, "status": status, "tests": by_test}
        )
    report: dict[str, Any] = {
        "revision": revision,
        "tree_clean": clean,
        "conditions": records,
        "passed": sum(r["status"] == "passed" for r in records),
        "failed": failed,
        "live_skipped": live_skips(cmap.entries),
    }
    write_json(root / COVERAGE_JSON, report)
    lines = [f"FAIL {c}" for c in failed]
    lines += [f"SKIP live: {s['probe']}: {s['reason']}" for s in report["live_skipped"]]
    lines.append(
        f"coverage: {report['passed']} of {len(records)} conditions proved by a "
        f"test that ran, {len(failed)} failed, {len(report['live_skipped'])} "
        f"live probes waiting; {COVERAGE_JSON} names {revision}"
    )
    if code != 0 and not failed:
        tail = output.strip().splitlines()[-15:]
        lines = [f"pytest exited {code}", *(f"  {t}" for t in tail), *lines]
    return CoverageResult(1 if failed or code else 0, lines, report)


# ---------------------------------------------------------------- the dev proof

Run = Callable[[Sequence[str], Path], tuple[int, str]]


def run_command(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """A command with the caller's environment less VIRTUAL_ENV, which would
    make uv in dev/ledger warn about the root project's environment."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, done.stdout + done.stderr


def ledger_steps() -> list[tuple[str, list[str]]]:
    recipe = ["just", "--justfile", f"{LEDGER}/justfile", "--working-directory", LEDGER]
    return [
        ("sync", ["uv", "sync", "--frozen", "--project", LEDGER]),
        ("verify", [*recipe, "verify"]),
        ("gate-schema-drift", [*recipe, "gate-schema-drift"]),
    ]


def doctor_facts(root: Path, codex_resumed: bool) -> dict[str, str]:
    """The doctor checks the requirements read, run as `tac doctor` runs them.
    Codex's are left out while it is paused, so its binary is never started."""
    wanted = {n for needs in REQUIREMENTS.values() for n in needs.doctor}
    if not codex_resumed:
        wanted = {n for n in wanted if "codex" not in n}
    checks = [c for c in CHECKS if c.name in wanted]
    return {r.name: str(r.status) for r in run_checks(root, checks)}


def doctor_from_json(path: Path) -> dict[str, str]:
    """The statuses `tac doctor --json` wrote."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {c["name"]: c["status"] for c in data["checks"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProofError(
            f"{path} is not the output of tac doctor --json: {exc}"
        ) from None


def answered_items(root: Path) -> frozenset[str]:
    """The owner items answered or approved. An item's own status is a hint for
    the page, never consent; here it only says whether a step was done."""
    try:
        items = load_items(human_paths(root))
    except HumanError as exc:
        raise ProofError(str(exc)) from None
    return frozenset(i.id for i in items if i.status in ("answered", "approved"))


def deployed_receipt_client(root: Path) -> ReceiptClient:
    def client(harness: str, probe: str) -> str:
        code, output = run_command(
            [
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "--project",
                ".agents",
                "tac",
                "receipt",
                "client",
                "--harness",
                harness,
                "--probe",
                probe,
            ],
            root,
        )
        if code != 0 and not output.strip().startswith("{"):
            raise ProofError(output.strip().splitlines()[-1] if output.strip() else "")
        return output

    return client


@dataclass
class DevTools:
    run: Run = run_command
    tests: Pytest | None = None
    doctor: Callable[[Path, bool], Mapping[str, str]] = doctor_facts
    receipt: Callable[[Path], ReceiptClient] = deployed_receipt_client


@dataclass(frozen=True, slots=True)
class DevResult:
    code: int
    lines: list[str]
    report: dict[str, Any]


def _render(root: Path, tools: DevTools) -> tuple[dict[str, Any], str]:
    code, output = tools.run(["just", "--summary"], root)
    recipes = output.split() if code == 0 else []
    if RENDER_RECIPE not in recipes:
        return {
            "status": "skipped",
            "reason": RENDER_SKIP,
        }, f"SKIP render: {RENDER_SKIP}"
    code, output = tools.run(["just", RENDER_RECIPE], root)
    status = "passed" if code == 0 else "failed"
    return {"status": status, "exit": code}, f"render: just {RENDER_RECIPE} exit {code}"


def _counts(outcomes: Mapping[str, Outcome], prefix: str) -> dict[str, int]:
    mine = [o for n, o in outcomes.items() if n.startswith(prefix + "::")]
    return {k: mine.count(k) for k in ("passed", "failed", "skipped")}


def dev(
    root: Path,
    *,
    require_live: bool = False,
    live: bool = False,
    harness: str | None = None,
    allow_dirty: bool = False,
    doctor_json: Path | None = None,
    tools: DevTools | None = None,
) -> DevResult:
    tools = tools or DevTools()
    if live and harness not in CLIENTS:
        raise ProofError("--live needs --harness claude or --harness codex")
    revision, clean = revision_of(root, allow_dirty)
    lines: list[str] = []
    ok = True

    ledger: dict[str, Any] = {}
    for name, argv in ledger_steps():
        code, output = tools.run(argv, root)
        ledger[name] = {"argv": argv, "exit": code}
        lines.append(f"ledger {name}: exit {code}")
        if code != 0:
            ok = False
            lines += [f"  {line}" for line in output.strip().splitlines()[-15:]]
            break
    render, line = _render(root, tools)
    lines.append(line)
    ok = ok and render["status"] != "failed"

    runner = tools.tests or Pytest(root)
    junit = root / OUT_DIR / "dev-proof-junit.xml"
    code, output = runner.run(list(PROOF_TESTS), junit)
    outcomes = junit_outcomes(root, junit) if junit.is_file() else {}
    bypass = _counts(outcomes, PROOF_TESTS[0])
    mutation = _counts(outcomes, PROOF_TESTS[1])
    for label, counts in (("bypass", bypass), ("mutation", mutation)):
        lines.append(
            f"{label}: {counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['skipped']} skipped"
        )
        if counts["failed"] or counts["skipped"] or not counts["passed"]:
            ok = False
    if code != 0:
        ok = False
        lines += [f"  {line}" for line in output.strip().splitlines()[-15:]]

    answered = answered_items(root)
    doctor = (
        doctor_from_json(doctor_json)
        if doctor_json is not None
        else tools.doctor(root, LIVE_STEP in answered)
    )
    facts = Facts(doctor, answered, live, harness)
    results = judge_live(
        LIVE_PROBES,
        facts,
        tools.receipt(root) if live else None,
        (lambda: trusted_key_from_revision(root, "HEAD")) if live else None,
    )
    for result in results:
        if result.status == "skipped":
            lines.append(f"SKIP live: {result.probe}: {result.reason}")
        else:
            lines.append(
                f"{result.status.upper()} live: {result.probe}: {result.reason}"
            )
    tally = {
        s: sum(r.status == s for r in results) for s in ("passed", "failed", "skipped")
    }
    lines.append(
        f"live: {tally['passed']} passed, {tally['failed']} failed, "
        f"{tally['skipped']} skipped"
    )
    if tally["failed"]:
        ok = False
    if require_live and tally["skipped"]:
        ok = False
        lines.append(f"--require-live: {tally['skipped']} live probes were skipped")

    report: dict[str, Any] = {
        "revision": revision,
        "tree_clean": clean,
        "ledger": ledger,
        "render": render,
        "bypass": bypass,
        "mutation": mutation,
        "live": [r.as_dict() for r in results],
    }
    write_json(root / DEV_JSON, report)
    lines.append(f"{DEV_JSON} names {revision}")
    return DevResult(0 if ok else 1, lines, report)
