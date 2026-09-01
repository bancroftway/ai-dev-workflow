# Angular + .NET Web API Monorepo

## Overview

A single-page Angular frontend backed by an ASP.NET Core Web API, both living in one repository
under `apps/`. Angular owns the UI and calls the API over HTTP/JSON; the API owns data access and
business logic. Good default when the team wants a mature, batteries-included frontend framework
(routing, forms, DI all built in) paired with a strongly-typed .NET backend.

## Repository layout

```
repo-root/
├── .gitignore
├── apps/
│   ├── web/                   # Angular app (Angular CLI workspace)
│   │   ├── src/
│   │   ├── proxy.conf.js      # dev-server proxy: /api -> $API_BASE_URL (default :5080)
│   │   ├── angular.json
│   │   └── package.json
│   ├── api/                   # ASP.NET Core Web API
│   │   ├── Program.cs
│   │   ├── Controllers/
│   │   └── Api.csproj
│   ├── api.Tests/             # xUnit test project for the API
│   └── Directory.Build.props  # written automatically once this stack is detected -- lives at
│                               # apps/, the common ancestor of Api.csproj AND Api.Tests.csproj,
│                               # so MSBuild's upward walk finds it from both
├── AngularDotnetApp.sln
└── .ai-dev-workflow/coverage-commands.json   # registers both ecosystems' coverage commands (see Testing)
```

## Scaffolding commands

1. `npx -p @angular/cli@latest ng new web --directory apps/web --routing --style=scss --skip-git`
2. `dotnet new webapi -o apps/api -n Api --use-controllers` (`--use-controllers` opts into the
   `Controllers/` layout this file documents -- the template defaults to Minimal APIs otherwise)
3. `dotnet new sln -n AngularDotnetApp`
4. `dotnet sln AngularDotnetApp.sln add apps/api/Api.csproj`
5. Create a root `.gitignore` (neither `dotnet new` nor Angular CLI's own `.gitignore` covers the
   *other* stack's artifacts, and nothing generates one for the repo root):
   ```gitignore
   node_modules/
   dist/
   coverage/
   .angular/
   bin/
   obj/
   TestResults/
   *.user
   ```
6. In `apps/web/src/environments/`, point the API base URL at `/api` (dev) and the real API origin
   (prod); add `apps/web/proxy.conf.js` — a `.js` file, NOT `.json`:
   ```js
   // .js because a .json proxy file cannot read the environment, and the API's port is not
   // guaranteed to be 5080: the e2e harness starts it on a port it verifies is free and exports
   // API_BASE_URL with the real value. A .json pinned to 5080 silently proxies to nothing the
   // moment that port is taken, and the failure surfaces as the UI being unable to reach the API
   // on an app that is entirely correct.
   module.exports = {
     "/api": {
       target: process.env.API_BASE_URL || "http://localhost:5080",
       secure: false,
       pathRewrite: { "^/api": "" },
     },
   };
   ```
7. In `apps/web/angular.json`, set the `serve` target's `options.proxyConfig` to
   `"apps/web/proxy.conf.js"`.

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
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with `dotnet test`.
- **Web (Vitest)**: install with `cd apps/web && npm install -D vitest @analogjs/vite-plugin-angular
  @analogjs/platform jsdom @vitest/coverage-v8`, add a `vite.config.ts` using the `angular()` plugin
  with `test: { globals: true, environment: "jsdom", setupFiles: ["src/test-setup.ts"],
  passWithNoTests: true }`, and a `src/test-setup.ts` that initializes Angular's TestBed
  environment. `passWithNoTests` matters: an AC-retirement fallback can legitimately leave a file
  with a placeholder test only, and without it a file Vitest discovers but that registers zero
  tests is a hard runner error, not a pass. Run with `npx vitest run`.

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

- Angular: standalone components, one component per file (`*.component.ts` + `.html` + `.scss`),
  feature folders under `src/app/features/<feature>/`, shared UI in `src/app/shared/`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: Angular's built-in ESLint config (`ng lint`, or `eslint --max-warnings=0` if this pipeline's
  own config applies); the API's analyzer warnings are build errors via `Directory.Build.props`.

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
  then initialize in `src/main.ts`, before `bootstrapApplication`:
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
  uses MSAL (`@azure/msal-angular`) for the sign-in UX and attaches bearer tokens to API calls;
  unauthenticated API calls must answer 401.
- **Test seam**: when `AIDW_TEST_AUTH=1` is set in the environment (and ONLY then), enable a test
  sign-in path (a test JWT signing endpoint on the API) so the Playwright suite can authenticate
  without a live Entra round-trip. It must be completely inert when the variable is unset — the
  enforcement gate probes without it.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps"
convention_roots: node=apps/web
