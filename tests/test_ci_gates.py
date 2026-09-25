"""CI's gates run only the base revision's code, against a base taken by commit.

On pull_request the checkout is the candidate's, so a gate script run from it
could be edited to exit 0 without judging anything, and a candidate test could
move a local ref so that a later step takes its scripts from the wrong commit.
So the workflow has two jobs: `verify` runs the candidate's code, and `gates`
runs none of it, takes the base and the head from the event payload by commit
id, and runs only gate scripts taken from that base. These tests hold the
`gates` job's steps to that, and run its own step text in a throwaway
repository whose candidate rewrote every gate script and moved the base ref.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tac.github import REQUIRED_CHECKS
from tests._gitrepo import REPO, commit_all, git, write

CI = REPO / ".github/workflows/ci.yml"
GATE_JOB = "gates"
FETCH = "Take the gate scripts from the base revision"
RUN_GATE = 'bash "$RUNNER_TEMP/gates/'
# The gate scripts CI must take from the base: every ci_*.sh, and the scan.
GATE_SCRIPTS = frozenset(
    {p.name for p in (REPO / "scripts").glob("ci_*.sh")} | {"private_scan.sh"}
)
# The two commits of a pull request, as the event payload names them.
BASE_SHA = "${{ github.event.pull_request.base.sha }}"
HEAD_SHA = "${{ github.event.pull_request.head.sha }}"
# A shell running a script from the checkout: `bash scripts/x.sh`, `./scripts/x`.
FROM_CHECKOUT = re.compile(
    r'(?:^|[\s;&|(])(?:(?:bash|sh|source|\.)\s+"?(?:\./)?|\./)scripts/', re.M
)
CANDIDATE = "candidate rewrote"
# The actions the gate job may use: the checkout that fetches the candidate as
# data, and uv to build the base's checker.
GATE_ACTIONS = frozenset({"actions/checkout", "astral-sh/setup-uv"})
# The programs a gate job step may start. git reads the candidate as data; bash
# runs only a script taken from the base, checked below.
GATE_COMMANDS = frozenset({"git", "mkdir", "echo", "cp", "exit", "bash"})
# Shell words that open or close a compound command, not programs.
KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!"}
    | {"{", "}"}
)
STEP_KEYS = frozenset({"name", "uses", "with", "env", "run"})


def workflow() -> dict:
    return yaml.safe_load(CI.read_text("utf-8"))


def jobs() -> dict[str, dict]:
    return workflow()["jobs"]


def fetch_step(job: dict) -> dict | None:
    return next((s for s in job["steps"] if s.get("name") == FETCH), None)


def gate_steps(job: dict) -> list[dict]:
    return [s for s in job["steps"] if RUN_GATE in s.get("run", "")]


def gate_name(step: dict) -> str:
    found = re.search(r'\$RUNNER_TEMP/gates/([\w.-]+)"', step["run"])
    assert found, step["run"]
    return found.group(1)


def fetched(job: dict) -> list[str]:
    step = fetch_step(job)
    return step["env"]["GATES"].split() if step else []


# ---- reading a run step's shell as the commands it starts


def _substitution(script: str, start: int) -> int:
    """The index just past the `)` that closes the `$(` ending at `start`."""
    depth, i = 1, start
    while depth:
        assert i < len(script), f"unclosed $( in {script!r}"
        depth += {"(": 1, ")": -1}.get(script[i], 0)
        i += 1
    return i


def commands(script: str) -> list[list[str]]:
    """Every simple command of a shell script as its words, quotes removed, a
    command substitution's own commands included wherever it stands. Enough
    shell for the workflow's steps; anything it cannot read fails the test."""
    out: list[list[str]] = []
    words: list[str] = []
    word: str | None = None
    quote = ""
    i = 0

    def end_word() -> None:
        nonlocal word
        if word is not None:
            words.append(word)
            word = None

    def end_command() -> None:
        nonlocal words
        end_word()
        if words:
            out.append(words)
        words = []

    while i < len(script):
        char = script[i]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word = (word or "") + char
            i += 1
            continue
        if script.startswith("$(", i):
            close = _substitution(script, i + 2)
            out.extend(commands(script[i + 2 : close - 1]))
            word = (word or "") + script[i:close]
            i = close
            continue
        if char == "\\":
            word = (word or "") + script[i + 1 : i + 2]
            i += 2
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            else:
                word = (word or "") + char
            i += 1
            continue
        if char in "'\"":
            quote = char
            word = word or ""
        elif char == "`":
            raise AssertionError(f"backticks are not read here: {script!r}")
        elif char == "#" and word is None:
            while i < len(script) and script[i] != "\n":
                i += 1
            continue
        elif char in " \t":
            end_word()
        elif char in "\n;&|()":
            end_command()
        else:
            word = (word or "") + char
        i += 1
    assert not quote, f"unclosed quote in {script!r}"
    end_command()
    return out


