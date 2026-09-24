import { auth } from "@clerk/nextjs/server";
import { del } from "@vercel/blob";
import { NextResponse } from "next/server";

const PRIVATE_IMPORT_HOST_SUFFIX = ".private.blob.vercel-storage.com";

function isPrivateImportUrl(value: unknown, userId: string): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    const parts = url.pathname.split("/");
    return (
      url.protocol === "https:" &&
      url.hostname.endsWith(PRIVATE_IMPORT_HOST_SUFFIX) &&
      parts.length === 5 &&
      parts[1] === "campaign-imports" &&
      parts[2] === userId &&
      /^\d+$/.test(parts[3]) &&
      /\.csv$/i.test(parts[4]) &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
}

export async function POST(request: Request): Promise<NextResponse> {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ detail: "Authentication required" }, { status: 401 });

  const body = await request.json().catch(() => null) as { urls?: unknown } | null;
  const urls = Array.isArray(body?.urls) ? body.urls.filter((url) => isPrivateImportUrl(url, userId)) : [];
  if (!urls.length || urls.length > 20) return NextResponse.json({ detail: "No valid import files were supplied" }, { status: 422 });

  await del(urls);
  return new NextResponse(null, { status: 204 });
}
