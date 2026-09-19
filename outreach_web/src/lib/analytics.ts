type AnalyticsParameter = string | number;

export type AnalyticsEvent =
  | "sign_up_complete"
  | "sender_connected"
  | "contacts_imported"
  | "campaign_launched";

declare global {
  interface Window {
    gtag?: (
      command: "event",
      eventName: AnalyticsEvent,
      parameters?: Record<string, AnalyticsParameter>,
    ) => void;
  }
}

function getStorageKey(key: string): string {
  return `outreach:analytics:${key}`;
}

export function trackEvent(
  eventName: AnalyticsEvent,
  parameters?: Record<string, AnalyticsParameter>,
): void {
  if (typeof window === "undefined") return;

  if (typeof window.gtag === "function") {
    window.gtag("event", eventName, parameters);
    return;
  }

  window.addEventListener(
    "outreach:analytics-ready",
    () => {
      window.gtag?.("event", eventName, parameters);
    },
    { once: true },
  );
}

/**
 * Records milestones that should only count once for the same user in a browser.
 * Event parameters deliberately exclude emails, names, campaign titles, and IDs.
 */
export function trackEventOnce(
  key: string,
  eventName: AnalyticsEvent,
  parameters?: Record<string, AnalyticsParameter>,
): void {
  if (typeof window === "undefined") return;

  const storageKey = getStorageKey(key);
  try {
    if (window.localStorage.getItem(storageKey)) return;
    window.localStorage.setItem(storageKey, "1");
  } catch {
    // Tracking remains best-effort when privacy settings disable local storage.
  }

  trackEvent(eventName, parameters);
}
