This project's ledger already has active Acceptance Criteria from earlier ticket(s), each already
covered by its own tests written when that earlier ticket ran this same stage. Scope your work to
the Acceptance Criteria in the approved Specification handed to you above -- this ticket's own
new/changed criteria -- rather than the ledger's full active list. You do not need to write, review,
or regenerate tests for another ticket's Acceptance Criteria: they already have their own covering
tests elsewhere in this repository, and writing a new test against an already-implemented feature
would immediately pass with no implementation of your own, which reads as tautological and is
rejected.

If a criterion in the Specification above is a revision of an existing one (its `existing_ac_id`
was set when it was specified), extend that criterion's existing tests with the new/changed
behavior rather than starting a parallel suite for it. Every criterion the Specification above
actually lists still needs its own new, currently-failing test exactly as described below --
ticket-scoping means you don't chase the rest of the ledger, not that you write fewer tests for
what this ticket does introduce.

Deletion propagation, gate-checked: if the Specification above lists `retired_ac_ids`/
`retired_us_ids`, those features are REMOVED -- DELETE every test case that names a retired id
(delete the whole file when it holds nothing else). A deterministic gate greps every test file
and fails this stage while any retired id remains.

Completed-work protection, also gate-checked: criteria already delivered by earlier runs (the
ledger stamps them coded/tested) keep their regression tests exactly as they are. Never add,
modify, rename, or delete a test for an already-delivered criterion -- the gate compares the tree
against the ledger and rejects both a missing regression test and new test lines naming a
completed criterion. A criterion already delivered needs NO new test from you even if it appears
in the Specification above.
