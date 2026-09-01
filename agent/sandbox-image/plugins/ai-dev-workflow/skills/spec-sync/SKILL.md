---
name: spec-sync
description: Guides how to correctly reference stable user-story and acceptance-criteria IDs (US-0001 for a story, US-0001.1 for its 1st criterion -- criterion ids ALWAYS share the US- prefix, never AC-) when drafting or revising a specification, so that a downstream deterministic ledger can allocate and validate IDs correctly. Use this skill whenever drafting, revising, or updating a specification document that has (or will have) a stable-ID registry -- especially when a prior version of the spec already exists and some stories are being kept, some changed, some removed, and some added for the first time. Also trigger when asked to "keep story numbering stable," "don't renumber," or "sync the spec with the ledger."
---

# Spec Sync

A specification that gets revised over and over is only useful for tracking progress if the
things it tracks -- user stories, acceptance criteria -- keep the same identity across revisions.
If "US-0007" means something different in every draft, nobody can build a test suite against it,
link a commit to it, or trust a traceability report that cites it. Your job when drafting or
revising a specification is to make each story and criterion's identity across revisions
unambiguous to whatever system assigns and checks the actual ID numbers -- you never assign an ID
yourself, you just say clearly enough which existing item (if any) each story corresponds to.

## You never allocate or invent an ID number

This is the one rule everything else here supports. A separate, deterministic system owns the
actual numbering -- it's the only thing that can guarantee a number is never reused, even after a
story is retired. A story id is always 4-digit zero-padded, e.g. `US-0007`; a criterion id is
always that SAME story number plus `.` plus the criterion's own number, e.g. `US-0007.2` for the
2nd criterion of story `US-0007` -- there is no `AC-` prefix anywhere in this system, and a
criterion's number is never zero-padded on its own (`US-0007.2`, not `US-0007.02`). If you write
`"id": "US-0042"` for a story you consider new, or reformat an existing id when citing it
(dropping zero-padding, swapping the prefix), you've corrupted the one thing this whole mechanism
exists to protect -- it will not resolve and your draft is rejected. Instead:

- **Revising an existing story or criterion**: cite its existing id explicitly, COPIED
  CHARACTER-FOR-CHARACTER from the prior draft/approved specification you were shown (e.g.
  `"existing_us_id": "US-0007"`, `"existing_ac_id": "US-0007.2"`) so the system knows to update
  that entry, not create a new one. If you can't point to the exact substring you're copying it
  from, you don't actually have it -- leave the citation `null` instead of guessing its shape.
- **A genuinely new story or criterion**: leave the existing-id field empty/`null` and say so --
  the system allocates the next number itself.

## Deciding "same story, revised" vs. "genuinely new"

This is the judgment call that actually matters here -- get this wrong and either stable stories
churn through pointless new numbers, or genuinely different work gets silently merged into an
old story's identity.

Match to an existing story when the underlying capability is the same, even if the wording,
scope, or acceptance criteria changed substantially. "Users can reset their password by email"
evolving into "users can reset their password by email or SMS" is the *same* story, revised --
not two stories. Treat it as new only when the capability itself is genuinely different, not
just differently worded. When you're not sure, lean toward matching the existing story if there's
a plausible line of descent from it -- an incorrectly-matched revision is easier for a human to
catch and split later than an incorrectly-new story is to later realize should have been a
revision (by then, whatever tests/commits reference the "new" id have to be re-linked).

## Stories that no longer belong in the spec

If a story or criterion existed in the prior version and doesn't belong in this draft anymore --
the feature was cut, descoped, or superseded -- say so explicitly by putting its id in
`retired_us_ids`/`retired_ac_ids` rather than just omitting it. An omission is NOT a retirement:
the downstream deterministic sync only ever retires an id named in one of these two fields, on
purpose, so that one ticket's own narrower draft can never accidentally retire another ticket's
unrelated stories just by not repeating them. An omission looks like an accident to whoever
reviews this; an explicit "this is retired" in the right field is a decision a human can evaluate.
Retired stories keep their id permanently -- the number is never reused for something else later,
even if the same feature comes back in a future revision (that becomes a new story, referencing
the retired one's id in its own narrative if useful context, not reusing the number itself).
Retiring a story also retires its own acceptance criteria -- you don't need to separately list a
retired story's ACs in `retired_ac_ids` too, though doing so is harmless.

## Reporting your findings

For every user story and acceptance criterion in your draft, make sure your structured response
includes, explicitly:

- `existing_us_id` / `existing_ac_id`: the id you're revising, or empty/null if this is new.
- `retired_us_ids` / `retired_ac_ids`: the ids of anything from the prior version that no longer
  belongs in this draft.

Don't leave this implicit in prose -- the field the downstream system reads is what matters, not
a sentence in your narrative that says "this is basically the same as before." Never put the same
id in both an `existing_*_id` citation and a `retired_*_ids` list in the same response -- an item
is either being revised or being retired, never both at once.
