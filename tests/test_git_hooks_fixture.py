"""The git hooks of hooks/git/prek.toml, installed by prek and exercised for real
(DESIGN 9 layer 3a, M2 `tac-hooks`, the prek clause of build condition C5).

Each test builds a throwaway repository under its temporary folder from this
project's inputs, installs the three hook types there with prek, and commits
and pushes to a local bare remote. PREK_HOME sits in the temporary folder, git
reads no global or system configuration, every hook is a local system command,
uv runs offline against the test's own environment, and every proxy points at
a closed port, so nothing is fetched. The shared `.git/hooks` of this checkout
is never touched, and the tests prove it.

The message rules themselves are unit-tested at the bottom, against
`tac.commits` directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tac.commits import AGENT_MARKERS, Rules, check_message, cleaned, from_agent
from tac.githooks import HOOKS_FILE
from tac.sync import sync
from tests._syncproject import REPO, copy_project

# Plain git: the fixture's hooks are the point, so no hooksPath override.
GIT = shutil.which("git") or "git"
PREK = Path(sys.prefix) / "bin" / "prek"
UV = os.environ.get("UV") or shutil.which("uv")
# What the throwaway project needs besides the sync inputs: the toolchain
# projects uv runs the hooks from, the scan, and what git keeps out of it.
EXTRA = (
    "pyproject.toml",
    "uv.lock",
    ".agents/pyproject.toml",
    ".agents/uv.lock",
    "scripts/private_scan.sh",
    ".gitignore",
)
APP = 'def greet(name: str) -> str:\n    return f"hello {name}"\n'
PASSING = (
    "from app import greet\n\n\n"
    'def test_greet() -> None:\n    assert greet("x") == "hello x"\n'
)
# A second, formatted change to stage.
MORE = APP + '\n\nNAME = "x"\n'
FAILING = "def test_greet() -> None:\n    assert 1 == 2\n"
GOOD = "Add a farewell so the fixture holds a second change"
TRAILER = "Co-Authored-By: A Model <model@example.com>"
CLOSED = "http://127.0.0.1:9"

pytestmark = pytest.mark.skipif(
    not PREK.is_file() or UV is None, reason="prek or uv is not installed"
)


@dataclass(frozen=True)
class Repo:
    root: Path
    remote: Path
    env: Mapping[str, str]

    def run(
        self, *argv: str, extra: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=self.root,
            env={**self.env, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def git(
        self, *args: str, extra: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run(GIT, *args, extra=extra)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def remote_head(self) -> str:
        done = subprocess.run(
            [GIT, "--git-dir", str(self.remote), "rev-parse", "--verify", "-q", "main"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        return done.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def fixture_env(tmp: Path) -> dict[str, str]:
    """Nothing from the session running the tests: no agent markers, no user
    git or uv configuration, prek's home in the temporary folder, uv offline
    against this test's own environment, and no way out to the network."""
    assert UV is not None
    for name in ("home", "prek-home", "uv-cache"):
        (tmp / name).mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join(
            [str(Path(UV).parent), str(PREK.parent), "/usr/bin", "/bin"]
        ),
        "HOME": str(tmp / "home"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": "en_US.UTF-8",
        "PREK_HOME": str(tmp / "prek-home"),
        "PREK_COLOR": "never",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "UV_OFFLINE": "1",
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(tmp / "uv-cache"),
        "HTTP_PROXY": CLOSED,
        "HTTPS_PROXY": CLOSED,
        "ALL_PROXY": CLOSED,
        "http_proxy": CLOSED,
        "https_proxy": CLOSED,
    }


