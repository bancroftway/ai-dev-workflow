# Vue + .NET Web API Monorepo

## Overview

A Vue 3 single-page app (built with Vite) talking to an ASP.NET Core Web API, both under `apps/`
in one repository. Vue owns the UI (Composition API + `<script setup>`); the API owns data access
and business logic over HTTP/JSON. Good default for a team that wants Vue's approachable
template/reactivity model paired with a strongly-typed .NET backend.

## Repository layout

```
repo-root/
├── apps/
│   ├── web/                  # Vue app (Vite)
│   │   ├── src/
│   │   ├── vite.config.ts    # dev-server proxy: /api -> http://localhost:5080
│   │   └── package.json
│   └── api/                  # ASP.NET Core Web API
│       ├── Program.cs
│       ├── Controllers/
│       └── Api.csproj
├── apps/api.Tests/           # xUnit test project for the API
├── VueDotnetApp.sln
└── Directory.Build.props     # written automatically once this stack is detected
```

## Scaffolding commands

1. `npm create vite@latest apps/web -- --template vue-ts`
2. `dotnet new webapi -o apps/api -n Api`
3. `dotnet new sln -n VueDotnetApp`
4. `dotnet sln VueDotnetApp.sln add apps/api/Api.csproj`
5. In `apps/web/vite.config.ts`, add a dev proxy:
   ```ts
   export default defineConfig({
     server: { proxy: { "/api": { target: "http://localhost:5080", changeOrigin: true } } },
   });
   ```

## Package managers

- `apps/web`: npm (Vite's default; `package-lock.json` committed).
- `apps/api`: NuGet, via the `.csproj`'s `<PackageReference>` items.

## Build & run commands

**Development**
- API: `dotnet run --project apps/api` — Kestrel on `http://localhost:5080`.
- Web: `cd apps/web && npm install && npm run dev` — Vite dev server on `http://localhost:5173`,
  proxying `/api` calls to the API above.

**Production**
- API: `dotnet publish apps/api -c Release -o out/api && dotnet out/api/Api.dll`
- Web: `cd apps/web && npm run build` — static output in `apps/web/dist`, served by any static file
  host (or `npm run preview` locally on `http://localhost:4173` to smoke-test the build).

## Testing

- **API (xUnit + coverlet)**: `dotnet new xunit -o apps/api.Tests -n Api.Tests`, then
  `dotnet add apps/api.Tests reference apps/api/Api.csproj`, `dotnet add apps/api.Tests package
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with
  `dotnet test`. Coverage (matches this pipeline's coverage gate exactly):
  `dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura
  /p:CoverletOutput=./TestResults/coverage.cobertura.xml`.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @vue/test-utils jsdom
  @vitest/coverage-v8`; add `test: { environment: "jsdom", globals: true }` to `vite.config.ts`.
  Run with `npx vitest run`, coverage with `npx vitest run --coverage`.

## Conventions

- Vue: Composition API with `<script setup lang="ts">` only (no Options API), one component per
  file under `src/components/`, routed pages under `src/pages/` (or `src/views/`), shared API
  client in `src/lib/api.ts`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: `eslint --max-warnings=0` + `vue-tsc --noEmit` on the web app; the API's analyzer warnings
  are build errors via `Directory.Build.props`.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps/api"
convention_roots: node=apps/web
