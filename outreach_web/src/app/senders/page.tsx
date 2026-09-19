"use client";

import { Suspense, useEffect, useState } from "react";
import useSWR from "swr";
import { useSearchParams } from "next/navigation";
import {
  CircleAlert,
  LoaderCircle,
  Plus,
  RadioTower,
  Unplug,
} from "lucide-react";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { trackEventOnce } from "@/lib/analytics";
import type { Sender, SenderGroup } from "@/types/senders";
import {
  ActionMenu,
  AppDialog,
  ConfirmDialog,
  MenuAction,
  Notice,
  PageHeading,
  PageState,
  Pager,
  StatusBadge,
} from "@/components/app-ui";

function GmailTrackingState({ sender }: { sender: Sender }) {
  if (sender.gmail_tracking_enabled) {
    return (
      <span className="sender-tracking-state is-active">
        <RadioTower size={14} aria-hidden="true" />
        Live reply tracking on
      </span>
    );
  }
  if (!sender.gmail_tracking_permission) {
    return (
      <span className="sender-tracking-state is-warning">
        <Unplug size={14} aria-hidden="true" />
        Reconnect Gmail to track replies
      </span>
    );
  }
  if (sender.gmail_tracking_status === "error") {
    return (
      <span
        className="sender-tracking-state is-error"
        title={sender.gmail_sync_error || undefined}
      >
        <CircleAlert size={14} aria-hidden="true" />
        Gmail tracking needs attention
      </span>
    );
  }
  return (
    <span className="sender-tracking-state is-pending">
      <LoaderCircle className="animate-spin" size={14} aria-hidden="true" />
      Setting up live tracking
    </span>
  );
}

