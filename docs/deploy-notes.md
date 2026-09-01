# Deploy notes

Operator-facing notes that don't belong in an implementation task breakdown
(`docs/superpowers/plans/*.md`) but need to be somewhere a future deploy actually gets read.

## In-flight sessions at deploy time (Phase E audit I-5, Ruling E-2)

**The question.** Part 1's Spec asked explicitly what happens to a session already mid-run when
this branch's provider-unification work ships, and named silence on it "the one option that's
actually wrong." Answered here.

**The fact.** `agent/src/graph.py`'s compiled LangGraph uses `InMemorySaver` (see
`GraphState.provider`'s own comment for the fullest treatment) -- every in-flight run's `stages`,
`run_id`, and pinned `provider` live in the agent process's memory **only**. There is no
process-external checkpoint store. A deploy that restarts the agent process drops all of it, for
every thread that was mid-run, with nothing durable left to resume into. The session's sandbox
container can outlive the restart (it is reaped on its own idle clock, independent of the agent
process), so without any mitigation the user sees nothing wrong immediately -- the board still
shows "in progress" -- until they poke the session and it fails in a confusing, undiagnosable way
on reattach (a fresh `intake_node` re-resolving state that no longer matches what the sandbox's
working tree actually reflects).

**The decision: option (b), accept orphaning, make it legible.** A drain window (stop accepting
new sessions, wait for every in-flight one to finish, then deploy) was considered and rejected: it
needs a "stop accepting new work" flag/mechanism this codebase has no version of today, to buy a
benefit (zero interrupted runs) that isn't actually necessary -- every interrupted run under option
(b) gets a clear, actionable, resubmit-and-retry path instead. That is a smaller, honest trade,
not a shortcut: nothing is being hidden, and nothing about a user's ticket is silently lost forever
(the sandbox's own git branch/work is untouched; only the agent process's in-memory run state is).

**The mitigation.** `agent/src/deploy_drain.py`: one plain DB query --
`SELECT session_id, run_id, current_stage FROM dbo.sessions WHERE status = 'in_progress' AND
(awaiting_gate = 0 OR awaiting_gate IS NULL)` -- finds every session still in flight and NOT
currently paused at a human gate, and marks each `failed` with a plain, user-visible reason
(`"interrupted by deploy -- resubmit to retry"`) via `session_store.close_session` -- so the board
shows a red pill with an actionable message instead of a silently stale "in progress" card.

Gate-paused sessions (`awaiting_gate = 1`) are deliberately excluded: they have a real recovery
path most other in-progress sessions don't (an approved stage's content survives in the sandbox's
own `.ai-dev-workflow/*.approved.json`, re-read on the next intake regardless of whether the
in-memory checkpoint did), so failing a human-waiting queue on every deploy would make the
mitigation worse than the problem for that population. One disclosed exception, not glossed over:
`tech-stack` is the only stage with neither an audit pass nor a `deterministic_verify` check, so a
freshly-LLM-drafted (not hydrated/prefilled) tech-stack draft is never persisted to disk before its
gate pauses -- a restart while specifically THAT gate is paused does lose the pending draft; the
next intake just re-runs detection from scratch. See `deploy_drain.py`'s own module docstring for
the full trace. Every other stage's gate-paused draft is confirmed durable.

**Revision note:** an earlier version of this script read `SandboxProvider.list_active()`, an
in-memory registry scoped to the process that provisioned each sandbox -- structurally unable to
see anything when run as a freshly spawned, separate process after the old agent process had
already exited, which is exactly how a deploy step invokes it. The DB query above needs no
in-process state at all, so there is no special caveat left: `deploy_drain.py --run` is now a
plain, ordinary deploy step.

Run it as a normal, detached step, any time after (or during) a deploy:
`cd agent && uv run python -m src.deploy_drain --run`. `cd agent && uv run python -m src.deploy_drain`
(no `--run`) runs its self-check against a real DB instead (scoped to its own seeded rows, safe to
run against a populated database) -- the safe default, matching every other module in this package.
