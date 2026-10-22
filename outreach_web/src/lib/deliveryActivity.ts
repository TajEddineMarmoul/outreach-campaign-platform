type DeliveryActivityLog = {
  status: string;
  response_status?: string | null;
  open_count?: number;
  click_count?: number;
  bounced_at?: string | null;
  responded_at?: string | null;
  last_clicked_at?: string | null;
  last_opened_at?: string | null;
  sent_at?: string | null;
  created_at?: string | null;
};

export function deliveryActivity(log: DeliveryActivityLog) {
  const sentAt = log.sent_at || log.created_at || null;
  if (log.status === "bounced") {
    return { kind: "bounced", label: "Email was undelivered to", at: log.bounced_at || sentAt };
  }
  if (["failed", "error"].includes(log.status)) {
    return { kind: "failed", label: "Send error for", at: sentAt };
  }
  if (log.response_status === "replied") {
    return { kind: "replied", label: "Reply received from", at: log.responded_at || sentAt };
  }
  if (log.response_status === "automated_response") {
    return { kind: "automated_response", label: "Automated reply from", at: log.responded_at || sentAt };
  }
  if (log.click_count) {
    return { kind: "clicked", label: "Link tracking recorded for", at: log.last_clicked_at || sentAt };
  }
  if (log.open_count) {
    return { kind: "opened", label: "Open tracking recorded for", at: log.last_opened_at || sentAt };
  }
  return { kind: "sent", label: log.status === "test_sent" ? "Test recorded for" : "Email sent to", at: sentAt };
}
