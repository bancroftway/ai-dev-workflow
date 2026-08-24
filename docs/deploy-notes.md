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

**The mitigation.** `agent/src/deploy_drain.py`: at the moment before a deploy replaces the agent
process, it walks every sandbox that process still has registered
(`SandboxProvider.list_active()`) and, for whichever of those sessions `dbo.sessions` still calls
`in_progress`, marks it `failed` with a plain, user-visible reason
(`"interrupted by deploy -- resubmit to retry"`) via `session_store.close_session` -- so the board
shows a red pill with an actionable message instead of a silently stale "in progress" card.

**Known limitation, not glossed over.** `SandboxProvider.list_active()` (both the local Docker and
Azure ACI implementations) reports session_ids from an **in-memory** dict scoped to the process
that provisioned them -- it does not query Docker/ACI directly. `deploy_drain.py` can therefore
only ever see what the *current* agent process itself provisioned. Run as a freshly spawned,
separate process **after** the old agent process has already exited (e.g. a naive post-deploy
step), it sees an empty registry and silently drains nothing -- correct behavior for the code as
written, but worthless as a mitigation if invoked that way. To do anything real, it must run
*inside* the process being replaced, e.g. wired into that process's own graceful-shutdown handling
(a SIGTERM/shutdown-event hook). `agent/main.py` has no shutdown hook of any kind today -- wiring
one up is a real follow-up task, deliberately left out of this fix's scope (it touches the FastAPI
app's lifecycle, not just this one script), and is the next thing to build if this mitigation is to
run automatically rather than being invoked by hand immediately before a manual restart.

Until that wiring exists, run it by hand, from inside the process about to be replaced (e.g. a
one-off `docker exec`/shell into the running agent container, not a new deploy step process):
`cd agent && uv run python -m src.deploy_drain --run`. `cd agent && uv run python -m src.deploy_drain`
(no `--run`) runs its offline self-check instead -- the safe default, matching every other module
in this package.
