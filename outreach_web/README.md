# Outreach frontend

The Next.js application for campaigns, contacts, senders, templates, analytics,
and account settings. Start with the [project README](../README.md) or the
[development guide](../docs/development.md) for backend and database setup.

## Run

Requires Node.js 20.9+ and npm. Run these commands from `outreach_web/`:

```bash
npm ci
```

Copy [.env.example](.env.example) to `.env.local`, then fill in the Clerk keys
and the same `LOCAL_DEV_USER_ID` configured on the API. Keep
`BACKEND_IDENTITY_SECRET` empty when using this local authentication mode. Next.js reads its own
environment file; the repository-root `.env` is for the backend.

```bash
npm run dev
```

Open [localhost:3000](http://localhost:3000). The API must also be running for
authenticated application pages to load data.

## Commands

| Command | Purpose |
| --- | --- |
| `npm run dev` | Start the development server |
| `npm run build` | Compile and type-check the production app |
| `npm start` | Serve an existing production build |
| `npm run lint` | Run ESLint |
| `npx tsc --noEmit` | Check TypeScript without building |
| `node --test ../tests/schedule_draft.test.cjs` | Check schedule validation, pacing payloads, and timezone offsets |

## Where to make changes

| Area | Files |
| --- | --- |
| Routes and shared layout | [src/app/](src/app/) |
| Campaign setup and running view | [Campaign workspace](src/components/campaigns/workspace/) |
| Schedule dialog | [ScheduleDialog.tsx](src/components/campaigns/dialogs/ScheduleDialog.tsx) |
| Shared timezone picker | [timezone-picker.tsx](src/components/ui/timezone-picker.tsx) |
| Date and timezone conversion | [timezones.ts](src/lib/timezones.ts) |
| Authenticated API requests | [api.ts](src/lib/api.ts) and [backend proxy](src/app/api/backend/%5B...path%5D/route.ts) |
| Page authentication | [proxy.ts](src/proxy.ts) |
| Global and campaign styles | [globals.css](src/app/globals.css) and [campaign-workspace.css](src/components/campaigns/workspace/campaign-workspace.css) |

Use `useApiClient()` for API mutations so requests pass through the authenticated
proxy. `BACKEND_URL`, `BACKEND_IDENTITY_SECRET`, and `CLERK_SECRET_KEY` stay server-side;
only variables beginning with `NEXT_PUBLIC_` are intended for browser code.

For UI changes, check keyboard operation, loading and error states, and narrow
screens alongside the build. Read [AGENTS.md](AGENTS.md) and the installed Next.js
guides under `node_modules/next/dist/docs/` before changing framework behavior.

Hosting and release configuration are covered in the
[deployment guide](../docs/deployment.md).
