import {
  metadataCorsOptionsRequestHandler,
  protectedResourceHandlerClerk,
} from "@clerk/mcp-tools/next";

import { SITE_URL } from "@/lib/site";

/**
 * OAuth 2.0 Protected Resource Metadata (RFC 9728) at the well-known root.
 *
 * The challenge from `/mcp` points at `/.well-known/oauth-protected-resource/mcp`,
 * which is the suffix form RFC 9728 recommends for a resource with a path.
 * This root document covers clients that probe the reserved path directly.
 */
const handler = protectedResourceHandlerClerk({
  resource: `${SITE_URL}/mcp`,
  scopes_supported: ["outreach:manage"],
  resource_documentation: `${SITE_URL}/settings`,
});

export { handler as GET };
export const OPTIONS = metadataCorsOptionsRequestHandler();
