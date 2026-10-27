"use client";

import useSWR from "swr";
import {
  CalendarDays,
  Clock3,
  Globe2,
  CircleCheck,
  Pause,
  Mail,
  MailOpen,
  MailX,
  MessageSquareReply,
  MousePointerClick,
  Bot,
  AlertCircle,
  ArrowRight,
  Loader2,
  Play,
  SlidersHorizontal,
} from "lucide-react";
import { formatViewerWindow } from "@/lib/timezones";
import { API_URL } from "@/lib/api";
import { deliveryActivity } from "@/lib/deliveryActivity";
import DeliveryOutcomeBar, {
  deliveryOutcomeSummary,
  deliveryProgressLabel,
  getDeliveryProgress,
} from "@/components/campaigns/DeliveryOutcomeBar";

export interface CampaignProgress {
  campaign_status: string;
  timezone: string;
  total_recipients: number;
  sent_count: number;
  replied_count: number;
  automated_response_count: number;
  opened_count: number;
  clicked_count: number;
  bounced_count: number;
  send_error_count: number;
  failed_count: number;
  skipped_count: number;
  queued_count: number;
  is_active: boolean;
  is_sending: boolean;
  is_waiting: boolean;
  /** True when a closed campaign has recipients waiting to continue. */
  resumable?: boolean;
  next_batch_at: string | null;
  current_recipient: string | null;
  pause_reason: string | null;
  dry_run: boolean;
  send_mode?: string;
  campaign_sent_today?: number | null;
  campaign_daily_cap?: number | null;
  campaign_reserved_today?: number | null;
  campaign_remaining_today?: number | null;
  autopilot_schedule:
    { day: string; cap: number; start: string; end: string }[] | null;
}
export interface ScheduleSummary {
  timezone?: string;
  autopilot_schedule?: {
    day: string;
    cap: number;
    start: string;
    end: string;
  }[];
  send_settings?: {
    mode?: string;
    delay_minutes?: number;
    draft_scheduled_at?: string;
    dry_run?: boolean;
  };
}

