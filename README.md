# DataWeb LLM

DataWeb LLM is a deployable conversational analytics app for uploading datasets, profiling their quality, and asking natural-language questions that are converted into guarded DuckDB SQL.

The production app is split into:

- `frontend/` - React + Vite dashboard
- `backend/` - FastAPI API for auth, datasets, and analysis
- `data_intelligence/` - reusable data loading, profiling, and SQL safety helpers
- `api/index.py` - Vercel serverless entrypoint
- `app.py` - legacy Streamlit prototype

## Features

- Account registration, sign-in, bearer sessions, and logout
- CSV, XLSX, JSON upload support
- Remote dataset import from raw URLs or GitHub blob URLs
- One-click sample dataset loading
- Persistent dataset metadata with Firestore support
- Firebase Storage support for saved parquet dataset files
- Dataset preview, schema text, column catalog, null-rate checks, and quality score
- Suggested questions generated from dataset shape
- LLM-powered natural-language to DuckDB SQL
- SQL safety checks: select-only validation, table allowlist, and result limits
- Query result table, answer summary, SQL explanation, CSV export, and copy-SQL action
- Vercel, Docker, and CI scaffolding

## Architecture

```text
React/Vite frontend
        |
        | /api
        v
FastAPI backend -> DuckDB in-memory query engine
        |
        +--> Firestore metadata/auth store
        +--> Firebase Storage or local storage for datasets
        +--> Groq LLM for SQL generation
```

## Local Setup

### 1. Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least:

```bash
GROQ_API_KEY=your_groq_key
ALLOW_SIGNUPS=true
SESSION_HOURS=24
```

Start the API:

```bash
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/api/health
```

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Open `http://127.0.0.1:5173`.

For local development, set:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api
```

On Vercel this can stay empty because the frontend falls back to same-origin `/api`.

## Environment Variables

### Backend

| Variable | Required | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` | Yes | Enables NL-to-SQL generation |
| `ALLOW_SIGNUPS` | No | Enables self-service registration |
| `SESSION_HOURS` | No | Session lifetime in hours |
| `ADMIN_EMAIL` | No | Optional bootstrap admin email |
| `ADMIN_PASSWORD` | No | Optional bootstrap admin password |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Cloud recommended | Firebase Admin credentials as JSON text |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | Local optional | Path to service account JSON |
| `FIREBASE_DATABASE_URL` | Optional | Firebase database URL for Admin SDK initialization |
| `FIREBASE_STORAGE_BUCKET` | Recommended | Bucket for persisted dataset files |
| `FIRESTORE_DATABASE_ID` | Optional | Defaults to `(default)` |
| `CORS_ORIGINS` | Optional | Comma-separated allowed frontend origins |

### Frontend

| Variable | Required | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | Local only | Backend API URL |
| `VITE_FIREBASE_*` | Optional | Firebase web app config for analytics/client integrations |

## Deploy To Vercel

This repository includes `vercel.json` for a single Vercel project:

- Build command: `cd frontend && npm install && npm run build`
- Output directory: `frontend/dist`
- API function: `api/index.py`
- API route: `/api/:path*`
- App fallback: `/index.html`

Steps:

1. Import `rohanshelar991/datawebllm` into Vercel.
2. Keep the Vercel root directory as the repository root.
3. Add backend environment variables in Vercel Project Settings.
4. Leave `VITE_API_BASE_URL` unset for production unless you deploy the API separately.
5. Deploy.
6. Check `/api/health`, then register, upload/import a dataset, and run a question.

Use `VERCEL_DEPLOY.md` for the full deployment checklist.

## Docker

Run the full web stack:

```bash
docker compose -f docker-compose.web.yml up --build
```

URLs:

- Frontend: `http://localhost:3000`
- Backend health: `http://localhost:8000/api/health`

## Validation

Run backend tests:

```bash
pytest
```

Run frontend build:

```bash
cd frontend
npm run build
```

Run the smoke test:

```bash
python3 scripts/smoke_test.py
```

## Repository Hygiene

Do not commit:

- `.env` files
- Firebase service account JSON files
- local SQLite databases
- uploaded dataset files
- generated `frontend/dist/` bundles

The repo tracks empty `backend/storage/.gitkeep` placeholders so local storage folders exist without shipping private runtime data.
