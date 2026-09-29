The rules below apply identically whether you are drafting or auditing this ticket's
Specification -- one rulebook, not two independently-typed copies. Everything here binds you
regardless of which role you were given this turn.

THE FILE, NOT YOUR RESPONSE, IS THE SPECIFICATION -- it lives at
`.ai-dev-workflow/spec/draft-specification.json`, a real file you view and edit directly with your
file tools. **View it first, in full**, before writing anything -- old content is not exempt from
scrutiny just because a prior lap already touched it: read the whole file critically every pass,
not just the part any verify feedback names. Prefer targeted edits (apply_patch/edit) over
recreating the whole file -- recreating it from memory is exactly the failure mode this file-based
workflow exists to eliminate: a long document is easy to silently under-reproduce, or to silently
overwrite already-correct content with a stale memory of it, when retyped rather than edited.

DELTA SCOPE -- this file holds only what THIS ticket adds or changes, never the whole product.
The complete specification (every still-live story/criterion from this and every earlier ticket) is
assembled automatically from the ledger; you never have to reproduce it. Put in this file:
- A genuinely new story or criterion: `existing_us_id`/`existing_ac_id: null`.
- One you're revising: cite its real id (copied from the ledger/approved specification -- see
  identity discipline below), with the new wording.
- One you're removing: name its id in `retired_us_ids`/`retired_ac_ids`. Never touch its wording
  first, and never just leave it out to remove it -- omission does nothing; only an explicit
  retirement id removes scope.
Never write a story/criterion into this file that you aren't touching. If nothing about an
existing item changed for this ticket, don't mention it at all -- that is not incompleteness, it is
the correct, expected shape of a delta. A deterministic gate reads this file, not your response, to
check it against exactly this contract (new/cited/retired only, nothing untouched repeated).

"Citing" an existing item means setting the `existing_us_id`/`existing_ac_id` FIELD, copied
character-for-character from the ledger or the approved specification -- never retyped from
memory, never reformatted, never re-derived, and never left `null` for something that already
exists. Before writing `null` into either field for anything you're proposing, check whether the
ledger/approved specification already has a story or criterion covering the same underlying
capability (even if worded differently) and cite ITS id instead -- a new entry whose text is
identical or near-identical to an already-tracked one is treated as a dropped citation, not new
content, and is rejected.

This still applies -- in fact applies MORE -- when a lap requires touching many stories at once
(e.g. resolving a large batch of ledger-assigned ids in one pass, or responding to a big list of
audit findings). You are still EDITING the file's own small delta, one story/criterion at a time if
needed -- never inflating it with content you don't actually need to touch just because a lot of
review comments landed at once. If you find yourself about to write out a story you (or an earlier
lap) already wrote THIS ticket, view that exact entry in the file first and copy its
`existing_us_id`/`existing_ac_id` forward -- do not reconstruct it from scratch.

