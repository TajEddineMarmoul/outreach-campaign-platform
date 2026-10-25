export interface DailyCapacity {
  campaign_id: number;
  timezone: string;
  date: string;
  sent_today: number;
  estimated_total_today: number;
  daily_limit: number;
  campaign_limit: number | null;
  sender_limit: number;
  competing_campaigns: number;
  state: string;
}

export default function DailyCapacityMeter({
  capacity,
  error = false,
}: {
  capacity?: DailyCapacity;
  error?: boolean;
}) {
  if (!capacity) {
    return <span className="app-muted app-daily-capacity-empty">{error ? "Capacity unavailable" : "Checking today…"}</span>;
  }
  const sent = Math.max(0, capacity.sent_today);
  const estimate = Math.max(sent, capacity.estimated_total_today);
  const limit = Math.max(0, capacity.daily_limit);
  const scale = Math.max(sent, estimate, limit, 1);
  const sentPercent = (sent / scale) * 100;
  const estimatePercent = (estimate / scale) * 100;
  const city = capacity.timezone.split("/").pop()?.replaceAll("_", " ") || capacity.timezone;
  const states: Record<string, string> = {
    paused: "Paused", draft: "Draft", ended: "Completed", stopped: "Stopped",
    day_disabled: "No sends today", window_closed: "Outside today’s window",
    no_senders: "No connected senders", test_mode: "Test mode",
  };
  const summary = `${sent} sent today; estimated total ${estimate}; daily limit ${limit}. ` +
    `Sender account limit: ${capacity.sender_limit}. ` +
    (capacity.campaign_limit === null ? "" : `Campaign limit: ${capacity.campaign_limit}. `) +
    `${capacity.competing_campaigns} other active campaigns share these senders. ` +
    `Today is ${capacity.date} in ${capacity.timezone}. ` +
    "The estimate includes sent emails and shares remaining account capacity with other campaigns. Timing and delivery errors may change it.";
  return (
    <div className="app-daily-capacity" role="img" aria-label={summary} title={summary}>
      <div className="app-daily-capacity-values" aria-hidden="true">
        <span><strong>{sent.toLocaleString()}</strong> sent</span>
        <span><strong>{estimate.toLocaleString()}</strong> est.</span>
        <span><strong>{limit.toLocaleString()}</strong> limit</span>
      </div>
      <div className="app-daily-capacity-track" aria-hidden="true">
        <span className="app-daily-capacity-estimate" style={{ width: `${estimatePercent}%` }} />
        <span className="app-daily-capacity-sent" style={{ width: `${sentPercent}%` }} />
        <span className="app-daily-capacity-unused" style={{ left: `${estimatePercent}%`, width: `${100 - estimatePercent}%` }} />
      </div>
      <div className="app-daily-capacity-caption" aria-hidden="true">
        <span>0</span><span>{states[capacity.state] || "Today"} · {city}</span>
      </div>
    </div>
  );
}
