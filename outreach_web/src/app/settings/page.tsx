"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { useUser } from "@clerk/nextjs";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { isAdminUser } from "@/lib/auth";
import { formatTimeZoneLabel, supportedTimeZones } from "@/lib/timezones";
import {
  AppDialog,
  ConfirmDialog,
  Notice,
  PageHeading,
  PageState,
  StatusBadge,
} from "@/components/app-ui";

interface Settings {
  timezone: string;
  max_daily_cap: number;
  bounce_rate_pause_threshold: number;
  max_consecutive_errors: number;
}

interface WorkspaceToken {
  id: number;
  name: string;
  prefix: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
}

function WorkspaceTokens() {
  const { API_URL, authFetch } = useApiClient();
  const { data: tokens, mutate } = useSWR<WorkspaceToken[]>(
    `${API_URL}/api/workspace-tokens`,
  );
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("Codex campaign manager");
  const [createdToken, setCreatedToken] = useState("");
  const [revokeId, setRevokeId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    try {
      const result = await checkResponse<{ token: string }>(
        await authFetch(`${API_URL}/api/workspace-tokens`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim(), expires_in_days: 365 }),
        }),
      );
      setCreatedToken(result.token);
      setOpen(false);
      await mutate();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (revokeId === null) return;
    setBusy(true);
    setActionError("");
    try {
      await checkResponse(
        await authFetch(`${API_URL}/api/workspace-tokens/${revokeId}`, {
          method: "DELETE",
        }),
      );
      await mutate();
      setRevokeId(null);
      setMessage("Access token revoked.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="app-panel app-settings-general">
      <h2>AI campaign access</h2>
      <p className="app-muted">
        Create a personal token for an MCP client to manage your campaigns and
        contacts. It has access to your workspace, so keep it private.
      </p>
      <Notice error={actionError} message={message} />
      {createdToken && (
        <div className="app-field" role="status">
          <label htmlFor="new-workspace-token">New token — copy it now; it will not be shown again</label>
          <input id="new-workspace-token" readOnly value={createdToken} onFocus={(event) => event.currentTarget.select()} />
          <div className="app-dialog-actions">
            <button className="app-button" onClick={() => void navigator.clipboard.writeText(createdToken)}>
              Copy token
            </button>
            <button className="app-button" onClick={() => setCreatedToken("")}>Done</button>
          </div>
        </div>
      )}
      <button className="app-button" onClick={() => { setActionError(""); setOpen(true); }}>
        Create access token
      </button>
      {tokens?.length ? (
        <ul className="app-list" aria-label="Workspace access tokens">
          {tokens.map((token) => (
            <li key={token.id} className="app-pager">
              <span>{token.name} ({token.prefix}…) — {token.revoked_at ? "Revoked" : `Expires ${new Date(token.expires_at).toLocaleDateString()}`}</span>
              {!token.revoked_at && <button className="app-button is-danger" onClick={() => setRevokeId(token.id)}>Revoke</button>}
            </li>
          ))}
        </ul>
      ) : null}
      <AppDialog
        open={open}
        onClose={() => { if (!busy) setOpen(false); }}
        title="Create AI access token"
        description="This token grants access to your workspace for one year. You can revoke it here at any time."
      >
        <form className="app-form" onSubmit={(event) => void create(event)}>
          <label className="app-field">
            Name
            <input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <Notice error={actionError} />
          <div className="app-dialog-actions">
            <button type="button" className="app-button" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
            <button className="app-button is-primary" disabled={busy || !name.trim()}>{busy ? "Creating…" : "Create token"}</button>
          </div>
        </form>
      </AppDialog>
      <ConfirmDialog
        open={revokeId !== null}
        title="Revoke this token?"
        description="Any MCP client using it will lose access immediately."
        busy={busy}
        error={actionError}
        onClose={() => { setRevokeId(null); setActionError(""); }}
        onConfirm={() => void revoke()}
        label="Revoke token"
      />
    </section>
  );
}

