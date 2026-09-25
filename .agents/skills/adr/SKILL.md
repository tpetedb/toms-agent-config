---
name: adr
description: Writes an architecture decision record in Michael Nygard's form (Title, Status, Context, Decision, Consequences) into docs/adr/ and adds it to the index. Use for "write an ADR", "record this decision", "why did we choose", "supersede ADR 3", or a choice a newcomer would ask why about.
allowed-tools: Read Bash(ls docs/adr*)
metadata:
  source: "https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/adr/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "generalised for any repository that uses TAC; the pointers to a private template repository were dropped; the description was shortened for the listing budget"
---
# Architecture decision records

An ADR is one short file per decision: the forces, the choice, what follows. The point is that the why outlives the people who knew it. Origin: Michael Nygard, 2011: https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions

Rules:

- One decision per file, `docs/adr/NNNN-short-title.md`, numbered in order, never renumbered. Why: links to it keep working.
- Five sections, always in this order: Title, Status, Context, Decision, Consequences. Why: a reader who knows the form finds the answer in ten seconds.
- Title: a short noun phrase with the number, "ADR 0003: Which skills ship, and the lint that holds them".
- Status: Proposed, Accepted, Deprecated, or Superseded by ADR NNNN, with an ISO 8601 date.
- Context: the forces as neutral facts (technical, legal, social, this project). No argument yet. Why: a reader must be able to disagree with the decision after reading the same facts.
- Decision: full sentences, active voice, "We will ...". Why: it reads as a commitment, not a wish.
- Consequences: all of them, the bad ones too, what becomes easier and what becomes harder. Why: the next person weighs the price that was paid.
- Never delete an ADR. A reversed decision is superseded by a new one that links back. Why: the history of why is the whole value.
- Write it on the day of the decision, one or two pages, and add a row to `docs/adr/README.md`.
- Which decisions: the ones that are hard to reverse or that a newcomer would ask why about. Not every choice; a line in `AGENTS.md` is enough for style.
- MADR (https://adr.github.io/madr/) adds Decision Drivers, Considered Options and pros and cons per option. Use MADR when several options were seriously weighed, Nygard's form when the choice was clear.
- No em dashes; plain prose, as everywhere in the repository.

Template:

```markdown
# ADR NNNN: <short noun phrase>

Status: Accepted, YYYY-MM-DD

## Context

<the forces, as facts: what is true about the project, the tools, the licence, the people>

## Decision

We will <what>. <one or two sentences on how>.

## Consequences

- <what becomes easier>
- <what becomes harder, and what we do about it>
- <what is now off the table>
```

Sources:

- Michael Nygard, Documenting Architecture Decisions (15 November 2011): https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- ADR home, the GitHub adr organisation: https://adr.github.io
- MADR 4.0.0: https://adr.github.io/madr/
- The live examples: `docs/adr/` in this repository.