function SendersContent() {
  const { API_URL, authFetch } = useApiClient();
  const {
    data: groups = [],
    error,
    isLoading,
    mutate,
  } = useSWR<SenderGroup[]>(`${API_URL}/api/sender-groups`);
  const params = useSearchParams();
  const [groupId, setGroupId] = useState<number | null>(null);
  const group = groups.find((item) => item.id === groupId) || groups[0];
  const [page, setPage] = useState(1);
  const [groupDialog, setGroupDialog] = useState<
    "create" | "rename" | "connect" | null
  >(null);
  const [groupName, setGroupName] = useState("");
  const [editing, setEditing] = useState<Sender | null>(null);
  const [removing, setRemoving] = useState<Sender | "group" | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (params.get("oauth")) {
      if (params.get("oauth") === "success") {
        const senderId = params.get("sender_id") || "connected";
        trackEventOnce(
          `sender:${senderId}:connected`,
          "sender_connected",
          { connection_method: "gmail_oauth" },
        );
      }
      window.sessionStorage.removeItem("pending_sender_group_id");
      void mutate();
    }
  }, [params, mutate]);
  const connect = async (id: number) => {
    setBusy(true);
    setActionError("");
    try {
      const result = await checkResponse<{ auth_url?: string }>(
        await authFetch(
          `${API_URL}/api/sender-groups/${id}/senders/oauth/start`,
          { method: "POST" },
        ),
      );
      if (!result.auth_url)
        throw new Error("Could not start the connection. Please try again.");
      window.sessionStorage.setItem("pending_sender_group_id", String(id));
      window.location.assign(result.auth_url);
    } catch (error) {
      setActionError(errorMessage(error));
      setBusy(false);
    }
  };
  const request = async <T = Record<string, unknown>>(
    path: string,
    method: string,
    body?: object,
  ) => {
    return checkResponse<T>(
      await authFetch(`${API_URL}${path}`, {
        method,
        ...(body
          ? {
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            }
          : {}),
      }),
    );
  };
  const saveGroup = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    try {
      const result = await request<{ id: number }>(
        `/api/sender-groups${groupDialog === "rename" ? `/${group.id}` : ""}`,
        groupDialog === "rename" ? "PATCH" : "POST",
        { name: groupName.trim() },
      );
      setGroupId(result.id);
      await mutate();
      const startConnection = groupDialog === "connect";
      setGroupDialog(null);
      setGroupName("");
      if (startConnection) {
        await connect(result.id);
        return;
      }
      setMessage("Group saved.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const saveSender = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    setActionError("");
    try {
      await request(`/api/senders/${editing.id}`, "PATCH", {
        display_name: editing.display_name,
        daily_cap: editing.daily_cap,
        group_id: editing.group_id,
      });
      await mutate();
      setEditing(null);
      setMessage("Sender updated.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const setDefault = async (id: number) => {
    setBusy(true);
    setActionError("");
    try {
      await request(`/api/senders/${id}/default`, "PATCH");
      await mutate();
      setMessage("Default sender updated.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    setActionError("");
    try {
      await request(
        removing === "group"
          ? `/api/sender-groups/${group.id}`
          : `/api/senders/${removing.id}`,
        "DELETE",
      );
      await mutate();
      setRemoving(null);
      setMessage("Removed successfully.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const beginGroup = (mode: "create" | "rename" | "connect") => {
    setGroupDialog(mode);
    setGroupName(mode === "rename" ? group.name : "");
    setActionError("");
  };
  const senders = group?.senders || [];
  const currentPage = Math.min(
    page,
    Math.max(1, Math.ceil(senders.length / 6)),
  );
  return (
    <div className="app-page">
      <PageHeading
        title="Senders"
        description="Email accounts used to send your campaigns."
        actions={
          <button
            className="app-button is-primary"
            disabled={busy || isLoading || !!error}
            onClick={() =>
              group ? void connect(group.id) : beginGroup("connect")
            }
          >
            <Plus size={18} />
            {busy && !groupDialog && !editing && !removing
              ? "Please wait…"
              : "Connect account"}
          </button>
        }
      />
      {!groupDialog && !editing && !removing && (
        <Notice
          error={
            actionError ||
            (params.get("oauth") === "error"
              ? "The account could not be connected. Please try again."
              : "")
          }
          message={
            message ||
            (params.get("oauth") === "success" ? "Account connected." : "")
          }
        />
      )}
      {group && (
        <div className="app-toolbar">
          <select
            aria-label="Sender group"
            className="app-select"
            value={group.id}
            onChange={(event) => {
              setGroupId(Number(event.target.value));
              setPage(1);
            }}
          >
            {groups.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <ActionMenu iconOnly label="Group actions" disabled={busy}>
            <MenuAction onClick={() => beginGroup("create")}>
              New group
            </MenuAction>
            <MenuAction onClick={() => beginGroup("rename")}>
              Rename group
            </MenuAction>
            <MenuAction
              danger
              disabled={!!senders.length}
              onClick={() => {
                setRemoving("group");
                setActionError("");
              }}
            >
              Delete empty group
            </MenuAction>
          </ActionMenu>
          <span className="app-count">
            {group.connected_sender_count} connected{" "}
            {group.connected_sender_count === 1 ? "account" : "accounts"}
          </span>
        </div>
      )}
      <div className="app-table-wrap">
        <PageState
          loading={isLoading}
          error={error}
          empty={!senders.length}
          retry={() => void mutate()}
        >
          <h2>
            {group ? "No accounts in this group" : "Connect your first account"}
          </h2>
          <p>
            {group
              ? "Connect a Gmail account to send from this group."
              : "Choose a group name, then connect your Gmail account."}
          </p>
        </PageState>
        {!isLoading && !error && senders.length > 0 && (
          <table className="app-table">
            <thead>
              <tr>
                <th scope="col">Account</th>
                <th scope="col">Status</th>
                <th scope="col">Daily limit</th>
                <th scope="col">Sent today</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {senders
                .slice((currentPage - 1) * 6, currentPage * 6)
                .map((sender) => (
                  <tr key={sender.id}>
                    <td>
                      <span className="app-name" style={{ cursor: "default" }}>
                        <span className="app-avatar" aria-hidden="true">
                          {(sender.display_name || sender.email)
                            .slice(0, 2)
                            .toUpperCase()}
                        </span>
                        <span>
                          {sender.display_name || sender.email}
                          {sender.is_default ? (
                            <span className="app-status ml-2">Default</span>
                          ) : null}
                          {sender.display_name && (
                            <span className="app-secondary">
                              {sender.email}
                            </span>
                          )}
                        </span>
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={sender.status} />
                      <GmailTrackingState sender={sender} />
                    </td>
                    <td>{sender.daily_cap} / day</td>
                    <td>
                      {sender.sent_today} / {sender.daily_cap}
                      <span className="app-progress" aria-hidden="true">
                        <span
                          style={{
                            width: `${Math.min(100, (sender.sent_today / sender.daily_cap) * 100)}%`,
                          }}
                        />
                      </span>
                    </td>
                    <td>
                      <ActionMenu
                        iconOnly
                        label={`Actions for ${sender.email}`}
                        disabled={busy}
                      >
                        <MenuAction
                          onClick={() => {
                            setEditing({ ...sender });
                            setActionError("");
                          }}
                        >
                          Edit account
                        </MenuAction>
                        <MenuAction
                          disabled={sender.is_default}
                          onClick={() => void setDefault(sender.id)}
                        >
                          Set as default
                        </MenuAction>
                        <MenuAction
                          onClick={() => void connect(sender.group_id)}
                        >
                          Reconnect account
                        </MenuAction>
                        <MenuAction
                          danger
                          onClick={() => {
                            setRemoving(sender);
                            setActionError("");
                          }}
                        >
                          Remove account
                        </MenuAction>
                      </ActionMenu>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </div>
      {senders.length > 6 && (
        <Pager
          page={currentPage}
          pageSize={6}
          total={senders.length}
          onChange={setPage}
        />
      )}
      <p className="app-footnote">
        Limits control daily sends. Connected accounts can also check Gmail for
        replies, automated responses, and undelivered addresses.
      </p>
      <AppDialog
        open={!!groupDialog}
        onClose={() => {
          if (!busy) setGroupDialog(null);
        }}
        title={
          groupDialog === "rename" ? "Rename group" : "Create a sender group"
        }
        description={
          groupDialog === "connect"
            ? "Accounts are organized into groups. Name your first group, then continue to Google."
            : "Campaigns use the accounts in the group you select."
        }
      >
        <form className="app-form" onSubmit={saveGroup}>
          <label className="app-field">
            Group name
            <input
              autoFocus
              required
              maxLength={160}
              value={groupName}
              onChange={(event) => setGroupName(event.target.value)}
              placeholder="e.g. Hiring team"
            />
          </label>
          <Notice error={actionError} />
          <div className="app-dialog-actions">
            <button
              type="button"
              className="app-button"
              disabled={busy}
              onClick={() => setGroupDialog(null)}
            >
              Cancel
            </button>
            <button
              className="app-button is-primary"
              disabled={busy || !groupName.trim()}
            >
              {busy
                ? "Please wait…"
                : groupDialog === "connect"
                  ? "Continue to Google"
                  : "Save group"}
            </button>
          </div>
        </form>
      </AppDialog>
      <AppDialog
        open={!!editing}
        onClose={() => {
          if (!busy) setEditing(null);
        }}
        title="Edit sender"
        description={editing?.email}
      >
        {editing && (
          <form className="app-form" onSubmit={saveSender}>
            <label className="app-field">
              Display name
              <input
                maxLength={200}
                value={editing.display_name}
                onChange={(event) =>
                  setEditing({ ...editing, display_name: event.target.value })
                }
              />
            </label>
            <label className="app-field">
              Daily limit
              <input
                type="number"
                min={1}
                max={500}
                required
                value={editing.daily_cap}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    daily_cap: Number(event.target.value),
                  })
                }
              />
            </label>
            <label className="app-field">
              Group
              <select
                className="app-select"
                value={editing.group_id}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    group_id: Number(event.target.value),
                  })
                }
              >
                {groups.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            {editing.last_error && (
              <p className="app-muted">
                Last connection error: {editing.last_error}
              </p>
            )}
            <Notice error={actionError} />
            <div className="app-dialog-actions">
              <button
                type="button"
                className="app-button"
                disabled={busy}
                onClick={() => setEditing(null)}
              >
                Cancel
              </button>
              <button className="app-button is-primary" disabled={busy}>
                {busy ? "Saving…" : "Save changes"}
              </button>
            </div>
          </form>
        )}
      </AppDialog>
      <ConfirmDialog
        open={!!removing}
        title={
          removing === "group"
            ? "Delete this empty group?"
            : "Remove this account?"
        }
        description={
          removing === "group"
            ? "The empty group will be deleted. Campaigns keep their messages and history."
            : "This account will stop being available for sending. You can reconnect it later."
        }
        busy={busy}
        error={actionError}
        onClose={() => setRemoving(null)}
        onConfirm={() => void remove()}
      />
    </div>
  );
}

export default function SendersPage() {
  return (
    <Suspense fallback={<PageState loading />}>
      <SendersContent />
    </Suspense>
  );
}