function ConnectedServices() {
  const { user } = useUser();
  const admin = isAdminUser(user);
  const { API_URL, authFetch } = useApiClient();
  const { data, error, isLoading, mutate } = useSWR<{
    credentials_json_present: boolean;
  }>(admin ? `${API_URL}/api/oauth/status` : null);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState("json");
  const [content, setContent] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    try {
      const payload =
        mode === "json"
          ? content
          : JSON.stringify({
              web: {
                client_id: clientId.trim(),
                client_secret: clientSecret.trim(),
                auth_uri: "https://accounts.google.com/o/oauth2/auth",
                token_uri: "https://oauth2.googleapis.com/token",
              },
            });
      JSON.parse(payload);
      await checkResponse(
        await authFetch(`${API_URL}/api/oauth/save-credentials-json`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: payload }),
        }),
      );
      await mutate();
      setOpen(false);
      setContent("");
      setClientId("");
      setClientSecret("");
      setMessage("Google credentials updated.");
    } catch (error) {
      setActionError(
        error instanceof SyntaxError
          ? "Please provide valid JSON."
          : errorMessage(error),
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <p className="app-muted">
        Manage the Gmail accounts your campaigns use from Senders.
      </p>
      <Link href="/senders" className="app-text-link">
        Manage sender accounts →
      </Link>
      {admin && (
        <>
          <PageState
            loading={isLoading}
            error={error}
            retry={() => void mutate()}
          />
          {data && (
            <div className="app-pager">
              <span>Google OAuth client</span>
              <StatusBadge
                status={
                  data.credentials_json_present ? "connected" : "not_configured"
                }
              />
            </div>
          )}
          <Notice message={message} />
          <button
            className="app-button"
            style={{ marginTop: 16 }}
            onClick={() => {
              setOpen(true);
              setActionError("");
            }}
          >
            Manage Google credentials
          </button>
          <details style={{ marginTop: 16 }} className="app-disclosure">
            <summary>Connection help</summary>
            <div className="app-disclosure-content">
              <p className="app-muted">
                Enable the Gmail API in your Google project and ensure the OAuth
                callback matches your backend’s configured callback URL.
              </p>
              <a
                className="app-text-link"
                href="https://console.cloud.google.com/apis/library/gmail.googleapis.com"
                target="_blank"
                rel="noopener noreferrer"
              >
                Open Gmail API settings ↗
              </a>
            </div>
          </details>
        </>
      )}
      <AppDialog
        open={open}
        onClose={() => {
          if (!busy) {
            setOpen(false);
            setContent("");
            setClientSecret("");
          }
        }}
        title="Google credentials"
        description="Administrator setting. Changes affect how sender accounts connect."
      >
        <form className="app-form" onSubmit={save}>
          <label className="app-field">
            Input method
            <select
              className="app-select"
              value={mode}
              onChange={(event) => setMode(event.target.value)}
            >
              <option value="json">Upload or paste JSON</option>
              <option value="manual">Enter client details</option>
            </select>
          </label>
          {mode === "json" ? (
            <>
              <label className="app-field">
                Upload JSON
                <input
                  type="file"
                  accept=".json,application/json"
                  onChange={async (event) => {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    if (file.size > 100000) {
                      setActionError(
                        "Choose a credentials file smaller than 100 KB.",
                      );
                      return;
                    }
                    setContent(await file.text());
                  }}
                />
              </label>
              <label className="app-field">
                Credentials JSON
                <textarea
                  required
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
            </>
          ) : (
            <>
              <label className="app-field">
                Client ID
                <input
                  required
                  value={clientId}
                  onChange={(event) => setClientId(event.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="app-field">
                Client secret
                <input
                  type="password"
                  required
                  value={clientSecret}
                  onChange={(event) => setClientSecret(event.target.value)}
                  autoComplete="new-password"
                />
              </label>
            </>
          )}
          <Notice error={actionError} />
          <div className="app-dialog-actions">
            <button
              type="button"
              className="app-button"
              disabled={busy}
              onClick={() => {
                setOpen(false);
                setContent("");
                setClientSecret("");
              }}
            >
              Cancel
            </button>
            <button className="app-button is-primary" disabled={busy}>
              {busy ? "Saving…" : "Save credentials"}
            </button>
          </div>
        </form>
      </AppDialog>
    </>
  );
}

function SettingsForm({
  initial,
  refresh,
}: {
  initial: Settings;
  refresh: () => Promise<unknown>;
}) {
  const { API_URL, authFetch } = useApiClient();
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [servicesOpen, setServicesOpen] = useState(false);
  const zones = useMemo(
    () => Array.from(new Set([initial.timezone, ...supportedTimeZones()])),
    [initial.timezone],
  );
  const save = async (event: React.FormEvent, advanced: boolean) => {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    setMessage("");
    try {
      await checkResponse(
        await authFetch(
          `${API_URL}/api/settings${advanced ? "" : "/timezone"}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(
              advanced
                ? { ...draft, timezone: initial.timezone }
                : { timezone: draft.timezone },
            ),
          },
        ),
      );
      await refresh();
      setMessage(
        advanced
          ? "Advanced settings saved."
          : "Default timezone saved. Existing campaigns keep their own timezone.",
      );
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="app-settings">
      <Notice error={actionError} message={message} />
      <section className="app-panel app-settings-general">
        <h2>General</h2>
        <form
          className="app-form"
          onSubmit={(event) => void save(event, false)}
        >
          <label className="app-field">
            Default timezone
            <select
              aria-label="Default timezone"
              aria-describedby="default-timezone-hint"
              className="app-select"
              value={draft.timezone}
              onChange={(event) =>
                setDraft({ ...draft, timezone: event.target.value })
              }
            >
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {formatTimeZoneLabel(zone)}
                </option>
              ))}
            </select>
            <small id="default-timezone-hint">
              Used when you create a new campaign. Each campaign can have its
              own timezone.
            </small>
          </label>
          <div>
            <button className="app-button is-primary" disabled={busy}>
              {busy ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      </section>
      <details
        className="app-disclosure"
        onToggle={(event) => setServicesOpen(event.currentTarget.open)}
      >
        <summary>Connected services</summary>
        <div className="app-disclosure-content">
          {servicesOpen && <ConnectedServices />}
        </div>
      </details>
      <details className="app-disclosure">
        <summary>Advanced settings</summary>
        <div className="app-disclosure-content">
          <form
            className="app-form"
            onSubmit={(event) => void save(event, true)}
          >
            <p className="app-muted">
              Workspace safety defaults. Each connected sender also has its own
              daily limit.
            </p>
            <label className="app-field">
              Maximum daily cap
              <input
                required
                type="number"
                min={1}
                max={10000}
                value={draft.max_daily_cap}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    max_daily_cap: Number(event.target.value),
                  })
                }
              />
            </label>
            <label className="app-field">
              Pause threshold (%)
              <input
                required
                type="number"
                step="0.01"
                min={0}
                max={100}
                value={draft.bounce_rate_pause_threshold}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    bounce_rate_pause_threshold: Number(event.target.value),
                  })
                }
              />
            </label>
            <label className="app-field">
              Maximum consecutive errors
              <input
                required
                type="number"
                min={1}
                max={1000}
                value={draft.max_consecutive_errors}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    max_consecutive_errors: Number(event.target.value),
                  })
                }
              />
            </label>
            <div>
              <button className="app-button" disabled={busy}>
                {busy ? "Saving…" : "Save advanced settings"}
              </button>
            </div>
          </form>
        </div>
      </details>
      <WorkspaceTokens />
      <Link href="/senders" className="app-text-link">
        Manage sender accounts →
      </Link>
    </div>
  );
}

export default function SettingsPage() {
  const { API_URL } = useApiClient();
  const { data, isLoading, error, mutate } = useSWR<Settings>(
    `${API_URL}/api/settings`,
  );
  return (
    <div className="app-page">
      <PageHeading
        title="Settings"
        description="Manage your workspace preferences."
      />
      <PageState
        loading={isLoading}
        error={error}
        retry={() => void mutate()}
      />
      {data && !error && <SettingsForm initial={data} refresh={mutate} />}
    </div>
  );
}
