import { verifyClerkToken } from "@clerk/mcp-tools/next";
import { auth } from "@clerk/nextjs/server";
import { createHmac } from "node:crypto";
import { withMcpAuth } from "mcp-handler";

export const runtime = "nodejs";
export const maxDuration = 300;

const RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp";
const REQUIRED_SCOPE = "outreach:manage";

async function proxyMcpRequest(request: Request): Promise<Response> {
  const userId = request.auth?.extra?.userId;
  if (typeof userId !== "string" || !userId) {
    return Response.json({ detail: "Authentication required" }, { status: 401 });
  }

  const backendUrl = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_API_URL || "";
  const identitySecret = process.env.BACKEND_IDENTITY_SECRET || process.env.APP_ACCESS_TOKEN || "";
  const localDevUserId = process.env.LOCAL_DEV_USER_ID || "";
  const isLocalDevelopment = process.env.APP_ENV !== "production" && Boolean(localDevUserId);
  if (!backendUrl || (!identitySecret && !isLocalDevelopment)) {
    return Response.json({ detail: "Backend connection is not configured" }, { status: 503 });
  }
  if (isLocalDevelopment && userId !== localDevUserId) {
    return Response.json({ detail: "This local app is configured for a different account" }, { status: 403 });
  }

  let target: URL;
  try {
    target = new URL("/mcp", backendUrl);
  } catch {
    return Response.json({ detail: "Backend URL is invalid" }, { status: 503 });
  }
  if (target.protocol !== "https:" && !(isLocalDevelopment && target.protocol === "http:" && ["localhost", "127.0.0.1"].includes(target.hostname))) {
    return Response.json({ detail: "Backend URL must use HTTPS" }, { status: 503 });
  }

  // Carry only MCP protocol headers. Never forward the Clerk OAuth token,
  // browser cookies, Origin, or user-supplied backend identity headers.
  const headers = new Headers();
  for (const name of ["content-type", "accept", "mcp-protocol-version", "mcp-session-id", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (isLocalDevelopment) {
    headers.set("authorization", `Bearer local_dev_${localDevUserId}`);
  } else {
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const payload = [timestamp, request.method.toUpperCase(), target.pathname, userId, "member"].join("\n");
    const signature = createHmac("sha256", identitySecret).update(payload, "utf8").digest("hex");
    headers.set("x-backend-user-id", userId);
    headers.set("x-backend-user-role", "member");
    headers.set("x-backend-auth-timestamp", timestamp);
    headers.set("x-backend-auth-signature", signature);
  }

  try {
    const backendResponse = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
      redirect: "manual",
      cache: "no-store",
    });
    if (backendResponse.status >= 300 && backendResponse.status < 400) {
      return Response.json({ detail: "Backend MCP endpoint redirected" }, { status: 502 });
    }
    const responseHeaders = new Headers();
    for (const name of ["content-type", "cache-control", "mcp-session-id", "www-authenticate"]) {
      const value = backendResponse.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new Response(backendResponse.body, { status: backendResponse.status, headers: responseHeaders });
  } catch {
    return Response.json({ detail: "Backend MCP endpoint is unavailable" }, { status: 503 });
  }
}

const handler = withMcpAuth(
  proxyMcpRequest,
  async (_, token) => verifyClerkToken(await auth({ acceptsToken: "oauth_token" }), token),
  {
    required: true,
    requiredScopes: [REQUIRED_SCOPE],
    resourceMetadataPath: RESOURCE_METADATA_PATH,
  },
);

export { handler as GET, handler as POST, handler as DELETE };
