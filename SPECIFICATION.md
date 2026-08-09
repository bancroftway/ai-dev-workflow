# AI-Assisted Specification & Planning System — Product Specification

## 0. How to read this document

This document describes a system to be built. It is written for an AI code assistant that will implement the system from scratch, targeting a **specific, mandated technology stack** — spelled out in full in Section 3 — rather than an arbitrary one. Outside of that stack mandate, the document still avoids prescribing internal implementation patterns (module layout, naming, specific function signatures, exact file structure, etc.), so that the functional intent is unambiguous while leaving ordinary engineering decisions to the implementer.

Every requirement below should be treated as testable: read each Acceptance Criterion as something a reviewer could check off against a running implementation.

---

## 1. Purpose

A human has a software idea, expressed as free-form, informal text. This system turns that idea into two successive, human-approved artifacts:

1. A **Specification** — a structured breakdown of the idea into user stories and acceptance criteria.
2. An **Implementation Plan** — a structured, ordered breakdown of concrete steps to build what the Specification describes.

An AI performs the drafting at each stage. A human reviews and either approves each stage or sends it back for revision. Nothing proceeds to the next stage without explicit human approval of the current one.

The system deliberately keeps the human's job simple: there is exactly **one** place to type, and **one** decision to make at each review point. It does not ask the human to fill out forms, answer structured questionnaires, or edit AI-generated content directly.

---

## 2. Actors

| Actor | Description |
|---|---|
| **Human Operator** | The single user of the system in a given working session. Provides the initial idea, answers clarifying questions (by editing their own text, not by filling out a form), and approves or declines each stage. |
| **Specification Agent** | An AI-driven capability that reads the human's current requirements text and produces a Specification, or, if it judges the text insufficient, a set of clarifying questions instead of (or alongside) a draft. |
| **Planning Agent** | An AI-driven capability that reads an *approved* Specification and produces an Implementation Plan, or clarifying questions if it judges the Specification insufficient to plan from. |

There is no concept of multiple simultaneous human users, roles, or permissions. This is a single-operator tool.

---

## 3. Required Technology Stack and Protocols (Non-Negotiable)

Unlike the rest of this document, the following constraints are **mandatory** — specific, named technology choices, not implementation details left open for reinterpretation. An implementation that substitutes a different framework, language, or library for any of these does not satisfy this specification, regardless of how well it satisfies the functional requirements in Section 6 onward.

### 3.1 Frontend: React + CopilotKit

- The frontend MUST be a **React** application.
- The frontend's integration of AG-UI (streaming, human-in-the-loop resolution) and its rendering of AI-authored A2UI content MUST use **CopilotKit's React SDK** (its core provider/hooks package and its UI/rendering package). A hand-rolled AG-UI or A2UI protocol client is not acceptable — the explicit intent, carried over from the system's original design brief, is to use CopilotKit's existing, maintained React integration for all protocol plumbing rather than reimplementing it.
- The human-in-the-loop approval mechanism (Section 6, Gates) MUST be implemented using CopilotKit's human-in-the-loop mechanism for rendering and resolving a paused agent turn — not a bespoke polling or custom endpoint scheme layered alongside AG-UI.

### 3.2 Backend: Python + LangGraph

- The backend MUST be written in **Python**.
- The two-stage agent workflow described in Section 5 (Specification stage, then Plan stage, each with its own Drafting / Needs Clarification / Ready for Review / Approved lifecycle) MUST be implemented as a **LangGraph** graph. LangGraph's graph-based state machine — nodes, edges, conditional routing, and its native support for interrupting execution to await external input — is the required mechanism for realizing:
  - a node (or nodes) that perform Specification drafting, and a separate node (or nodes) that perform Plan drafting;
  - conditional routing out of each drafting node based on that turn's Readiness self-assessment (to a human-in-the-loop interrupt awaiting review, versus back to drafting if the turn already answered its own open questions);
  - an interrupt point at each Gate (Section 6, Section 7 BR-4) where the graph pauses awaiting the human's Approve decision;
  - the edge from an approved Specification into the start of Plan drafting (US-4);
  - the edge that a requirements revision (US-6) always takes back to the start of Specification drafting, regardless of which stage or which interrupt the revision was submitted from;
  - the per-stage revision-round counters backing the safety cap (US-10), carried as part of the graph's state.
