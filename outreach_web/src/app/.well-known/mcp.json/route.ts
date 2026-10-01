import { SITE_URL } from "@/lib/site";

/**
 * MCP server card for agent discovery (SEP-2127).
 *
 * Published on the same origin as the MCP endpoint, because a client that
 * fetches this card must be able to reach the server it advertises. The card
 * carries no authentication field: a client calls the endpoint, receives the
 * OAuth challenge, and follows it to
 * /.well-known/oauth-protected-resource/mcp.
 */
export const dynamic = "force-static";

const CARD = {
  $schema: "https://static.modelcontextprotocol.io/schemas/2025-10-17/server.schema.json",
  name: "outreach-campaigns",
  description:
    "Manage Outreach email campaigns: inspect and update campaign setup, filter audiences, add or remove recipients, preview personalized messages, launch, pause, resume or stop sending, read delivery activity, and manage senders, templates, contacts and workspace sending limits.",
  version: "1.0.0",
  // Spelled out here because this card is often the only document an agent
  // reads before it tries to connect. Without it a client tends to look for an
  // API key, or reports the endpoint's 401 as an outage.
  instructions: [
    "Authentication is OAuth 2.0 authorization code with PKCE. There is no API key and no token file.",
    `Required scope: outreach:manage. Endpoint: ${SITE_URL}/mcp (streamable HTTP, stateless).`,
    `Register a client at the registration_endpoint from ${SITE_URL}/.well-known/oauth-authorization-server using token_endpoint_auth_method "none".`,
    "Send the account owner to the authorization endpoint to sign in and approve access; no credential is shared with the agent.",
    `A 401 from the endpoint is expected and correct: read its WWW-Authenticate header for the scope and the resource metadata URL.`,
    `Full setup guide: ${SITE_URL}/llms.txt`,
  ].join(" "),
  documentation: `${SITE_URL}/mcp-guide`,
  remotes: [
    {
      type: "streamable-http",
      url: `${SITE_URL}/mcp`,
    },
  ],
} as const;

export function GET() {
  return Response.json(CARD, {
    headers: {
      "Cache-Control": "public, max-age=3600",
    },
  });
}

export function HEAD() {
  return new Response(null, {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=3600",
    },
  });
}
