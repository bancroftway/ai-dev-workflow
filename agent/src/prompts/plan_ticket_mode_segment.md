This project already has an approved Implementation Plan baseline: `.ai-dev-workflow/04-plan.approved.json`
already holds an earlier ticket's approved plan for this same project. If this prompt also gives you
an "immediately-prior draft," that IS the prior ticket's own approved plan, not an abandoned attempt
of your own -- read it as the project's existing architecture and frame this draft as EXTENDING it
for this ticket's own Specification, not a from-scratch replacement of everything the project has
ever planned.

You do not need to restate every existing Plan Step: reuse the exact same id and description for
any step this ticket's Specification doesn't touch (including scaffolding/infrastructure work an
earlier ticket's plan already called for -- verify against the actual repo state before assuming
it was actually built, rather than just planned), and mint new ids only for the concrete actions
this ticket's own Specification actually requires. Leaving an earlier ticket's step out of this draft does not undo
or remove it -- it simply means this ticket has no reason to mention it, and every step you omit
keeps whatever the project already built. Add Risk Notes only for risks this ticket itself
introduces, and add Diagrams/Wireframes only for what this ticket adds or changes, not a redraw of
the whole existing system.

Carry-over discipline, gate-checked: a step you restate from the prior plan must keep BOTH its id
and its description byte-for-byte -- any edit to the description makes it a "new/changed" step to
the deterministic gate, which then rejects it if it cites only already-delivered criteria. Never
write a new or changed step whose only cited criteria are already coded and tested (the ledger
stamps them; delivered work is never re-planned). Drop any prior step whose every cited criterion
this ticket's Specification retires -- that feature is removed.
