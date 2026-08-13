---
name: spec-sync
description: Guides how to correctly reference stable user-story and acceptance-criteria IDs (US-#### / AC-####.#) when drafting or revising a specification, so that a downstream deterministic ledger can allocate and validate IDs correctly. Use this skill whenever drafting, revising, or updating a specification document that has (or will have) a stable-ID registry -- especially when a prior version of the spec already exists and some stories are being kept, some changed, some removed, and some added for the first time. Also trigger when asked to "keep story numbering stable," "don't renumber," or "sync the spec with the ledger."
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
actual `US-####`/`AC-####.#` numbering -- it's the only thing that can guarantee a number is never
reused, even after a story is retired. If you write `"id": "US-0042"` for a story you consider
new, you're guessing at a number that system hasn't allocated yet, and if you guess wrong (a
collision, a gap, a number that's actually retired) you've corrupted the one thing this whole
mechanism exists to protect. Instead:

- **Revising an existing story or criterion**: cite its existing id explicitly (e.g.
  `"existing_us_id": "US-0007"`) so the system knows to update that entry, not create a new one.
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
the feature was cut, descoped, or superseded -- say so explicitly (cite its id, mark it as no
longer active) rather than just omitting it. An omission looks like an accident to whoever
reviews this; an explicit "this is retired, here's why" is a decision a human can evaluate.
Retired stories keep their id permanently -- the number is never reused for something else later,
even if the same feature comes back in a future revision (that becomes a new story, referencing
the retired one's id in its own narrative if useful context, not reusing the number itself).

## Reporting your findings

For every user story and acceptance criterion in your draft, make sure your structured response
includes, explicitly:

- `existing_us_id` / `existing_ac_id`: the id you're revising, or empty/null if this is new.
- For anything you believe should be retired from the prior version: its id and a short reason.

Don't leave this implicit in prose -- the field the downstream system reads is what matters, not
a sentence in your narrative that says "this is basically the same as before."