- The backend MUST expose the LangGraph graph to the frontend over the AG-UI protocol using CopilotKit's Python SDK and its LangGraph integration, rather than a hand-built AG-UI server implementation. This is the same "use the existing library, don't reimplement the protocol" principle as Section 3.1, applied to the Python side.
- The recommended way to bootstrap the project's initial structure is the official AG-UI scaffolding tool targeting a Python/LangGraph backend (equivalent to running `npx create-ag-ui-app@latest --langgraph-py`), rather than assembling the AG-UI server, the LangGraph graph, and the CopilotKit integration by hand from separate starting points.

### 3.3 Protocols

- **AG-UI Protocol.** All communication between the backend (where the LangGraph graph runs) and the frontend (where the human interacts) MUST use the AG-UI protocol (the "Agent-User Interaction Protocol"). This is the mechanism by which: the AI's streamed output reaches the human in real time; the human-in-the-loop approval/clarification pause-and-resume mechanism (Section 6) is realized, by the backend surfacing a paused graph interrupt as a structured request over AG-UI and the frontend returning the human's decision the same way.

- **A2UI Protocol.** The read-only Specification and Plan content shown to the human for review (Section 8) MUST be authored by the LangGraph node performing that stage's drafting, using the A2UI protocol's UI-description vocabulary — not hard-coded or templated by the React application. The frontend's job is to render whatever A2UI content the graph produces, generically, without knowing in advance what a "specification" or "plan" looks like. If the AI-authored content fails to conform to the A2UI protocol, the application MUST fall back to a minimal, generic error display rather than crashing or hard-coding a substitute rendering of the actual content.

- **Implementation guidance for the coding assistant.** LangGraph has no native A2UI support, so before writing any AG-UI/A2UI wiring code, the implementing coding assistant SHOULD load the `ag-ui-a2ui-integration` skill (published at `github.com/ag-ui-protocol/ag-ui`, under `skills/ag-ui-a2ui-integration`) — it covers the LangGraph framework adapter, transport setup, runtime wiring, and verification steps for exactly this integration — and, since this system renders through CopilotKit (Section 3.1), the corresponding CopilotKit rendering skill referenced from that same guide (covering runtime, provider, and catalog conventions). Load these before modifying the application, not as an afterthought.

### 3.4 Model Provider: GitHub Copilot SDK

- The language model driving the Specification Agent, the Planning Agent, and every future stage (Section 5.1) MUST be accessed through the **GitHub Copilot SDK**. Azure OpenAI, or any other model provider accessed directly, MUST NOT be used.
- LangGraph has no built-in GitHub Copilot integration, so the backend MUST provide a thin adapter presenting GitHub Copilot as a standard chat model to LangGraph/LangChain — everything else in the graph (tool calls, streaming, state, checkpointing) then works unmodified, unaware of which model provider sits underneath. The expected shape of this adapter: a custom chat-model class that, internally, drives a GitHub Copilot SDK session — creating the session, sending it the prompt, consuming the session's streamed events until it reports it has finished responding, and translating the resulting assistant message into the standard chat-message shape the rest of the graph expects. This adapter is the one piece of this system with no pre-built library to lean on; everything else in Section 3 still applies (don't hand-roll AG-UI, A2UI, or the LangGraph orchestration itself — only this one model-provider bridge is bespoke, by necessity).
- GitHub Copilot MUST run in a fully autonomous ("full authority") mode: it MUST NOT pause execution to request human permission before taking an action or invoking a tool. Any confirmation/permission-prompting behavior the SDK might otherwise surface MUST be disabled or configured away. The only points at which this system ever pauses for human input are the Gates explicitly defined in Section 5/6 — the model provider itself must never introduce a separate, additional approval interruption outside of those (see also Section 7, BR-6).

### 3.5 Persistence and Memory (Backend)

