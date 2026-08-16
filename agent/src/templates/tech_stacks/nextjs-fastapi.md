# Next.js + FastAPI Monorepo

## Overview

A Next.js (App Router) frontend paired with a Python FastAPI backend, both under `apps/` in one
repository. Good default when the team wants React with built-in routing/SSR on the frontend and
a modern, typed, async-first Python API (automatic OpenAPI docs, `pydantic` validation) for data
access.

## Repository layout

```
repo-root/
├── apps/
│   ├── web/                  # Next.js app (App Router)
│   │   ├── src/app/
│   │   ├── next.config.ts    # rewrite: /api/* -> http://localhost:8000/*
│   │   └── package.json
│   └── api/                  # FastAPI app
│       ├── main.py
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
4. `pip install fastapi "uvicorn[standard]" pytest pytest-cov httpx`
5. `pip freeze > requirements.txt`
6. Create `apps/api/main.py`:
   ```python
   from fastapi import FastAPI
   from fastapi.middleware.cors import CORSMiddleware

   app = FastAPI()
   app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

   @app.get("/health")
   def health():
       return {"status": "ok"}
   ```
7. In `apps/web/next.config.ts`, proxy API calls in development:
   ```ts
   const nextConfig = {
     async rewrites() {
       return [{ source: "/api/:path*", destination: "http://localhost:8000/:path*" }];
     },
   };
   ```

## Package managers

- `apps/web`: npm (`create-next-app`'s default; `package-lock.json` committed).
- `apps/api`: pip, with `requirements.txt` pinned via `pip freeze` and a per-app `.venv`.

## Build & run commands

**Development**
- API: `cd apps/api && uvicorn main:app --reload --port 8000` — interactive docs at
  `http://localhost:8000/docs`.
- Web: `cd apps/web && npm run dev` — Next.js dev server on `http://localhost:3000`, rewriting
  `/api/*` to the API above.

**Production**
- API: `cd apps/api && uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4`
- Web: `cd apps/web && npm run build && npm start` — Next.js production server on
  `http://localhost:3000` (or `$PORT`).

## Testing

- **API (pytest)**: put tests under `apps/api/tests/`, use `fastapi.testclient.TestClient` (or
  `httpx.AsyncClient`) against the `app` object — no running server needed. Run with
  `cd apps/api && pytest`. Coverage in the Cobertura XML format this pipeline's coverage gate
  replays: `pytest --cov=. --cov-report=xml:coverage.cobertura.xml`. Record that exact command
  (with `root: "apps/api"`, `format: "cobertura"`, `artifact: "apps/api/coverage.cobertura.xml"`)
  in `.ai-dev-workflow/coverage-commands.json` once tests exist, so the coverage gate can replay
  it.
- **Web (Vitest)**: `cd apps/web && npm install -D vitest @testing-library/react
  @testing-library/jest-dom jsdom @vitejs/plugin-react @vitest/coverage-v8`; add a
  `vitest.config.ts` with the React plugin and `test: { environment: "jsdom", globals: true }`.
  Run with `npx vitest run`, coverage with `npx vitest run --coverage`.

## Conventions

- Next.js: Server Components by default, `"use client"` only where interactivity is genuinely
  needed; routes under `src/app/`, shared UI in `src/components/`, API client in `src/lib/api.ts`.
- API: one `APIRouter` per resource under `apps/api/routers/`, request/response models as
  `pydantic.BaseModel` subclasses in `apps/api/schemas.py`; keep `main.py` to app setup and router
  registration only.
- Lint: `eslint --max-warnings=0` + `tsc --noEmit` on the web app; `ruff check .` + `mypy .` on the
  API (both build-blocking, per this pipeline's Python conventions).

## Stack facts

dotnet_detected: false
dotnet_solution_root: ""
convention_roots: node=apps/web python=apps/api
