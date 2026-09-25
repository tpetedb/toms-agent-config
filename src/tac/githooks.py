"""The git hooks prek installs (layer 3a, design section 9), held to hooks.toml.

`hooks/git/prek.toml` defines them; `[git]` in `.agents/config/hooks.toml`
names them per stage, in order. `tac check` reads both and refuses any
difference, and any hook that is not a local system command, since a remote
hook repository is fetched over the network and runs code nobody reviewed here.
The lock hashes `hooks/git/` with the rest of the hooks tree.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tac.config import CONFIG_DIR, Config

HOOKS_FILE = "hooks/git/prek.toml"
# The stage each [git] list covers, as git and prek name the hook type.
STAGES = {
    "pre-commit": "pre_commit",
    "commit-msg": "commit_msg",
    "pre-push": "pre_push",
}


def stage_ids(data: dict[str, object]) -> tuple[dict[str, list[str]], list[str]]:
    """The hook ids per stage in file order, and every problem found."""
    problems: list[str] = []
    found: dict[str, list[str]] = {stage: [] for stage in STAGES}
    repos = data.get("repos")
    if not isinstance(repos, list) or not repos:
        return found, [f"{HOOKS_FILE}: no [[repos]]"]
    for repo in repos:
        if not isinstance(repo, dict) or repo.get("repo") != "local":
            problems.append(f"{HOOKS_FILE}: every repo is local; nothing is fetched")
            continue
        hooks = repo.get("hooks")
        for hook in hooks if isinstance(hooks, list) else []:
            if not isinstance(hook, dict):
                problems.append(f"{HOOKS_FILE}: a hook that is not a table")
                continue
            hid = str(hook.get("id", "?"))
            if hook.get("language") != "system":
                problems.append(f"{HOOKS_FILE}: {hid} is not language = system")
            stages = hook.get("stages")
            if not isinstance(stages, list) or len(stages) != 1:
                problems.append(f"{HOOKS_FILE}: {hid} names exactly one stage")
                continue
            if stages[0] not in STAGES:
                problems.append(
                    f"{HOOKS_FILE}: {hid} runs at {stages[0]}, not one of "
                    f"{', '.join(STAGES)}"
                )
                continue
            found[stages[0]].append(hid)
    return found, problems


def check_git_hooks(root: Path, config: Config) -> list[str]:
    """Every way the prek config and [git] in hooks.toml disagree."""
    path = root / HOOKS_FILE
    if not path.is_file():
        return [f"{HOOKS_FILE}: missing; [git] in {CONFIG_DIR}/hooks.toml names it"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return [f"{HOOKS_FILE}: not valid TOML: {e}"]
    found, problems = stage_ids(data)
    if data.get("default_install_hook_types") != list(STAGES):
        problems.append(
            f"{HOOKS_FILE}: default_install_hook_types must be {list(STAGES)}"
        )
    for stage, key in STAGES.items():
        named = list(getattr(config.hooks.git, key))
        if found[stage] != named:
            problems.append(
                f"{HOOKS_FILE}: {stage} runs {found[stage]}, but [git] {key} in "
                f"{CONFIG_DIR}/hooks.toml names {named}"
            )
    return problems
