import {
  metadataCorsOptionsRequestHandler,
  protectedResourceHandlerClerk,
} from "@clerk/mcp-tools/next";

const handler = protectedResourceHandlerClerk({
  scopes_supported: ["outreach:manage"],
  resource_documentation: "https://www.outreachemails.online/settings",
});

export { handler as GET };
export const OPTIONS = metadataCorsOptionsRequestHandler();
