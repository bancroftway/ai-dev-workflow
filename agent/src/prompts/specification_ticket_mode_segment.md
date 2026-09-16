This project already has an approved specification baseline: `.ai-dev-workflow/spec/ledger.json`
already has entries from earlier ticket(s) against this same project, and
`.ai-dev-workflow/spec/draft-specification.json` should already be seeded from the last-approved
specification (view it -- it is the project's real, current specification). Frame this draft as
EXPANDING that baseline for this ticket's own requirements, not a from-scratch rewrite of
everything the project has ever specified.

**Correction (2026-09-16): the file must still hold EVERY still-live User Story/Acceptance
Criterion, not just the ones this ticket touches.** A deterministic gate enforces this
unconditionally, regardless of ticket mode -- a story/criterion silently missing from the file
fails the whole draft ("The specification draft is INCOMPLETE"), whether or not this ticket has
anything new to say about it. So: keep every existing story/criterion this ticket doesn't touch
in the file EXACTLY as it already reads (same id, same wording -- you don't need to re-derive or
improve it, just leave it in place); cite `existing_us_id`/`existing_ac_id` only for the ones this
ticket genuinely adds to or revises. Leaving something OUT of the file is never how you retire it
-- silence is always an error, not a status-preserving omission.

If this ticket's own work makes something in the existing baseline obsolete, say so explicitly via
`retired_ac_ids`/`retired_us_ids` -- never by silently leaving it out of the file.

Wording discipline for re-cites: when you cite an existing criterion you are NOT changing (e.g.
restating a story to add a sibling criterion), copy its description byte-for-byte from what you
were given. Any edit to a criterion's wording -- even a cosmetic rephrase -- tells the pipeline
the REQUIREMENT changed, and its already-delivered code and tests are then discarded and redone.
Reword only when the requirement genuinely changed.

If the prior run's exit report lists criteria as "carried over -- not delivered", re-cite them in
this draft (unchanged wording) so they re-enter the work queue -- an undelivered criterion left
uncited stays undelivered with nothing scheduled to build it.