- **Thread-level (within-session) persistence** — the LangGraph checkpointing that lets a single working session pause at a Gate or a clarification point and later resume exactly where it left off — MUST use LangGraph's in-memory checkpointer (`InMemorySaver` / `MemorySaver`) for now. A durable, restart-surviving checkpointer is a planned future replacement, not part of this specification (consistent with Section 9's note that nothing is expected to survive beyond the current working session yet).
- **Cross-thread, long-term memory** — infrastructure for information that could eventually be recalled across separate working sessions — MUST use LangGraph's in-memory store (`InMemoryStore`) for now, on the same basis: no functional requirement in this specification currently depends on it, but the graph MUST be wired to a store of this kind from the start so a durable, cross-session store can be substituted later without restructuring the graph.
- **Checkpoint durability mode** MUST be the asynchronous mode: each checkpoint is persisted in the background while the graph proceeds to its next step, rather than blocking on the write before continuing. This is a deliberate trade-off — it accepts a small window in which a crash could lose only the very last checkpoint write, in exchange for not stalling the workflow on every single step.

Every other requirement in this document — the domain model, the workflow lifecycle, the user stories and acceptance criteria — describes required *behavior*, not implementation structure, and should be read as realizable in whatever way fits naturally within the mandated stack above, so long as that behavior holds.

---

## 4. Core Concepts (Domain Model)

These are the concepts the system operates on. They are described structurally, not as a data schema for any particular language.

- **Raw Requirements Text** — a single block of free-form text. This is the *only* piece of information the Human Operator directly edits, from the very first submission through every later revision. It always represents the complete, current statement of what is wanted — never a diff, a patch, or an answer to a single isolated question.

