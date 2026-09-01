This repository already has existing application code and conventions in place (from an earlier
ticket against this same project, or from onboarding an existing codebase) -- this is not a blank
repo you are scaffolding from zero. Before writing new code, look at how the existing codebase
already does the things your own work needs to do: naming, file/folder layout, state-management
and data-access patterns, error-handling conventions, and (for a UI stack) component and styling
structure. Extend those same patterns for this ticket's own work rather than introducing a second,
inconsistent way of doing the same thing. Diverge from an existing convention only when it is
genuinely incompatible with what this ticket requires, and say so in `known_gaps` when you do.

If the approved Specification lists `retired_us_ids`/`retired_ac_ids`, those features are REMOVED
from the product: delete the code paths that exist solely to serve them -- endpoints, handlers,
UI elements, state, helpers used by nothing else. Their tests are already gone, and the surviving
suite is your regression guard: it must stay green after the removal. List each removal in your
`changed_files` with its reason.

Criteria the ledger already marks delivered (coded/tested by earlier runs) are settled: do not
rework their code beyond what your currently-failing tests require, and never touch their
regression tests -- a deterministic gate rejects new or modified test lines naming a completed
criterion.
