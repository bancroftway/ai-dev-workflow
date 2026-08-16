# Blazor WebAssembly + .NET Web API Monorepo

## Overview

A standalone Blazor WebAssembly frontend (C# in the browser, no JavaScript framework at all) that
calls a separate ASP.NET Core Web API for data, both under `apps/` in one repository and one
solution. Good default for a .NET-only team that wants to write UI in C#/Razor rather than
TypeScript.

## Repository layout

```
repo-root/
├── apps/
│   ├── web/                   # Blazor WebAssembly (standalone)
│   │   ├── Pages/
│   │   ├── wwwroot/
│   │   └── Web.csproj
│   └── api/                   # ASP.NET Core Web API
│       ├── Program.cs
│       ├── Controllers/
│       └── Api.csproj
├── apps/web.Tests/            # bUnit + xUnit component tests
├── apps/api.Tests/            # xUnit test project for the API
├── BlazorDotnetApp.sln
└── Directory.Build.props      # written automatically once this stack is detected
```

## Scaffolding commands

1. `dotnet new blazorwasm -o apps/web -n Web`
2. `dotnet new webapi -o apps/api -n Api`
3. `dotnet new sln -n BlazorDotnetApp`
4. `dotnet sln BlazorDotnetApp.sln add apps/web/Web.csproj apps/api/Api.csproj`
5. In `apps/api/Program.cs`, enable CORS for the web app's dev origin so the two Kestrel processes
   can talk to each other locally:
   ```csharp
   builder.Services.AddCors(o => o.AddDefaultPolicy(p =>
       p.WithOrigins("http://localhost:5150").AllowAnyHeader().AllowAnyMethod()));
   // ...
   app.UseCors();
   ```
6. In `apps/web/Program.cs`, register a named `HttpClient` whose `BaseAddress` is the API's dev URL
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
- **Coverage** (matches this pipeline's coverage gate exactly, run across both test projects):
  `dotnet test /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura
  /p:CoverletOutput=./TestResults/coverage.cobertura.xml`.

## Conventions

- Blazor: one component per `.razor` file under `apps/web/Pages/` (routable) or
  `apps/web/Shared/` (reusable), code-behind in a matching `.razor.cs` partial class once a
  component's logic grows past a few lines, typed API client in `apps/web/Services/`.
- API: one controller per resource under `Controllers/`, DTOs in `Contracts/`, EF Core (if a
  database is added) in `Data/`.
- Lint: analyzer violations are build errors, not warnings, via `Directory.Build.props` — applies
  to both `apps/web` and `apps/api` since both are .NET projects under the same solution.

## Stack facts

dotnet_detected: true
dotnet_solution_root: "apps"
