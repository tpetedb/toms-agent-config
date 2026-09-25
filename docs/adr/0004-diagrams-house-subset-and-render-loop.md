# ADR 0004: Diagrams in an ISO 5807 house subset, proved by a pinned render loop and bound by render.lock

Status: Accepted, 2026-09-25

## Context

- `docs/DESIGN.md` section 14 asks for exactly seven lifecycle diagrams and one per enabled pipeline, listed in `.agents/config/github.toml` `[diagrams]` and checked by `tac check`, rendered by a pinned loop, and reviewed against the ISO/IEC/IEEE 42010:2022 checklist. `standards.toml` `[diagrams]` names the ISO 5807:1985 house subset and "mermaid-cli-pinned".
- The owner reads diagrams on GitHub, in editors and in rendered reports, and the conventions block at the top of DESIGN.md already fixes six shapes, one palette colour each, and a legend.
- mermaid-cli 12.0.0 is the current release on 2026-09-25. It needs node 22.13 or later and downloads a headless Chromium on its first run, so a render is slow, needs the network and cannot run in every place `tac check` runs.
- An SVG from mermaid-cli carries generated ids, fonts and layout numbers that change between versions and machines, so a committed SVG would differ on every render and prove nothing about the source it came from.
- `.agents/config/` is judged by the base revision's checker in CI, so no new configuration key can be added for this; mise's npm backend can pin an npm package in `mise.toml` `[tools]`.

## Decision

We will draw every diagram as a Mermaid `flowchart` in the house subset: stadium, rectangle, rhombus, parallelogram, cylinder and a red parallelogram, with the classes `term`, `proc`, `dec`, `io`, `store` and `stop`, the palette's classDefs pasted verbatim, a dotted edge only for read-only access, and a legend naming each class used. `docs/diagrams/README.md` documents it with the 42010 checklist (stakeholders, concerns, viewpoints, views, with C4 context, container and component as the presentation), and `tac diagrams check` lints every `.mmd` against it.

We will keep one source per inventory entry, `docs/diagrams/lifecycle-<name>.mmd` and `docs/diagrams/pipeline-<name>.mmd`, and derive the inventory from `github.toml` and `[pipelines] enabled`; `tac check` names a missing or an extra file wherever a project keeps `docs/diagrams/`.

We will pin mermaid-cli in `mise.toml` as `"npm:@mermaid-js/mermaid-cli" = "12.0.0"`. `tac diagrams render` (`just diagrams-render`) runs that version through `npx --yes` on every `.mmd` and every fenced mermaid block under `docs/`, into a scratch folder, stops at the first failure, and writes `docs/diagrams/render.lock`: the version and the sha256 of every source. We will commit no SVG. `tac diagrams check` (`just diagrams-check`), `tac check` and `tests/test_diagrams.py` compare the sources to the lock without node, and CI renders again on every pull request and fails when a render fails or the lock would change.

## Consequences

- A diagram cannot change without a render that proves it still parses: the lock names every source edited since the last render, and CI checks the lock against the sources on every pull request.
- The lock proves syntax and freshness only. Whether a diagram says what its source says is the reviewer's, against the checklist; until the other provider reviews again, a Claude reviewer records it and the cross-provider review of the twelve diagrams is pending.
- Rendering needs node 22.13 or later and the network on the first run; a machine without them can still check, but not render. CI does not render yet: on the ubuntu runner image headless Chromium cannot start its sandbox, since Ubuntu 23.10 and later restrict unprivileged user namespaces through AppArmor, and each way around that loosens a security control, so how CI renders is the owner's decision (TODO.HUMAN.md). Until then the render runs on the developer's machine.
- Any fenced mermaid block added to a Markdown file under `docs/` becomes a source, so a pull request that adds one must carry a refreshed lock. Two branches that both add blocks to the same file will conflict in the lock and are resolved by rendering again.
- Nothing rendered is published. A reader who wants a picture renders it, or relies on GitHub's own Mermaid rendering of the Markdown files.
