---
name: changelog
description: Keep a Changelog 1.1.0 through fragments. A change adds changelog.d/<slug>.<type>.md in the same commit and never edits CHANGELOG.md; just changelog previews the next section. Use for "add a changelog entry", "release notes", "cut a release", or whether a change is Added, Changed or Fixed.
allowed-tools: Read Bash(git log *) Bash(git diff *) Bash(just changelog)
metadata:
  source: "https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/changelog/SKILL.md, merged with the owner's dotfiles toolbox, config/claude/skills/keepachangelog/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "two skills merged into one; fragments under changelog.d/ assembled by towncrier replace the hand-edited file and vibe-map's own script; the toolbox's release tool and a private template repository are no longer named; em dashes removed"
---
# Changelog

A changelog tells a person what changed for them between two versions. It is not the git log. Guide: https://keepachangelog.com/en/1.1.0/

## The rule in this repository: a fragment, never an edit

Two branches that both add a line under `## [Unreleased]` conflict on the same lines every time, and the conflict is never interesting. So every change writes its entry to its own file:

- Add `changelog.d/<slug>.<type>.md` in the same commit as the change. The type is one of the six headings in lower case: `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`. The slug is a few words with hyphens, unique in the folder.
- The body is one or two plain sentences for the person using the project: what they can now do, what they must change. No heading, no bullet marker.
- Never edit `CHANGELOG.md` on a branch. Only the release assembles it.
- `just changelog` prints what the next section would say (`towncrier build --draft --version Unreleased`). Run it before you commit; a fragment that reads badly there reads badly in the release.
- CI and review expect a fragment for every user-visible change under `src/`, `hooks/`, `templates/`, `contracts/` or `.agents/`. A pure test or refactor needs none.

Choosing the type:

| The change | Type |
|---|---|
| Something a person can now do that they could not | `added` |
| Existing behaviour now differs | `changed` (say so plainly when it breaks something) |
| Still works, will go | `deprecated` |
| Gone | `removed` |
| Was wrong, now right | `fixed` |
| Closes a hole | `security` |

## The published file

`CHANGELOG.md` stays in Keep a Changelog 1.1.0 form:

1. A preamble: title, one line on what the file is, links to Keep a Changelog and Semantic Versioning.
2. `## [Unreleased]` on top, above the towncrier `start_string` marker.
3. `## [X.Y.Z] - YYYY-MM-DD` per release, newest first, the date in ISO 8601. A pulled release keeps its section with `[YANKED]` after the date.
4. Only the six headings, in the order above, empty ones left out.
5. A link block at the bottom: `[Unreleased]` compares the latest tag to `HEAD`, each version compares the previous tag to its own, and the oldest points at its tag. GitHub writes `/compare/v1...v2`; GitLab writes `/-/compare/v1...v2`.

## Cutting a release

The `release` pipeline does this; by hand it is:

1. Pick the number with the `semver` skill from the pending fragments.
2. `uv run --frozen towncrier build --version X.Y.Z` moves the fragments into a dated section and deletes them. Fix the link block.
3. Commit the changelog and the version bump alone, never with a sweep of other files, then tag that commit. Why: the tag's tree must hold its own release section.
4. Never rewrite a released section; correct it with a new entry.

Hard-won rules:

- Strip credentials from a remote URL before it reaches the link block. A CI remote often carries a token in its user part; rebuild it as `https://host/path` without the user part and without `.git`.
- Make the release step safe to rerun: stamped but not tagged must resume to the tag, not fail forever.

Sources:

- Keep a Changelog 1.1.0, Olivier Lacan: https://keepachangelog.com/en/1.1.0/
- Semantic Versioning 2.0.0: https://semver.org/spec/v2.0.0.html
- towncrier configuration (`start_string`, `title_format`, custom types): https://towncrier.readthedocs.io/en/stable/configuration.html
- This repository: `changelog.d/README.md`, `[tool.towncrier]` in `pyproject.toml`.
