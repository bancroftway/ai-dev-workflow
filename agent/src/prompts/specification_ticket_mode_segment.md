This project already has an approved specification baseline: `.ai-dev-workflow/spec/ledger.json`
has entries from earlier ticket(s) against this same project, and
`.ai-dev-workflow/03-specification.approved.json` is the project's real, current, COMPLETE
specification -- every story and criterion from every earlier ticket, still live. View it before
drafting so you know what already exists: don't propose something that's already there, and know
the real ids you'll need to cite below.

`.ai-dev-workflow/spec/draft-specification.json` (the file you edit) is re-seeded for every new
ticket EMPTY of stories on purpose -- it is this ticket's delta sketchpad, not a copy of the whole
project to maintain. Put only what THIS ticket actually adds or changes into it:

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

STORY DECISIONS -- the file's `story_decisions` holds one row per story of the approved
specification, seeded undecided (`decision: null`). Fill in every row, `decision` and a one-line
`reason`:
- `unchanged`: this ticket's requirements leave the story as it is.
- `modified`: they change it -- re-emit it citing `existing_us_id` with the new wording or
  criteria, add a criterion to it, retire one of its criteria, or reopen one of its criteria via
  `bug_affected_ac_ids` (a bug ticket modifies the story even with every word unchanged).
- `retired`: they remove it -- name it in `retired_us_ids`.

Weigh EVERY new requirement against EVERY existing story before deciding: does it contradict,
narrow, extend or replace the story? A requirement changes a story even when it never names it --
"notes are permanent once saved" retires "delete a note"; "notes are capped at 500 characters"
modifies "create a note". The decision must match what the file actually does: a deterministic gate
rejects a story declared `modified` that the file doesn't change, one declared `unchanged` that the
file retires or changes, a missing or duplicate row, and a blank reason. Never add a row for a story
created in this ticket.

When this round's PRD merge changed existing requirements, you are shown them as `PC-n` changes.
Put each `PC-n` in the `prd_change_ids` of the `modified`/`retired` row it drives, or -- only when no
story is affected -- in `prd_changes_without_story` with a reason. A PRD change cited only by an
`unchanged` story is a contradiction the gate rejects.

If the prior run's exit report lists criteria as "carried over -- not delivered", re-cite them in
this draft (unchanged wording, via `existing_ac_id`) so they re-enter the work queue -- an
undelivered criterion left uncited stays undelivered with nothing scheduled to build it. Re-citing
with unchanged wording leaves its story `unchanged` (a bug reopen via `bug_affected_ac_ids` does
not -- that is `modified`).
