REPO MEMORY (durable, committed with the pipeline's artifacts):

This repository keeps a long-term memory file at `.ai-dev-workflow/memory.md`. It survives across tickets and sessions because it lives in the repo itself -- the sandbox is rebuilt from scratch every run, so this file is the ONLY place a lesson outlives the container.

- BEFORE starting: read `.ai-dev-workflow/memory.md` if it exists. It records what previous runs learned the hard way about THIS repo -- build quirks, commands that work (and the ones that look right but don't), config traps, flaky areas. Trust it over your assumptions; verify it over trusting it blindly if something looks stale.
- BEFORE finishing: append only DURABLE, repo-specific learnings you paid for this run -- a build/tooling quirk, a command that had to be spelled a particular way, a config trap, a dependency pin that matters. One dated bullet each (`- 2026-08-26: ...`) under a short topic heading. NEVER task narration, nothing the code itself already says, no secrets.
- Keep it small: prune bullets that are stale or duplicated while you're there. If the file is pushing past ~200 lines, consolidate before appending. An overgrown memory file is context nobody can afford to read.
