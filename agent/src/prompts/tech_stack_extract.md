You are extracting structured data from an already human-approved Tech Stack document. Do not
explore the repository -- you have no tools for that and shouldn't need any: everything you need
is in the markdown text given to you.

Extract the TechStack schema faithfully from that text:

- `summary`: one or two sentences describing the stack at a glance, drawn from the text.
- `languages`, `frameworks`, `package_managers`, `testing_frameworks`, `conventions`: lists, taken
  directly from what the text states. Leave a list empty if the text doesn't mention that category
  -- never invent an entry to fill a category.
- `dotnet_detected`/`dotnet_solution_root`: set only when the text actually describes a .NET
  solution and states (or clearly implies) where its root lives. Otherwise `dotnet_detected` is
  false and `dotnet_solution_root` is null.
- `convention_roots`: only include a key when the text states a confident root directory for that
  ecosystem (`node`, `python`). Omit ecosystems the text doesn't mention.
- `conventions_applied`: always leave empty. That field is populated later, by deterministic code
  after this stage approves, not by you.

Do not add facts the text doesn't state. A field the text is silent on should be empty/false/null,
not guessed. This is a straight extraction task, not an analysis task.
