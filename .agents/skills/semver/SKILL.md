---
name: semver
description: Picks the next version with Semantic Versioning 2.0.0 (MAJOR.MINOR.PATCH), sets it in pyproject.toml and tags it. Use for "bump the version", "what version is this", "is this a breaking change", "cut a release", "tag it", or what 0.x or 1.0.0-rc.1 means.
allowed-tools: Read Bash(uv run tac --version) Bash(git tag -l *)
metadata:
  source: "https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/semver/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "the version commands became uv run tac --version and pyproject.toml; the release steps follow the changelog skill's fragments and the release pipeline"
---
# Semantic Versioning

A version number is a promise about compatibility and nothing else. Spec: https://semver.org/spec/v2.0.0.html

Rules:

- MAJOR.MINOR.PATCH. Bump MAJOR for a change that breaks what people rely on, MINOR for a backward-compatible addition, PATCH for a backward-compatible fix. Why: the number alone tells a reader whether an upgrade can hurt.
- Reset the lower parts on a bump: 0.2.3 plus a feature is 0.3.0, not 0.3.3.
- Decide the bump from the effect on the user, not the size of the diff. A one-line change to a command's output breaks every script that parses it; a changed config key or `schema_version` breaks every adopting project until `tac migrate` runs.
- Before 1.0.0 anything may change (spec, rule 4). While on 0.y.z, features bump MINOR and fixes bump PATCH. `uv run tac --version` prints the number it is on now; never quote one from memory.
- A pre-release is `1.0.0-rc.1` and sorts before `1.0.0`; build metadata is `1.0.0+abc123` and is ignored for ordering.
- A released version is final. Found a mistake? Release the next number.
- One source: `version` in `pyproject.toml`. `tac.__version__` reads the installed metadata (`importlib.metadata.version`), so there is nothing else to keep in agreement. Tag the commit `vX.Y.Z` so git and `CHANGELOG.md` say the same thing.
- Read the pending fragments in `changelog.d/` to decide (the `changelog` skill). The entry comes first, the bump second: you cannot pick the number before you know what changed.

The release checklist:

```text
1. uv run tac --version                       what we have now
2. just changelog                             the pending fragments:
     removed, or a changed that breaks        -> MAJOR (MINOR while 0.y.z)
     added                                     -> MINOR
     only fixed or security                    -> PATCH
3. set version in pyproject.toml, then uv lock
4. uv run --frozen towncrier build --version X.Y.Z
5. uv run tac --version prints X.Y.Z; commit "Release X.Y.Z: <why>"
6. the owner tags vX.Y.Z on the merged commit and publishes the release
```

In this repository steps 1 to 5 are the `release` pipeline's plan and publish stages, and step 6 is the owner's own action (`.agents/config/pipelines/release.toml`).

Sources:

- Semantic Versioning 2.0.0, Tom Preston-Werner: https://semver.org/spec/v2.0.0.html
- Keep a Changelog 1.1.0: https://keepachangelog.com/en/1.1.0/
