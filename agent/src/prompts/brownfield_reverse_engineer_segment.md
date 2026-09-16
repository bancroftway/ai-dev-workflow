This is a ONE-TIME baseline pass for a brownfield repository this pipeline has never onboarded
before. There is no human-written requirements document yet -- the grounding context below is
real, deterministically-gathered evidence from the repository itself (schema/migration/route files
and their content), not a request from a person. Read the actual repository (your file tools are
real; use them) and describe its CURRENT, as-built behavior, the same way you would specify or plan
a real ticket, with these differences:

- Every User Story/Acceptance Criterion (or Plan Step) you write describes something that ALREADY
  EXISTS and ALREADY WORKS in this codebase today -- not something to build. Cite concrete evidence
  (a file path, a route handler, a passing test) in the relevant description text; never speculate
  about behavior you cannot actually find in the repository.
- Since nothing has been onboarded before, every id is genuinely new -- there is no prior file/
  ledger content to cite via `existing_us_id`/`existing_ac_id` (or a Plan Step's own stable id
  reused from a previous revision). Leave every citation field `null`/absent, exactly as the real
  first-draft-ever case already works.
- Include an ER diagram (data model) and an architecture diagram (system structure) -- a real
  brownfield repository always has SOMETHING to diagram; do not skip these even for a small repo.
- Confidence matters: when the repository gives you only partial or ambiguous evidence for
  something, say so directly in the story/step's own description text (there is no separate
  confidence field) rather than asserting it as fact.
- This pass has no audit -- what you submit, once it passes verification and a human approves it,
  becomes this project's permanent baseline. Be as thorough and accurate as you would be knowing
  there is no second opinion checking your work afterward.

Everything else -- file-editing discipline, id-citation rules, the metadata-only response shape,
identity/retirement conventions -- is identical to a normal ticket's own drafting rules, because
this genuinely is that same drafting process, just describing what already exists instead of what
a person asked for.
