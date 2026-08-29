This project already has an approved specification baseline: `.ai-dev-workflow/spec/ledger.json`
already has entries from earlier ticket(s) against this same project. Frame this draft as
EXPANDING that baseline for this ticket's own requirements, not a from-scratch rewrite of
everything the project has ever specified.

You do not need to restate or re-describe every existing User Story/Acceptance Criterion the
ledger already holds -- only include the ones this ticket actually touches: new stories/criteria
this ticket introduces, plus `existing_us_id`/`existing_ac_id` citations for anything this ticket
revises. A story or criterion this ticket has no reason to mention simply keeps its current status
untouched -- leaving it out of this draft is not how you retire it, and it is not a gap you need to
fill in before you can set readiness to true.

If this ticket's own work makes something in the existing baseline obsolete, say so explicitly via
`retired_ac_ids`/`retired_us_ids` -- never by silently leaving it out.

Wording discipline for re-cites: when you cite an existing criterion you are NOT changing (e.g.
restating a story to add a sibling criterion), copy its description byte-for-byte from what you
were given. Any edit to a criterion's wording -- even a cosmetic rephrase -- tells the pipeline
the REQUIREMENT changed, and its already-delivered code and tests are then discarded and redone.
Reword only when the requirement genuinely changed.

If the prior run's exit report lists criteria as "carried over -- not delivered", re-cite them in
this draft (unchanged wording) so they re-enter the work queue -- an undelivered criterion left
uncited stays undelivered with nothing scheduled to build it.
