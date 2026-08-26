# Next.js + .NET Web API Monorepo

## Overview

A Next.js (App Router) frontend that server-renders pages and calls an ASP.NET Core Web API for
data, both under `apps/` in one repository. Good default when the team wants React with built-in
routing/SSR plus a strongly-typed .NET backend for business logic and data access.

## Repository layout

```
repo-root/
├── .gitignore
├── apps/
│   ├── web/                   # Next.js app (App Router)
│   │   ├── src/app/
│   │   ├── next.config.ts     # rewrite: /api/* -> http://localhost:5080/*
│   │   └── package.json
│   ├── api/                   # ASP.NET Core Web API
│   │   ├── Program.cs
│   │   ├── Controllers/
│   │   └── Api.csproj
│   ├── api.Tests/              # xUnit test project for the API
│   └── Directory.Build.props   # written automatically once this stack is detected -- lives at
│                                # apps/, the common ancestor of Api.csproj AND Api.Tests.csproj,
│                                # so MSBuild's upward walk finds it from both
├── NextjsDotnetApp.sln
└── .ai-dev-workflow/coverage-commands.json   # registers both ecosystems' coverage commands (see Testing)
```

## Scaffolding commands

1. `npx create-next-app@latest apps/web --ts --app` (answer the remaining prompts, or pass
   `--eslint --src-dir --import-alias "@/*"` up front for a fully non-interactive scaffold).
2. `dotnet new webapi -o apps/api -n Api --use-controllers` (`--use-controllers` opts into the
   `Controllers/` layout this file documents -- the template defaults to Minimal APIs otherwise)
3. `dotnet new sln -n NextjsDotnetApp`
4. `dotnet sln NextjsDotnetApp.sln add apps/api/Api.csproj`
5. Create a root `.gitignore` (neither `dotnet new` nor `create-next-app`'s own `.gitignore` covers
   the *other* stack's artifacts, and nothing generates one for the repo root):
   ```gitignore
   node_modules/
   .next/
   coverage/
   bin/
   obj/
   TestResults/
   *.user
   ```
6. In `apps/web/next.config.ts`, proxy API calls in development so the browser only ever talks to
   one origin:
   ```ts
   const nextConfig = {
     async rewrites() {
       // process.env, not a literal -- the e2e harness picks a free port for the API and
       // exports API_BASE_URL. A hardcoded 5080 rewrites to nothing once that port is taken.
       const api = process.env.API_BASE_URL || "http://localhost:5080";
       return [{ source: "/api/:path*", destination: `${api}/:path*` }];
     },
   };
   ```

## Package managers

- `apps/web`: npm (`create-next-app`'s default; `package-lock.json` committed).
- `apps/api`: NuGet, via the `.csproj`'s `<PackageReference>` items.

## Build & run commands

**Development**
- API: `dotnet run --project apps/api` — Kestrel on `http://localhost:5080`.
- Web: `cd apps/web && npm run dev` — Next.js dev server on `http://localhost:3000`, rewriting
  `/api/*` to the API above.

**Production**
- API: `dotnet publish apps/api -c Release -o out/api && dotnet out/api/Api.dll`
- Web: `cd apps/web && npm run build && npm start` — Next.js production server on
  `http://localhost:3000` (or `$PORT`).

## Testing

- **API (xUnit + coverlet)**: `dotnet new xunit -o apps/api.Tests -n Api.Tests`, then
  `dotnet add apps/api.Tests reference apps/api/Api.csproj`, `dotnet add apps/api.Tests package
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with `dotnet test`.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @testing-library/react
  @testing-library/jest-dom jsdom @vitejs/plugin-react @vitest/coverage-v8`; add a
  `vitest.config.ts` with the React plugin and
  `test: { environment: "jsdom", globals: true, passWithNoTests: true }` -- the last option matters:
  an AC-retirement fallback can legitimately leave a file with a placeholder test only, and without
  it a file Vitest discovers but that registers zero tests is a hard runner error, not a pass.
  Run with `npx vitest run`. Use Playwright (already available to this pipeline's build stage via
  MCP) for anything that needs a real browser/route.

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

- Next.js: Server Components by default, `"use client"` only where interactivity is genuinely
  needed; routes under `src/app/`, shared UI in `src/components/`, API client in `src/lib/api.ts`.
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
- **Web (Next.js 15/16)**: `cd apps/web && npm install @opentelemetry/sdk-node
  @opentelemetry/exporter-trace-otlp-http`. `instrumentation.ts` at the app root with the runtime
  guard — `NodeSDK` crashes on the edge runtime, so the import must be dynamic and node-only:
  ```ts
  // instrumentation.ts
  export async function register() {
    if (process.env.NEXT_RUNTIME === "nodejs") {
      await import("./instrumentation.node");
    }
  }
  ```
  ```ts
  // instrumentation.node.ts
  import { NodeSDK, tracing } from "@opentelemetry/sdk-node";
  const sdk = new NodeSDK({
    serviceName: "web",
    spanProcessors: [new tracing.SimpleSpanProcessor(new tracing.ConsoleSpanExporter())],
  });
  sdk.start();
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
- **Web**: protect every page with Auth.js (`next-auth`) + the Microsoft Entra ID provider and a
  root `middleware.ts` whose matcher excludes ONLY the allowlisted anonymous routes and Next's
  own static assets (`_next/*`). Unauthenticated page loads redirect to the provider.
- **Test seam**: when `process.env.AIDW_TEST_AUTH === "1"` (and ONLY then), enable a test sign-in
  path (a Credentials provider on the web side; a test JWT authentication scheme on the API) so
  the Playwright suite can authenticate without a live Entra round-trip. It must be completely
  inert when the variable is unset — the enforcement gate probes without it.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps"
convention_roots: node=apps/web