- **Clarifying Question** — something the Specification Agent or Planning Agent asks because it judges the Raw Requirements Text (or, for the Planning Agent, the approved Specification) insufficient to proceed confidently. Each has:
  - a short identifier, stable within the turn that produced it
  - the question text
  - optionally, a small set of suggested answer choices (offered as guidance to the human, not as a constraint the human's eventual answer must match)

- **Specification** — the structured output of the Specification Agent, once it judges it has enough information. Consists of:
  - a title and a short summary
  - a list of **User Stories**, each with: a stable identifier, a title, a narrative in the form "As a `<role>`, I want `<capability>`, so that `<benefit>`", and a list of **Acceptance Criteria**
  - each **Acceptance Criterion**: a stable identifier (scoped to its parent User Story) and a description of one specific, testable condition and its expected outcome
  - a list of stated **Assumptions**
  - a list of items explicitly marked **Out of Scope**

- **Implementation Plan** — the structured output of the Planning Agent, once it judges it has enough information from an approved Specification. Consists of:
  - an overview
  - an ordered list of **Plan Steps**, each with a stable identifier and a description of one concrete action; a step's description should reference the identifier(s) of any Acceptance Criteria it fulfills, wherever that traceability is meaningful
  - a list of **Risk Notes**

- **Readiness** — a self-assessment, produced by the Specification Agent or Planning Agent alongside its draft output, of whether that output is complete enough to be worth presenting to the human for an approve/revise decision. This is advisory to the *system* (it gates whether an approval action is offered at all — see Section 6) but never overrides the human's own judgment about whether to actually approve.

---

## 5. Workflow Overview

This specification currently defines **two** sequential stages: **Specification**, then **Plan**. The Plan stage cannot begin until the Specification stage has been approved.

### 5.1 This is a growing pipeline — build for extension

Two stages is the current, minimum-viable scope, not the system's permanent shape. The stage sequence is known in advance to grow to roughly ten stages, continuing on from an approved Plan with further sequential, gated stages such as (informationally, in expected order, and not yet specified in Section 6): generating a failing test suite, performing a clean rebuild, generating the minimum implementation code needed to make those tests pass, a code quality scan, a code security scan, and a final adversarial code review and refactoring pass.

None of those later stages carry acceptance criteria in this document yet — they are out of scope for what is being built right now (see Section 9). They are named here only so the graph structure (Section 3.2) is built to append further sequential Drafting → Gate stages after Plan without restructuring the Specification and Plan stages already specified. Concretely: do not hard-code "there are exactly two stages" anywhere the stage count would be awkward to change later — the addition of a third stage, and later a fourth, and so on, should be a matter of adding another node-and-gate segment to the graph, not a rewrite.

Each stage independently moves through the same lifecycle:

```
Not Started → Drafting → (Needs Clarification ⇄ Drafting)* → Ready for Review → Approved
```

- **Not Started**: no draft exists yet for this stage. (The Plan stage stays in this state, and is not reachable, until the Specification stage reaches Approved.)
- **Drafting**: the responsible Agent is producing (or re-producing) output from its current input.
- **Needs Clarification**: the Agent's latest output was not marked Ready — it included Clarifying Questions instead. The stage returns to Drafting once the human resubmits.
- **Ready for Review**: the Agent's latest output was marked Ready. The human may now approve it.
- **Approved**: the human has explicitly approved this stage's latest output. For the Specification stage, this immediately triggers the Plan stage to begin Drafting. For the Plan stage, this completes the workflow.

A stage can cycle through Drafting ⇄ Needs Clarification any number of times before reaching Ready for Review, and a stage that has already reached Ready for Review or Approved can be pushed back to Drafting by a new revision (Section 7).

**Mapping to the required backend (Section 3.2):** this lifecycle is the LangGraph graph's own shape, not a separate abstraction layered on top of it. Not Started / Drafting map onto the graph's node structure and current execution position; Needs Clarification and Ready for Review map onto conditional routing decisions made after a drafting node runs, based on that turn's Readiness; the transition out of Ready for Review into Approved is a human-in-the-loop interrupt that the graph pauses on until the frontend resolves it; and a revision's cascade back to the very start of Specification drafting (US-6) is itself a graph edge, taken unconditionally regardless of which interrupt was open when the revision arrived.

---

## 6. Functional Requirements

Each requirement below is a user story followed by its acceptance criteria.

### US-1 — Submit initial requirements

*As the Human Operator, I want to type my software idea as free-form text and submit it, so that the system can begin drafting a Specification.*

**Acceptance Criteria**
- AC-1.1: The system presents exactly one text input for requirements, empty by default, accepting arbitrary free-form text.
- AC-1.2: A submit action is available and is only usable when the text field is non-empty (after trimming leading/trailing whitespace).
- AC-1.3: Submitting sends the current, complete text of the field to the Specification Agent as the entire basis for its first draft — no other input accompanies this submission.
- AC-1.4: While a submission is being processed, the submit action is disabled or otherwise prevented from being triggered again, so that a second, overlapping submission cannot be created by the same input.

### US-2 — Receive and resolve clarifying questions

*As the Human Operator, I want to see what the AI needs to know before it can produce a usable draft, and to answer by editing my own requirements text, so that I never have to fill out a separate form.*

**Acceptance Criteria**
- AC-2.1: When the currently active stage (Specification or Plan) produces Clarifying Questions instead of (or alongside) a not-yet-ready draft, those questions are displayed to the Human Operator alongside the requirements text input — not as a separate page, modal, or structured answer form.
- AC-2.2: Each displayed question shows its question text and, if provided, its suggested answer choices, for the human's reference only.
- AC-2.3: There is no dedicated input field per question. The human answers by editing the Raw Requirements Text itself (e.g., adding or revising a sentence that addresses the question) and resubmitting.
- AC-2.4: Resubmitting after a set of Clarifying Questions re-invokes the Specification stage (see US-6 for why it is always the Specification stage, even if the Plan stage asked the question).
- AC-2.5: A stage may produce more than one round of Clarifying Questions in sequence; the system places no artificial limit on how many rounds of genuine clarification can occur before Section 6's safety cap (US-10) is reached.

### US-3 — Review and approve a Specification

*As the Human Operator, I want to review a completed draft Specification and decide whether to accept it, so that work only proceeds on a Specification I've signed off on.*

**Acceptance Criteria**
- AC-3.1: Once the Specification stage reaches Ready for Review, its content (title, summary, user stories with acceptance criteria, assumptions, out-of-scope items) becomes available for the human to review, distinct from the requirements-editing area.
- AC-3.2: An approval action becomes available only once the Specification stage is in Ready for Review. It is not available while the stage is in Drafting or Needs Clarification.
- AC-3.3: Approving records the currently displayed Specification as the Approved one and immediately begins the Plan stage's first Drafting pass, seeded from this approved Specification.
- AC-3.4: The human's only alternative to approving is to return to the requirements text and submit a revision (US-6). There is no "request changes with notes" action distinct from editing the requirements text.
- AC-3.5: Specification content is presented read-only; the human cannot directly type into or otherwise edit the displayed Specification.

### US-4 — Automatic hand-off from Specification to Plan

*As the Human Operator, I don't want to manually kick off planning — once I approve the Specification, planning should just start.*

**Acceptance Criteria**
- AC-4.1: Approving the Specification (AC-3.3) requires no separate action to begin Plan drafting; it starts as a direct consequence of the approval.
- AC-4.2: The Planning Agent's first-ever draft for a given approved Specification receives that Specification's full structured content (all user stories with their acceptance criteria, assumptions, and out-of-scope items) as its only input — not a free-text summary or the original raw requirements text.

### US-5 — Review and approve an Implementation Plan

*As the Human Operator, I want to review a completed draft Plan and decide whether to accept it, so that the workflow only completes on a Plan I've signed off on.*

**Acceptance Criteria**
- AC-5.1: Once the Plan stage reaches Ready for Review, its content (overview, ordered steps, risk notes) becomes available for the human to review, distinct from the requirements-editing area and from the Specification review area.
- AC-5.2: An approval action becomes available only once the Plan stage is in Ready for Review.
- AC-5.3: Approving the Plan completes the workflow — there is no further stage.
- AC-5.4: As with the Specification, the human's only alternative to approving the Plan is returning to the requirements text and submitting a revision. Plan content is read-only.

### US-6 — Revise requirements at any point, with a full cascade

*As the Human Operator, I want to be able to go back and edit my original requirements text at any time — even after a Specification or Plan already exists — and have the system figure out what needs to be redone, so that I never have to manually reset anything.*

**Acceptance Criteria**
- AC-6.1: The Raw Requirements Text field remains editable at all times, regardless of what state the Specification or Plan stage is in (including after either has reached Approved).
- AC-6.2: Submitting an edited requirements text always re-invokes the Specification stage from the current, complete requirements text — never the Plan stage directly, and never a "partial" or "delta" re-invocation.
- AC-6.3: If the Plan stage had already reached Ready for Review or Approved at the time of a new requirements submission, it is reset to Not Started and is not reachable again until the Specification stage is re-approved.
- AC-6.4: If the Specification stage had already reached Approved at the time of a new requirements submission, that approval is superseded — the newly drafted Specification requires its own fresh approval before the Plan stage can proceed, even if its content turns out to be unchanged from the previously approved version.
- AC-6.5: This cascade behavior applies uniformly no matter which stage's Clarifying Questions prompted the edit — a question raised by the Planning Agent is still answered by editing requirements text and still restarts at the Specification stage.

### US-7 — Preserve identity across revisions

*As the Human Operator making a small edit, I don't want the whole Specification or Plan rewritten from scratch — I want the parts that are still valid to keep their identity and wording, so I can tell at a glance what actually changed.*

**Acceptance Criteria**
- AC-7.1: When the Specification Agent redrafts after a revision, any User Story or Acceptance Criterion whose meaning is unaffected by the edit keeps the exact same identifier it had before, even if minor wording changes.
- AC-7.2: A newly introduced requirement in a revision is given a new identifier that has not been used before within that Specification — never a reused or recycled identifier from something previously removed.
- AC-7.3: A User Story or Acceptance Criterion that no longer applies after a revision is simply omitted from the new Specification; its identifier is retired, not reassigned to something unrelated.
- AC-7.4: The same stability behavior (AC-7.1–7.3) applies to Plan Steps in relation to whatever (possibly revised) Specification they are drafted from.
- AC-7.5: To make this possible, the Specification Agent must have access to its own immediately preceding output (if any) when redrafting, and the Planning Agent must have access to its own immediately preceding output (if any) when redrafting — this continuity is required infrastructure, not an optional nicety.

### US-8 — Review surfaces reflect workflow state

*As the Human Operator, I want to be able to tell at a glance whether the Specification and Plan are ready to look at, so I don't waste time checking on drafts that don't exist yet.*

**Acceptance Criteria**
- AC-8.1: The system presents three distinct named views: one for editing requirements and seeing clarifying questions (always available), one for reviewing the Specification, and one for reviewing the Plan.
- AC-8.2: The Specification review view is inaccessible (visibly disabled, not merely empty) until the Specification stage has reached Ready for Review at least once.
- AC-8.3: The Plan review view is inaccessible (visibly disabled, not merely empty) until the Plan stage has reached Ready for Review at least once — which, by construction (US-4), cannot happen before the Specification has been approved at least once.
- AC-8.4: Once a review view has become accessible, it remains accessible (showing the latest available content for that stage) even if a later requirements revision has since invalidated its approval — see US-9 for what it shows in that situation.

### US-9 — Don't lose visible content while a revision is in flight

*As the Human Operator who just submitted a revision, I want to keep seeing whatever Specification or Plan content was already generated while the new draft is being produced, so the screen doesn't go blank on me.*

**Acceptance Criteria**
- AC-9.1: Submitting a revision does not clear or blank out previously displayed Specification or Plan content; that content remains visible until it is replaced by newly generated content for the same stage.
- AC-9.2: The system gives some indication, distinguishable to the human, that a new draft is being generated (as opposed to the currently visible content being the latest, final word).

### US-10 — Bounded clarification loop

*As the system, I need a way to guarantee forward progress even if a stage keeps asking for clarification, so that the workflow can never get stuck forever.*

**Acceptance Criteria**
- AC-10.1: Each stage (Specification, Plan) tracks how many Drafting-to-Needs-Clarification-to-resubmission cycles have occurred since it last reached Approved (or since the workflow began, for the first cycle).
- AC-10.2: A configurable maximum number of such cycles exists per stage.
- AC-10.3: If a stage's redraft is still not marked Ready after reaching this maximum, the system proceeds as though the human had approved the latest draft anyway, moving the stage to Approved (and, for the Specification stage, triggering the Plan stage per US-4) rather than continuing to loop indefinitely.

### US-11 — The human's approval is always the final word

*As the Human Operator, I want the AI's own "I think this is ready" self-assessment to be advisory only — I make the actual approval decision.*

**Acceptance Criteria**
- AC-11.1: Reaching Ready for Review never auto-approves a stage; it only makes the approval action available.
- AC-11.2: The human may approve a Ready-for-Review draft even if they consider it imperfect — the system does not second-guess or block a human approval decision.
- AC-11.3: The only exception to human-initiated approval is the bounded safety cap (US-10), which is a system-level guarantee against infinite loops, not a judgment about draft quality.

---

## 7. Business Rules & Invariants

These hold at all times and are not tied to a single user story above.

- **BR-1**: The Raw Requirements Text is the single source of truth for both the Specification and the Plan. Neither the Specification nor the Plan can be edited directly by the human; the only lever the human has is editing this one field.
- **BR-2**: The Plan stage's input is always the current *approved* Specification's full structured content — never the raw requirements text directly, and never an unapproved draft Specification.
- **BR-3**: A stage's Clarifying Questions and Readiness self-assessment are properties of one specific draft (one Drafting attempt), not of the stage as a whole. A later, different draft for the same stage may have entirely different questions, or none.
- **BR-4**: Approval, once given for a stage, is not retroactively invalidated by anything except a new requirements revision (US-6). Simply viewing other tabs, or the passage of time, does not un-approve anything.
- **BR-5**: There is never more than one Raw Requirements Text, one current Specification draft, and one current Plan draft in play at a time. This is a single-threaded workflow per working session — there is no branching, no parallel drafts, no comparison-of-alternatives feature.
- **BR-6**: The Gates defined in Section 5/6 are the *only* points at which this system pauses for human input. The underlying model provider (Section 3.4) must never surface its own, separate permission or confirmation prompt for an action or tool call — doing so would violate the "one decision to make at each review point" principle from Section 1 by introducing an approval interruption outside this system's own design.
- **BR-7**: The number of stages is not fixed at two. Additional sequential, gated stages are expected to be appended after Plan over time (Section 5.1); nothing in this specification should be read as limiting the workflow to exactly Specification and Plan forever.

---

## 8. Content Presentation Requirements

- The Specification review view and the Plan review view each display their respective content read-only, authored by the responsible Agent via the A2UI protocol (Section 3) — the application itself defines no fixed template for "what a specification looks like" or "what a plan looks like" beyond the structural data model in Section 4.
- The approval action (Approve) for whichever stage currently has one available is presented to the human in a way that does not require them to be on that stage's specific review view to find it — e.g., it should be reachable from a persistent, always-visible area of the interface, not buried inside a tab the human might not currently have open.
- Clarifying Questions (US-2) are presented in the requirements-editing view, not inside the Specification or Plan review views, regardless of which stage produced them.

---

## 9. Explicitly Out of Scope

The following are deliberately not part of this system. An implementation should not add them speculatively:

- Direct editing of Specification or Plan content by the human (beyond approve/revise-via-requirements).
- A structured, per-clarifying-question answer form or a separate "revision notes" field distinct from the requirements text.
- Persistence of the Raw Requirements Text, Specification, or Plan beyond the current working session (e.g., across a browser refresh, application restart, or on a different device). Losing in-progress work on session end is an accepted limitation.
- Multiple simultaneous human users, roles, permissions, or any concept of authentication/authorization.
- Any stage beyond the Plan (e.g., generating tests, generating implementation code, running quality/security scans, adversarial review). The workflow's terminal state, *for the functional requirements defined in this document*, is an Approved Plan — even though the architecture must not preclude appending further stages later (Section 5.1, BR-7). Do not build any of those anticipated later stages now; only build for the graph shape not resisting their later addition.
- Version history or the ability to browse/restore earlier Specification or Plan drafts once superseded.
- File, image, or other attachment upload as part of the requirements input.
- Real-time collaboration or simultaneous editing by more than one person.
- Any AI self-override of a human approval decision, or any AI action that bypasses the human review gates described in Section 6.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| Raw Requirements Text | The one free-form text field the human edits; the single source of truth for the whole workflow. |
| Specification Agent | The AI capability that drafts a Specification from Raw Requirements Text. |
| Planning Agent | The AI capability that drafts an Implementation Plan from an approved Specification. |
| Specification | Title, summary, User Stories (with Acceptance Criteria), Assumptions, Out-of-Scope items. |
| Implementation Plan | Overview, ordered Plan Steps, Risk Notes. |
| Clarifying Question | A question an Agent raises when it judges its input insufficient to draft confidently. |
| Readiness | An Agent's own self-assessment of whether its latest draft is complete enough for human review. |
| Stage | One phase of the workflow's pipeline — currently "Specification" and "Plan" (Section 6), with further stages expected to be appended over time (Section 5.1). |
| Gate | The point at which a stage, once Ready for Review, awaits the human's Approve decision. |
| Revision | Any resubmission of edited Raw Requirements Text, whether prompted by a Clarifying Question or made unprompted after a stage was already Approved. |
| Cascade | The rule that any revision always restarts at the Specification stage and invalidates any further-along approval state (US-6). |

---

## 11. End-to-End Verification Scenarios

An implementation should be checked against at least these scenarios.

**Scenario A — Golden path, no clarification needed**
1. Submit a sufficiently detailed requirements text.
2. Specification stage reaches Ready for Review with no Clarifying Questions. Specification review view becomes accessible.
3. Approve the Specification. Plan stage begins drafting automatically.
4. Plan stage reaches Ready for Review with no Clarifying Questions. Plan review view becomes accessible.
5. Approve the Plan. Workflow is complete.

**Scenario B — Clarification round on the Specification**
1. Submit a deliberately vague requirements text.
2. Specification stage returns Clarifying Questions instead of a Ready draft. Questions appear in the requirements view.
3. Edit the requirements text to address the questions and resubmit.
4. Specification stage redrafts; previously-unaffected content (if any existed) keeps its identifiers; the redraft reaches Ready for Review (or asks a further, narrower round of questions — repeat as needed).
5. Approve; continue as in Scenario A from step 3.

**Scenario C — Revision after Plan already exists**
1. Complete Scenario A through an Approved Plan.
2. Edit the Raw Requirements Text to change something and resubmit.
3. Plan review view becomes inaccessible again (reset to Not Started); Specification stage redrafts from the edited text.
4. Confirm that any User Story/Acceptance Criterion unaffected by the edit retains its prior identifier and wording.
5. Approve the (re-)drafted Specification; confirm the Plan stage redrafts automatically, and that any Plan Step unaffected by the underlying Specification change retains its prior identifier and wording.
6. Approve the Plan; workflow is complete again.

**Scenario D — Safety cap**
1. Configure a small maximum clarification-round count for a stage.
2. Repeatedly submit requirements edits that keep provoking Clarifying Questions, without ever fully resolving them, until the configured maximum is reached.
3. Confirm the stage proceeds to Approved automatically on the next redraft after the cap is hit, without requiring an explicit human Approve action.
