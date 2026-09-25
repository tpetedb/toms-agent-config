order: <id>
<!-- The line above is the tag line: the order this pull request lands, as
     `order: <id>` alone on the first line, or `order: none` for a one-line fix
     by a person. CI judges the orders a pull request carries from the base
     revision, whatever this says; the line tells a reader which one to open. -->

## What and why

<!-- One or two plain sentences: what changed and why. -->

## Acceptance

<!-- Every criterion of work/orders/<id>/order.toml with its command and the
     result `just work-check <id>` printed, never from memory. -->

| Criterion | Command | Result |
|---|---|---|
| c1 | `<command>` | <pass, or the failing line> |

- [ ] `just verify` is green on this head
- [ ] `bash scripts/private_scan.sh` is clean
- [ ] a fragment is in `changelog.d/`
- [ ] the review from the other provider is in `work/orders/<id>/review.toml`, naming this head

## Left open

<!-- What this does not do, and the item or order that picks it up; or none. -->

## For the owner

<!-- The one merge command, with this pull request's number and head sha. The
     merge fails if the branch moved after your review. -->

```sh
gh pr review <N> --approve && gh pr merge <N> --squash --match-head-commit <sha>
```
