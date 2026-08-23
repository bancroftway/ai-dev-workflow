-- dbo.projects (agent/src/project_store.py) -- the grouping key above dbo.sessions that Part 3
-- (docs/superpowers/plans/part-3-tickets-tasks.md) calls a "project." Ruling 1: a "ticket" is NOT
-- a new table -- one dbo.sessions row already == one LangGraph thread == one full pipeline run,
-- exactly what the product calls a ticket. Only the grouping concept above it was missing.
--
-- owner/repo nullable until scaffolded (Ruling 2): the "+ New Project" path creates this row from
-- just a name + tech-stack choice, before any GitHub repo exists -- set_project_repo backfills
-- both once Task 3's scaffolding actually creates and clones the repo, the same
-- populate-now-backfill-later idiom sessions_api.py's own provision_session already uses for
-- dbo.sessions.title. The filtered unique index (SQL Server partial index) stops
-- Connect-a-Repository from double-creating a project for a repo that's already connected, without
-- colliding with the many simultaneously-NULL not-yet-scaffolded new-project rows.
CREATE TABLE dbo.projects (
    project_id      UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    name            NVARCHAR(200)    NOT NULL,
    owner           NVARCHAR(255)    NULL,   -- NULL until the repo exists (new-project path);
                                              -- set immediately (connect-repo path)
    repo            NVARCHAR(255)    NULL,
    tech_stack_id   NVARCHAR(100)    NULL,   -- catalog id (app_discovery.load_stack_catalog) or
                                              -- NULL for free-text / brownfield-detected
    tech_stack_text NVARCHAR(MAX)    NULL,
    created_by      NVARCHAR(255)    NOT NULL,
    created_at      DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE UNIQUE INDEX UX_projects_owner_repo ON dbo.projects(owner, repo)
    WHERE owner IS NOT NULL AND repo IS NOT NULL;

-- Every session/ticket belongs to exactly one project from here on (Ruling 1). NULL stays valid at
-- the schema level only for rows that predate this migration; every session_store.create_session
-- call after this ships passes a real project_id (enforced in Python as a required kwarg, not by a
-- NOT NULL column, so existing rows aren't retroactively broken).
ALTER TABLE dbo.sessions ADD project_id UNIQUEIDENTIFIER NULL REFERENCES dbo.projects(project_id);

-- Mirrors IX_sessions_repo_recent's shape -- the board's (Task 9) project-scoped listing query
-- (GET /sessions?owner=&repo=&project_id=), same "most recent first" access pattern.
CREATE INDEX IX_sessions_project ON dbo.sessions(project_id, started_at DESC);

-- awaiting_gate: verified genuinely missing before adding, not speculative. Traced graph.py's
-- interrupt()-based gate mechanism (make_gate_node/gate_node) against session_store.
-- update_current_stage's own call site (_run_post_approve_hook) and found current_stage cannot
-- carry this signal on its own:
--   - update_current_stage(session_id, stage_spec.key) only ever runs AFTER a stage's approval
--     resolves (gate_node post-interrupt-resume, auto_approve_node, or make_draft_node's hydrate
--     short-circuit -- graph.py's _run_post_approve_hook, the one choke point all three share).
--   - So while stage X is either still drafting OR paused at its own gate awaiting a human,
--     current_stage still reads whatever the PREVIOUS stage's key was in both cases -- there is
--     nothing in dbo.sessions today that tells those two states apart, not even which one X is in.
--   - LangGraph's own bookkeeping of "this thread is currently sitting inside an interrupt()" lives
--     only in the compiled graph's InMemorySaver checkpointer (graph.py, in-process, per the
--     comments at intake_node re: run_id/provider not surviving a restart) -- not durable, not
--     visible to a separate process, and gone entirely after a redeploy. The board's GET /sessions
--     is a plain DB read (Ruling 5: polling, no live subscription), so it has no access to that
--     in-memory state even when it happens to be correct.
-- Conclusion: a new durable column is genuinely needed. Set to 1 immediately before gate_node's
-- interrupt() call actually pauses (session_store.set_awaiting_gate), cleared back to 0
-- unconditionally by update_current_stage's same UPDATE (and by touch_run on a resume, so a
-- process restart while paused can't leave a stale 1 behind once that session is next touched).
-- NULL (via this ALTER's default-free ADD) only ever describes a pre-migration row that predates
-- the column; every session created after this ships gets a real 0/1 from create_session onward
-- (implicitly 0/NULL at INSERT time -- see session_store.create_session, unset until the first
-- gate pause).
ALTER TABLE dbo.sessions ADD awaiting_gate BIT NULL;
