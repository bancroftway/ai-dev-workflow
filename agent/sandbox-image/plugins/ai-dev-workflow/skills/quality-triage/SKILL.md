---
name: quality-triage
description: Guides triaging static-analysis findings (Roslyn, SonarAnalyzer, dotnet format, jscpd duplication reports, and similar deterministic code-quality scanners) into fix-or-suppress decisions with real justification. Use this skill whenever asked to review, triage, or resolve a list of analyzer/linter findings, decide which warnings to fix vs. suppress, judge whether a duplication report represents real copy-paste debt or acceptable boilerplate, or write a justification for suppressing a code-quality finding. Also trigger on "go through these lint errors," "should we suppress this warning," or "review this SARIF/analyzer report."
---

# Quality Triage

Every finding you triage gets one of two outcomes: fixed, or suppressed with a justification that
someone will read months from now with no memory of this conversation. A weak justification isn't
just unhelpful -- it actively hides a real problem behind a paper trail that looks like due
diligence. Your job is to make each decision defensible on its own, not to clear the list quickly.

## Calibrate severity by what the rule actually protects against

Not every finding deserves the same scrutiny. Weight your fix-vs-suppress instinct by category:

- **Reliability and security-adjacent findings** (null-dereference risk, resource leaks, injection-
  shaped patterns, unhandled exceptions on likely paths): default to fixing. These are the findings
  most likely to represent a real bug, not a style preference.
- **Style and naming findings**: suppression is more defensible here, especially if the pattern is
  pervasive and pre-existing rather than something newly introduced -- a repo-wide naming
  convention that predates this analyzer run isn't a new problem to fix opportunistically, it's an
  existing choice someone already made.

## Recognize false positives without over-suppressing

Some categories of code generate findings that aren't really about code quality at all: generated
code (EF migrations, designer files, source-generator output), test fixtures with intentionally
unusual patterns, and code explicitly scoped by an `.editorconfig`/analyzer config exception. When
a rule is a false positive for a whole *class* of files, prefer a global exclusion (an
`.editorconfig` scope, a `GlobalSuppressions.cs` entry, a build-config `NoWarn`) over repeating the
same per-line pragma dozens of times -- but still record *why* that class-level exclusion exists,
the same as you would for any other suppression. A silent, unexplained global exclusion is worse
than a single unexplained pragma, because it silences an entire category of future findings too.

## Judging duplication findings

Distinguish accidental copy-paste (fix: extract the shared logic) from duplication that's actually
fine -- generated DTOs, parallel test fixtures that are duplicated *on purpose* for readability,
boilerplate where extracting a shared abstraction would increase coupling more than it reduces
repetition. Ask: would extracting this actually make the code easier to maintain, or would it just
introduce an indirection that makes two unrelated concerns depend on the same helper? If it's the
latter, suppression is the right call, not a workaround.

## What separates a justified suppression from a rubber stamp

A justification is real when it states a *specific* reason tied to this particular finding: why
the pattern the rule is checking for doesn't apply here, or why the cost of fixing it right now
outweighs the benefit, with enough detail that someone unfamiliar with this code could evaluate
whether they agree. Reject your own justification if it would fit unchanged onto a completely
different finding -- "not important," "will fix later," "known issue" apply to literally anything
and prove nothing about *this* finding specifically. A justification under about 15 words with no
rule-specific reasoning is a rubber stamp, not a decision.

## Blast radius matters

A suppression scoped to one line in one file is a contained decision. A suppression that disables
a rule repo-wide, or for an entire directory, affects code you haven't looked at and code that
doesn't exist yet. Flag anything at that scope explicitly as needing separate, deliberate sign-off
beyond the normal triage flow -- don't let a repo-wide suppression ride through on the same
one-line justification that would be fine for a single finding.

## Reporting your findings

For every finding: your decision (fix or suppress), your justification (specific, not generic),
and if you're suppressing, the exact marker text to insert (matching whatever convention the
finding's tool uses) including a reference back to this triage decision so it's traceable later.