def shared_hooks() -> dict[str, float]:
    """This checkout's shared hooks folder, by name and modification time."""
    common = subprocess.run(
        [
            GIT,
            "-C",
            str(REPO),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    folder = Path(common) / "hooks"
    return (
        {p.name: p.stat().st_mtime for p in folder.iterdir()} if folder.is_dir() else {}
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    before = shared_hooks()
    root = copy_project(tmp_path / "project")
    for rel in EXTRA:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_fast.py").write_text(PASSING, encoding="utf-8")
    sync(root, links=False)
    remote = tmp_path / "remote.git"
    fixture = Repo(root, remote, fixture_env(tmp_path))
    for argv in (
        (GIT, "init", "-q", "--bare", "-b", "main", str(remote)),
        (GIT, "init", "-q", "-b", "main", str(root)),
        (GIT, "-C", str(root), "config", "user.name", "Fixture Person"),
        (GIT, "-C", str(root), "config", "user.email", "person@example.com"),
        (GIT, "-C", str(root), "remote", "add", "origin", str(remote)),
        (GIT, "-C", str(root), "add", "-A"),
        (GIT, "-C", str(root), "commit", "-q", "-m", "Start the fixture project"),
    ):
        done = fixture.run(*argv)
        assert done.returncode == 0, done.stderr
    installed = fixture.run(str(PREK), "install", "--config", HOOKS_FILE)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    hooks = root / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "pre-push"):
        shim = (hooks / name).read_text(encoding="utf-8")
        assert f'--config="{HOOKS_FILE}"' in shim, name
    assert shared_hooks() == before, "prek wrote into this checkout's .git/hooks"
    return fixture


def commit(repo: Repo, message: str, **env: str) -> subprocess.CompletedProcess[str]:
    return repo.git("commit", "-m", message, extra=env)


def said(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr


# ---------------------------------------------------------------- the hooks


def test_a_clean_change_commits_and_pushes(repo: Repo) -> None:
    repo.write(
        "app.py", APP + '\n\ndef part(name: str) -> str:\n    return f"bye {name}"\n'
    )
    assert repo.git("add", "app.py").returncode == 0
    done = commit(repo, GOOD)
    assert done.returncode == 0, said(done)
    for hook in ("ruff format --check", "ruff check", "tac check --staged"):
        assert hook in said(done), hook
    assert "tac check --commit-msg" in said(done)
    pushed = repo.git("push", "-q", "origin", "main")
    assert pushed.returncode == 0, said(pushed)
    assert "fast tests" in said(pushed)
    assert repo.remote_head() == repo.head()


@pytest.mark.parametrize(
    ("message", "why"),
    [
        (GOOD + "\n\nBecause a body is prose, not a trailer.", "only trailers"),
        ("Add " + "a very long subject " * 5, "over the 72"),
        ("Add a farewell — and say why", "em dash"),
        (GOOD + "\nNo blank line before this.", "leave one blank"),
    ],
)
def test_an_invalid_commit_message_is_refused(
    repo: Repo, message: str, why: str
) -> None:
    before = repo.head()
    repo.write("app.py", MORE)
    repo.git("add", "app.py")
    done = commit(repo, message)
    assert done.returncode != 0, said(done)
    assert "commit message:" in said(done) and why in said(done)
    assert repo.head() == before


def test_an_agent_commit_needs_the_required_trailer(repo: Repo) -> None:
    repo.write("app.py", MORE)
    repo.git("add", "app.py")
    before = repo.head()
    refused = commit(repo, GOOD, CLAUDECODE="1")
    assert refused.returncode != 0
    assert "carries a Co-Authored-By: trailer" in said(refused)
    assert repo.head() == before
    done = commit(repo, f"{GOOD}\n\n{TRAILER}", CLAUDECODE="1")
    assert done.returncode == 0, said(done)


def test_a_push_with_a_failing_fast_test_is_refused(repo: Repo) -> None:
    repo.write("tests/test_fast.py", FAILING)
    repo.git("add", "tests/test_fast.py")
    done = commit(repo, "Break the fast test so the push is refused")
    assert done.returncode == 0, said(done)
    pushed = repo.git("push", "-q", "origin", "main")
    assert pushed.returncode != 0, said(pushed)
    assert "fast tests" in said(pushed) and "1 failed" in said(pushed)
    assert repo.remote_head() == ""


def test_a_staged_hand_edit_of_a_generated_file_is_refused(repo: Repo) -> None:
    before = repo.head()
    repo.write("AGENTS.md", (repo.root / "AGENTS.md").read_text() + "\nA hand edit.\n")
    repo.git("add", "AGENTS.md")
    done = commit(repo, "Edit the brief by hand, which the check refuses")
    assert done.returncode != 0
    assert "AGENTS.md: edited by hand" in said(done)
    assert repo.head() == before


# ---------------------------------------------------------------- the message rules

HOUSE = Rules("what-and-why", 72, ("Co-Authored-By",), em_dashes=False)


@pytest.mark.parametrize(
    "message",
    [
        GOOD,
        GOOD + "\n",
        f"{GOOD}\n\n{TRAILER}",
        f"{GOOD}\n\n{TRAILER}\nSigned-off-by: A Person <p@example.com>",
        f"{GOOD}\n# a comment git drops\n",
        f"{GOOD}\n\n# ------------------------ >8 ------------------------\nthe diff",
    ],
)
def test_a_house_message_passes(message: str) -> None:
    assert check_message(message, HOUSE, agent=False) == []
    if TRAILER in message:
        assert check_message(message, HOUSE, agent=True) == []


def test_the_waived_parts_are_not_held() -> None:
    lifted = Rules("what-and-why", None, (), em_dashes=True)
    assert check_message("x " * 60 + "—", lifted, agent=True) == []


def test_conventional_commits_take_a_type_and_a_body() -> None:
    rules = Rules("conventional-1.0.0", 72, (), em_dashes=False)
    assert (
        check_message(
            "feat(hooks): wire the guard\n\nWhy it matters.", rules, agent=False
        )
        == []
    )
    assert "type(scope)" in check_message("Wire the guard", rules, agent=False)[0]


def test_an_empty_message_is_refused() -> None:
    assert check_message("# only a comment\n\n", HOUSE, agent=False) == [
        "the commit message is empty"
    ]
    assert cleaned("\n\nsubject\n\n") == ["subject"]


def test_an_agent_session_is_told_by_its_client_markers() -> None:
    assert not from_agent({"PATH": "/usr/bin"})
    for name in AGENT_MARKERS:
        assert from_agent({name: "1"}), name
    assert not from_agent({"CLAUDECODE": ""})
