/**
 * Builds a URL for src/app/api/github/raw/route.ts (the hardened repo-content proxy) -- shared by
 * AppShell's live Report tab and the past-session report page so the query-param shape (and its
 * `.ai-dev-workflow/`-relative path convention) has exactly one definition.
 */
export function rawProxyUrl(owner: string, repo: string, path: string): string {
  const params = new URLSearchParams({ owner, repo, path });
  return `/api/github/raw?${params.toString()}`;
}
