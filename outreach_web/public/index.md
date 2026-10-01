# Outreach

Outreach is an email campaign platform. You import contacts, write one message
that is personalized for each contact from your own spreadsheet columns, and send
it on a schedule you control.

- Website: https://www.outreachemails.online
- App: https://www.outreachemails.online/campaigns
- Support: Gmail and Google Workspace sender accounts

## What it does

- **Personalization** — keep an email column plus any other columns, and each
  message is written per contact from those values.
- **Scheduling** — send immediately, at a set time, or across a daily window with
  limits so a mailbox is not flooded.
- **Safety** — daily caps, warmup, bounce-rate and consecutive-error thresholds,
  a do-not-contact list, and a deliberate launch step. Importing contacts and
  writing a message never sends email.
- **Tracking** — opens, replies, bounces, and per-campaign delivery progress.
- **Templates** — save a message once and reuse it.
- **Senders** — connect Gmail accounts and group them so campaigns send from the
  right mailbox.

## For AI agents

Outreach runs a remote Model Context Protocol server, so an agent can manage a
workspace directly: campaigns, audiences, senders, contacts, templates and
sending limits.

- **Endpoint:** https://www.outreachemails.online/mcp
- **Transport:** streamable HTTP, stateless
- **Authentication:** OAuth 2.0 authorization code with PKCE — there is no API key
- **Required scope:** `outreach:manage`

Setup guide and full details: https://www.outreachemails.online/llms.txt

Human-readable version: https://www.outreachemails.online/mcp-guide

Server card: https://www.outreachemails.online/.well-known/mcp.json

A `401` from the endpoint is the expected first response. Its
`WWW-Authenticate` header names the required scope and the metadata URL to fetch
next.

## Getting started

1. Sign up at https://www.outreachemails.online/sign-up
2. Connect a Gmail sender under **Senders**.
3. Import contacts, or paste them, under **Contacts**.
4. Create a campaign, review the audience and a personalized preview, then launch.

## Notes

- Importing and drafting never send email. Sending starts only after you launch.
- Every workspace is scoped to one signed-in account.
- An agent approved through OAuth can read and change the same data as that
  signed-in account, and can send email. It cannot administer global Gmail OAuth
  credentials or create access tokens.
