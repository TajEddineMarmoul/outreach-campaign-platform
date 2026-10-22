"use client";

import { useState } from "react";
import useSWR from "swr";
import { ChevronLeft, ChevronRight, Clock, Download, ExternalLink, Loader2, MailOpen, MousePointerClick, PauseCircle, RefreshCw, Send } from "lucide-react";
import { API_URL, checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { StatusBadge } from "@/components/app-ui";

interface LogEntry {
  id: number;
  recipient_email: string;
  sender_email: string;
  subject: string;
  gmail_thread_id: string | null;
  status: string;
  response_status: string | null;
  open_count: number;
  click_count: number;
  first_opened_at: string | null;
  last_opened_at: string | null;
  first_clicked_at: string | null;
  last_clicked_at: string | null;
  error_message: string | null;
  attempt_number: number;
  attempt_count: number;
  sent_at: string | null;
  bounced_at: string | null;
  responded_at: string | null;
  created_at: string | null;
}

interface LogsResponse { items: LogEntry[]; total: number; page: number; page_size: number; pages: number; }
type EngagementFilter = "all" | "opened" | "clicked" | "replied" | "undelivered";
const ENGAGEMENT_FILTERS: { value: EngagementFilter; label: string }[] = [
  { value: "all", label: "All activity" },
  { value: "undelivered", label: "Undelivered" },
  { value: "opened", label: "Opened" },
  { value: "clicked", label: "Clicked" },
  { value: "replied", label: "Replied" },
];
interface CampaignState {
  campaign_status: string;
  is_sending: boolean;
  is_waiting: boolean;
  next_batch_at: string | null;
  pause_reason: string | null;
}

function activityDetail(log: LogEntry) {
  if (log.error_message) return log.error_message;
  if (log.response_status === "replied") return "A person wrote back";
  if (log.response_status === "automated_response") {
    return "Out-of-office or automated response";
  }
  if (log.status === "bounced") {
    return "The recipient mail server returned this email";
  }
  if (log.status === "sent" || log.status === "success") {
    return "Accepted by Gmail";
  }
  return "—";
}

function gmailThreadUrl(log: LogEntry) {
  return `https://mail.google.com/mail/u/0/?authuser=${encodeURIComponent(log.sender_email)}#all/${encodeURIComponent(log.gmail_thread_id || "")}`;
}

export default function LogsSection({ campaignId }: { campaignId: string }) {
  const [page, setPage] = useState(1);
  const [engagement, setEngagement] = useState<EngagementFilter>("all");
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState("");
  const { data, isLoading, mutate } = useSWR<LogsResponse>(
    `${API_URL}/api/campaigns/${campaignId}/send-logs?page=${page}&page_size=10&engagement=${engagement}`
  );
  const { data: state, mutate: mutateState } = useSWR<CampaignState>(
    `${API_URL}/api/campaigns/${campaignId}/send-progress`,
    { refreshInterval: 3000 }
  );
  const { authFetch } = useApiClient();

  const exportLogs = async () => {
    const response = await authFetch(`${API_URL}/api/campaigns/${campaignId}/send-logs/export`);
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `campaign_${campaignId}_send_log.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-slate-400">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading logs...
      </div>
    );
  }

  const logs = data?.items || [];
  const refresh = async () => {
    setSyncing(true);
    setSyncError("");
    try {
      const response = await authFetch(`${API_URL}/api/campaigns/${campaignId}/sync-gmail-activity`, { method: "POST" });
      if (!response.ok) await checkResponse(response);
      const result = await response.json();
      await Promise.all([mutate(), mutateState()]);
      if (result.errors || result.reconnect_required) {
        throw new Error(result.reconnect_required
          ? "Reconnect the affected Gmail account to check delivery failures."
          : "Gmail could not be fully checked. Try again or reconnect the affected account.");
      }
    } catch (error) {
      setSyncError(errorMessage(error, "Could not check Gmail right now."));
    } finally {
      setSyncing(false);
    }
  };

  if (data?.total === 0 && engagement === "all" && !state?.is_waiting && !state?.is_sending && state?.campaign_status !== "paused") {
    return (
      <div className="text-center py-16">
        <p className="text-sm text-slate-400 mb-4">No send logs yet.</p>
        <button
          onClick={() => void refresh()}
          className="text-xs text-blue-600 hover:text-blue-800 font-medium flex items-center gap-1 mx-auto"
        >
          <RefreshCw className="w-3 h-3" />
          Refresh
        </button>
      </div>
    );
  }

  return (
    <div>
      {state && (state.is_sending || state.is_waiting || state.campaign_status === "paused") && (
        <div className={`flex items-start gap-3 mb-4 px-4 py-3 border rounded-lg ${state.campaign_status === "paused" ? "border-amber-200 bg-amber-50 text-amber-800" : "border-blue-200 bg-blue-50 text-blue-800"}`}>
          {state.is_sending ? <Send className="w-4 h-4 mt-0.5 shrink-0" /> : state.campaign_status === "paused" ? <PauseCircle className="w-4 h-4 mt-0.5 shrink-0" /> : <Clock className="w-4 h-4 mt-0.5 shrink-0" />}
          <div>
            <div className="text-sm font-semibold">
              {state.is_sending ? "Sending a batch now" : state.campaign_status === "paused" ? "Campaign paused" : "Waiting for the next batch"}
            </div>
            <div className="text-xs mt-0.5 opacity-80">
              {state.campaign_status === "paused"
                ? state.pause_reason === "daily_caps_reached" ? "All senders reached today's daily cap." : "Sending will continue after the campaign is resumed."
                : state.next_batch_at ? `Next batch: ${new Date(state.next_batch_at).toLocaleString()}` : "The worker is checking for due work."}
            </div>
          </div>
        </div>
      )}
      {syncError && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800" role="alert">
          {syncError}
        </div>
      )}
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs text-slate-400">{data?.total || 0} delivery entries</span>
        <div className="flex gap-2">
          <button
            onClick={() => void refresh()}
            disabled={syncing}
            className="flex min-h-9 items-center gap-1.5 rounded-md px-3 text-xs font-semibold text-blue-700 transition-colors hover:bg-blue-50 hover:text-blue-900 disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${syncing ? "animate-spin" : ""}`} aria-hidden="true" />
            {syncing ? "Checking Gmail…" : "Check Gmail"}
          </button>
          <button
            onClick={() => void exportLogs()}
            className="flex min-h-9 items-center gap-1.5 rounded-md px-3 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900"
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            Export CSV
          </button>
        </div>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Filter delivery activity">
        <span className="mr-1 text-xs font-medium text-slate-500">Filter</span>
        {ENGAGEMENT_FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={engagement === option.value}
            onClick={() => {
              setPage(1);
              setEngagement(option.value);
            }}
            className={`min-h-8 rounded-full border px-3 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${engagement === option.value ? "border-blue-700 bg-blue-700 text-white" : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"}`}
          >
            {option.label}
          </button>
        ))}
      </div>
      <div className="max-h-[calc(100vh-280px)] overflow-y-auto border border-slate-200 rounded-lg">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 sticky top-0">
            <tr className="text-left text-slate-500 font-semibold">
              <th className="px-4 py-2.5">Recipient</th>
              <th className="px-4 py-2.5">Sender</th>
              <th className="px-4 py-2.5">Subject</th>
              <th className="px-4 py-2.5">Result</th>
              <th className="px-4 py-2.5">Engagement</th>
              <th className="px-4 py-2.5">Response</th>
              <th className="px-4 py-2.5">Details</th>
              <th className="px-4 py-2.5">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {logs.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-slate-400">
                  {engagement === "all"
                    ? "No delivery activity yet."
                    : "No recipients match this filter."}
                </td>
              </tr>
            ) : logs.map((log) => (
              <tr key={log.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-2.5 text-slate-700 max-w-[200px] truncate">{log.recipient_email}</td>
                <td className="px-4 py-2.5 text-slate-500">{log.sender_email || "-"}</td>
                <td className="px-4 py-2.5 text-slate-600 max-w-[250px]">
                  {log.gmail_thread_id ? (
                    <a
                      href={gmailThreadUrl(log)}
                      target="_blank"
                      rel="noreferrer"
                      title="Open in Gmail"
                      aria-label={`Open email to ${log.recipient_email} in Gmail`}
                      className="inline-flex max-w-full items-center gap-1 text-blue-700 hover:text-blue-900 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                    >
                      <span className="truncate">{log.subject}</span>
                      <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" />
                    </a>
                  ) : (
                    <span className="block truncate">{log.subject}</span>
                  )}
                </td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={log.status} title={log.error_message || undefined} />
                  {log.status === "failed" && log.attempt_count > 1 && (
                    <div className="mt-1 text-[10px] text-slate-400">
                      Attempt {log.attempt_number}/{log.attempt_count}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2.5 text-slate-600">
                  {log.open_count || log.click_count ? (
                    <div className="space-y-1">
                      {log.status === "bounced" && <p className="text-amber-700">Unverified tracking; delivery failed</p>}
                      {log.open_count > 0 && <div className="flex items-center gap-1.5"><MailOpen className="h-3.5 w-3.5 text-blue-600" />{log.open_count} open{log.open_count === 1 ? "" : "s"}</div>}
                      {log.click_count > 0 && <div className="flex items-center gap-1.5"><MousePointerClick className="h-3.5 w-3.5 text-emerald-600" />{log.click_count} click{log.click_count === 1 ? "" : "s"}</div>}
                    </div>
                  ) : "-"}
                </td>
                <td className="px-4 py-2.5">
                  {log.response_status ? (
                    <StatusBadge status={log.response_status} />
                  ) : "-"}
                </td>
                <td className="px-4 py-2.5 text-slate-600 max-w-[280px]">
                  <span className="block truncate" title={activityDetail(log)}>
                    {activityDetail(log)}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-slate-400">
                  {log.responded_at || log.bounced_at || log.sent_at || log.created_at
                    ? new Date(log.responded_at || log.bounced_at || log.sent_at || log.created_at || "").toLocaleString()
                    : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(data?.pages || 1) > 1 && (
        <div className="flex items-center justify-end gap-2 mt-3">
          <button aria-label="Previous log page" title="Previous page" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} className="flex min-h-9 min-w-9 items-center justify-center rounded-md border border-slate-200 disabled:opacity-40 hover:bg-slate-50">
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="text-xs text-slate-500">Page {page} of {data?.pages || 1}</span>
          <button aria-label="Next log page" title="Next page" disabled={page >= (data?.pages || 1)} onClick={() => setPage((value) => value + 1)} className="flex min-h-9 min-w-9 items-center justify-center rounded-md border border-slate-200 disabled:opacity-40 hover:bg-slate-50">
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  );
}
