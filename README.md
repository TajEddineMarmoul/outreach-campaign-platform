# Outreach App

Create personalized Gmail campaigns, choose sender accounts, and control when
messages go out. Review the audience and message before launch, then follow
delivery progress and Gmail responses from the campaign page.

The app uses **Next.js and React**, **FastAPI**, and **PostgreSQL**. Clerk handles
browser sign-in. A delivery worker processes saved jobs independently of the browser.

## Start here

| I want to… | Read |
| --- | --- |
| Run the app locally | [Development setup](docs/development.md) |
| Create or schedule a campaign | [Campaign guide](docs/campaigns.md) |
| Work on the frontend | [Frontend README](outreach_web/README.md) |
| Configure hosting or upgrade the database | [Deployment guide](docs/deployment.md) |
| Find release notes and design references | [Documentation index](docs/README.md) |

## Local development

Use Python 3.12+, Node.js 20.9+, and an initialized development PostgreSQL
database. [Setup](docs/development.md) covers dependency installation, Clerk,
both environment files, and the existing fresh-database migration limitation.

Once configured, start the API and frontend from the repository root:

```powershell
# Windows PowerShell
.\run_servers.bat
```

```bash
# macOS / Linux
bash run_servers.sh
```

Open the [app](http://localhost:3000), [API health check](http://127.0.0.1:8000/health),
or [interactive API reference](http://127.0.0.1:8000/docs).
The launchers leave the delivery worker off. Keep the local delivery lock enabled
and automatic migrations disabled as shown in [.env.example](.env.example).

## Production

Open the [production app](https://www.outreachemails.online/), [API health check](https://api.outreachemails.online/health),
or [interactive API reference](https://api.outreachemails.online/docs).

## How delivery works

1. The frontend sends authenticated requests through its server-side API proxy.
2. The API saves campaign settings and delivery jobs in PostgreSQL.
3. The worker claims due jobs, checks sending limits, and records each result.

Closing the browser does not pause a launched campaign. Use **Pause sending**
to stop future work. The hosted setup invokes the worker through a scheduled
HTTP endpoint; a separate worker process is also available.

New Autopilot schedules default to **Spread evenly through the window**. Campaign
timezones are searchable by city and show current UTC offsets. See the
[campaign guide](docs/campaigns.md) for pacing, limits, and timezone behavior.

## Repository map

| Path | Purpose |
| --- | --- |
| [outreach_web/](outreach_web/) | Next.js pages, campaign workspace, and authenticated API proxy |
| [api/](api/) | FastAPI routes, request validation, and authentication |
| [src/platform/](src/platform/) | Database models, OAuth, delivery jobs, scheduler, and Gmail activity |
| [src/db/](src/db/) | PostgreSQL compatibility layer used by existing routes |
| [alembic/](alembic/) | Database migrations |
| [tests/](tests/) | Python tests and schedule regression tests |
| [docs/](docs/) | Guides, release history, and design references |

The older `app.py` interface and supporting modules remain in the repository.
The launchers above run the FastAPI and Next.js application.
