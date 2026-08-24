# React + Express Monorepo

## Overview

An all-TypeScript, all-Node monorepo: a React single-page app (built with Vite) calling an Express
API, wired together with npm workspaces so one install and one lockfile cover both apps. Good
default for a small team that wants one language end to end and no framework opinions beyond
"React for UI, Express for HTTP".

## Repository layout

```
repo-root/
├── .gitignore
├── package.json               # npm workspaces root: ["apps/web", "apps/api"]
├── package-lock.json
├── apps/
│   ├── web/                   # React app (Vite)
│   │   ├── src/
│   │   ├── vite.config.ts     # dev-server proxy: /api -> http://localhost:4000
│   │   └── package.json
│   └── api/                   # Express API (TypeScript)
│       ├── src/index.ts
│       ├── tsconfig.json
│       └── package.json
├── .ai-dev-workflow/coverage-commands.json   # registers both workspaces' coverage commands (see Testing)
└── (no Directory.Build.props/ruff.toml/mypy.ini — pure Node stack)
```

## Scaffolding commands

1. `npm create vite@latest apps/web -- --template react-ts`
2. `mkdir -p apps/api/src && cd apps/api && npm init -y`
3. `npm install express`
4. `npm install -D typescript tsx @types/express @types/node`
5. `npx tsc --init --rootDir src --outDir dist`
6. Create `apps/api/src/index.ts`:
   ```ts
   import express from "express";
   const app = express();
   app.use(express.json());
   app.get("/health", (_req, res) => res.json({ status: "ok" }));
   app.listen(4000, () => console.log("api listening on http://localhost:4000"));
   ```
7. At the repo root, create a workspaces `package.json`:
   ```json
   { "name": "react-express-app", "private": true, "workspaces": ["apps/web", "apps/api"] }
   ```
8. Create a root `.gitignore` (Vite's and `npm init`'s own ignores, if any, are per-workspace, not
   for the repo root):
   ```gitignore
   node_modules/
   dist/
   coverage/
   ```
9. `npm install` (run once, from the repo root, to hoist and link both workspaces).
10. In `apps/web/vite.config.ts`, add a dev proxy:
    ```ts
    export default defineConfig({
      server: { proxy: { "/api": { target: "http://localhost:4000", changeOrigin: true } } },
    });
    ```

## Package managers

npm workspaces — one lockfile at the repo root covers both `apps/web` and `apps/api`; run
`npm install` from the root after adding a dependency to either app.

## Build & run commands

**Development**
- API: `npm run dev --workspace apps/api` (e.g. `tsx watch src/index.ts` as the `dev` script) —
  `http://localhost:4000`.
- Web: `npm run dev --workspace apps/web` — Vite dev server on `http://localhost:5173`, proxying
  `/api` calls to the API above.

**Production**
- API: `npm run build --workspace apps/api` (`tsc`) then `node apps/api/dist/index.js`.
- Web: `npm run build --workspace apps/web` — static output in `apps/web/dist`, served by any
  static file host (or `npm run preview --workspace apps/web` on `http://localhost:4173` to
  smoke-test the build).

## Testing

- **API (Vitest)**: `npm install -D vitest supertest @types/supertest --workspace apps/api`. Export
  the Express `app` (don't call `.listen()` at import time) so tests can drive it with
  `supertest(app)`. Add `test: { passWithNoTests: true }` to `apps/api`'s own vitest config -- an
  AC-retirement fallback can legitimately leave a file with a placeholder test only, and without
  this a file Vitest discovers but that registers zero tests is a hard runner error, not a pass.
  Run with `npm run test --workspace apps/api` (`vitest run`).
- **Web (Vitest)**: `npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
  @vitest/coverage-v8 --workspace apps/web`; add
  `test: { environment: "jsdom", globals: true, passWithNoTests: true }` to `vite.config.ts` (same
  reason as the API workspace above). Run with `npm run test --workspace apps/web`.

**Coverage contract**: the two workspaces have different Vitest environments (`jsdom` for the web
app's components, plain Node for the API), so a single un-scoped `vitest run` at the repo root
cannot safely cover both. Register a per-workspace entry in `.ai-dev-workflow/coverage-commands.json`
instead (this pipeline's coverage gate replays this contract INSTEAD of its own js legacy
fallback when present, so register BOTH entries together, never just one):
```json
{
  "entries": [
    {
      "command": "npx vitest run --coverage --coverage.reporter=json-summary",
      "artifact": "apps/web/coverage/coverage-summary.json",
      "format": "istanbul-json-summary",
      "root": "apps/web"
    },
    {
      "command": "npx vitest run --coverage --coverage.reporter=json-summary",
      "artifact": "apps/api/coverage/coverage-summary.json",
      "format": "istanbul-json-summary",
      "root": "apps/api"
    }
  ]
}
```

## Conventions

- React: function components + hooks only, one component per file under `src/components/`,
  page-level components under `src/pages/`, shared API client in `src/lib/api.ts`.
- API: one router per resource under `src/routes/`, request/response types in `src/types.ts`,
  `src/index.ts` limited to app setup and route registration.
- Lint: `eslint --max-warnings=0` + `tsc --noEmit`, enforced identically in both workspaces.

## Stack facts

dotnet_detected: false
dotnet_solution_root: ""
convention_roots: node=
