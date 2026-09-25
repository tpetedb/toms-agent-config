# Third-party and carried-over material

What in this repository did not start here, where it came from and under which terms. docs/DESIGN.md section 16 is the full reuse list; a line moves here when its material lands. TAC itself is released under the MIT licence in [LICENSE](LICENSE) (Q9, [ADR 0002](docs/adr/0002-owner-decisions-2026-09-25.md)).

## Third-party

| What | Source | Licence | Where it lives here |
|---|---|---|---|
| The ruleset, collaborator-permission and repository schemas, dereferenced, and the published examples the repository and collaborator fixtures started from | [github/rest-api-description](https://github.com/github/rest-api-description) `descriptions/api.github.com/api.github.com.json`, at the commit recorded in the file | MIT, Copyright (c) GitHub | `tests/fixtures/github/openapi_rulesets.json` (cut by `scripts/github_openapi_subset.py`), `tests/fixtures/github/repo.json`, `tests/fixtures/github/collaborator_*.json` |
| The `ponytail` skill, trimmed to the portable subset and opt-in for builders (Q1) | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) `skills/ponytail/SKILL.md` and `LICENSE` at commit `e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156`; its hooks, commands, MCP server, plugins and other five skills were not taken | MIT, Copyright (c) 2026 DietrichGebert | `.agents/skills/ponytail/` |
| The `verification-before-completion` skill | [obra/superpowers](https://github.com/obra/superpowers) `skills/verification-before-completion/` at commit `b36e0829c6d0140e93cfef2ca599b1b07d4a7797`, by way of vibe-map | MIT, Copyright (c) 2025 Jesse Vincent | `.agents/skills/verification-before-completion/` |
| The `webapp-testing` skill with its scripts and examples | [anthropics/skills](https://github.com/anthropics/skills) `skills/webapp-testing/` at commit `34040c9c568585f6929bedeaad110ad08f079624`, by way of vibe-map | Apache-2.0, Copyright 2026 Anthropic, PBC | `.agents/skills/webapp-testing/` |

Each vendored skill keeps its licence text in a `LICENSE` file next to its `SKILL.md`, and its frontmatter `metadata` and the note at the top of its body say what was changed. The `webapp-testing` scripts and examples are the only vendored code and are kept verbatim. The development tools in `pyproject.toml` are installed from PyPI at build time under their own licences and are not redistributed here.

## Carried over from the owner's own public projects

| What | Source | Licence | Where it lives here |
|---|---|---|---|
| The work-order tool, its format, templates, README and tests | [vibe-map](https://github.com/tpetedb/vibe-map) `tools/work.py`, `work/`, `tests/test_work.py` | MIT, Copyright (c) 2026 Tom Peters | `src/tac/work.py`, `src/tac/work_cli.py`, `work/`, `tests/test_work.py` |
| The work-order decision record | [vibe-map ADR 0016](https://github.com/tpetedb/vibe-map/blob/main/docs/adr/0016-work-orders.md) | MIT, Copyright (c) 2026 Tom Peters | `docs/adr/0001-work-orders.md` |
| The `work-order` skill | [vibe-map](https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/work-order/SKILL.md) `.agents/skills/work-order/` | MIT, Copyright (c) 2026 Tom Peters | `.agents/skills/work-order/` |
| The `adr`, `changelog`, `semver`, `justfile` and `readme-quickstart` skills | [vibe-map](https://github.com/tpetedb/vibe-map/tree/main/.agents/skills) `.agents/skills/<name>/SKILL.md` | MIT, Copyright (c) 2026 Tom Peters | `.agents/skills/<name>/` |
| The `mermaid-diagrams`, `research-first`, `owner-report` (with its two scripts), `house-style` (was `toms-style`), `hooks-authoring` (was `claude-hooks-authoring`), `gitlab-ci` and `data-platform` skills, and the Keep a Changelog rules merged into `changelog` | the owner's dotfiles toolbox, not public: `config/claude/skills/<name>/SKILL.md` | MIT, Copyright (c) 2026 Tom Peters (Q9) | `.agents/skills/<name>/` |
| The `shared-memory` skill, written new for `tac memory` | this repository, `docs/DESIGN.md` section 6 | MIT, Copyright (c) 2026 Tom Peters | `.agents/skills/shared-memory/` |

Each was generalised on the way in: the teams, paths and constants that named vibe-map's own files now live in `.agents/config/teams.toml`, and every skill's `metadata` records its source, author, licence and what changed ([ADR 0003](docs/adr/0003-skills-provenance-and-lint.md)).
