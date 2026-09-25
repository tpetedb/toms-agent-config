# toms-agent-config

One authored `.agents/` tree that every coding-agent harness (Claude Code, Codex, GitHub Copilot, opencode, pi, Gemini CLI) is made to obey: generated harness files, typed handoffs between agents, an attributed memory bus, a human loop and enforcement at the repository boundary.

Status: under construction. The design lands first in docs/DESIGN.md through a pull request.

## Quickstart

```sh
bash bootstrap.sh   # installs uv if absent, builds .venv and .agents/.venv
just verify         # ruff, basedpyright, pytest, the private scan
just doctor         # named checks; exits non-zero until every one passes
```

## Licence

MIT, see [LICENSE](LICENSE). Material that did not start here, and its terms, is listed in [THIRD_PARTY.md](THIRD_PARTY.md).