function displayTime(value: string) {
  const [hour, minute] = value.split(":").map(Number);
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour >= 12 ? "PM" : "AM"}`;
}

export function CurrentSchedule({
  summary,
  progress,
  onEditSchedule,
}: {
  summary?: ScheduleSummary;
  progress?: CampaignProgress;
  onEditSchedule?: () => void;
}) {
  const zone = progress?.timezone || summary?.timezone || "UTC";
  const viewerZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const schedule =
    progress?.autopilot_schedule || summary?.autopilot_schedule || [];
  const mode = progress?.send_mode || summary?.send_settings?.mode;
  const uniform =
    schedule.length > 0 &&
    schedule.every(
      (day) => day.start === schedule[0].start && day.end === schedule[0].end,
    );
  const isWeekdays =
    schedule.length === 5 &&
    ["monday", "tuesday", "wednesday", "thursday", "friday"].every((day) =>
      schedule.some((item) => item.day === day),
    );
  const conversion =
    mode === "autopilot" && uniform && zone !== viewerZone
      ? formatViewerWindow(schedule[0].start, schedule[0].end, zone, viewerZone)
      : null;
  const paused = progress?.campaign_status === "paused";
  const stopped = ["stopped", "ended", "completed"].includes(progress?.campaign_status || "");
  const delivery = progress
    ? getDeliveryProgress({
        totalRecipients: progress.total_recipients,
        sentCount: progress.sent_count,
        undeliveredCount: progress.bounced_count,
        sendErrorCount: progress.send_error_count,
        skippedCount: progress.skipped_count,
      })
    : null;
  const deliveryFinished = Boolean(delivery?.isComplete);
  const stateTitle = paused
    ? "Sending is paused"
    : deliveryFinished
      ? "Delivery finished"
      : stopped
        ? "Sending stopped"
      : progress?.dry_run
        ? "Test mode is active"
        : progress?.is_active
          ? mode === "autopilot"
            ? "Autopilot is running"
            : "Campaign is running"
          : "Schedule saved";
  const next = progress?.next_batch_at
    ? new Date(progress.next_batch_at).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : null;
  const reason =
    progress?.pause_reason === "campaign_daily_cap_reached"
      ? "Today's campaign limit has been reached."
      : progress?.pause_reason === "daily_caps_reached"
        ? "All senders have reached today's limit."
        : null;
  const hasDailyCapacity =
    mode === "autopilot" && progress?.campaign_daily_cap != null;
  return (
    <section className="campaign-panel campaign-schedule-card">
      <div className="campaign-panel-heading">
        <h2>Current schedule</h2>
        {onEditSchedule && <button className="campaign-text-button" onClick={onEditSchedule}><SlidersHorizontal size={16} /> Edit</button>}
      </div>
      <div className="campaign-schedule-lines">
        <p>
          <CalendarDays />
          <span>
            {mode === "autopilot" && schedule.length
              ? isWeekdays
                ? "Mon–Fri"
                : schedule.map((day) => day.day.slice(0, 3)).join(", ")
              : mode === "schedule"
                ? "Scheduled send"
                : mode === "send_now"
                  ? "Send when launched"
                  : "Not set yet"}
          </span>
        </p>
        {mode === "autopilot" && uniform && (
          <p>
            <Clock3 />
            <span>
              {displayTime(schedule[0].start)}–{displayTime(schedule[0].end)}
            </span>
          </p>
        )}
        {mode === "autopilot" &&
          !uniform &&
          schedule.map((day) => (
            <p key={day.day}>
              <Clock3 />
              <span className="capitalize">
                {day.day.slice(0, 3)} · {displayTime(day.start)}–
                {displayTime(day.end)}
              </span>
            </p>
          ))}
        {mode === "schedule" && summary?.send_settings?.draft_scheduled_at && (
          <p>
            <Clock3 />
            <span>
              {new Date(
                summary.send_settings.draft_scheduled_at,
              ).toLocaleString(undefined, {
                timeZone: zone,
                dateStyle: "medium",
                timeStyle: "short",
              })}
            </span>
          </p>
        )}
        <p>
          <Globe2 />
          <span>{zone}</span>
        </p>
        {conversion && (
          <p className="campaign-local-time">
            <Clock3 />
            <span>
              {conversion.range} in{" "}
              {viewerZone.split("/").pop()?.replaceAll("_", " ")}
              <small>{conversion.date}</small>
            </span>
          </p>
        )}
      </div>
      {hasDailyCapacity && (
        <section className="campaign-daily-capacity" aria-label="Today’s sending capacity">
          <div className="campaign-daily-capacity-heading">
            <div>
              <p>Full-day target</p>
              <strong>{progress!.campaign_daily_cap} emails per full sending window</strong>
            </div>
          </div>
          <dl className="campaign-daily-capacity-stats">
            <div>
              <dt>Sent today</dt>
              <dd>{progress!.campaign_sent_today ?? 0}</dd>
            </div>
            <div>
              <dt>Scheduled today</dt>
              <dd>{progress!.campaign_reserved_today ?? 0}</dd>
            </div>
            <div>
              <dt>Daily allowance left</dt>
              <dd>{progress!.campaign_remaining_today ?? 0}</dd>
            </div>
          </dl>
          <p className="campaign-daily-capacity-note">
            The daily maximum includes emails already sent. A partial day may send fewer; the pace never speeds up to catch up. Sender limits also apply.
          </p>
        </section>
      )}
      {progress && (
        <div
          className={`campaign-schedule-state ${paused || (stopped && !deliveryFinished) ? "is-paused" : ""}`}
        >
          {paused ? (
            <Pause size={25} />
          ) : stopped ? (
            <CircleCheck size={25} />
          ) : (
            <CircleCheck size={30} />
          )}
          <div>
            <strong>{stateTitle}</strong>
            <p>
              {paused
                ? "Resume when you're ready. No new emails will start."
                : deliveryFinished
                  ? `All ${delivery!.totalRecipients} recipient${delivery!.totalRecipients === 1 ? " has" : "s have"} a final delivery result.`
                  : stopped
                    ? `${delivery?.remainingCount || 0} recipient${delivery?.remainingCount === 1 ? " remains" : "s remain"} without a final delivery result.`
                  : progress.is_sending
                    ? "Sending the current batch"
                    : reason ||
                      (next
                        ? `Next check: ${next}`
                        : "Waiting for the next eligible batch")}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}

export default function CampaignOverview({
  campaignId,
  summary,
  onActivity,
  onEditSchedule,
  onResume,
  resuming = false,
}: {
  campaignId: string;
  summary?: ScheduleSummary;
  onActivity: () => void;
  onEditSchedule?: () => void;
  onResume?: () => void;
  resuming?: boolean;
}) {
  const {
    data: progress,
    error,
    isLoading,
  } = useSWR<CampaignProgress>(
    `${API_URL}/api/campaigns/${campaignId}/send-progress`,
    { refreshInterval: 3000 },
  );
  const { data: recipients, error: recipientsError } = useSWR<{
    items: {
      contact_id: number;
      email: string;
      custom_fields: Record<string, unknown>;
      status: string;
    }[];
    total?: number;
  }>(
    `${API_URL}/api/campaigns/${campaignId}/recipients?page=1&page_size=3&pending_only=true`,
    { refreshInterval: 3000 },
  );
  const { data: logs, error: logsError } = useSWR<{
    items: {
      id: number;
      status: string;
      response_status: string | null;
      open_count: number;
      click_count: number;
      recipient_email: string;
      sent_at: string | null;
      bounced_at: string | null;
      responded_at: string | null;
      last_opened_at: string | null;
      last_clicked_at: string | null;
      created_at: string | null;
    }[];
  }>(`${API_URL}/api/campaigns/${campaignId}/send-logs?page=1&page_size=3`, {
    refreshInterval: 3000,
  });
  if (isLoading)
    return (
      <div className="campaign-empty">
        <Loader2 className="animate-spin" />
        Loading campaign progress…
      </div>
    );
  if (error || !progress)
    return (
      <div className="campaign-notice is-error" role="alert">
        Could not load campaign progress. Refresh the page to try again.
      </div>
    );
  const delivery = getDeliveryProgress({
    totalRecipients: progress.total_recipients,
    sentCount: progress.sent_count,
    undeliveredCount: progress.bounced_count,
    sendErrorCount: progress.send_error_count,
    skippedCount: progress.skipped_count,
  });
  const deliveryStats = [
    { value: progress.sent_count, label: "sent", style: "is-blue" },
    { value: progress.bounced_count, label: "undelivered", style: "is-orange" },
    { value: progress.send_error_count, label: "send errors", style: "is-red" },
    { value: progress.skipped_count, label: "skipped", style: "is-muted" },
    { value: delivery.remainingCount, label: "remaining", style: "" },
  ];
  // Recipients already accepted for sending. This excludes send errors, which
  // are summary-only and are not re-queued by "Resume sending".
  const pendingCount = recipients?.total ?? delivery.remainingCount;
  const responseStats = [
    { value: progress.replied_count, label: "human replies", style: "is-blue" },
    { value: progress.automated_response_count, label: "automated replies", style: "is-amber" },
  ];
  const engagementStats = [
    { value: progress.opened_count, label: "opened", style: "is-blue" },
    { value: progress.clicked_count, label: "clicked", style: "is-green" },
  ];
  return (
    <>
      {progress.dry_run && (
        <div className="campaign-notice">
          Test mode is active. No real emails are being sent.
        </div>
      )}
      {progress.resumable && ["ended", "stopped"].includes(progress.campaign_status) && (
        <div className="campaign-notice is-resume" role="status">
          <div>
            <strong>
              {pendingCount} recipient
              {pendingCount === 1 ? " has" : "s have"} no delivery result yet.
            </strong>
            <span>
              {progress.campaign_status === "ended"
                ? "Delivery is finished. Resume sending to reach the recipients you added."
                : progress.campaign_status === "stopped"
                  ? "Sending stopped before every recipient had a result. Resume sending to continue with the recipients still waiting."
                  : "Resume sending when you are ready to continue."}
            </span>
          </div>
          {onResume && (
            <button
              className="campaign-button is-primary"
              type="button"
              onClick={onResume}
              disabled={resuming}
            >
              {resuming ? (
                <Loader2 size={17} className="animate-spin" />
              ) : (
                <Play size={17} />
              )}
              {resuming ? "Resuming…" : "Resume sending"}
            </button>
          )}
        </div>
      )}
      <div className="campaign-overview-grid">
        <div className="campaign-outcome-board">
          <section>
            <h2>Delivery</h2>
            <dl className="campaign-stats is-delivery">
              {deliveryStats.map((stat) => (
                <div key={stat.label}>
                  <dd className={stat.style}>{stat.value}</dd>
                  <dt>{stat.label}</dt>
                </div>
              ))}
            </dl>
          </section>
          <section>
            <h2>Responses</h2>
            <dl className="campaign-stats is-responses">
              {responseStats.map((stat) => (
                <div key={stat.label}>
                  <dd className={stat.style}>{stat.value}</dd>
                  <dt>{stat.label}</dt>
                </div>
              ))}
            </dl>
          </section>
          <section>
            <h2>Engagement</h2>
            <p className="text-xs text-slate-500">Tracking does not confirm delivery. Failed sends are excluded.</p>
            <dl className="campaign-stats is-responses">
              {engagementStats.map((stat) => (
                <div key={stat.label}>
                  <dd className={stat.style}>{stat.value}</dd>
                  <dt>{stat.label}</dt>
                </div>
              ))}
            </dl>
          </section>
        </div>
        <section className="campaign-panel campaign-progress-card">
          <div className="campaign-progress-heading">
            <h2>{delivery.isComplete ? "Delivery finished" : "Sending progress"}</h2>
            <p className="campaign-progress-label">
              {deliveryProgressLabel(delivery)}
            </p>
          </div>
          <DeliveryOutcomeBar progress={delivery} className="campaign-delivery-outcomes" />
          {delivery.isComplete && (
            <p className="campaign-delivery-summary">
              {deliveryOutcomeSummary(delivery)}. No recipients remain to send.
            </p>
          )}
          <div className="campaign-upcoming-table">
            <table>
              <thead>
                <tr>
                  <th>Upcoming recipients</th>
                  <th>Company</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {recipients?.items.map((recipient, index) => {
                  const fields = recipient.custom_fields;
                  const name = String(
                    fields.full_name || fields.first_name || recipient.email,
                  );
                  const initials = name
                    .split(/[\s@.]+/)
                    .slice(0, 2)
                    .map((part) => part[0])
                    .join("")
                    .toUpperCase();
                  return (
                    <tr key={recipient.contact_id}>
                      <td>
                        <span className={`campaign-avatar avatar-${index}`}>
                          {initials}
                        </span>
                        <span>{name}</span>
                      </td>
                      <td>
                        {String(fields.company || fields.company_name || "—")}
                      </td>
                      <td>
                        {progress.campaign_status === "paused"
                          ? "Paused"
                          : recipient.status === "queued"
                            ? "Queued"
                            : "Ready"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {!recipients?.items.length && (
            <p className="campaign-table-empty">
              {recipientsError
                ? "Could not load upcoming recipients."
                : !recipients
                  ? "Loading recipients…"
                  : "No recipients waiting to send."}
            </p>
          )}
        </section>
        <CurrentSchedule
          summary={summary}
          progress={progress}
          onEditSchedule={onEditSchedule}
        />
      </div>
      <section className="campaign-panel campaign-recent-activity">
        <div className="campaign-panel-heading">
          <h2>Recent activity</h2>
          <button className="campaign-text-button" onClick={onActivity}>
            View all <ArrowRight size={16} />
          </button>
        </div>
        {logs?.items.map((log) => {
          const activity = deliveryActivity(log);
          return (
          <div key={log.id} className="campaign-activity-row">
            {activity.kind === "bounced" ? (
              <MailX className="is-orange" />
            ) : activity.kind === "failed" ? (
              <AlertCircle className="is-red" />
            ) : activity.kind === "replied" ? (
              <MessageSquareReply className="is-blue" />
            ) : activity.kind === "automated_response" ? (
              <Bot />
            ) : activity.kind === "clicked" ? (
              <MousePointerClick className="is-green" />
            ) : activity.kind === "opened" ? (
              <MailOpen className="is-blue" />
            ) : (
              <Mail className="is-blue" />
            )}
            <span>
              {activity.label}{" "}
              {log.recipient_email}
            </span>
            <time dateTime={activity.at || undefined}>
              {activity.at
                ? new Date(activity.at).toLocaleTimeString(
                    undefined,
                    { hour: "numeric", minute: "2-digit" },
                  )
                : "—"}
            </time>
          </div>
          );
        })}
        {!logs?.items.length && (
          <p className="campaign-table-empty">
            {logsError
              ? "Could not load recent activity."
              : !logs
                ? "Loading activity…"
                : "Delivery activity will appear here once sending begins."}
          </p>
        )}
      </section>
    </>
  );
}
