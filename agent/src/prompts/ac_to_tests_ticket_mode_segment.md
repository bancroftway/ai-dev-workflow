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
