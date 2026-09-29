# Outreach campaign MCP server

The MCP server lets an AI client manage the owner's Outreach workspace through
the production API. Each API request still passes the app's campaign and contact
ownership checks. It cannot administer global Gmail OAuth credentials or create
access tokens.

There are two ways to connect, and both expose the same tools:

| Mode | Endpoint | Authentication |
| --- | --- | --- |
| Hosted | `https://www.outreachemails.online/mcp` | Sign in at Clerk; the agent receives a scoped OAuth token |
| Local | stdio process on your machine | A revocable workspace access token file |

## Hosted connection

Add the server URL to the agent:

```
https://www.outreachemails.online/mcp
```

The agent registers itself and opens a Clerk sign-in page. Sign in with the
Outreach account that owns the workspace, review the requested
`outreach:manage` scope, and approve. No password or token is shared with the
agent, and access can be revoked from the Clerk account's connected
applications.

The request path has two independent checks. The website verifies the agent's
Clerk OAuth token and requires the `outreach:manage` scope; it then signs a
short-lived assertion for that Clerk user, which the API verifies before it
serves any workspace data. The MCP endpoint rejects unsigned requests.

Client onboarding is configured in the Clerk dashboard under
**Developers → OAuth applications → Settings**: dynamic client registration and
client ID metadata documents are both published, and `outreach:manage` is a
default scope so a client that omits the scope parameter still receives it.

## Local connection

From the repository root, install the extra local dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
```

In Outreach, open **Settings → AI campaign access → Create access token**.
Copy the token once. Save it as a single line at
`%LOCALAPPDATA%\Outreach\mcp-token`, or set `OUTREACH_MCP_TOKEN_FILE` to
another private path. `OUTREACH_MCP_TOKEN` is also supported for hosts that
inject secrets into the server process. The token is valid for one year and
can be revoked on the same Settings page.

On a trusted workstation that already has database access, the one-time
provisioning command can create the token and save the file without showing
the secret in the terminal:

```powershell
.\.venv\Scripts\python.exe scripts\provision_mcp_token.py --user-id <current-Clerk-user-ID>
```

Add the stdio server to Codex, using absolute paths:

```powershell
codex mcp add outreach -- C:\path\to\outreach_app\.venv\Scripts\python.exe C:\path\to\outreach_app\scripts\outreach_mcp_server.py
```

Restart the MCP client after registration. The server defaults to
`https://api.outreachemails.online`; `OUTREACH_MCP_API_URL` can point it at
another HTTPS deployment or a localhost development server. No database
credentials or Clerk secrets are placed in Codex's MCP configuration.

## Tools

| Tool | Use |
| --- | --- |
| `workspace_overview` | Find campaigns, sender groups, and workspace limits in one result. |
| `inspect_campaign` | See message, schedule, sender group, attachments, validation, audience facets, and progress. |
| `search_audience` | Search saved contacts or a campaign's recipients by status, source, company, title, industry, country, or custom field. |
| `create_campaign` | Create and optionally configure a draft and import a pasted CSV audience in one call. |
| `update_campaign_setup` | Update message, sender group, schedule draft, and tracking in one call. |
| `add_campaign_recipients` | Add saved contacts, people, CSV data, or a public Google Sheet. |
| `attach_saved_audience_by_filter` | Select matching saved contacts and attach them in one call. |
| `preview_campaign` | Render personalized samples and run recipient validation. |
| `launch_campaign` | Send now, schedule, or start Autopilot using the API's launch checks. |
| `control_campaign` | Pause, resume, or stop future sends. |
| `campaign_activity` | Get progress and send/reply/bounce logs together. |
| `manage_campaign_recipient` | Remove, reset, or clear campaign recipients. |
| `duplicate_campaign` | Copy a campaign into a draft. |
| `delete_campaign` | Permanently delete a campaign after its exact name is supplied. |
| `update_autopilot_limits` | Change daily caps while Autopilot is running. |
| `sender_groups` | Manage groups and start a sender connection. |
| `manage_sender` | Edit a sender's daily cap, display name, group, or default status. |
| `manage_template` | Manage reusable message templates. |
| `workspace_settings` | View or change timezone and sending safety limits. |
| `manage_contact` | Save a contact or add an address to do-not-contact. |

Create and update tools can perform several API calls. If a later step fails,
they return the campaign ID and the steps that succeeded so the client can
resume without repeating them. Delivery and deletion remain separate tools:
building a draft never sends email.

## Maintenance

The API schema includes `alembic/versions/0015_workspace_access_tokens.py`.
Apply it before using workspace tokens in production.

To stop a hosted client, revoke it in Clerk under the account's connected
applications. To stop a local client, revoke its token in Settings and create a
new one. Existing campaigns and contacts are unaffected.

Both modes call the same API, so an agent that has `outreach:manage` can read
and change campaigns, audiences, senders, contacts, templates, and settings,
including sending email. Treat an approved agent as equivalent to a signed-in
browser session.