def programs(script: str) -> list[list[str]]:
    """Each command less its leading keywords and variable assignments: the
    program it starts and that program's arguments. A `for` line starts none."""
    started = []
    for words in commands(script):
        while words and (
            words[0] in KEYWORDS or re.fullmatch(r"[A-Za-z_]\w*=.*", words[0])
        ):
            words = words[1:]
        if words and words[0] not in {"for", "in"}:
            started.append(words)
    return started


def test_commands_reads_substitutions_quotes_and_separators() -> None:
    script = (
        'x="$(uv sync --frozen)" && echo "a; b" || { true; }\n'
        "if ! git cat-file -e \"$B:p\" 2>/dev/null; then echo 'c|d'; fi\n"
        "# a comment names pytest\n"
        'for n in $G; do bash "$RUNNER_TEMP/gates/$n"; done\n'
    )
    assert [words[0] for words in programs(script)] == [
        "uv",
        "echo",
        "true",
        "git",
        "echo",
        "bash",
    ]


# ---- the structure of the workflow


def test_the_ruleset_requires_both_jobs() -> None:
    assert set(REQUIRED_CHECKS) == set(jobs()) == {"verify", GATE_JOB}


def test_the_gate_job_runs_no_candidate_code() -> None:
    assert "defaults" not in workflow()
    job = jobs()[GATE_JOB]
    # A job can reach code by other roads than its steps.
    assert set(job) <= {"runs-on", "timeout-minutes", "steps"}, set(job)
    mise_uv = tomllib.loads((REPO / "mise.toml").read_text("utf-8"))["tools"]["uv"]
    for step in job["steps"]:
        # No shell: a custom one could start anything; no working-directory.
        assert set(step) <= STEP_KEYS, step
        if "uses" in step:
            action = step["uses"].split("@", 1)[0]
            assert action in GATE_ACTIONS, step["uses"]
            if action == "astral-sh/setup-uv":
                # A named uv and no cache: nothing of the candidate's picks the
                # uv or the wheels that build the base's checker.
                assert step["with"] == {"version": mise_uv, "enable-cache": False}
            continue
        for words in programs(step["run"]):
            assert words[0] in GATE_COMMANDS, (step["name"], words)
            if words[0] == "bash":
                # bash only ever runs a script the fetch step took from the base.
                assert words[1].startswith("$RUNNER_TEMP/gates/"), words


def test_the_gate_job_takes_base_and_head_from_the_event_by_commit() -> None:
    job = jobs()[GATE_JOB]
    # No ref name reaches a gate as the base: a ref in the clone can be moved.
    text = yaml.safe_dump(job)
    for mutable in ("github.base_ref", "origin/", "refs/", "BASE_REF"):
        assert mutable not in text, mutable
    for step in job["steps"]:
        env = step.get("env", {})
        assert env.get("BASE_SHA", BASE_SHA) == BASE_SHA, step["name"]
        assert env.get("HEAD_SHA", HEAD_SHA) == HEAD_SHA, step["name"]
        run = step.get("run", "")
        # A value reaches the shell as a variable, never as script text.
        assert "${{" not in run, step["name"]
        if "$BASE_SHA" in run:
            assert "BASE_SHA" in env, step["name"]
    for step in gate_steps(job):
        # Every gate is told the base by its commit id.
        assert '"$BASE_SHA' in step["run"].split("gates/", 1)[1], step["run"]


def test_every_gate_script_runs_from_the_base_copy_in_the_gate_job() -> None:
    wired: set[str] = set()
    for name, job in jobs().items():
        steps = job["steps"]
        for step in steps:
            # Nothing runs a script from the candidate's checkout.
            assert not FROM_CHECKOUT.search(step.get("run", "")), (name, step)
        for step in gate_steps(job):
            assert name == GATE_JOB, (name, step["name"])
            gate = gate_name(step)
            assert gate in fetched(job), (name, gate)
            # Taken before it runs, from a checkout that has the base commit.
            assert steps.index(fetch_step(job)) < steps.index(step), (name, gate)
            assert steps[0]["with"]["fetch-depth"] == 0, name
            wired.add(gate)
    assert wired == GATE_SCRIPTS


