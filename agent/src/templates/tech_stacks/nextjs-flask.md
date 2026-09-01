# Next.js + Flask API Monorepo

## Overview

A Next.js (App Router) frontend paired with a small Python Flask API, both under `apps/` in one
repository. Good default when the team wants React with built-in routing/SSR on the frontend and a
lightweight, unopinionated Python backend (thin routes, no ORM assumed) for data access.

## Repository layout

```
repo-root/
├── .gitignore
├── apps/
│   ├── web/                  # Next.js app (App Router)
│   │   ├── src/app/
│   │   ├── next.config.ts    # rewrite: /api/* -> http://localhost:5000/*
│   │   └── package.json
│   └── api/                  # Flask API
│       ├── app.py
│       ├── requirements.txt
│       └── tests/
├── ruff.toml, mypy.ini        # written automatically once Python is detected
└── .ai-dev-workflow/coverage-commands.json   # registers both ecosystems' coverage commands (see Testing)
```

(no root package.json — each app manages its own dependencies independently)

## Scaffolding commands

1. `npx create-next-app@latest apps/web --ts --app` (answer the remaining prompts, or pass
   `--eslint --src-dir --import-alias "@/*"` up front for a fully non-interactive scaffold).
2. `mkdir -p apps/api && cd apps/api`
3. `python -m venv .venv && source .venv/bin/activate` (`.venv\Scripts\activate` on Windows)
4. `pip install flask flask-cors pytest pytest-cov`
5. `pip freeze > requirements.txt`
6. Create `apps/api/app.py`:
   ```python
   from flask import Flask
   from flask_cors import CORS

   app = Flask(__name__)
   CORS(app)

   @app.get("/health")
   def health():
       return {"status": "ok"}
   ```
7. Create a root `.gitignore` (Flask's manual setup and `create-next-app` each cover only their
   own app, and nothing generates one for the repo root):
   ```gitignore
   node_modules/
   .next/
   coverage/
   .venv/
   __pycache__/
   *.pyc
   ```
8. In `apps/web/next.config.ts`, proxy API calls in development:
   ```ts
   const nextConfig = {
     async rewrites() {
       return [{ source: "/api/:path*", destination: "http://localhost:5000/:path*" }];
     },
   };
   ```

## Package managers

- `apps/web`: npm (`create-next-app`'s default; `package-lock.json` committed).
- `apps/api`: pip, with `requirements.txt` pinned via `pip freeze` and a per-app `.venv`.

## Build & run commands

**Development**
- API: `cd apps/api && flask --app app run --debug --port 5000`
- Web: `cd apps/web && npm run dev` — Next.js dev server on `http://localhost:3000`, rewriting
  `/api/*` to the API above.

**Production**
- API: `cd apps/api && pip install gunicorn && gunicorn -w 4 -b 0.0.0.0:8000 app:app`
- Web: `cd apps/web && npm run build && npm start` — Next.js production server on
  `http://localhost:3000` (or `$PORT`).

## Testing

- **API (pytest)**: put tests under `apps/api/tests/`, run with `cd apps/api && pytest`.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @testing-library/react
  @testing-library/jest-dom jsdom @vitejs/plugin-react @vitest/coverage-v8`; add a
  `vitest.config.ts` with the React plugin and
  `test: { environment: "jsdom", globals: true, passWithNoTests: true }` -- the last option matters:
  an AC-retirement fallback can legitimately leave a file with a placeholder test only, and without
  it a file Vitest discovers but that registers zero tests is a hard runner error, not a pass.
  Run with `npx vitest run`.

**Coverage contract** (this pipeline's coverage gate replays `.ai-dev-workflow/coverage-commands.json`
when present, INSTEAD of its own dotnet/js/python legacy fallback -- registering only the Python
entry would silently exempt the frontend from the 95% threshold forever, so register BOTH entries
together, never just one):
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
      "command": "pytest --cov=. --cov-report=xml:coverage.cobertura.xml",
      "artifact": "apps/api/coverage.cobertura.xml",
      "format": "cobertura",
      "root": "apps/api"
    }
  ]
}
```

## Conventions

- Next.js: Server Components by default, `"use client"` only where interactivity is genuinely
  needed; routes under `src/app/`, shared UI in `src/components/`, API client in `src/lib/api.ts`.
- API: one Blueprint per resource under `apps/api/blueprints/`, request/response shapes validated
  with plain dataclasses or `pydantic` if added; keep `app.py` to app setup and blueprint
  registration only.
- Lint: `eslint --max-warnings=0` + `tsc --noEmit` on the web app; `ruff check .` + `mypy .` on the
  API (both build-blocking, per this pipeline's Python conventions).

## Observability

Both apps ship OpenTelemetry from the first commit, Console exporter by default (the pipeline's
coverage gate verifies the packages are present; respect `OTEL_EXPORTER_OTLP_ENDPOINT` /
`OTEL_TRACES_EXPORTER=none` when set, the same convention as everywhere else in this pipeline).

- **API**: `cd apps/api && pip install opentelemetry-sdk opentelemetry-instrumentation-flask`
  (re-freeze `requirements.txt`), then at startup in `app.py`:
  ```python
  from opentelemetry import trace
  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
  from opentelemetry.instrumentation.flask import FlaskInstrumentor
  provider = TracerProvider()
  provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
  trace.set_tracer_provider(provider)
  FlaskInstrumentor().instrument_app(app)
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

- **API**: validate the Entra-issued JWT bearer on every endpoint — verify the signature against
  the tenant's JWKS, audience = the app's client id; env vars `CLIENT_ID` / `TENANT_ID` (or the
  `AzureAd__*` names) arrive at runtime, never hardcode values. Unauthenticated API calls answer
  401, never a 200 login page — only allowlisted paths are exempt.
- **Web**: protect every page with Auth.js (`next-auth`) + the Microsoft Entra ID provider and a
  root `middleware.ts` whose matcher excludes ONLY the allowlisted anonymous routes and Next's
  own static assets (`_next/*`). Unauthenticated page loads redirect to the provider.
- **Test seam**: when `process.env.AIDW_TEST_AUTH === "1"` (and ONLY then), enable a test sign-in
  path (a Credentials provider on the web side; a test JWT issuance path on the API) so the
  Playwright suite can authenticate without a live Entra round-trip. It must be completely inert
  when the variable is unset — the enforcement gate probes without it.

## Stack facts

dotnet_detected: false
dotnet_solution_root: ""
convention_roots: node=apps/web python=apps/api
