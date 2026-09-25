order: <id>
role: builder

PR: #<n>, head <sha>, CI <green or the failing job>.
Criteria: <c1 pass, c2 pass, ...> (from `just work-check <id>`, not from memory).
Rejected: <finding and the evidence that it does not reproduce>, or none.
Outside my order: <file and what it needs>, or none.
Under 250 words. The first line is read by a hook: if the order does not hold,
you are sent back with the failing check.
