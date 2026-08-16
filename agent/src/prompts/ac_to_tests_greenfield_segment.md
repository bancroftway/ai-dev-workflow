This is a greenfield repository: the application itself may not be scaffolded yet (the Plan's
first milestone scaffolds it; that milestone may or may not have run before this stage). You may
create test-project scaffolding under test paths (e.g. a new test project file, a
`playwright.config.ts` -- whose `use` block must set `screenshot: 'on'` so even passing e2e runs
capture visual evidence, a test framework's own config) so the failing tests you write actually
compile -- the write-scope gate allows only test paths, and that allowance covers this. You may
NOT create or edit application/production source code to make a test compile, even a stub --
compile-enabling stubs for not-yet-existing application symbols belong to a later, separate
rebuild-fix stage, never to this one. A test that references a symbol the application doesn't have
yet is expected and correct at this stage.
