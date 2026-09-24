# work/

How more than one agent works here at once without stepping on each other, and how anyone can tell that a task is done. An order is one unit of work: the files it owns and acceptance criteria that are commands. `tac work` checks it; the `just work-*` recipes, the pipeline gates, the hooks and CI all call the same code. It is ported from vibe-map's `tools/work.py`, where the format was proven over some fifty orders. [ADR 0001](../docs/adr/0001-work-orders.md) has the reasons, and the `work-order` skill in `.agents/skills/` is what an agent loads.

| Path | What it is |
|---|---|
| `.agents/config/teams.toml` | The teams and the paths each answers for, plus the knobs of `tac work`. A path belongs to the first team that covers it. |
| `goals/<goal>.toml` | A goal: the sentence, what it leaves out, its orders. `just work-plan <goal>` turns it into launch groups. |
| `orders/<id>/order.toml` | One task: team, branch, builder and provider, the files it owns, criteria that are commands (`check`) or rulings (`judge`). |
| `orders/<id>/review.toml` | The ruling of someone who did not build it, on the other provider, tied to the commit they read. |
| `orders/<id>/signoff-<team>.toml` | Another team's manager agreeing to a change in its files. |
| `orders/<id>/result.json` | What the last check measured. Not committed; good only for the exact tree it saw, and a convenience for the builder: `work-accept` measures again for itself. |
| `orders/<id>/touched.json` | Which agents edited under this order, written by the hook. Not committed. |
| `orders/<id>/repair.md` | The last repair handoff `work-repair` rendered. Not committed. |
| `templates/` | Start from these. `just work-new <id> <team> "<title>"` writes an order. |
| `orders/example/` | The format, kept as a reference. Its branch is never checked out, so it never guards anything. |

An order is active while its branch is checked out in some worktree, and landed once its accepting review is on the base branch. One order, one branch, one pull request: `work-validate` names two orders that share a branch. Both are derived; nothing stores a status. `just work-new` writes a draft: it loads, and starts guarding, once `owns`, `builder` and a criterion are filled in. Someone else's draft in another worktree is named by `work-validate` and never fails it.

## The recipes

| Recipe | Who runs it | What it decides |
|---|---|---|
| `just work-new <id> <team> "<title>"` | a manager | writes a draft order on this branch |
| `just work-validate` | anyone | every order is readable and no two active orders own the same file |
| `just work-check <id>` | the builder, the build gate | nothing outside `owns` changed (the fragment and the `anyone` paths belong to everyone), then every criterion's command |
| `just work-repair <id>` | the build gate, on a failed check | renders the failing criteria into the repair handoff, runs one bounded builder turn, then `work-check` again and exits with its status |
| `just work-packet <id>` | the reviewer | what to read, and the commit to put in `reviewed` |
| `just work-review <id>` | the review gate | a readable review, current for this branch, by someone other than the builder, from the other provider, accepting every criterion |
| `just work-accept <id>` | the final gate | the checks measured afresh, the review, and every cross team's sign-off |
| `just work-plan <goal>` | a manager | launch groups: needs first, disjoint files, the agent budget and each team's `max_workers` |
| `just work-board` | anyone | every active order in every worktree |
| `just work-sweep` | a release | removes the orders that have landed |
| `just work-post`, `work-thread`, `work-say` | anyone | the order's one status comment on its issue, the issue read from people who can push, a remark in a role |

Exit codes: 0 when the order holds, 1 when it does not yet, 2 when a file cannot be read or git cannot answer. An empty git answer never reads as "nothing changed".

## Hooks remind; the checks decide

`tac work hook pre-tool` refuses an edit outside the orders on the current branch and names the team that owns the file. It lets go on anything it cannot read, so a broken or half-written order stays repairable; `work-check` refuses that order later anyway. It also notes which subagent worked on which order (`touched.json`), and `tac work hook stop` sends that subagent back once if it stops while the order fails: a builder to the checks, someone who only wrote into the order's folder to a readable review. A session is sent back only when its report's first line is `order: <id>` (see `templates/report.md`), because Stop fires at the end of every turn. A hook is a reminder for a cooperative agent, not the gate: an edit made through a shell is seen by no hook. What decides whether work lands is `work-check`, `work-review`, `work-accept` and CI, and those fail closed.

## Review

A review names the full commit it read. It stays current while neither the order file nor the branch's own contribution to the files it owns has changed since that commit, compared as a verbatim patch id: a clean sync with the base keeps a review, any edit ends it, a change of indentation included. The review and sign-offs are left out of that comparison, or an order that owns `work/` would end its own review by committing it. With `review_provider = "other"` in `teams.toml`, the order names the builder's provider and the review names its own, and the two differ.

CI runs `tac work ci` on every pull request, after the tests so it can never hide a failing one. The orders a pull request carries are those whose folder it touches and those written for its branch. One that ships code ships its accepted review and every sign-off, so until the review lands that one step is red and everything else is green.

Orders that have landed are removed when a release is cut, like changelog fragments.
