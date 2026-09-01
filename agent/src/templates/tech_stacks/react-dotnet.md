# React + .NET Web API Monorepo

## Overview

A React single-page app (built with Vite) talking to an ASP.NET Core Web API, both under `apps/`
in one repository. React owns the UI; the API owns data access and business logic over HTTP/JSON.
Good default for a team that wants a lightweight, unopinionated frontend build (Vite + React,
no framework-imposed structure) paired with a strongly-typed .NET backend.

## Repository layout

```
repo-root/
├── .gitignore
├── apps/
│   ├── web/                   # React app (Vite)
│   │   ├── src/
│   │   ├── vite.config.ts     # dev-server proxy: /api -> http://localhost:5080
│   │   └── package.json
│   ├── api/                   # ASP.NET Core Web API
│   │   ├── Program.cs
│   │   ├── Controllers/
│   │   └── Api.csproj
│   ├── api.Tests/              # xUnit test project for the API
│   └── Directory.Build.props   # written automatically once this stack is detected -- lives at
│                                # apps/, the common ancestor of Api.csproj AND Api.Tests.csproj,
│                                # so MSBuild's upward walk finds it from both
├── ReactDotnetApp.sln
└── .ai-dev-workflow/coverage-commands.json   # registers both ecosystems' coverage commands (see Testing)
```

## Scaffolding commands

1. `npm create vite@latest apps/web -- --template react-ts`
2. `dotnet new webapi -o apps/api -n Api --use-controllers` (`--use-controllers` opts into the
   `Controllers/` layout this file documents -- the template defaults to Minimal APIs otherwise)
3. `dotnet new sln -n ReactDotnetApp`
4. `dotnet sln ReactDotnetApp.sln add apps/api/Api.csproj`
5. Create a root `.gitignore` (neither `dotnet new` nor Vite's own `.gitignore` covers the *other*
   stack's artifacts, and nothing generates one for the repo root):
   ```gitignore
   node_modules/
   dist/
   coverage/
   bin/
   obj/
   TestResults/
   *.user
   ```
6. In `apps/web/vite.config.ts`, add a dev proxy:
   ```ts
   export default defineConfig({
     // process.env, not a literal: the e2e harness starts the API on a port it verifies is
     // free and exports API_BASE_URL with the real value. A hardcoded 5080 proxies to
     // nothing the moment that port is taken, and the UI reports it cannot reach the API
     // on an app that is entirely correct.
     server: { proxy: { "/api": { target: process.env.API_BASE_URL || "http://localhost:5080", changeOrigin: true } } },
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
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with `dotnet test`.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @testing-library/react
  @testing-library/jest-dom jsdom @vitest/coverage-v8`; add `test: { environment: "jsdom",
  globals: true, passWithNoTests: true }` to `vite.config.ts` -- the last option matters: an
  AC-retirement fallback can legitimately leave a file with a placeholder test only, and without it
  a file Vitest discovers but that registers zero tests is a hard runner error, not a pass.
  Run with `npx vitest run`.

**Coverage contract** (this pipeline's coverage gate replays `.ai-dev-workflow/coverage-commands.json`
when present, INSTEAD of its own dotnet/js legacy fallback -- a partial contract silently exempts
whichever ecosystem it omits from the 95% threshold, so register BOTH entries together, never just
one):
```json
{
  "entries": [
    {
      "command": "dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura /p:CoverletOutput=./TestResults/coverage.cobertura.xml",
      "artifact": "TestResults/coverage.cobertura.xml",
      "format": "cobertura",
      "root": ""
    },
    {
      "command": "npx vitest run --coverage --coverage.reporter=json-summary",
      "artifact": "apps/web/coverage/coverage-summary.json",
      "format": "istanbul-json-summary",
      "root": "apps/web"
    }
  ]
}
```

## Conventions

- React: function components + hooks only, one component per file under `src/components/`,
  page-level components under `src/pages/`, shared API client in `src/lib/api.ts`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: `eslint --max-warnings=0` + `tsc --noEmit` on the web app; the API's analyzer warnings are
  build errors via `Directory.Build.props`.

## Observability

Both apps ship OpenTelemetry from the first commit, Console exporter by default (the pipeline's
coverage gate verifies the packages are present; respect `OTEL_EXPORTER_OTLP_ENDPOINT` /
`OTEL_TRACES_EXPORTER=none` when set, the same convention as everywhere else in this pipeline).

- **API**: `dotnet add apps/api package OpenTelemetry.Extensions.Hosting`, `dotnet add apps/api
  package OpenTelemetry.Instrumentation.AspNetCore`, `dotnet add apps/api package
  OpenTelemetry.Exporter.Console`, then in `Program.cs`:
  ```csharp
  builder.Services.AddOpenTelemetry()
      .ConfigureResource(r => r.AddService("api"))
      .WithTracing(t => t.AddAspNetCoreInstrumentation().AddConsoleExporter());
  ```
- **Web**: `cd apps/web && npm install @opentelemetry/sdk-trace-web @opentelemetry/context-zone`,
  then initialize in `src/main.tsx`:
  ```ts
  import { WebTracerProvider, SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-web";
  const provider = new WebTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
  });
  provider.register();
  ```

## Authentication

Only when this run carries the enterprise authentication requirement (the pipeline injects it,
with the anonymous-route allowlist, when the repo's settings demand it):

- **API**: `dotnet add apps/api package Microsoft.Identity.Web`; in `Program.cs` bind the standard
  section — env vars `AzureAd__ClientId` / `AzureAd__ClientSecret` / `AzureAd__TenantId` arrive at
  runtime, never hardcode values:
  ```csharp
  builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
      .AddMicrosoftIdentityWebApi(builder.Configuration.GetSection("AzureAd"));
  builder.Services.AddAuthorizationBuilder()
      .SetFallbackPolicy(new AuthorizationPolicyBuilder().RequireAuthenticatedUser().Build());
  app.UseAuthentication();
  app.UseAuthorization();
  ```
  The fallback policy is the point: every endpoint requires auth unless it opts out with
  `[AllowAnonymous]` — and only allowlisted routes may opt out. Unauthenticated API calls answer
  401, never a 200 login page.
- **Web**: the dev server serves the SPA shell to everyone (the enforcement gate knows this and
  probes the API instead), so REAL enforcement lives entirely in the API's JWT validation. The SPA
  uses MSAL (`@azure/msal-browser`) for the sign-in UX and attaches bearer tokens to API calls;
  unauthenticated API calls must answer 401.
- **Test seam**: when `AIDW_TEST_AUTH=1` is set in the environment (and ONLY then), enable a test
  sign-in path (a test JWT signing endpoint on the API) so the Playwright suite can authenticate
  without a live Entra round-trip. It must be completely inert when the variable is unset — the
  enforcement gate probes without it.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps"
convention_roots: node=apps/web
