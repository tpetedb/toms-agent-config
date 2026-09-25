# changelog.d

One file per change: `<slug>.<type>.md`, where the type is one of `added`,
`changed`, `deprecated`, `removed`, `fixed` or `security`. The body is one or
two plain sentences a user would want to read.

A branch adds its fragment in the same commit as the change and never edits
`CHANGELOG.md`, so two branches never conflict over the same lines. `just
changelog` prints what the next release section would say; a release runs
towncrier to move the fragments into `CHANGELOG.md`.
