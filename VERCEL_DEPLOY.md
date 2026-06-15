# Vercel Deployment Checklist

Use one Vercel project for both the React frontend and FastAPI backend.

## Project Settings

- Repository: `rohanshelar991/datawebllm`
- Root Directory: repository root
- Build Command: handled by `vercel.json`
- Output Directory: handled by `vercel.json`
- API Entry: `api/index.py`

## Required Environment Variables

Set these in Vercel Project Settings -> Environment Variables:

```text
GROQ_API_KEY=<your-groq-api-key>
ALLOW_SIGNUPS=true
SESSION_HOURS=24
FIREBASE_SERVICE_ACCOUNT_JSON=<full-firebase-service-account-json>
FIREBASE_STORAGE_BUCKET=<your-firebase-storage-bucket>
```

Recommended:

```text
FIREBASE_DATABASE_URL=<your-firebase-database-url>
ADMIN_EMAIL=<admin@example.com>
ADMIN_PASSWORD=<strong-admin-password>
CORS_ORIGINS=https://<your-vercel-domain>
```

Optional Firestore overrides:

```text
FIRESTORE_DATABASE_ID=(default)
FIRESTORE_USERS_COLLECTION=users
FIRESTORE_SESSIONS_COLLECTION=sessions
FIRESTORE_DATASETS_COLLECTION=datasets
FIRESTORE_DATASET_MANIFESTS_COLLECTION=dataset_manifests
FIRESTORE_DATASET_CHUNKS_COLLECTION=dataset_chunks
```

## Frontend Variables

Leave `VITE_API_BASE_URL` unset on Vercel so the app uses same-origin `/api`.

Set `VITE_FIREBASE_*` only if you need client-side Firebase analytics or client integrations:

```text
VITE_FIREBASE_API_KEY=<firebase-web-api-key>
VITE_FIREBASE_AUTH_DOMAIN=<project>.firebaseapp.com
VITE_FIREBASE_DATABASE_URL=<firebase-database-url>
VITE_FIREBASE_PROJECT_ID=<project-id>
VITE_FIREBASE_STORAGE_BUCKET=<bucket>
VITE_FIREBASE_MESSAGING_SENDER_ID=<sender-id>
VITE_FIREBASE_APP_ID=<app-id>
VITE_FIREBASE_MEASUREMENT_ID=<measurement-id>
```

## Deploy

1. Import the GitHub repository into Vercel.
2. Add the environment variables above for Production, Preview, and Development as needed.
3. Trigger a deployment from Vercel or push a commit to `main`.
4. Open `https://<your-domain>/api/health`.

Expected health response:

```json
{
  "status": "ok",
  "app_name": "Conversational Data Intelligence API",
  "auth_mode": "session",
  "allow_signups": true,
  "llm_configured": true
}
```

## Smoke Test

1. Open the deployed app.
2. Create an account or sign in with the admin account.
3. Load the sample dataset, import a remote CSV/JSON URL, or upload a local file.
4. Ask a question such as `How many rows are in this dataset?`.
5. Confirm the answer, SQL, rows returned, copy-SQL, and CSV export all work.

## Production Notes

- Prefer `FIREBASE_SERVICE_ACCOUNT_JSON` on Vercel because file paths are not portable.
- Rotate any key that was ever pasted into chat, committed locally, or shared during setup.
- Keep local `backend/storage/` files out of git; Vercel should use Firebase-backed persistence.
- If you deploy the API separately from the frontend, set `VITE_API_BASE_URL` to the external API base URL.
