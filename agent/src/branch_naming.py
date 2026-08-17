"""The one place the per-session work-branch name format exists.

Computed once (sessions_api.provision_session, at session creation) and stored in
dbo.sessions.work_branch -- every other consumer (git_ops.py, entrypoint.sh via the WORK_BRANCH
container env, the frontend via GET /sessions/{id}) reads that stored value rather than
recomputing this format itself.
"""

from __future__ import annotations


def work_branch_for(session_id: str) -> str:
    """ai-dev-workflow/<session_id> -- the "ai-dev-workflow/" prefix marks it as tool-created;
    session_id (a UUID) makes it unique per session, restoring "exactly one writer per branch"
    (WS0's single shared `ai-dev-workflow` branch gave that up; this reinstates it)."""
    return f"ai-dev-workflow/{session_id}"


def _demo() -> None:
    assert work_branch_for("abc-123") == "ai-dev-workflow/abc-123"
    print("branch_naming self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.branch_naming
    _demo()
