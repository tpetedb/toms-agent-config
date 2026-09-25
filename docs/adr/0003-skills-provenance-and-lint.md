# ADR 0003: Which skills ship, where their provenance lives, and the lint that holds them

Status: Accepted, 2026-09-25

## Context

- `docs/DESIGN.md` section 12 names the skills TAC ships: seventeen, from three places. Seven come from the owner's private dotfiles toolbox, eight from the owner's public vibe-map repository (two of those vendored there from third parties), one is new, and one, `ponytail`, is third-party.
- The repository is public and MIT (Q9, [ADR 0002](0002-owner-decisions-2026-09-25.md)). A skill copied from a private source can carry private names; a vendored skill carries someone else's licence, which must travel with it.
- The [Agent Skills specification](https://agentskills.io/specification) is what both Claude Code and Codex read. It allows six top-level fields (`name`, `description`, `license`, `compatibility`, `allowed-tools`, `metadata`), limits `name` to 64 characters that equal the folder, `description` to 1,024, and makes `metadata` a map of strings to strings. Anything else is a client extension another client may ignore or refuse.
- Every session lists every skill's name and description, inside a budget, and cuts descriptions when the sum is over it. On 2026-09-25 Claude Code's docs give 1% of the model's context window (with a 1,536 character cap per entry) and Codex's give 2%, or 8,000 characters when the window is unknown. At 200,000 tokens and four characters per token both come to 8,000 characters.
- Q1 answered yes to `ponytail` as an opt-in skill for builders only. It pushes toward the smallest diff, which is right for a builder and wrong for a reviewer. Q2 moved the owner's user-level third-party skills aside and kept the caveman package excluded.
- `.agents/config/` is judged by the base revision's checker in CI, which refuses a key it does not know, and generated files are judged the same way.

## Decision

We will ship exactly the seventeen skills of section 12 under `.agents/skills/<name>/`, one folder each, and keep skills that belong to one product (vibe-map's `obsidian-notes`, `camp-progress`, `council`, `develop-camp`, `install-camp`, `duckdb-sql`, `python-data`) and to one private tool in that product or tool.

We will record provenance in each skill's own frontmatter `metadata`: `source` (a URL at a commit where there is one, or the private path), `author`, `licence` and `changes`, all strings. A vendored skill also keeps its `LICENSE` file beside `SKILL.md`, a note at the top of its body, and a line in `THIRD_PARTY.md`. Private names are replaced by roles ("the owner"), and the private scan is the judge.

We will hold every skill to the portable subset with `tests/test_skills.py`, run as `just skills-lint`: the specification's limits and fields, string metadata, provenance present, a body under 500 lines, `--help` exiting 0 from every bundled script at any depth, a frontmatter with no key given twice, no em dash and no pictograph in any text file, and the sum of names and descriptions under 8,000 characters. The test's docstring cites the two pages and the date, so the number is checked again when a client changes it.

We will vendor `ponytail` from commit `e3ba2aa6` only as its `SKILL.md` and `LICENSE`, trimmed to the portable subset, with `metadata.opt_in: "true"` and `metadata.roles: "builder"`. `tac sync` never links an opt-in skill into `.claude/skills/`, so no client lists it in every session; it reaches a session only where a pipeline stage names it. The lint refuses an opt-in skill in any team's `skills` or in a review or signoff stage.

No caveman package, skill or seed ships, and no skill mentions it. That question is closed.

## Consequences

- Anyone can tell where a skill came from and under which terms without leaving the file, and a copy into another repository carries its provenance along.
- The lint turns a description that grew, a field only one client knows, or a pasted em dash into a red test before a client silently cuts or ignores it. Its budget number is a documented constant and must be checked again when a client's docs change.
- `metadata` values are strings, so the opt-in flag is the string `"true"`. `tac sync` also accepts the YAML boolean, but the lint refuses it.
- Nothing gives the new skills to a team or `ponytail` to a builder yet. Adding `ponytail` to the `build` stage of `order.toml` and the new skills to the teams' `skills` lists is a value change in `.agents/config/`, left as follow-up config for the owner or the chief once this lands.
- `skills/_index/` is not generated yet. The lint computes the budget from the folders; the index waits for a release whose base checker knows that output.
- Vendored text must lose em dashes and pictographs to pass the floor, so each vendored skill's note has to stay honest about every change made.
