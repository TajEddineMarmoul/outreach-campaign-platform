import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

// Paths served to signed-out visitors. Anything an AI agent needs in order to
// discover and connect to the MCP server belongs here, or the agent is sent to
// a sign-in page instead of the document it asked for.
const PUBLIC_PATHS = new Set([
  "/",
  "/animation-lab",
  // Agent-facing: a text version of the product and the connect guide, both
  // advertised from the document head of every page.
  "/index.md",
  "/llms.txt",
  "/mcp-guide",
  "/robots.txt",
  "/sitemap.xml",
  "/opengraph-image",
  "/mcp",
]);

// Prefixes served to signed-out visitors. Every well-known document is public,
// matched by prefix so a file extension cannot change how a path is handled.
const PUBLIC_PREFIXES = ["/.well-known/", "/sign-in", "/sign-up"];

function isPublicPath(pathname: string): boolean {
  return (
    PUBLIC_PATHS.has(pathname) ||
    PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))
  );
}

export default clerkMiddleware(async (auth, request) => {
  const url = new URL(request.url);

  if (isPublicPath(url.pathname)) return;

  const { userId } = await auth();
  if (!userId) {
    if (url.pathname.startsWith("/api/")) {
      return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
    }

    const signInUrl = new URL("/sign-in", request.url);
    signInUrl.searchParams.set("redirect_url", request.url);
    return NextResponse.redirect(signInUrl);
  }
});

export const config = {
  matcher: [
    "/((?!_next|.well-known|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
