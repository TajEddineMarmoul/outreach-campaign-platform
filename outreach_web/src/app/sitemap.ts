import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    {
      url: SITE_URL,
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      // Public on purpose: agents and account owners use it to connect a client.
      url: `${SITE_URL}/mcp-guide`,
      changeFrequency: "monthly",
      priority: 0.6,
    },
  ];
}