def test_actionlint_accepts_the_workflow() -> None:
    actionlint = Path(sys.executable).with_name("actionlint")
    done = subprocess.run(
        [str(actionlint), str(CI)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


# ---- running the gate job's own steps against a pull request fixture


def env_of(step: dict, values: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in step.get("env", {}).items():
        for expression, stands_for in values.items():
            value = value.replace(expression, stands_for)
        assert "${{" not in value, (key, value)
        out[key] = value
    return out


def run_step(
    root: Path, step: dict, runner_temp: Path, values: dict[str, str]
) -> tuple[int, str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environ |= {"RUNNER_TEMP": str(runner_temp), "GITHUB_REPOSITORY": "example/demo"}
    environ |= env_of(step, values)
    # The runner's default shell for a run step on Linux.
    done = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def stub(name: str, says: str) -> str:
    return f"#!/usr/bin/env bash\nprintf '%s {name} %s\\n' {says!r} \"$*\"\n"


def rewritten(name: str) -> str:
    return f"#!/usr/bin/env bash\necho '{CANDIDATE} {name}'\nexit 0\n"


def pull_request(
    tmp_path: Path, gates: list[str], base_has: bool
) -> tuple[Path, dict[str, str]]:
    """A base on main and a candidate that rewrote every gate script, with the
    values the event payload would carry for it."""
    root = tmp_path / "demo"
    root.mkdir()
    git(root, "init", "-q")
    for name in gates:
        if base_has:
            write(root, f"scripts/{name}", stub(name, "base"))
    base = commit_all(root, "base")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    git(root, "checkout", "-q", "-b", "candidate")
    for name in gates:
        write(root, f"scripts/{name}", rewritten(name))
    head = commit_all(root, "candidate rewrites its gate scripts to pass")
    values = {
        BASE_SHA: base,
        HEAD_SHA: head,
        "${{ github.base_ref }}": "main",
        "${{ github.head_ref }}": "candidate",
    }
    return root, values


def run_job(
    tmp_path: Path, job: dict, base_has: bool, move_refs: bool = False
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    gates = fetched(job)
    root, values = pull_request(tmp_path, gates, base_has)
    if move_refs:
        # What a candidate test could do on a shared runner: point every ref
        # named after the base at the candidate's own commit.
        head = values[HEAD_SHA]
        for ref in ("refs/heads/main", "refs/remotes/origin/main"):
            git(root, "update-ref", ref, head)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    code, output = run_step(root, fetch_step(job) or {}, runner_temp, values)
    assert code == 0, output
    results = [("fetch", output)]
    for step in gate_steps(job):
        code, output = run_step(root, step, runner_temp, values)
        assert code == 0, output
        results.append((gate_name(step), output))
    return values, results


@pytest.mark.parametrize("move_refs", [False, True], ids=["refs", "moved-refs"])
def test_a_gate_script_the_candidate_rewrote_is_not_the_one_ci_runs(
    tmp_path: Path, move_refs: bool
) -> None:
    job = jobs()[GATE_JOB]
    values, results = run_job(tmp_path, job, base_has=True, move_refs=move_refs)
    assert "::warning::" not in results[0][1], results[0][1]
    assert {gate for gate, _ in results[1:]} == GATE_SCRIPTS
    for gate, output in results[1:]:
        # The base's copy ran, told the base by its commit id.
        assert f"base {gate} " in output, output
        assert values[BASE_SHA] in output, output
        assert CANDIDATE not in output, output


def test_a_base_that_predates_a_gate_runs_the_candidates_copy_loudly(
    tmp_path: Path,
) -> None:
    job = jobs()[GATE_JOB]
    _values, results = run_job(tmp_path, job, base_has=False)
    for gate in fetched(job):
        assert f"has no scripts/{gate}; the candidate's copy runs" in results[0][1]
    for gate, output in results[1:]:
        assert f"{CANDIDATE} {gate}" in output, output


def test_a_base_commit_missing_from_the_clone_stops_the_gates(tmp_path: Path) -> None:
    job = jobs()[GATE_JOB]
    root, values = pull_request(tmp_path, fetched(job), base_has=True)
    values[BASE_SHA] = "0" * 40
    code, output = run_step(root, fetch_step(job) or {}, tmp_path, values)
    assert code != 0
    assert "is not in this clone" in output
