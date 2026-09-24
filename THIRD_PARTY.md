# Third-party and carried-over material

What in this repository did not start here, where it came from and under which terms. docs/DESIGN.md section 16 is the full reuse list; a line moves here when its material lands. TAC's own licence is the owner's decision (TODO.HUMAN.md, Q9).

## Third-party

None yet. No third-party code or text is vendored in this repository. The development tools in `pyproject.toml` are installed from PyPI at build time under their own licences and are not redistributed here.

## Carried over from the owner's own public projects

| What | Source | Licence | Where it lives here |
|---|---|---|---|
| The work-order tool, its format, templates, README and tests | [vibe-map](https://github.com/tpetedb/vibe-map) `tools/work.py`, `work/`, `tests/test_work.py` | MIT, Copyright (c) 2026 Tom Peters | `src/tac/work.py`, `src/tac/work_cli.py`, `work/`, `tests/test_work.py` |
| The work-order decision record | [vibe-map ADR 0016](https://github.com/tpetedb/vibe-map/blob/main/docs/adr/0016-work-orders.md) | MIT, Copyright (c) 2026 Tom Peters | `docs/adr/0001-work-orders.md` |
| The `work-order` skill | [vibe-map](https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/work-order/SKILL.md) `.agents/skills/work-order/` | MIT, Copyright (c) 2026 Tom Peters | `.agents/skills/work-order/` |

Each was generalised on the way in: the teams, paths and constants that named vibe-map's own files now live in `.agents/config/teams.toml`.
