export interface Sender {
  id: number;
  group_id: number | null;
  group_ids: number[];
  email: string;
  display_name: string;
  status: string;
  daily_cap: number;
  is_default: boolean;
  sent_today: number;
  daily_cap_remaining: number;
  connected_at: string | null;
  revoked_at: string | null;
  removed_at: string | null;
  last_error: string | null;
  gmail_tracking_enabled: boolean;
  gmail_tracking_permission: boolean;
  gmail_tracking_status: "needs_reconnect" | "pending" | "active" | "error" | "disabled";
  gmail_watch_expiration_at: string | null;
  gmail_sync_checked_at: string | null;
  gmail_sync_error: string | null;
}

export interface SenderGroup {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
  senders: Sender[];
  connected_sender_count: number;
  sender_count: number;
  total_daily_cap: number;
  error_sender_count: number;
}
