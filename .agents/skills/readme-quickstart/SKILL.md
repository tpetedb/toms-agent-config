---
name: readme-quickstart
description: Writes or repairs a README.md or QUICKSTART.md that gets a stranger from clone to a working run on the first screen, with exact commands and their output. Use for "write a README", "add a quickstart", "how do people get started", "onboarding docs", or a project with no README.
allowed-tools: Read Bash(ls *) Bash(cat README.md)
metadata:
  source: "https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/readme-quickstart/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "the pointers to a private template repository were dropped; the QUICKSTART.md table rules of this repository were added; the description was shortened for the listing budget"
---
# README and quickstart

The README is the first thing a person sees and the last thing maintainers update. GitHub shows it under the file list, so it is the front page. Guide: https://www.makeareadme.com

Rules:

- `README.md` at the repository root. Line one: the name. Line two: one sentence saying what this is and for whom. Why: a reader decides in ten seconds whether to keep reading.
- The quickstart is on the first screen: prerequisites, install, run, in that order, every command in a fenced block, with the output the reader should see.
- Every command in the README is copied from a terminal where it just worked. Run them again when they change. Why: a README that lies costs more than no README.
- Order after the quickstart: Usage, Project structure, Architecture, Contributing, License. makeareadme adds Badges, Visuals, Support, Roadmap, Authors, Project status; take the ones that apply, skip the rest.
- Write for the newest reader. Define or link every term the project invented.
- Link, do not duplicate: `CHANGELOG.md` for what changed, `docs/adr/` for why, `docs/` for the long version. Two copies drift, one link does not.
- Say the licence in one line and name the file.
- The README changes in the same commit as the command it describes.
- Two screens at most. If it grows, move the long parts to `docs/` and link them; a `QUICKSTART.md` when the quickstart alone needs a page.

## QUICKSTART.md in this repository

`QUICKSTART.md` holds the tables a person uses every day: "Where to tweak" (the file, the key, the check) and "Checks that tell you it worked" (the recipe and what green means).

- Every recipe a person now runs, and every knob they now tweak, gets a row in the same change.
- The tables are append-only: add a row next to its neighbours, never rewrite someone else's row in passing.
- A link in a row is relative and must resolve; the check column names a `just` recipe, not a raw command.

Template for a README:

````markdown
# <name>

<one sentence: what it does, for whom>

## Quickstart

Needs: <tool and version>, <tool>.

```sh
git clone <url> && cd <name>
<install command>
<run command>
```

You should see: `<first line of the output>`.

## Usage

<the two or three commands people run every day, one line each on what it does>

## Project structure

- `<dir>/`: <what lives there>

## Contributing

<branch, test command, style rule>. What changed: CHANGELOG.md. Why: docs/adr/.

## License

MIT, see LICENSE.
````

Sources:

- Make a README, Danny Guo: https://www.makeareadme.com
- GitHub docs, About READMEs: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes
- The live examples: `README.md` and `QUICKSTART.md` in this repository.
