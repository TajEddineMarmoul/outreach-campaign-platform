# Deployment and operations

This guide describes the hosting model recorded in the repository. Confirm project
settings and scheduler configuration in the hosting dashboards before a release;
those settings are not all managed by checked-in files.

## Services

| Service | Configuration |
| --- | --- |
| Frontend | Vercel project `outreach-web`, rooted at `outreach_web/` ([production](https://www.outreachemails.online/)) |
| API | Vercel project `outreach-api`, using root [server.py](../server.py) and [vercel.json](../vercel.json) ([production](https://api.outreachemails.online/)) |
| Database | Supabase PostgreSQL shared by the API and worker |
| Worker invocation | Supabase `pg_cron` and `pg_net` call `POST /internal/worker/tick` once per minute |

The recorded release path is `main` through the two connected Vercel projects.
The [GCP workflow](../.github/workflows/deploy-gcp.yml) is a manual legacy path;
its [configuration notes](../.github/DEPLOYMENT_SECRETS.md) describe its limitations.
Provider pricing, quotas, and account-specific domains are managed outside this guide.

## Environment reference

### API and standalone worker

| Variable | Purpose |
| --- | --- |
| `APP_ENV=production` | Enforce production database, encryption, and authentication behavior. |
| `DATABASE_URL` | PostgreSQL connection string used by both data-access layers. |
| `APP_ENCRYPTION_KEY` | Stable key for encrypted Gmail sender credentials; API and worker must match. |
| `BACKEND_IDENTITY_SECRET` | A 32+-character random secret shared with the frontend. The frontend uses it to sign a short-lived assertion containing the Clerk user ID; the API verifies it before accessing that user's data. |
| `BACKEND_URL` | Public API origin, also used for the Gmail OAuth callback. |
| `TRACKING_BASE_URL` | Optional public API origin used in open-pixel and click-redirect links. Defaults to `BACKEND_URL`; set it only when tracking should use a separate public domain. |
| `FRONTEND_URL` | Frontend origin used after OAuth completes. |
| `RUN_DATABASE_MIGRATIONS=false` | Keep API startup migrations off; upgrade explicitly. |
| `OUTREACH_SAFE_LOCAL_MODE=false` | Allow the hosted delivery runtime to process jobs. |
| `WORKER_TICK_TOKEN` | Secret required in the tick request's `X-Worker-Token` header. |
| `WORKER_MAX_JOBS` | Jobs processed per tick; defaults to 25 and is clamped to 1–25. |
| `GMAIL_CREDENTIALS_JSON` | Google OAuth client JSON for a serverless API. |

`APP_DATABASE_URL` overrides the ORM connection only. Prefer `DATABASE_URL` for
the whole application and avoid pointing the two layers at different databases.

For a persistent host, `GMAIL_CREDENTIALS_PATH` can identify the OAuth client JSON
file instead. Sender OAuth tokens are stored encrypted in PostgreSQL by the current
platform. Provision the client configuration before connecting senders; the Gmail
callback is `BACKEND_URL` followed by `/api/oauth/callback`.

### Frontend

| Variable | Purpose |
| --- | --- |
| `APP_ENV=production` | Disable the local user-ID authentication path. |
| `BACKEND_URL` | API origin targeted by the server-side proxy. |
| `BACKEND_IDENTITY_SECRET` | Same private secret as the API. It remains server-only and signs a request for the currently authenticated Clerk user. |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk browser configuration. |
| `CLERK_SECRET_KEY` | Matching Clerk server credential. |
| `NEXT_PUBLIC_API_URL` | Optional public base used to construct API URLs; `useApiClient()` maps matching requests through the proxy. |

Keep private credentials out of `NEXT_PUBLIC_` variables. Clerk verifies browser
sessions; the proxy then signs an assertion for that specific Clerk user on the
server. The API rejects shared bearer-token access in production, so each signed-in
user reaches only the workspace keyed by their own Clerk user ID.

### Required tenant-isolation release

This release can safely use the existing `APP_ACCESS_TOKEN` as a one-release
compatibility source for the signing key, so the API and frontend can be deployed
without an access gap. It is no longer accepted as a bearer credential. Immediately
afterward, set the same new `BACKEND_IDENTITY_SECRET` on both Vercel projects and
remove `APP_ACCESS_TOKEN` and `APP_USER_ID` from both projects. Generate the secret
with a cryptographically secure password generator (at least 32 random characters).
The API intentionally fails closed if neither secret is configured.

Earlier versions stored the production owner's records under an email address while
new code uses the immutable Clerk user ID. Before or immediately after this release,
run `python scripts/migrate_legacy_user_id.py --from-user-id <old-email> --to-user-id
<clerk-user-id> --dry-run`, review the counts, then run it again with `--apply`.
The script refuses to merge into a Clerk user ID that already has records.

### Optional Gmail activity synchronization

Set `GMAIL_PUBSUB_TOPIC`, `GMAIL_PUBSUB_PUSH_AUDIENCE`, and
`GMAIL_PUBSUB_PUSH_SERVICE_ACCOUNT` on the API/worker when configuring Gmail push
notifications. The push audience identifies the `/api/webhooks/gmail` endpoint.
The worker renews due watches and processes queued notifications. Sender read
permission and a working push subscription are required for ongoing response
updates. These variables alone do not create the topic or subscription.

## Database upgrades

Run Alembic with the intended deployment environment and database, using a backup
and a tested upgrade path:

```bash
python -m alembic current
python -m alembic heads
python -m alembic upgrade head
```

The latest checked-in revision is `0014_email_engagement`. Query `heads` when preparing
a release instead of using an older release note's target. Restart or redeploy the
application after upgrading, and verify its health and authenticated pages.

**Fresh database limitation:** `0001_application_schema` creates tables from the
current ORM metadata, while later revisions also create or alter those objects.
For example, `0005` creates `autopilot_day_schedules` again. The historical release
notes report this failure on an empty PostgreSQL database, and the conflicting code
is still present. An empty-database upgrade is not a verified bootstrap path.
Use an initialized development database or an isolated schema-only copy; do not
stamp an incomplete schema as current to suppress migration errors.

## Worker execution

The API does not start a background delivery thread. The scheduled tick endpoint
recovers stale jobs and runs a bounded worker cycle. Each request needs the matching
`X-Worker-Token`, and delivery must be enabled. Ensure the scheduler continues to run
after API deployment. The checked-in function timeout is 300 seconds.

A persistent host can run `python -m src.platform.worker` instead. This standalone
entry point attempts an Alembic upgrade before its loop; `RUN_DATABASE_MIGRATIONS`
controls API startup only. Local launchers deliberately omit the worker.

For release validation, check the frontend and API commit IDs, `/health`, signed-in
campaign pages, and scheduler results. Test provider transmission through the mocked
delivery suite before any deliberately configured live sending check.
