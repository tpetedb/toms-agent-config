from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture(autouse=True)
def no_live_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests speak to GitHub only through recorded fixtures, never live."""

    # pytest.fail is a BaseException, so a check's own except cannot swallow it.
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a test tried to reach GitHub live")

    monkeypatch.setattr("tac.github.GhTransport.call", refuse)
    monkeypatch.setattr("tac.github.AnonymousTransport.call", refuse)
    # The doctor's session check reads as though gh were logged out; tests of
    # the gh call itself use a fake gh executable.
    monkeypatch.setattr("tac.doctor.gh_auth_status", lambda _gh, _env: None)


@pytest.fixture(autouse=True)
def no_owner_client_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trust probes read an empty home, never the owner's client configs."""
    home = tmp_path_factory.mktemp("client-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
