"use client";

import { useState } from "react";
import useSWR from "swr";
import { Plus, Unplug } from "lucide-react";
import { AppDialog, Notice, PageState, StatusBadge } from "@/components/app-ui";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import type { Sender, SenderGroup } from "@/types/senders";

export default function AddSenderDialog({ group, onClose, onAdded, initialMode = "saved", returnPath, beforeConnect }: {
  group: SenderGroup;
  onClose: () => void;
  onAdded: () => Promise<unknown>;
  initialMode?: "saved" | "new";
  returnPath?: string;
  beforeConnect?: () => Promise<void>;
}) {
  const { API_URL, authFetch } = useApiClient();
  const { data: saved, error, isLoading, mutate } = useSWR<Sender[]>(`${API_URL}/api/senders`);
  const [mode, setMode] = useState(initialMode);
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const existing = new Set(group.senders.map((sender) => sender.id));
  const available = (saved || []).filter((sender) => !existing.has(sender.id));
  const addSaved = async () => {
    if (!selected.length || busy) return;
    setBusy(true);
    setProblem("");
    try {
      await checkResponse(await authFetch(`${API_URL}/api/sender-groups/${group.id}/senders`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sender_ids: selected }),
      }));
      await onAdded();
      onClose();
    } catch (error) {
      setProblem(errorMessage(error));
    } finally { setBusy(false); }
  };
  const connect = async () => {
    setBusy(true);
    setProblem("");
    try {
      await beforeConnect?.();
      const result = await checkResponse<{ auth_url: string }>(await authFetch(
        `${API_URL}/api/sender-groups/${group.id}/senders/oauth/start`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ return_path: returnPath || `${window.location.pathname}${window.location.search}` }),
        },
      ));
      if (!result.auth_url) throw new Error("Could not open Google. Please try again.");
      window.sessionStorage.setItem("pending_sender_group_id", String(group.id));
      window.location.assign(result.auth_url);
    } catch (error) {
      setProblem(errorMessage(error));
      setBusy(false);
    }
  };
  return (
    <AppDialog open onClose={() => { if (!busy) onClose(); }} title={`Add sender to ${group.name}`}
      description="Reuse a saved Gmail connection or connect another account.">
      <div className="sender-add-options" aria-label="How to add a sender">
        <button className={`app-button ${mode === "saved" ? "is-primary" : ""}`} aria-pressed={mode === "saved"} disabled={busy} onClick={() => setMode("saved")}>Use saved account</button>
        <button className={`app-button ${mode === "new" ? "is-primary" : ""}`} aria-pressed={mode === "new"} disabled={busy} onClick={() => setMode("new")}>Connect new account</button>
      </div>
      <Notice error={problem} />
      {mode === "saved" ? (
        <>
          <p className="app-muted sender-dialog-copy">Your existing connection stays signed in. Accounts can belong to several groups.</p>
          <PageState loading={isLoading} error={error} empty={!available.length} retry={() => void mutate()}>
            <p>{saved?.length ? "All saved accounts are already in this group." : "No saved accounts yet."}</p>
            <button className="app-button" onClick={() => setMode("new")}>Connect a Gmail account</button>
          </PageState>
          {available.length > 0 && (
            <div className="sender-saved-choices">
              {available.map((sender) => (
                <label key={sender.id} className={`sender-saved-choice ${selected.includes(sender.id) ? "is-selected" : ""}`}>
                  <input type="checkbox" checked={selected.includes(sender.id)} disabled={busy}
                    onChange={(event) => setSelected(event.target.checked ? [...selected, sender.id] : selected.filter((id) => id !== sender.id))} />
                  <span className="sender-saved-choice-name"><strong>{sender.email}</strong><small>{sender.display_name || "Saved Gmail account"} · {sender.daily_cap}/day shared limit</small></span>
                  <StatusBadge status={sender.status} />
                </label>
              ))}
            </div>
          )}
          <div className="app-dialog-actions">
            <button className="app-button" disabled={busy} onClick={onClose}>Cancel</button>
            <button className="app-button is-primary" disabled={busy || !selected.length} onClick={() => void addSaved()}>
              <Plus size={16} />{busy ? "Adding…" : `Add ${selected.length || ""} ${selected.length === 1 ? "account" : "accounts"}`}
            </button>
          </div>
        </>
      ) : (
        <div className="sender-new-connection">
          <Unplug size={28} aria-hidden="true" />
          <p>Sign in with Google to save this Gmail connection and add it to {group.name}.</p>
          <p className="app-muted">If the account is already saved, its other groups stay connected.</p>
          <div className="app-dialog-actions">
            <button className="app-button" disabled={busy} onClick={onClose}>Cancel</button>
            <button className="app-button is-primary" disabled={busy} onClick={() => void connect()}>{busy ? "Opening Google…" : "Continue to Google"}</button>
          </div>
        </div>
      )}
    </AppDialog>
  );
}
