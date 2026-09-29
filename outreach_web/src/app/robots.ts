import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: ["/", "/.well-known/mcp.json"],
      disallow: [
        "/analytics/",
        "/animation-lab/",
        "/api/",
        "/campaigns/",
        "/contacts/",
        "/senders/",
        "/settings/",
        "/sign-in/",
        "/sign-up/",
        "/templates/",
        "/welcome/",
      ],
    },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
