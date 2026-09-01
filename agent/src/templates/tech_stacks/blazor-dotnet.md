# Blazor WebAssembly + .NET Web API Monorepo

## Overview

A standalone Blazor WebAssembly frontend (C# in the browser, no JavaScript framework at all) that
calls a separate ASP.NET Core Web API for data, both under `apps/` in one repository and one
solution. Good default for a .NET-only team that wants to write UI in C#/Razor rather than
TypeScript.

## Repository layout

```
repo-root/
├── .gitignore
├── apps/
│   ├── web/                   # Blazor WebAssembly (standalone)
│   │   ├── Pages/
│   │   ├── wwwroot/
│   │   └── Web.csproj
│   ├── api/                   # ASP.NET Core Web API
│   │   ├── Program.cs
│   │   ├── Controllers/
│   │   └── Api.csproj
│   ├── web.Tests/              # bUnit + xUnit component tests
│   ├── api.Tests/               # xUnit test project for the API
│   └── Directory.Build.props   # written automatically once this stack is detected -- lives at
│                                # apps/, the common ancestor of every .csproj here
├── BlazorDotnetApp.sln
└── .ai-dev-workflow/coverage-commands.json   # registers the merged coverage command (see Testing)
```

## Scaffolding commands

1. `dotnet new blazorwasm -o apps/web -n Web`
2. `dotnet new webapi -o apps/api -n Api --use-controllers` (`--use-controllers` opts into the
   `Controllers/` layout this file documents -- the template defaults to Minimal APIs otherwise)
3. `dotnet new sln -n BlazorDotnetApp`
4. `dotnet sln BlazorDotnetApp.sln add apps/web/Web.csproj apps/api/Api.csproj`
5. Create a root `.gitignore` (nothing generates one for the repo root):
   ```gitignore
   bin/
   obj/
   TestResults/
   *.user
   ```
6. In `apps/api/Program.cs`, enable CORS for the web app's dev origin so the two Kestrel processes
   can talk to each other locally. Read the origin from the environment, falling back to the
   documented dev port — do NOT hardcode it as the only value:
   ```csharp
   var webOrigin = Environment.GetEnvironmentVariable("WEB_ORIGIN") ?? "http://localhost:5150";
   builder.Services.AddCors(o => o.AddDefaultPolicy(p =>
       p.WithOrigins(webOrigin).AllowAnyHeader().AllowAnyMethod()));
   // ...
   app.UseCors();
   ```
   Why the env var matters: the e2e harness starts both processes on ports it verifies are free,
   and if 5150 is taken it puts the web app somewhere else and exports `WEB_ORIGIN` with the real
   value. A policy pinned to the literal 5150 then rejects every browser request, and the failure
   shows up far away as the UI reporting it cannot reach the API — on an app that is entirely
   correct. This was observed twice before the fallback was made an override.
7. In `apps/web/Program.cs`, register a named `HttpClient` whose `BaseAddress` is the API's dev URL
   (`http://localhost:5080`), configurable via `apps/web/wwwroot/appsettings.json` for other
   environments.

## Package managers

NuGet only, via each project's `.csproj` `<PackageReference>` items — no JavaScript tooling
anywhere in this stack.

## Build & run commands

**Development**
- API: `dotnet run --project apps/api` — Kestrel on `http://localhost:5080`.
- Web: `dotnet watch --project apps/web` — Blazor WASM dev server on `http://localhost:5150`,
  hot-reloading on save, calling the API above.

**Production**
- API: `dotnet publish apps/api -c Release -o out/api && dotnet out/api/Api.dll`
- Web: `dotnet publish apps/web -c Release -o out/web` — static output in
  `out/web/wwwroot`, served by any static file host (or by the API project itself via
  `UseStaticFiles`/`MapFallbackToFile` if deploying same-origin, which also removes the CORS need).

## Testing

- **Web (bUnit + xUnit)**: `dotnet new xunit -o apps/web.Tests -n Web.Tests`, then
  `dotnet add apps/web.Tests package bunit`, `dotnet add apps/web.Tests package coverlet.msbuild`,
  `dotnet add apps/web.Tests reference apps/web/Web.csproj`, and
  `dotnet sln add apps/web.Tests/Web.Tests.csproj`. bUnit renders each component in an isolated
  test context and asserts on the resulting markup — no browser needed. Run with `dotnet test`.
- **API (xUnit + coverlet)**: `dotnet new xunit -o apps/api.Tests -n Api.Tests`, then
  `dotnet add apps/api.Tests reference apps/api/Api.csproj`, `dotnet add apps/api.Tests package
  coverlet.msbuild`, and `dotnet sln add apps/api.Tests/Api.Tests.csproj`. Run with `dotnet test`.

**Coverage contract**: both test projects share the same relative `CoverletOutput` path, so one
`dotnet test` invoked at the solution root merges both into a single Cobertura file. Register it
in `.ai-dev-workflow/coverage-commands.json` (this pipeline's coverage gate replays this contract
INSTEAD of its own legacy dotnet fallback when present, so keep it in sync if the command changes):
```json
{
  "entries": [
    {
      "command": "dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura /p:CoverletOutput=./TestResults/coverage.cobertura.xml",
      "artifact": "TestResults/coverage.cobertura.xml",
      "format": "cobertura",
      "root": ""
    }
  ]
}
```

## Conventions

- Blazor: one component per `.razor` file under `apps/web/Pages/` (routable) or
  `apps/web/Shared/` (reusable), code-behind in a matching `.razor.cs` partial class once a
  component's logic grows past a few lines, typed API client in `apps/web/Services/`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: analyzer violations are build errors, not warnings, via `Directory.Build.props` — applies
  to both `apps/web` and `apps/api` since both are .NET projects under the same solution.

## Observability

The API ships OpenTelemetry from the first commit, Console exporter by default (the pipeline's
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
- **Web**: the WASM client is not separately instrumented — the API's ASP.NET Core instrumentation
  carries the tracing story for this stack.

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
  probes the API instead), so REAL enforcement lives entirely in the API's JWT validation. The
  client uses Blazor's built-in MSAL support (`Microsoft.Authentication.WebAssembly.Msal`) for the
  sign-in UX and attaches bearer tokens to API calls; unauthenticated API calls must answer 401.
- **Test seam**: when `AIDW_TEST_AUTH=1` is set in the environment (and ONLY then), enable a test
  sign-in path (a test JWT signing endpoint on the API) so the Playwright suite can authenticate
  without a live Entra round-trip. It must be completely inert when the variable is unset — the
  enforcement gate probes without it.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps"
