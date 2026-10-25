"use client";

import { CircleAlert, LoaderCircle, RadioTower, Unplug } from "lucide-react";
import { ActionMenu, MenuAction, StatusBadge } from "@/components/app-ui";
import type { Sender } from "@/types/senders";

function TrackingState({ sender }: { sender: Sender }) {
  if (sender.gmail_tracking_enabled) return <span className="sender-tracking-state is-active"><RadioTower size={14} />Live reply tracking on</span>;
  if (!sender.gmail_tracking_permission) return <span className="sender-tracking-state is-warning"><Unplug size={14} />Reconnect to track replies</span>;
  if (sender.gmail_tracking_status === "error") return <span className="sender-tracking-state is-error" title={sender.gmail_sync_error || undefined}><CircleAlert size={14} />Tracking needs attention</span>;
  return <span className="sender-tracking-state is-pending"><LoaderCircle size={14} className="animate-spin" />Setting up tracking</span>;
}

export default function SenderAccountsTable({ senders, inGroup = false, busy, onEdit, onDefault, onReconnect, onRemove, onAddToGroup }: {
  senders: Sender[];
  inGroup?: boolean;
  busy: boolean;
  onEdit: (sender: Sender) => void;
  onDefault: (sender: Sender) => void;
  onReconnect: (sender: Sender) => void;
  onRemove: (sender: Sender) => void;
  onAddToGroup: (sender: Sender) => void;
}) {
  return (
    <div className="app-table-wrap sender-accounts-table">
      <table className="app-table">
        <thead><tr><th scope="col">Account</th><th scope="col">Status</th><th scope="col">Shared daily limit</th><th scope="col">Sent today</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{senders.map((sender) => (
          <tr key={sender.id}>
            <td><span className="app-name sender-account-name">
              <span className="app-avatar" aria-hidden="true">{(sender.display_name || sender.email).slice(0, 2).toUpperCase()}</span>
              <span>{sender.display_name || sender.email}{sender.is_default && <span className="app-status ml-2">Default</span>}
                {sender.display_name && <span className="app-secondary">{sender.email}</span>}
                <span className="app-secondary">{sender.group_ids?.length ?? (sender.group_id ? 1 : 0)} {(sender.group_ids?.length ?? (sender.group_id ? 1 : 0)) === 1 ? "group" : "groups"}</span>
              </span>
            </span></td>
            <td><StatusBadge status={sender.status} /><TrackingState sender={sender} /></td>
            <td>{sender.daily_cap} / day</td>
            <td>{sender.sent_today} / {sender.daily_cap}<span className="app-progress" aria-label={`${sender.sent_today} of ${sender.daily_cap} emails sent today`}><span style={{ width: `${Math.min(100, (sender.sent_today / Math.max(1, sender.daily_cap)) * 100)}%` }} /></span></td>
            <td><ActionMenu iconOnly label={`Actions for ${sender.email}`} disabled={busy}>
              <MenuAction onClick={() => onEdit(sender)}>Edit account</MenuAction>
              <MenuAction onClick={() => onAddToGroup(sender)}>Add to another group</MenuAction>
              <MenuAction disabled={sender.is_default} onClick={() => onDefault(sender)}>Set as default</MenuAction>
              <MenuAction onClick={() => onReconnect(sender)}>Reconnect Gmail</MenuAction>
              <MenuAction danger onClick={() => onRemove(sender)}>{inGroup ? "Remove from this group" : "Disconnect from all groups"}</MenuAction>
            </ActionMenu></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
