import { auth } from "@clerk/nextjs/server";
import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import { NextResponse } from "next/server";

const MAX_CSV_FILE_BYTES = 100 * 1024 * 1024;

function isOwnedImportPath(pathname: string, userId: string): boolean {
  const parts = pathname.split("/");
  return (
    parts.length === 4 &&
    parts[0] === "campaign-imports" &&
    parts[1] === userId &&
    /^\d+$/.test(parts[2]) &&
    /\.csv$/i.test(parts[3])
  );
}

export const runtime = "nodejs";

export async function POST(request: Request): Promise<NextResponse> {
  const body = (await request.json()) as HandleUploadBody;
  try {
    const response = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname) => {
        const { userId } = await auth();
        if (!userId) throw new Error("Authentication required");
        if (!isOwnedImportPath(pathname, userId)) throw new Error("Invalid import upload path");
        return {
          allowedContentTypes: ["text/csv"],
          maximumSizeInBytes: MAX_CSV_FILE_BYTES,
          addRandomSuffix: true,
        };
      },
    });
    return NextResponse.json(response);
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "Could not prepare the CSV upload" },
      { status: 400 },
    );
  }
}
