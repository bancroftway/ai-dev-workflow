# Next.js + Flask API Monorepo

## Overview

A Next.js (App Router) frontend paired with a small Python Flask API, both under `apps/` in one
repository. Good default when the team wants React with built-in routing/SSR on the frontend and a
lightweight, unopinionated Python backend (thin routes, no ORM assumed) for data access.

## Repository layout

```
repo-root/
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
└── (no root package.json — each app manages its own dependencies independently)
```

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
7. In `apps/web/next.config.ts`, proxy API calls in development:
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

- **API (pytest)**: put tests under `apps/api/tests/`, run with `cd apps/api && pytest`. Coverage
  in the Cobertura XML format this pipeline's coverage gate replays:
  `pytest --cov=. --cov-report=xml:coverage.cobertura.xml`. Record that exact command (with
  `root: "apps/api"`, `format: "cobertura"`, `artifact: "apps/api/coverage.cobertura.xml"`) in
  `.ai-dev-workflow/coverage-commands.json` once tests exist, so the coverage gate can replay it.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @testing-library/react
  @testing-library/jest-dom jsdom @vitejs/plugin-react @vitest/coverage-v8`; add a
  `vitest.config.ts` with the React plugin and `test: { environment: "jsdom", globals: true }`.
  Run with `npx vitest run`, coverage with `npx vitest run --coverage`.

## Conventions

- Next.js: Server Components by default, `"use client"` only where interactivity is genuinely
  needed; routes under `src/app/`, shared UI in `src/components/`, API client in `src/lib/api.ts`.
- API: one Blueprint per resource under `apps/api/blueprints/`, request/response shapes validated
  with plain dataclasses or `pydantic` if added; keep `app.py` to app setup and blueprint
  registration only.
- Lint: `eslint --max-warnings=0` + `tsc --noEmit` on the web app; `ruff check .` + `mypy .` on the
  API (both build-blocking, per this pipeline's Python conventions).

## Stack facts

dotnet_detected: false
dotnet_solution_root: ""
convention_roots: node=apps/web python=apps/api
