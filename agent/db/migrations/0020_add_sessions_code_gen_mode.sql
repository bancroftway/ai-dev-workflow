-- Part 2 of the plan (Task 1, "backend mode threading"): a per-session code generation mode --
-- yolo / draft_verify / mission_critical -- that a later task uses to make a stage's audit/verify
-- steps skippable in the LangGraph pipeline (graph.py's GraphState.code_gen_mode), and a further
-- later task exposes via a frontend popup. Mirrors 0008_add_sessions_provider.sql's own shape
-- exactly: a nullable, write-once-per-session column with a CHECK constraint over the closed
-- vocabulary.
--
-- NULL means "provisioned before this migration, or the frontend didn't ask" -- read by
-- graph.py's _resolve_thread_code_gen_mode as "no pinned value, fall back to the resolve-fallback
-- default" (today's actual, unconditional behavior for every existing session -- audit AND full
-- verify both always on -- which is what "mission_critical" means under this plan's new
-- vocabulary), never as an implicit "yolo" default. This column's job is only to remember what a
-- PRIOR provision actually used, once one has happened -- exactly the same contract `provider`
-- already has (0008_add_sessions_provider.sql).
ALTER TABLE dbo.sessions
  ADD code_gen_mode NVARCHAR(20) NULL
        CONSTRAINT CK_sessions_code_gen_mode
        CHECK (code_gen_mode IN ('yolo','draft_verify','mission_critical'));
