# Local development

Run commands from the repository root unless a step changes directories.

## Prerequisites

- Python 3.12+ and Node.js 20.9+ with npm.
- An initialized development PostgreSQL database. The full app needs
  `DATABASE_URL`; the ORM's SQLite fallback does not support all existing routes.
- A Clerk application and a user who can sign in to it.

This checkout has a [known fresh-database migration issue](deployment.md#database-upgrades).
Use an initialized development database or an isolated schema-only copy of an
existing deployment. Starting the servers does not resolve that issue.

## 1. Install dependencies

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path outreach_web/.env.local)) { Copy-Item outreach_web/.env.example outreach_web/.env.local }
```

macOS / Linux:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
[ -e .env ] || cp .env.example .env
[ -e outreach_web/.env.local ] || cp outreach_web/.env.example outreach_web/.env.local
```

Then install the frontend dependencies:

```bash
cd outreach_web
npm ci
cd ..
```

## 2. Configure both processes

| File | Values to set |
| --- | --- |
| Root `.env` | `DATABASE_URL`, `APP_ENCRYPTION_KEY`, `LOCAL_DEV_USER_ID`, `BACKEND_URL`, `FRONTEND_URL` |
| `outreach_web/.env.local` | Clerk publishable and secret keys, `LOCAL_DEV_USER_ID`, `BACKEND_URL` |

For local authentication, set `APP_ENV=development`, use the same Clerk user ID
as `LOCAL_DEV_USER_ID` in both files, and leave `BACKEND_IDENTITY_SECRET` empty in both.
The frontend verifies that the signed-in account matches that ID.

Keep these backend values for local UI and API work:

```dotenv
OUTREACH_SAFE_LOCAL_MODE=true
OUTREACH_ALLOW_DELIVERY=false
RUN_DATABASE_MIGRATIONS=false
```

The delivery lock prevents launching sends and processing delivery work. It does
not make the database read-only: editing a campaign still saves changes. Use a
development database for experiments.

For a new database's encryption key, run this with the virtual environment's
Python and save the result in `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Use the existing key when working with existing encrypted sender credentials.
Changing the key does not re-encrypt those credentials. Shell environment values
take precedence over dotenv files; restart both processes after configuration changes.

## 3. Start the app

Windows: `.\run_servers.bat`. macOS / Linux: `bash run_servers.sh`.
Both launchers start only the API and frontend. On Windows, close each server
window to stop it; on macOS / Linux, Ctrl+C stops both.

To start them separately, use two terminals:

```powershell
# Windows, repository root
.\.venv\Scripts\python -m uvicorn api.main:app --port 8000 --reload
```

```bash
# macOS / Linux, repository root
.venv/bin/python -m uvicorn api.main:app --port 8000 --reload
```

```bash
# Frontend terminal, either platform
cd outreach_web
npm run dev
```

The [health endpoint](http://127.0.0.1:8000/health) should return
`{"status":"ok","database":"ok"}`. It checks database connectivity, not schema
completeness. Sign in at [localhost:3000](http://localhost:3000) and confirm that
the campaign list loads. Avoid starting a second launcher on the same ports.

## Check a change

From `outreach_web/`:

```bash
npx tsc --noEmit
npm run lint
npm run build
node --test ../tests/schedule_draft.test.cjs
```

For the backend, use the virtual environment's Python from the repository root:

```bash
python -m pytest tests/test_delivery_safety.py -q
```

Select additional files in [tests/](../tests/) for the behavior you change.
Some older tests access the configured PostgreSQL database. Use a disposable
test database and inspect fixtures before running the entire suite. Delivery
integration tests also need test-specific authentication and delivery flags;
keep those overrides in the test process, separate from the local server.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `DATABASE_URL is required` | Set it in the root `.env`; `APP_DATABASE_URL` alone does not configure the compatibility layer. |
| Backend connection is not configured | Set the frontend's `BACKEND_URL` and either local user ID or the shared `BACKEND_IDENTITY_SECRET`. |
| Configured for a different account | Sign in with the Clerk account matching `LOCAL_DEV_USER_ID`. |
| Invalid authentication token | Confirm that the frontend and API use the same `BACKEND_IDENTITY_SECRET` in production, or use the configured local user ID locally. |
| Encrypted credentials cannot be decrypted | Use the database's original `APP_ENCRYPTION_KEY`. |
| Missing table or column | Check the database revision against the [migration guide](deployment.md#database-upgrades). |
| Delivery is locked | Expected in local mode; use mocked delivery tests for development. |

To connect Gmail, configure a Google OAuth web client through the supported
credentials file or `GMAIL_CREDENTIALS_JSON`. Its redirect URI must exactly match
`BACKEND_URL` followed by `/api/oauth/callback`. For example, use
`http://127.0.0.1:8000/api/oauth/callback` when that is the configured backend host.
