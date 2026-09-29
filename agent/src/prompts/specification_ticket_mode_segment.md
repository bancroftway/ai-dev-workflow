This project already has an approved specification baseline: `.ai-dev-workflow/spec/ledger.json`
has entries from earlier ticket(s) against this same project, and
`.ai-dev-workflow/03-specification.approved.json` is the project's real, current, COMPLETE
specification -- every story and criterion from every earlier ticket, still live. View it before
drafting so you know what already exists: don't propose something that's already there, and know
the real ids you'll need to cite below.

`.ai-dev-workflow/spec/draft-specification.json` (the file you edit) is seeded EMPTY of stories on
purpose -- it is this ticket's delta sketchpad, not a copy of the whole project to maintain. Put
only what THIS ticket actually adds or changes into it:

- A genuinely new story or criterion: write it with `existing_us_id`/`existing_ac_id: null`.
- Something this ticket revises: cite its real id via `existing_us_id`/`existing_ac_id`, with the
  new wording. Copy the id character-for-character from the approved specification you viewed --
  never invent or re-derive it.
- Something this ticket removes: name its id in `retired_us_ids`/`retired_ac_ids`. Never touch its
  wording first and never leave it out silently -- omission is not retirement, it does nothing.

Never re-emit a story or criterion this ticket isn't touching -- leaving it out of the draft file
does NOT remove it from the project; the complete document is assembled automatically from the
ledger, not from what this file repeats. If nothing about an existing story/criterion changed for
this ticket, don't mention it at all.

Wording discipline for anything you DO cite: when revising, only change the wording if the
requirement genuinely changed. Any edit to a criterion's description -- even a cosmetic rephrase --
tells the pipeline the REQUIREMENT changed, and its already-delivered code and tests are discarded
and redone.

If the prior run's exit report lists criteria as "carried over -- not delivered", re-cite them in
this draft (unchanged wording, via `existing_ac_id`) so they re-enter the work queue -- an
undelivered criterion left uncited stays undelivered with nothing scheduled to build it.
