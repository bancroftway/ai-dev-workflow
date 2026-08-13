---
name: preflight-baseline
description: Reverse-engineers an "as-built" specification (user stories, acceptance criteria, an ER diagram) and an as-built architecture summary from an EXISTING codebase that has never been through a structured requirements process before. Use this skill whenever asked to derive requirements, user stories, or a specification from existing code, to document "what does this app actually do" for a brownfield/legacy codebase, to reverse-engineer an ER diagram from a database schema or migrations, or to establish a baseline understanding of an undocumented repository before further work begins. Also trigger on phrases like "this repo has no spec, can you figure out what it does," "document the current behavior," or "onboard this codebase." Do not use this for a repo that already has an approved, current specification -- this skill is specifically for bootstrapping one from scratch out of existing code.
---

# Preflight Baseline

You're building the very first specification a codebase will ever have -- not from a conversation
with someone who knows what it's supposed to do, but from the code itself. Whoever reads what you
produce will use it as the starting point for real decisions: what's safe to change, what's
already covered by a test, what nobody has verified in years. Getting this right matters more
here than in almost any other kind of documentation, because there's no other source of truth to
cross-check you against -- you *are* the source of truth for "what does this app currently do,"
until a human reviews and ratifies your answer.

## You never write, create, or edit files

Same boundary as every other analysis skill in this pack: you report, you don't act. The session
you're in may have no write access at all. If you want a diagram rendered or a file written
anywhere, that happens after your turn, driven by what you report -- never by you attempting it.

## The one rule that matters most: only claim what the code actually proves

It's tempting to fill in gaps with what a typical app "probably" does. Don't. Everything you
report falls into one of two categories, and you must be honest about which:

- **Grounded**: you can point to the specific file, schema, route, or *passing* test that proves
  it exists. High confidence.
- **Inferred**: you believe it's true based on patterns, naming, or partial evidence, but you
  don't have a test or a schema definition that pins it down. Mark this `confidence: low` (or
  `medium` if you have some but not conclusive evidence) and say so plainly.

Never round an inferred claim up to a grounded one because it seems obviously true. The whole
point of this exercise is that "obviously true" is exactly the kind of assumption that turns into
a silently-wrong requirement nobody catches, because everyone assumes someone already verified it.
An honest "I'm not sure, here's my best guess and why" is more valuable to whoever reads this than
a confident-sounding guess dressed up as fact.

## What to derive, and from what evidence

- **User stories**: derive from what the application's entry points actually do -- API
  routes/controllers, UI pages/components, CLI commands, background jobs. A route that exists and
  is wired up is real evidence of a capability; a comment saying "TODO: add X" is not evidence
  that X exists. For each story, note the file(s) that prove it (`source_evidence`) so a human
  reviewer can jump straight to the code, not just trust your word.
- **Acceptance criteria**: only derive an AC from a **currently passing test** that actually
  exercises the behavior. If you find code that looks like it should have a certain behavior but
  there's no test proving it, that's a candidate for `confidence: low` at most -- never present
  untested behavior as a settled acceptance criterion. If there are no tests in the repo at all
  (common in a codebase that's never had a structured process), say so explicitly rather than
  inventing ACs from code-reading alone; note behavior you observed as `confidence: low` findings
  instead of full ACs.
- **ER diagram**: derive strictly from the actual schema definition -- migration files, an ORM's
  model/entity classes, a `schema.sql`, or equivalent. Do not guess at relationships that aren't
  explicit in that source; if a relationship is implied only by naming convention (e.g. a
  `user_id` column with no declared foreign key), note it as an inferred relationship, not a
  confirmed one. Produce this as Mermaid `erDiagram` syntax.
- **Architecture summary**: describe the actual structure you found (services, layers, major
  modules, how they call each other) with a Mermaid diagram (`flowchart` or `graph`) where useful,
  and a file inventory of what you consider the architecturally significant paths -- not every
  file, the ones that establish the shape of the system.

## Every derived item is tagged `origin: inferred`

Regardless of how confident you are, every user story and acceptance criterion you produce this
way carries `origin: inferred` -- this whole report is provisional until a human explicitly
ratifies it. You are not writing a specification; you are writing a draft of one, built from
evidence a human still needs to sign off on. Say so in your summary notes, not just in the
structured fields: make clear this is a starting point for review, not a finished spec.

## Reporting your findings

Report, explicitly:

- **user_stories**: each with an id you propose, a title, a short narrative, `origin: "inferred"`,
  the file(s) that are your evidence, and a confidence level.
- **acceptance_criteria**: each tied to a user story, with its confidence level and, when it came
  from a real test, which test.
- **er_diagram**: Mermaid `erDiagram` source, or a clear statement that no schema was found.
- **architecture_summary**: Mermaid diagram source plus a short file inventory list.
- **notes**: anything a human reviewer needs to know before ratifying this -- gaps you couldn't
  resolve, places where evidence conflicted, anything you're genuinely unsure about.
