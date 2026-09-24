from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from tac import __version__
from tac.cli import cli
from tac.doctor import CHECKS


def test_version_prints_the_installed_version() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"tac, version {__version__}"
    assert __version__ != "0+unknown"


def test_doctor_names_every_check_and_fails_on_an_empty_root(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["doctor", "--root", str(tmp_path)])
    assert result.exit_code == 1
    for check in CHECKS:
        assert check.name in result.output
    assert "checks pass" in result.output


def test_doctor_json_reports_each_check(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["doctor", "--json", "--root", str(tmp_path)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert [c["name"] for c in payload["checks"]] == [c.name for c in CHECKS]
    assert {c["status"] for c in payload["checks"]} <= {"pass", "fail", "unknown"}
    # The report may be shared; it names the folder, never an absolute path.
    assert payload["root"] == tmp_path.name
