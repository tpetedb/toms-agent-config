"""The macOS confinement claims rest on a host run, since CI's Linux runner skips
every Seatbelt test: docs/evidence/m0-host-run.md has to name the exact command
and a result with nothing skipped."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs" / "evidence" / "m0-host-run.md"
COMMAND = "uv run --frozen pytest -q -rs tests/test_runner_skeleton.py"


def test_the_host_run_names_its_command_and_a_clean_result() -> None:
    text = EVIDENCE.read_text(encoding="utf-8")
    assert f"```sh\n{COMMAND}\n```" in text
    found = re.search(r"Result: `(\d+) passed`, none skipped\.", text)
    assert found is not None, "no pass count recorded for the host run"
    assert int(found.group(1)) > 0


def test_ci_points_at_the_host_run() -> None:
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "docs/evidence/m0-host-run.md" in workflow
