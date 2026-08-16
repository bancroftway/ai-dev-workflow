# Angular + .NET Web API Monorepo

## Overview

A single-page Angular frontend backed by an ASP.NET Core Web API, both living in one repository
under `apps/`. Angular owns the UI and calls the API over HTTP/JSON; the API owns data access and
business logic. Good default when the team wants a mature, batteries-included frontend framework
(routing, forms, DI all built in) paired with a strongly-typed .NET backend.

## Repository layout

```
repo-root/
├── apps/
│   ├── web/                  # Angular app (Angular CLI workspace)
│   │   ├── src/
│   │   ├── proxy.conf.json   # dev-server proxy: /api -> http://localhost:5080
│   │   ├── angular.json
│   │   └── package.json
│   └── api/                  # ASP.NET Core Web API
│       ├── Program.cs
│       ├── Controllers/
│       └── Api.csproj
├── apps/api.Tests/           # xUnit test project for the API
├── AngularDotnetApp.sln
└── Directory.Build.props     # written automatically once this stack is detected
```

## Scaffolding commands

1. `npx -p @angular/cli@latest ng new web --directory apps/web --routing --style=scss --skip-git`
2. `dotnet new webapi -o apps/api -n Api`
3. `dotnet new sln -n AngularDotnetApp`
4. `dotnet sln AngularDotnetApp.sln add apps/api/Api.csproj`
5. In `apps/web/src/environments/`, point the API base URL at `/api` (dev) and the real API origin
   (prod); add `apps/web/proxy.conf.json`:
   ```json
   { "/api": { "target": "http://localhost:5080", "secure": false, "pathRewrite": { "^/api": "" } } }
   ```
6. In `apps/web/angular.json`, set the `serve` target's `options.proxyConfig` to
   `"apps/web/proxy.conf.json"`.

## Package managers

- `apps/web`: npm (Angular CLI's default; the generated `package-lock.json` is committed).
- `apps/api`: NuGet, via the `.csproj`'s `<PackageReference>` items.

## Build & run commands

**Development**
- API: `dotnet run --project apps/api` — Kestrel on `http://localhost:5080` (set in
  `apps/api/Properties/launchSettings.json`).
- Web: `cd apps/web && npm start` — Angular dev server on `http://localhost:4200`, proxying `/api`
  calls to the API above.

**Production**
- API: `dotnet publish apps/api -c Release -o out/api && dotnet out/api/Api.dll`
- Web: `cd apps/web && npm run build` — static output in `apps/web/dist/web/browser`, served by any
  static file host (or by the API itself via `UseStaticFiles`/`MapFallbackToFile` if deploying
  same-origin).

## Testing

- **API (xUnit + coverlet)**: `dotnet new xunit -o apps/api.Tests -n Api.Tests`, then
  `dotnet add apps/api.Tests reference apps/api/Api.csproj`, `dotnet add apps/api.Tests package
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with
  `dotnet test`. Coverage (matches this pipeline's coverage gate exactly):
  `dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura
  /p:CoverletOutput=./TestResults/coverage.cobertura.xml`.
- **Web (Vitest)**: install with `cd apps/web && npm install -D vitest @analogjs/vite-plugin-angular
  @analogjs/platform jsdom @vitest/coverage-v8`, add a `vite.config.ts` using the `angular()` plugin
  with `test: { globals: true, environment: "jsdom", setupFiles: ["src/test-setup.ts"] }`, and a
  `src/test-setup.ts` that initializes Angular's TestBed environment. Run with `npx vitest run`,
  coverage with `npx vitest run --coverage`.

## Conventions

- Angular: standalone components, one component per file (`*.component.ts` + `.html` + `.scss`),
  feature folders under `src/app/features/<feature>/`, shared UI in `src/app/shared/`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: Angular's built-in ESLint config (`ng lint`, or `eslint --max-warnings=0` if this pipeline's
  own config applies); the API's analyzer warnings are build errors via `Directory.Build.props`.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps/api"
convention_roots: node=apps/web
