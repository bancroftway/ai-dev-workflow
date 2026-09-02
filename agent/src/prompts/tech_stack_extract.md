You are extracting structured data from an already human-approved Tech Stack document. Do not
explore the repository -- you have no tools for that and shouldn't need any: everything you need
is in the markdown text given to you.

Extract the TechStack schema faithfully from that text. Every field below carries an explicit
present/absent (or detected/not_detected) status -- report the honest status the text supports,
never invent an entry just to make a category present.

- `summary`: one or two sentences describing the stack at a glance, drawn from the text.
- `languages`, `frameworks`, `package_managers`, `testing_frameworks`, `conventions`: each is a
  `status`/`values`/`reason` object, not a bare list. Where the text lists items under that
  category's heading, that's `status: "present"` with those items as `values`. Where it instead
  shows a reason sentence (or "(not checked)"), that's `status: "absent"` with that sentence copied
  into `reason` -- `reason` is required whenever `status` is `"absent"`.
- `dotnet`: one object, not two separate fields. "Detected. Solution root: `X`" means
  `status: "detected"`, `solution_root: "X"`. "Detected, but no confident solution root." means
  `status: "detected"`, `solution_root: null`, and `reason` set to that sentence (required whenever
  `solution_root` is null). Anything else under the `.NET` heading means `status: "not_detected"`
  with that text copied into `reason`.
- `convention_roots`: a list of per-ecosystem entries (one for `node`, one for `python`), not a
  dict keyed by ecosystem. A bullet like `` `node`: `path` `` (or "(repository root)" for `""`)
  means `status: "present"`, `root: "path"` (or `""`); a bullet giving a reason instead of a path
  means `status: "absent"` with that reason. If the text has no such section at all, leave
  `convention_roots` empty.
- `auth_kind`: read the sentence naming the detected auth (e.g. "Detected auth: **entra**") and
  report that exact value -- one of `entra`, `google`, `generic-oidc`, `custom`, `none`.
- `config_inventory`: a `status`/`values`/`reason` object, extracted the same way as the other
  present/absent fields above -- the listed config keys if present, else the reason text (or
  "(not checked)").
- `conventions_applied`: always leave empty. That field is populated later, by deterministic code
  after this stage approves, not by you.

Do not add facts the text doesn't state. A field the text is silent on should be reported as
absent/not_detected with an honest reason, not guessed. This is a straight extraction task, not an
analysis task.
