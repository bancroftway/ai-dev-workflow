import { defineConfig, type Project } from '@playwright/test';

// Test personas + local IdP are injected by the pipeline at run time (AIDW_TEST_USERS is a JSON
// array of { name, email, roles }; AIDW_IDP_URL is the fake identity provider). When present, one
// project per persona loads that persona's saved storageState (a global-setup logs each in once);
// otherwise a single anonymous project runs, exactly as before.
const personas: Array<{ name: string; email?: string; roles?: string[] }> = (() => {
  try {
    return JSON.parse(process.env.AIDW_TEST_USERS ?? '[]');
  } catch {
    return [];
  }
})();

const slug = (p: { email?: string; name: string }) =>
  (p.email || p.name).replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').toLowerCase();

const projects: Project[] =
  personas.length > 0
    ? personas.map((p) => ({
        name: slug(p),
        use: { storageState: `test-results/.auth/${slug(p)}.json` },
      }))
    : [{ name: 'default' }];

export default defineConfig({
  testDir: './tests/e2e',
  // Logs each persona in through the IdP and saves storageState. No-op (file may be absent) when
  // no personas are configured, so it is safe to reference unconditionally only when personas exist.
  ...(personas.length > 0 ? { globalSetup: './tests/e2e/aidw.global-setup.ts' } : {}),
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:3000',
    screenshot: 'on',
  },
  projects,
});
