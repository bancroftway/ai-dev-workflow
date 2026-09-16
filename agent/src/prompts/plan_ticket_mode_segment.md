This project already has an approved Implementation Plan baseline: `.ai-dev-workflow/04-plan.approved.json`
already holds an earlier ticket's approved plan for this same project, and
`.ai-dev-workflow/plan/_draft/steps.json`/`manifest.json` should already be seeded from it (view
them -- they are the project's existing architecture; treat what's already there as the project's
real state, not an abandoned attempt of your own). Frame this draft as EXTENDING that baseline for
this ticket's own Specification, not a from-scratch replacement of everything the project has ever
planned.

THE STRONGEST FORM OF REDRAFT COMPLETENESS APPLIES HERE -- `steps.json` must hold EVERY still-live
step from the WHOLE project's last-approved plan, not just this ticket's own: reuse the exact same
id and description for any step this ticket's Specification doesn't touch (including
scaffolding/infrastructure work an earlier ticket's plan already called for -- verify against the
actual repo state before assuming it was actually built, rather than just planned), and mint new
ids only for the concrete actions this ticket's own Specification actually requires. Leaving an
earlier ticket's step OUT of `steps.json` is NOT how you remove it -- silence is treated as an
error, and a deterministic gate rejects a file missing any still-live step from the project's
entire history. The ONLY way a step leaves the plan is naming it in `retired_step_ids`. Same
discipline for `manifest.json`'s wireframes/diagrams. Add Risk Notes only for risks this ticket
itself introduces, and add NEW Diagrams/Wireframes only for what this ticket adds or changes --
but every EXISTING one must still be carried forward in the file (or explicitly retired), not
silently dropped just because this ticket doesn't touch it.

Carry-over discipline, gate-checked: a step you restate from the prior plan must keep BOTH its id
and its description byte-for-byte -- any edit to the description makes it a "new/changed" step to
the deterministic gate, which then rejects it if it cites only already-delivered criteria. Never
write a new or changed step whose only cited criteria are already coded and tested (the ledger
stamps them; delivered work is never re-planned). Drop any prior step whose every cited criterion
this ticket's Specification retires -- that feature is removed; name it in `retired_step_ids`.
