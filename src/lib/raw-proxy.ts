/**
 * Builds a URL for src/app/api/github/raw/route.ts (the hardened repo-content proxy) -- shared by
 * AppShell's live Report tab and the past-session report page so the query-param shape (and its
 * `.ai-dev-workflow/`-relative path convention) has exactly one definition.
 *
 * `ref` is this session's own work_branch (agent/src/branch_naming.py) -- screenshots/reports now
 * live on a per-session branch, not one repo-shared `ai-dev-workflow` branch, so there is no
 * longer a default to fall back to; every caller must resolve and pass its own.
 */
export function rawProxyUrl(owner: string, repo: string, path: string, ref: string): string {
  const params = new URLSearchParams({ owner, repo, path, ref });
  return `/api/github/raw?${params.toString()}`;
}
