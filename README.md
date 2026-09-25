# toms-agent-config

One authored `.agents/` tree that every coding-agent harness (Claude Code, Codex, GitHub Copilot, opencode, pi, Gemini CLI) is made to obey: generated harness files, typed handoffs between agents, an attributed memory bus, a human loop and enforcement at the repository boundary.

Status: the design ([docs/DESIGN.md](docs/DESIGN.md)), M0 and M1 are on main; M2 onward is in progress.

## Quickstart

For the full guide, and where to tweak things, see [QUICKSTART.md](QUICKSTART.md).

```sh
bash bootstrap.sh   # installs uv if absent, builds .venv and .agents/.venv
just verify         # ruff, basedpyright, pytest, the private scan
just doctor         # named checks; exits non-zero until every one passes
```

## Licence

MIT, see [LICENSE](LICENSE). Material that did not start here, and its terms, is listed in [THIRD_PARTY.md](THIRD_PARTY.md).
