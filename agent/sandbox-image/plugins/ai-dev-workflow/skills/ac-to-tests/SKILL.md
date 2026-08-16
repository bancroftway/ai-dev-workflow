---
name: ac-to-tests
description: Guides converting acceptance criteria into real, provably-failing tests -- choosing the right test kind (unit, integration, or end-to-end/Playwright) per criterion, writing tests that actually exercise the behavior instead of trivially passing, and embedding the acceptance-criteria ID directly in each test's name for traceability. Use this skill whenever asked to write tests from acceptance criteria or a specification, to convert user stories into a test suite, to set up RED tests before implementation (TDD red phase), or to decide whether a given requirement needs a unit test, an integration test, or a browser/E2E test. Also trigger on "turn these ACs into tests," "write failing tests for this spec," or "what kind of test proves this criterion."
---

# AC to Tests

A test that exists only to satisfy a checklist is worse than no test at all -- it creates false
confidence. Your job is to write tests that would actually catch a regression in the behavior an
acceptance criterion describes, choosing the right level to test at, and to make sure whoever
reads the test suite later can trace every acceptance criterion to the test(s) that prove it.

## Choose the test kind the criterion actually needs -- don't default to one level

- **Unit test**: the criterion describes a self-contained piece of logic -- a calculation, a
  validation rule, a state transition -- that doesn't need a database, network, or UI to verify.
  If you can construct the inputs and assert the output in-process, this is a unit test.
- **Integration test**: the criterion depends on how multiple components actually work together --
  a database write followed by a read, an API endpoint's full request/response cycle, a message
  queue interaction. If mocking out the collaborators would mean you're no longer testing the
  actual integration the criterion cares about, this needs to be an integration test instead.
- **End-to-end / Playwright skeleton**: the criterion describes something a *user* does through
  the UI -- clicks, navigation, what they see on screen. No amount of unit or integration testing
  proves a user can actually do this; only driving the real UI does. When you're not sure whether
  an AC is UI-relevant, ask: "would this criterion be false if the button were simply missing from
  the page, even though the underlying API worked correctly?" If yes, it needs an E2E test.

An acceptance criterion sometimes needs more than one test kind -- e.g. a validation rule tested
at the unit level, plus an integration test proving the API actually returns the right error when
that validation fails. Don't force everything into exactly one test.

## Write tests that would actually fail if the behavior were wrong or missing

Before implementation exists, every test you write for a not-yet-built criterion must fail --
that's what proves the test is actually checking something, not just passing by accident because
it never really exercised the code path. A test that asserts `true === true`, or that only checks
a function doesn't throw without checking what it returns, or that mocks away the exact behavior
the criterion is about, isn't proving the criterion at all. Ask yourself: "if someone implemented
this criterion completely wrong, or not at all, would this test actually catch it?" If the answer
is no, the test isn't done yet, regardless of whether it currently passes or fails.

## Embed the acceptance-criteria ID in every test name

This is what makes a later traceability report (which AC has which tests, which commit
implemented it) possible without manual bookkeeping -- do this for every test you write, not just
the "important" ones:

- **Identifier-based test frameworks (xUnit, NUnit, JUnit, etc.)**: since identifiers can't contain
  `-` or `.`, build the test name as `Test_AC_{us_digits}_{ac_digits}_{PascalCaseSlug}`. An
  acceptance criterion id of `AC-0007.2` becomes `Test_AC_0007_2_UserCanResetPasswordByEmail`.
- **String-title test frameworks (Vitest, Jest, Playwright, etc.)**: prefix the test's title
  string with the id in brackets exactly as written, e.g.
  `test("[AC-0007.2] user can reset password by email", ...)`.

If a single test genuinely proves more than one acceptance criterion, pick the primary one for the
test name and note the others in a comment -- don't invent a combined id that isn't a real
identifier.

## Write negative, edge, adversarial, and validator tests -- not just the happy path

A suite that only proves a criterion works when everything goes right hasn't proven the criterion
is safe -- it's proven the demo works. For every criterion that has a wrong-input path, a boundary,
a trust boundary, or a validator/guard, add the tests below in whichever kind (unit, integration,
e2e) actually exercises that behavior, alongside the happy-path test, not instead of it:

- **Negative tests** (invalid input, unauthorized/forbidden access, wrong state): assert the
  SPECIFIC rejection, never just that something failed.
  - xUnit/.NET: `Assert.Throws<ValidationException>(...)` and assert on `.Message`/`.ErrorCode`, or
    for an API integration test, assert `response.StatusCode == HttpStatusCode.Forbidden` plus the
    response body's error code -- not just that the call didn't return 200.
  - vitest/JS-TS: `expect(() => parse(input)).toThrow(/must be positive/)`, or for an endpoint,
    `expect(response.status).toBe(401)` together with `expect(body.error).toBe("unauthorized")`.
  - pytest/python: `with pytest.raises(ValidationError, match="must be positive"): ...`, or
    `assert response.status_code == 403 and response.json()["error"] == "forbidden"`.
- **Edge/boundary tests** (empty, null, max/min, off-by-one, unicode/whitespace, duplicates): assert
  the actual boundary behavior on both sides, e.g. a 101-character input is rejected and a
  100-character input is accepted -- one side alone doesn't prove where the line is.
  - xUnit/.NET: `[Theory] [InlineData(100, true)] [InlineData(101, false)]` against one length guard.
  - vitest/JS-TS: `expect(isValidLength("a".repeat(100))).toBe(true)` and the 101-char call `.toBe(false)`.
  - pytest/python: `assert is_valid_length("a" * 100) is True` and `assert is_valid_length("a" * 101) is False`.
- **Adversarial tests** (wherever the AC touches a trust boundary): feed injection payloads (a
  `' OR '1'='1` string, a `<script>` tag), oversized/malformed payloads, and path-traversal strings
  (`../../etc/passwd`), and assert the system neutralizes or rejects them (escaped output, a
  400/422 response, no traversal outside the intended root) -- not merely that the process didn't
  crash.
  - xUnit/.NET: post `"'; DROP TABLE Users;--"` and assert `response.StatusCode == HttpStatusCode.UnprocessableEntity` plus the row count is unchanged.
  - vitest/JS-TS: `expect(render({name: "<script>alert(1)</script>"})).not.toContain("<script>")`.
  - pytest/python: `assert "etc/passwd" not in resolve_path("../../etc/passwd")` (resolves inside the sandbox root).
- **Validator-targeting tests**: for every validator/guard the AC implies, write one case just
  inside its boundary that must pass and one just outside it that must fail -- a min-length-3 rule
  needs a 2-character case that fails and a 3-character case that passes, not two cases that both
  sit comfortably on one side.
  - xUnit/.NET: `[InlineData("ab", false)] [InlineData("abc", true)]` against the same `MinLength(3)` guard.
  - vitest/JS-TS: `expect(validate("ab")).toBe(false)` paired with `expect(validate("abc")).toBe(true)`.
  - pytest/python: `assert validate("ab") is False` paired with `assert validate("abc") is True`.

**Distinct observable behavior** means each test's assertion would fail for a genuinely different
reason than any other test's -- a different status code, a different error type or message, a
different persisted or returned value. If two tests would both go red for the exact same underlying
bug, one of them isn't earning its place.

Anti-patterns that look like coverage but aren't:
- Asserting only that "an error happened" -- a bare `toThrow()` or `Assert.ThrowsAny<Exception>()`
  with no check on which error or why passes for the right bug and every wrong one alike.
- Restating one boundary check in five disguises -- an empty string, a whitespace-only string,
  `null`, `undefined`, and a tab character all asserting the identical "field required" error is one
  behavior proven five times, not five behaviors. Keep the ones that are genuinely distinct code
  paths (empty vs. whitespace-only sometimes are) and drop the rest.
- A validator test that only probes deep inside or deep outside the boundary -- it can't distinguish
  a correct implementation from one that's off by one.

## Playwright configs must match the sandbox image's own browser build

Any `playwright.config.*` you author must set `use: { screenshot: 'on' }` -- the later e2e stage
harvests every test's screenshot (not just failures') into the run's report, and a config that
only captures on failure silently drops passing-test evidence. Also pin `@playwright/test` to
exactly `1.63.0-alpha-2026-08-05` (the sandbox image's own `PLAYWRIGHT_VERSION`,
agent/sandbox-image/Dockerfile) in package.json -- a different version's test runner is not
guaranteed compatible with the browser build already baked into this image.

## Reporting your findings

For each acceptance criterion you're given, report: which test kind(s) you chose and why, the
test file(s) and test name(s) you wrote (matching the naming convention above), and if you
deliberately skipped a criterion (e.g. it's not yet testable for a stated reason), say so
explicitly rather than silently omitting it.
