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
