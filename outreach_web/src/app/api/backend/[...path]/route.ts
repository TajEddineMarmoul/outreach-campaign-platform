import { auth, currentUser } from "@clerk/nextjs/server";
import { createHmac } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const maxDuration = 300;

async function proxyRequest(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { userId } = await auth();
  if (!userId) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const localDevUserId = process.env.LOCAL_DEV_USER_ID || "";
  // APP_ACCESS_TOKEN is a one-release compatibility source for the signing
  // key. It is no longer sent as a bearer credential to the API.
  const backendIdentitySecret = process.env.BACKEND_IDENTITY_SECRET || process.env.APP_ACCESS_TOKEN || "";
  const isLocalDevelopment =
    process.env.APP_ENV !== "production" && Boolean(localDevUserId);
  const backendUrl = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_API_URL || "";
  if ((!backendIdentitySecret && !isLocalDevelopment) || !backendUrl) {
    return NextResponse.json({ detail: "Backend connection is not configured" }, { status: 503 });
  }
  if (isLocalDevelopment && userId !== localDevUserId) {
    return NextResponse.json(
      { detail: "This local app is configured for a different account" },
      { status: 403 }
    );
  }

  const { path } = await context.params;
  const target = new URL(path.join("/"), `${backendUrl.replace(/\/$/, "")}/`);
  target.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete("authorization");
  headers.delete("cookie");
  headers.delete("host");
  headers.delete("content-length");
  if (isLocalDevelopment) {
    headers.set("authorization", `Bearer local_dev_${localDevUserId}`);
  } else {
    // Client-side metadata is only a UI hint. Ask Clerk from the server for
    // the one global-configuration route, then bind that result to the HMAC
    // assertion so it cannot be forged or replayed as an admin request.
    const needsAdminRole = ["/api/oauth/status", "/api/oauth/save-credentials-json"].includes(target.pathname);
    const clerkUser = needsAdminRole ? await currentUser() : null;
    const role = clerkUser?.publicMetadata?.role === "admin" ? "admin" : "member";
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const identityPayload = [timestamp, request.method.toUpperCase(), target.pathname, userId, role].join("\n");
    const signature = createHmac("sha256", backendIdentitySecret)
      .update(identityPayload, "utf8")
      .digest("hex");
    headers.set("x-backend-user-id", userId);
    headers.set("x-backend-user-role", role);
    headers.set("x-backend-auth-timestamp", timestamp);
    headers.set("x-backend-auth-signature", signature);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let backendResponse: Response;
  try {
    backendResponse = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { detail: "The backend service is unavailable" },
      { status: 503 }
    );
  }

  const responseHeaders = new Headers();
  for (const name of ["content-type", "content-disposition", "cache-control", "location"]) {
    const value = backendResponse.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }

  return new Response(backendResponse.body, {
    status: backendResponse.status,
    headers: responseHeaders,
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