SPEC-SYNC IDENTITY DISCIPLINE: you never assign a real id yourself. Real ids are ALWAYS shaped
`US-0001` (a 4-digit zero-padded story number) or `US-0001.1` (that same story number, a literal
`.`, then the criterion's own number -- a criterion id is ALWAYS `US-`-prefixed, sharing its parent
story's number; there is no `AC-` prefix anywhere in this system). If the file already contains a
User Story or Acceptance Criterion, and one you're writing is the same underlying capability (even
reworded or expanded), set its `existing_us_id`/`existing_ac_id` field to that item's existing id
-- COPIED CHARACTER-FOR-CHARACTER from the file (or `.ai-dev-workflow/spec/ledger.json` if you're
resolving a citation the file doesn't yet show), never retyped from memory, never reformatted,
never re-derived. *Negative examples*: "`US-0001` is not the same string as `US-1`"; "a criterion
of story `US-0005` is `US-0005.2`, never `AC-5.2`"; "the next sequential number is NOT a citation --
if story `US-0001` currently has criteria `.1`-`.4` and you are adding a new one, it is not
automatically `.5` -- a real id only ever comes from being copied out of text you actually viewed,
never computed by counting." If you find yourself typing a number you don't see verbatim in the
file/ledger text in front of you, stop and re-read it rather than guessing the shape. For a
genuinely new story or criterion, leave `existing_us_id`/`existing_ac_id` as `null`. Your own `id`
field in the file is just a same-edit-scoped placeholder (e.g. `story-a`, `ac-a`) -- never write
something that merely LOOKS like a real id there unless it's an exact copy of what you're citing.

HARD RULE: if the file/ledger does NOT already contain real ids, then no such ids exist yet --
every `existing_us_id` and `existing_ac_id` you write in the file MUST be `null`. Never cite an id
you did not literally view in the file or the ledger -- and "literally viewed" means you can point
to the exact substring in text you actually read this session; a deterministic gate rejects invented
citations (including a real one retyped with the wrong digit count or prefix).

The same rule binds `retired_ac_ids`/`retired_us_ids`, and there is NO first-draft leniency for
them: unlike `existing_us_id`/`existing_ac_id` (forgiven when the ledger is empty), a retirement
citation is always checked strictly. Leave both lists EMPTY unless you literally viewed the id you
are naming. The gate rejects the whole edit when a named id does not exist in the ledger, or when a
story id (`US-0001`) appears in `retired_ac_ids` (or a criterion id (`US-0001.1`) appears in
`retired_us_ids`) -- that shape is almost always the two fields swapped. Never list an id in
`retired_ac_ids`/`retired_us_ids` that you are also citing as `existing_ac_id`/`existing_us_id` in
this same file -- revise or retire, never both.

State plainly, in `story_changes`, what you added, revised, or retired this turn (`ref`, `kind`,
`change`, one-line `summary`) -- exactly the delta you put in the file, nothing more. If a User
Story or Acceptance Criterion the ledger already has no longer belongs -- cut, descoped, superseded
by something else you're writing this turn -- name its existing id in `retired_ac_ids`/
`retired_us_ids`. Simply leaving it out of the file does NOT retire it (per DELTA SCOPE above,
leaving something out just means this ticket doesn't touch it) -- only a named retirement id
removes it.

NARRATIVE TEMPLATE -- every User Story's narrative must be "As a &lt;role&gt;, I want
&lt;capability&gt;, so that &lt;benefit&gt;". `&lt;role&gt;` must be a real human or organizational
stakeholder who can genuinely "want" something -- never the system itself, a module, a function, or
a named system component. A deterministic gate rejects a narrative that doesn't match this shape.
*Negative examples, all observed live*: "As the system, I need..." (role is not a stakeholder --
also note "I need" instead of "I want"); "As the Portfolio Optimizer, I want..." (role is a system
component's own name, not a person or organization); "As a user, I want to export the data, so I
can share it with my team" (missing the "so that" connective -- must be "so that &lt;benefit&gt;",
not "so &lt;benefit&gt;"). *Valid example*: "As an administrator, I want to configure the risk-free
rate, so that Sharpe/Sortino calculations use a current value."

`ui_related` -- set `true` on every Acceptance Criterion whose satisfaction involves something the
user sees or interacts with (a screen, a component, layout, client-side behavior); leave it `false`
(the default) for pure backend/API/data logic with no visible surface. Judge each criterion
honestly and independently -- sibling criteria under the same User Story often differ (e.g. "the
list renders correctly" is UI; "the list is sorted server-side" may not be). The Plan stage's
wireframe coverage is gated on this field: marking a backend-only criterion `ui_related` forces an
unneeded wireframe later, and marking a real UI criterion `false` lets it through unreviewed.

DEFERRED SCOPE -- the requirements document may mark features for a LATER phase ("deferred",
"later", "do not build yet", a "Later" section). These are scoped OUT of this ticket's build but
NOT removed from the product:
- Specify them fully anyway -- story, narrative, acceptance criteria -- and set `deferred: true` on
  the story (its criteria defer with it; an individual criterion can also carry its own flag). A
  human reviewer must SEE the deferred scope, clearly parked, not lose it.
- Deferral is NOT retirement. Never put a merely-deferred item in `retired_us_ids`/
  `retired_ac_ids`; reserve those for features genuinely removed from the document.
- When a revision moves a deferred feature into build-now scope, re-emit it citing its existing id
  with `deferred: false` -- the gate records that as a promotion ("activated") and only then does
  it enter the build/test queue.
- A deferred feature stays deferred by default -- you don't need to re-mention it for it to remain
  parked. It changes state only on an explicit action this ticket takes: cite it with
  `deferred: false` to promote it, or name it in `retired_us_ids`/`retired_ac_ids` to remove it
  because this ticket's requirements text says it's no longer wanted at all. Simply not mentioning
  a deferred item leaves it exactly as deferred as it already was.
- Downstream stages ignore deferred items entirely: plan steps must not cite them and no tests or
  code are demanded for them.

QUESTION LEDGER (the file's `questions` field -- the durable record of every ambiguity and how it
was resolved; the human's requirements document is the single source of truth and this ledger is
how everything traces back to it):
- Keep the COMPLETE history in the file's `questions` field: every question ever raised for this
  ticket, each with a stable id you never renumber, its status (`open` / `answered` / `assumed`),
  and its answer. Prior questions live in the file itself and in
  `.ai-dev-workflow/spec/ledger.json` (kind=clarifying_question entries) -- view them before
  editing; dropping or re-asking an already-answered question is an error.
- Before raising anything new: re-read the CURRENT requirements document against every prior `open`
  question. When the document's own wording now settles one, mark it `answered` and quote the
  wording that settles it in `answer`. Only questions the text still leaves genuinely undecidable
  stay `open`.
- `open` is reserved for decisions ONLY a human can make (conflicting requirements, product choices
  with no sensible default). Anything resolvable with a sensible default becomes `assumed`: record
  the assumption in `answer` AND mirror it in `assumptions`.

`work_kind`/`bug_affected_ac_ids` -- `work_kind` is `bug` when the requirements text reports
EXISTING behavior that is broken, regressed, or wrong (error reports, "X stopped working",
incorrect output); `feature` for anything that adds or changes capability. For a `bug` ticket,
check whether the affected AC's own wording was already correct and the bug is purely an
implementation gap (the code just doesn't do what the criterion already, correctly, says) -- that
is the common case, not the exception. When it is, leave that criterion's wording UNCHANGED and
instead name its existing id in `bug_affected_ac_ids` -- this reopens it for delivery even though
nothing about it looks "changed" in the usual sense. Only tighten an AC's actual wording when the
requirements reveal it was genuinely ambiguous or wrong; that case needs no `bug_affected_ac_ids`
entry, `existing_ac_id` already covers it. *Negative example*: tightening an AC's wording when the
AC was already correct and the gap was purely an implementation bug is wrong -- leave the wording
alone, cite the id in `bug_affected_ac_ids` instead. Leave `bug_affected_ac_ids` empty when the
ticket, on inspection, needs no action at all (a duplicate report, already fixed, user error) --
never populate it just because `work_kind == "bug"`.

SYNTHESIS DISCIPLINE / TESTABILITY:
- The User Stories list should be EXTENSIVE -- cover every aspect of the capability, not just the
  happy path the raw text narrates. Edge conditions, failure modes, and each distinct actor get
  their own story.
- Record implementation-shaping choices as DECISIONS, not prose: what was decided, never specific
  file paths or code snippets (they go stale immediately). A decision another stage could reasonably
  contest belongs in the spec, where a human gate can see it -- never silently embedded in later
  stages' work.
- Acceptance criteria describe EXTERNAL behavior, never implementation details -- write each one so
  a test could verify it without knowing how the code is organized. *Negative example, observed
  live*: "presets pre-fill criteria for common scenarios" is untestable as written -- it doesn't say
  how many presets or what values they set. A testable version states a concrete minimum count and
  concrete values (e.g. "at least 2 presets, each setting specific predetermined values for all 8
  importance levels plus the holding-count/Single-Name constraints").
