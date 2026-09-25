"""CI runs every gate script from the base revision, never the candidate's copy.

On pull_request the checkout is the candidate's, so a gate script run from it
could be edited to exit 0 without judging anything. These tests run the
workflow's own step text in a throwaway repository whose candidate rewrote every
gate script, and prove the base's copy is the one that ran.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._gitrepo import REPO, commit_all, git, write

CI = REPO / ".github/workflows/ci.yml"
FETCH = "Take the gate scripts from the base revision"
RUN_GATE = 'bash "$RUNNER_TEMP/gates/'
# The gate scripts CI must take from the base: every ci_*.sh, and the scan.
GATE_SCRIPTS = frozenset(
    {p.name for p in (REPO / "scripts").glob("ci_*.sh")} | {"private_scan.sh"}
)
# What the workflow's expressions stand for in the fixture.
EXPRESSIONS = {
    "${{ github.base_ref }}": "main",
    "${{ github.head_ref }}": "candidate",
    "${{ github.event.pull_request.head.sha }}": "candidate",
}
# A shell running a script from the checkout: `bash scripts/x.sh`, `./scripts/x`.
FROM_CHECKOUT = re.compile(
    r'(?:^|[\s;&|(])(?:(?:bash|sh|source|\.)\s+"?(?:\./)?|\./)scripts/', re.M
)
CANDIDATE = "candidate rewrote"


def jobs() -> dict[str, dict]:
    return yaml.safe_load(CI.read_text("utf-8"))["jobs"]


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


def test_every_gate_script_runs_from_the_base_copy_in_its_job() -> None:
    wired: set[str] = set()
    for name, job in jobs().items():
        steps = job["steps"]
        for step in steps:
            # Nothing runs a script from the candidate's checkout.
            assert not FROM_CHECKOUT.search(step.get("run", "")), (name, step)
        for step in gate_steps(job):
            gate = gate_name(step)
            assert gate in fetched(job), (name, gate)
            # Taken before it runs, from a checkout that has the base branch.
            assert steps.index(fetch_step(job)) < steps.index(step), (name, gate)
            assert steps[0]["with"]["fetch-depth"] == 0, name
            wired.add(gate)
    assert wired == GATE_SCRIPTS


def env_of(step: dict) -> dict[str, str]:
    out = {}
    for key, value in step.get("env", {}).items():
        for expression, stands_for in EXPRESSIONS.items():
            value = value.replace(expression, stands_for)
        assert "${{" not in value, (key, value)
        out[key] = value
    return out


def run_step(root: Path, step: dict, runner_temp: Path) -> tuple[int, str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environ |= {"RUNNER_TEMP": str(runner_temp), "GITHUB_REPOSITORY": "example/demo"}
    environ |= env_of(step)
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


def pull_request(tmp_path: Path, gates: list[str], base_has: bool) -> Path:
    """A base on origin/main and a candidate that rewrote every gate script."""
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
    commit_all(root, "candidate rewrites its gate scripts to pass")
    return root


def run_job(tmp_path: Path, job: dict, base_has: bool) -> list[tuple[str, str]]:
    gates = fetched(job)
    root = pull_request(tmp_path, gates, base_has)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    code, output = run_step(root, fetch_step(job) or {}, runner_temp)
    assert code == 0, output
    results = [("fetch", output)]
    for step in gate_steps(job):
        code, output = run_step(root, step, runner_temp)
        assert code == 0, output
        results.append((gate_name(step), output))
    return results


JOBS = sorted(name for name, job in jobs().items() if fetch_step(job))


@pytest.mark.parametrize("job", JOBS)
def test_a_gate_script_the_candidate_rewrote_is_not_the_one_ci_runs(
    tmp_path: Path, job: str
) -> None:
    results = run_job(tmp_path, jobs()[job], base_has=True)
    assert "::warning::" not in results[0][1], results[0][1]
    for gate, output in results[1:]:
        # The base's copy ran, with the base ref wherever the gate takes one.
        assert f"base {gate} " in output, output
        assert "origin/main" in output or gate == "private_scan.sh", output
        assert CANDIDATE not in output, output


@pytest.mark.parametrize("job", JOBS)
def test_a_base_that_predates_a_gate_runs_the_candidates_copy_loudly(
    tmp_path: Path, job: str
) -> None:
    results = run_job(tmp_path, jobs()[job], base_has=False)
    for gate in fetched(jobs()[job]):
        assert f"has no scripts/{gate}; the candidate's copy runs" in results[0][1]
    for gate, output in results[1:]:
        assert f"{CANDIDATE} {gate}" in output, output
