"use client";

import { useState } from "react";
import useSWR from "swr";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { trackEvent } from "@/lib/analytics";
import {
  ActionMenu,
  AppDialog,
  ConfirmDialog,
  MenuAction,
  Notice,
  PageHeading,
  PageState,
  Pager,
  SearchField,
  StatusBadge,
  formatDate,
} from "@/components/app-ui";

interface Contact {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  company: string;
  status: string;
}
interface Library {
  items: Contact[];
  total: number;
}
interface Group {
  id: number;
  name: string;
}
interface Blocked {
  email: string;
  created_at?: string;
}
type ContactFilter = "answered" | "clicked" | "ready" | "undelivered";
const contactFilters: { value: ContactFilter; label: string }[] = [
  { value: "answered", label: "Answered" },
  { value: "clicked", label: "Clicked" },
  { value: "ready", label: "Ready" },
  { value: "undelivered", label: "Undelivered" },
];
const emptyContact = { email: "", first_name: "", last_name: "", company: "" };

export default function ContactsPage() {
  const { API_URL, authFetch } = useApiClient();
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState("");
  const [statuses, setStatuses] = useState<ContactFilter[]>([]);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number[]>([]);
  const [dialog, setDialog] = useState<"add" | "import" | "dnc" | null>(null);
  const [draft, setDraft] = useState(emptyContact);
  const [file, setFile] = useState<File | null>(null);
  const [blockEmail, setBlockEmail] = useState("");
  const [blockContact, setBlockContact] = useState<Contact | null>(null);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const query = new URLSearchParams();
  query.set("search", search);
  query.set("page", String(page));
  query.set("page_size", "6");
  if (group) query.set("campaign_id", group);
  statuses.forEach((status) => query.append("statuses", status));
  const { data, error, isLoading, mutate } = useSWR<Library>(
    `${API_URL}/api/contacts/library?${query}`,
  );
  const { data: groups = [] } = useSWR<Group[]>(`${API_URL}/api/campaigns`);
  const {
    data: blocked = [],
    error: blockedError,
    isLoading: blockedLoading,
    mutate: refreshBlocked,
  } = useSWR<Blocked[]>(
    dialog === "dnc" ? `${API_URL}/api/contacts/dnc` : null,
  );
  const items = data?.items || [];
  const changePage = (value: number) => {
    setPage(value);
    setSelected([]);
  };
  const open = (value: typeof dialog) => {
    setDialog(value);
    setActionError("");
    setMessage("");
  };
  const addContact = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    try {
      await checkResponse(
        await authFetch(`${API_URL}/api/contacts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...draft, email: draft.email.trim() }),
        }),
      );
      setDialog(null);
      setDraft(emptyContact);
      setMessage("Contact saved. Add them to an audience from a campaign.");
      await mutate();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const importFile = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      setActionError("Choose a CSV smaller than 5 MB.");
      return;
    }
    setBusy(true);
    setActionError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await checkResponse<{
        imported?: number;
        duplicates?: number;
        skipped_missing_email?: number;
        skipped_missing_required?: number;
        do_not_contact?: number;
      }>(
        await authFetch(`${API_URL}/api/contacts/import`, {
          method: "POST",
          body,
        }),
      );
      if (result.imported) {
        trackEvent("contacts_imported", {
          contact_count: result.imported,
          import_source: "contact_library_csv",
        });
      }
      setDialog(null);
      setFile(null);
      setMessage(
        `Import complete: ${result.imported ?? 0} imported, ${result.duplicates ?? 0} duplicates, ${(result.skipped_missing_email ?? 0) + (result.skipped_missing_required ?? 0)} skipped, ${result.do_not_contact ?? 0} blocked.`,
      );
      await mutate();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const block = async (email: string) => {
    setBusy(true);
    setActionError("");
    try {
      await checkResponse(
        await authFetch(`${API_URL}/api/contacts/dnc`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email.trim() }),
        }),
      );
      await Promise.all([mutate(), refreshBlocked()]);
      setBlockContact(null);
      setBlockEmail("");
      setMessage("Email added to do not contact.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const exportContacts = async () => {
    setExporting(true);
    setActionError("");
    const exportQuery = new URLSearchParams();
    exportQuery.set("search", search);
    if (group) exportQuery.set("campaign_id", group);
    statuses.forEach((status) => exportQuery.append("statuses", status));
    selected.forEach((id) => exportQuery.append("contact_ids", String(id)));
    try {
      const response = await authFetch(
        `${API_URL}/api/contacts/export?${exportQuery.toString()}`,
      );
      if (!response.ok) {
        await checkResponse(response, "Couldn’t export contacts. Please try again.");
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "contacts.csv";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage(
        selected.length
          ? `Exported ${selected.length} selected contact${selected.length === 1 ? "" : "s"}.`
          : `Exported ${data?.total ?? 0} contact${data?.total === 1 ? "" : "s"}.`,
      );
    } catch (error) {
      setActionError(errorMessage(error, "Couldn’t export contacts. Please try again."));
    } finally {
      setExporting(false);
    }
  };
  return (
    <div className="app-page">
      <PageHeading
        title="Contacts"
        description="People saved for your outreach."
        actions={
          <>
            <ActionMenu label="+ Add contacts" primary>
              <MenuAction onClick={() => open("add")}>
                Add one person
              </MenuAction>
              <MenuAction onClick={() => open("import")}>Import CSV</MenuAction>
            </ActionMenu>
            <ActionMenu>
              <MenuAction onClick={() => open("dnc")}>
                Manage do not contact
              </MenuAction>
              <MenuAction disabled={!data?.total || exporting} onClick={() => void exportContacts()}>
                {selected.length
                  ? "Export selected contacts"
                  : exporting
                    ? "Exporting…"
                    : statuses.length || group || search
                      ? "Export filtered contacts"
                      : "Export all contacts"}
              </MenuAction>
            </ActionMenu>
          </>
        }
      />
      {!dialog && !blockContact && (
        <Notice message={message} error={actionError} />
      )}
      <div className="app-toolbar">
        <SearchField
          label="Search contacts"
          value={search}
          onChange={(value) => {
            setSearch(value);
            changePage(1);
          }}
        />
        <select
          aria-label="Filter contacts by campaign group"
          className="app-select"
          value={group}
          onChange={(event) => {
            setGroup(event.target.value);
            changePage(1);
            setSelected([]);
          }}
        >
          <option value="">All groups</option>
          {groups.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <details className="app-filter-menu">
          <summary className="app-select">
            Status: {statuses.length ? statuses.map((status) => contactFilters.find((item) => item.value === status)?.label).join(", ") : "All"}
          </summary>
          <div className="app-filter-menu-panel">
            <fieldset>
              <legend>Filter by status</legend>
              {contactFilters.map((filter) => (
                <label key={filter.value} className="app-filter-menu-option">
                  <input
                    type="checkbox"
                    checked={statuses.includes(filter.value)}
                    onChange={(event) => {
                      setStatuses((current) =>
                        event.target.checked
                          ? [...current, filter.value]
                          : current.filter((status) => status !== filter.value),
                      );
                      changePage(1);
                      setSelected([]);
                    }}
                  />
                  {filter.label}
                </label>
              ))}
            </fieldset>
            <p>Contacts matching any selected status are shown.</p>
          </div>
        </details>
        <span className="app-count">
          {selected.length
            ? `${selected.length} selected`
            : `${data?.total ?? 0} contacts`}
        </span>
      </div>
      <div className="app-table-wrap">
        <PageState
          loading={isLoading}
          error={error}
          empty={!items.length}
          retry={() => void mutate()}
        >
          {!search && !group ? (
            <>
              <h2>Start with the people you want to reach</h2>
              <p>Add a contact or import a CSV using Add contacts.</p>
            </>
          ) : undefined}
        </PageState>
        {!isLoading && !error && items.length > 0 && (
          <table className="app-table">
            <thead>
              <tr>
                <th className="app-check-cell">
                  <label className="app-check">
                    <input
                      type="checkbox"
                      aria-label="Select all contacts on this page"
                      checked={selected.length === items.length}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? items.map((item) => item.id)
                            : [],
                        )
                      }
                    />
                  </label>
                </th>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Company</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td className="app-check-cell">
                    <label className="app-check">
                      <input
                        type="checkbox"
                        aria-label={`Select ${item.email}`}
                        checked={selected.includes(item.id)}
                        onChange={(event) =>
                          setSelected(
                            event.target.checked
                              ? [...selected, item.id]
                              : selected.filter((id) => id !== item.id),
                          )
                        }
                      />
                    </label>
                  </td>
                  <td>
                    <span className="app-name" style={{ cursor: "default" }}>
                      <span className="app-avatar" aria-hidden="true">
                        {(
                          (item.first_name[0] || item.email[0]) +
                          (item.last_name[0] || "")
                        ).toUpperCase()}
                      </span>
                      {`${item.first_name} ${item.last_name}`.trim() || "—"}
                    </span>
                  </td>
                  <td>{item.email}</td>
                  <td>{item.company || "—"}</td>
                  <td>
                    <StatusBadge status={item.status} />
                  </td>
                  <td>
                    <ActionMenu iconOnly label={`Actions for ${item.email}`}>
                      <MenuAction
                        disabled={item.status === "do_not_contact"}
                        onClick={() => {
                          setBlockContact(item);
                          setActionError("");
                        }}
                      >
                        Add to do not contact
                      </MenuAction>
                    </ActionMenu>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {!!data?.total && (
        <Pager
          page={page}
          pageSize={6}
          total={data.total}
          onChange={changePage}
        />
      )}
      <AppDialog
        open={dialog === "add"}
        onClose={() => {
          if (!busy) setDialog(null);
        }}
        title="Add a contact"
        description="Save a person to your contacts. This does not send an email."
      >
        <form className="app-form" onSubmit={addContact}>
          <label className="app-field">
            Email
            <input
              autoFocus
              required
              type="email"
              maxLength={320}
              value={draft.email}
              onChange={(event) =>
                setDraft({ ...draft, email: event.target.value })
              }
            />
          </label>
          <label className="app-field">
            First name
            <input
              maxLength={200}
              value={draft.first_name}
              onChange={(event) =>
                setDraft({ ...draft, first_name: event.target.value })
              }
            />
          </label>
          <label className="app-field">
            Last name
            <input
              maxLength={200}
              value={draft.last_name}
              onChange={(event) =>
                setDraft({ ...draft, last_name: event.target.value })
              }
            />
          </label>
          <label className="app-field">
            Company
            <input
              maxLength={200}
              value={draft.company}
              onChange={(event) =>
                setDraft({ ...draft, company: event.target.value })
              }
            />
          </label>
          <Notice error={actionError} />
          <div className="app-dialog-actions">
            <button
              type="button"
              className="app-button"
              disabled={busy}
              onClick={() => setDialog(null)}
            >
              Cancel
            </button>
            <button className="app-button is-primary" disabled={busy}>
              {busy ? "Saving…" : "Save contact"}
            </button>
          </div>
        </form>
      </AppDialog>
      <AppDialog
        open={dialog === "import"}
        onClose={() => {
          if (!busy) setDialog(null);
        }}
        title="Import contacts"
        description="Choose a CSV with an email column. Existing contacts with matching emails will be updated; do-not-contact restrictions are kept."
      >
        <form className="app-form" onSubmit={importFile}>
          <label className="app-field">
            CSV file
            <input
              type="file"
              required
              accept=".csv,text/csv"
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
            <small>
              Up to 5 MB and 10,000 rows. Name and company columns are optional.
            </small>
          </label>
          <Notice error={actionError} />
          <div className="app-dialog-actions">
            <button
              type="button"
              className="app-button"
              disabled={busy}
              onClick={() => setDialog(null)}
            >
              Cancel
            </button>
            <button className="app-button is-primary" disabled={busy || !file}>
              {busy ? "Importing…" : "Import contacts"}
            </button>
          </div>
        </form>
      </AppDialog>
      <AppDialog
        open={dialog === "dnc"}
        onClose={() => {
          if (!busy) setDialog(null);
        }}
        title="Do not contact"
        description="These emails are blocked from future sending. Emails already sending may finish."
      >
        <form
          className="app-form"
          onSubmit={(event) => {
            event.preventDefault();
            void block(blockEmail);
          }}
        >
          <label className="app-field">
            Email to block
            <input
              type="email"
              required
              value={blockEmail}
              onChange={(event) => setBlockEmail(event.target.value)}
            />
          </label>
          <button className="app-button" disabled={busy}>
            {busy ? "Adding…" : "Add to do not contact"}
          </button>
        </form>
        <Notice error={actionError} message={message} />
        <PageState
          loading={blockedLoading}
          error={blockedError}
          empty={!blocked.length}
          retry={() => void refreshBlocked()}
        >
          No blocked emails.
        </PageState>
        {!blockedError &&
          blocked.map((item) => (
            <div key={item.email} className="app-pager">
              <span>{item.email}</span>
              <span>{formatDate(item.created_at)}</span>
            </div>
          ))}
      </AppDialog>
      <ConfirmDialog
        open={!!blockContact}
        title="Block future emails?"
        description={`Add ${blockContact?.email} to do not contact across all campaigns. An email already sending may finish.`}
        label="Block email"
        busy={busy}
        error={actionError}
        onClose={() => setBlockContact(null)}
        onConfirm={() => {
          if (blockContact) void block(blockContact.email);
        }}
      />
    </div>
  );
}
