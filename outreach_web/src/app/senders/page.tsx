"use client";

import { Suspense, useEffect, useState } from "react";
import useSWR, { mutate as mutateCache } from "swr";
import { useSearchParams } from "next/navigation";
import { ChevronDown, Folder, Plus } from "lucide-react";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { trackEventOnce } from "@/lib/analytics";
import type { Sender, SenderGroup } from "@/types/senders";
import { ActionMenu, AppDialog, ConfirmDialog, MenuAction, Notice, PageHeading, PageState, SearchField } from "@/components/app-ui";
import AddSenderDialog from "@/components/senders/AddSenderDialog";
import SenderAccountsTable from "@/components/senders/SenderAccountsTable";

function SendersContent() {
  const { API_URL, authFetch } = useApiClient();
  const { data: groups = [], error, isLoading, mutate: refreshGroups } = useSWR<SenderGroup[]>(`${API_URL}/api/sender-groups`);
  const { data: saved = [], error: savedError, isLoading: savedLoading, mutate: refreshSaved } = useSWR<Sender[]>(`${API_URL}/api/senders`);
  const params = useSearchParams();
  const [expanded, setExpanded] = useState<number[]>(() => params.get("group_id") ? [Number(params.get("group_id"))] : []);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [groupDialog, setGroupDialog] = useState<{ group?: SenderGroup } | null>(null);
  const [groupName, setGroupName] = useState("");
  const [adding, setAdding] = useState<{ group: SenderGroup; mode: "saved" | "new" } | null>(null);
  const [editing, setEditing] = useState<Sender | null>(null);
  const [attaching, setAttaching] = useState<Sender | null>(null);
  const [targetGroup, setTargetGroup] = useState("");
  const [removing, setRemoving] = useState<{ group: SenderGroup } | { sender: Sender } | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (params.get("oauth") === "success") {
      trackEventOnce(`sender:${params.get("sender_id") || "connected"}:connected`, "sender_connected", { connection_method: "gmail_oauth" });
      window.sessionStorage.removeItem("pending_sender_group_id");
    }
  }, [params]);
  const request = async <T = Record<string, unknown>,>(path: string, method: string, body?: object) => checkResponse<T>(
    await authFetch(`${API_URL}${path}`, { method, ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) }),
  );
  const refresh = async () => {
    await Promise.all([refreshGroups(), refreshSaved(), mutateCache((key) => typeof key === "string" && (key.includes("/daily-capacity") || key.includes("/send-progress") || key.endsWith("/summary")))]);
  };
  const perform = async (action: () => Promise<unknown>, success: string) => {
    if (busy) return;
    setBusy(true); setProblem(""); setMessage("");
    try { await action(); await refresh(); setMessage(success); }
    catch (error) { setProblem(errorMessage(error)); }
    finally { setBusy(false); }
  };
  const beginGroup = (group?: SenderGroup) => { setGroupName(group?.name || ""); setGroupDialog({ group }); setProblem(""); };
  const saveGroup = async (event: React.FormEvent) => {
    event.preventDefault();
    await perform(async () => {
      const group = groupDialog?.group;
      const result = await request<SenderGroup>(`/api/sender-groups${group ? `/${group.id}` : ""}`, group ? "PATCH" : "POST", { name: groupName.trim() });
      setGroupDialog(null);
      if (!expanded.includes(result.id)) setExpanded([...expanded, result.id]);
      if (!group) setAdding({ group: result, mode: "saved" });
    }, "Group saved.");
  };
  const saveAccount = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    await perform(async () => { await request(`/api/senders/${editing.id}`, "PATCH", { display_name: editing.display_name, daily_cap: editing.daily_cap }); setEditing(null); }, "Saved account updated.");
  };
  const attach = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!attaching || !targetGroup) return;
    await perform(async () => { await request(`/api/sender-groups/${targetGroup}/senders`, "POST", { sender_ids: [attaching.id] }); setExpanded([...new Set([...expanded, Number(targetGroup)])]); setAttaching(null); }, "Account added. Your existing Gmail connection is shared.");
  };
  const remove = async () => {
    if (!removing) return;
    await perform(async () => { await request("group" in removing ? `/api/sender-groups/${removing.group.id}` : `/api/senders/${removing.sender.id}`, "DELETE"); setRemoving(null); }, "group" in removing ? "Group deleted. Its Gmail accounts are still saved." : "Account disconnected from all groups.");
  };
  const addToAnother = (sender: Sender) => { setAttaching(sender); setTargetGroup(""); setProblem(""); };
  const reconnect = (sender: Sender, group?: SenderGroup) => {
    const target = group || groups.find((item) => item.senders.some((member) => member.id === sender.id));
    if (target) setAdding({ group: target, mode: "new" });
    else { addToAnother(sender); setMessage("Add this saved account to a group to reconnect Gmail."); }
  };
  const tableProps = {
    busy, onEdit: (sender: Sender) => { setEditing({ ...sender }); setProblem(""); },
    onDefault: (sender: Sender) => void perform(() => request(`/api/senders/${sender.id}/default`, "PATCH"), "Default account updated."),
    onAddToGroup: addToAnother,
  };
  const query = search.trim().toLowerCase();
  const filtered = groups.filter((group) => !query || group.name.toLowerCase().includes(query) || group.senders.some((sender) => `${sender.email} ${sender.display_name}`.toLowerCase().includes(query)));
  const modalOpen = Boolean(groupDialog || editing || attaching || removing || adding);
  const attachChoices = groups.filter((group) => !group.senders.some((sender) => sender.id === attaching?.id));
  return (
    <div className="app-page sender-groups-page">
      <PageHeading title="Senders" description="Organize your saved Gmail accounts into campaign groups." actions={<>
        <button className="app-button" aria-expanded={libraryOpen} aria-controls="saved-sender-accounts" onClick={() => setLibraryOpen(!libraryOpen)}>Saved accounts ({saved.length})</button>
        <button className="app-button is-primary" disabled={busy || isLoading || Boolean(error)} onClick={() => beginGroup()}><Plus size={18} />New group</button>
      </>} />
      {!modalOpen && <Notice error={problem || (params.get("oauth") === "error" ? "Gmail could not be connected. Please try again." : "")}
        message={message || (params.get("oauth") === "success" ? "Gmail connected. Your other groups are preserved." : "")} />}
      <div className="app-toolbar"><SearchField label="Search groups or accounts" value={search} onChange={setSearch} /><span className="app-count">{groups.length} {groups.length === 1 ? "group" : "groups"} · {saved.length} saved {saved.length === 1 ? "account" : "accounts"}</span></div>
      <PageState loading={isLoading} error={error} empty={!filtered.length} retry={() => void refreshGroups()}>
        <h2>{groups.length ? "No matching groups" : "Create your first sender group"}</h2><p>{groups.length ? "Try another group name or email address." : "Add a saved Gmail account or connect a new one inside your group."}</p>
        {!groups.length && <button className="app-button" onClick={() => beginGroup()}>Create group</button>}
      </PageState>
      <div className="sender-group-list">
        {filtered.map((group) => {
          const open = expanded.includes(group.id);
          return <section className="sender-group-card" key={group.id} aria-label={group.name}>
            <header className="sender-group-header">
              <button className="sender-group-toggle" aria-expanded={open} aria-controls={`sender-group-${group.id}`} onClick={() => setExpanded(open ? expanded.filter((id) => id !== group.id) : [...expanded, group.id])}>
                <ChevronDown size={18} className={open ? "is-open" : ""} aria-hidden="true" /><Folder size={20} aria-hidden="true" />
                <span><strong>{group.name}</strong><small>{group.senders.length} {group.senders.length === 1 ? "account" : "accounts"} · {group.connected_sender_count} connected</small></span>
              </button>
              <div className="sender-group-actions">
                <button className="app-button" disabled={busy} onClick={() => { setAdding({ group, mode: "saved" }); setProblem(""); }} aria-label={`Add sender to ${group.name}`}><Plus size={16} />Add sender</button>
                <ActionMenu iconOnly label={`Actions for ${group.name}`} disabled={busy}>
                  <MenuAction onClick={() => beginGroup(group)}>Rename group</MenuAction>
                  <MenuAction danger onClick={() => { setRemoving({ group }); setProblem(""); }}>Delete group</MenuAction>
                </ActionMenu>
              </div>
            </header>
            {open && <div id={`sender-group-${group.id}`} className="sender-group-body">
              {group.senders.length ? <SenderAccountsTable {...tableProps} senders={group.senders} inGroup onReconnect={(sender) => reconnect(sender, group)}
                onRemove={(sender) => void perform(() => request(`/api/sender-groups/${group.id}/senders/${sender.id}`, "DELETE"), `Removed ${sender.email} from ${group.name}. The Gmail connection is still saved.`)} />
                : <div className="sender-group-empty"><p>No accounts in this group yet.</p><button className="app-button" onClick={() => setAdding({ group, mode: "saved" })}>Add an account</button></div>}
            </div>}
          </section>;
        })}
      </div>
      <p className="app-footnote">One Gmail account can belong to several groups. Its connection, daily limit, and sent count are shared across those groups.</p>
      {libraryOpen && <section id="saved-sender-accounts" className="sender-library" aria-label="Saved Gmail accounts">
        <h2>Saved accounts</h2><p className="app-muted">Manage each Gmail connection here, including accounts that are not in a group.</p>
        <PageState loading={savedLoading} error={savedError} empty={!saved.length} retry={() => void refreshSaved()}><p>Connect a Gmail account from any group to save it here.</p></PageState>
        {saved.length > 0 && <SenderAccountsTable {...tableProps} senders={saved} onReconnect={(sender) => reconnect(sender)} onRemove={(sender) => { setRemoving({ sender }); setProblem(""); }} />}
      </section>}
      {adding && <AddSenderDialog key={`${adding.group.id}-${adding.mode}`} group={adding.group} initialMode={adding.mode} onClose={() => setAdding(null)} onAdded={async () => { await refresh(); setExpanded([...new Set([...expanded, adding.group.id])]); setMessage("Saved accounts added. Their other groups stay connected."); }} />}
      <AppDialog open={Boolean(groupDialog)} onClose={() => { if (!busy) setGroupDialog(null); }} title={groupDialog?.group ? "Rename group" : "Create a sender group"} description="Campaigns send through the accounts in the group you choose.">
        <form className="app-form" onSubmit={saveGroup}><label className="app-field">Group name<input autoFocus required maxLength={160} value={groupName} onChange={(event) => setGroupName(event.target.value)} placeholder="e.g. Hiring team" /></label><Notice error={problem} /><div className="app-dialog-actions"><button type="button" className="app-button" disabled={busy} onClick={() => setGroupDialog(null)}>Cancel</button><button className="app-button is-primary" disabled={busy || !groupName.trim()}>{busy ? "Saving…" : "Save group"}</button></div></form>
      </AppDialog>
      <AppDialog open={Boolean(editing)} onClose={() => { if (!busy) setEditing(null); }} title="Edit saved account" description={editing?.email}>
        {editing && <form className="app-form" onSubmit={saveAccount}>
          <label className="app-field">Display name<input maxLength={200} value={editing.display_name} onChange={(event) => setEditing({ ...editing, display_name: event.target.value })} /></label>
          <label className="app-field">Account daily limit<input type="number" min="1" max="500" required value={editing.daily_cap} onChange={(event) => setEditing({ ...editing, daily_cap: Number(event.target.value) })} /></label>
          <p className="app-muted sender-dialog-copy">The name and account limit apply in all {editing.group_ids?.length ?? (editing.group_id ? 1 : 0)} of its groups. Your Gmail connection stays signed in.</p>
          <Notice error={problem} /><div className="app-dialog-actions"><button type="button" className="app-button" disabled={busy} onClick={() => setEditing(null)}>Cancel</button><button className="app-button is-primary" disabled={busy}>{busy ? "Saving…" : "Save account"}</button></div>
        </form>}
      </AppDialog>
      <AppDialog open={Boolean(attaching)} onClose={() => { if (!busy) setAttaching(null); }} title="Add saved account to a group" description={attaching?.email}>
        <form className="app-form" onSubmit={attach}>
          {attachChoices.length ? <label className="app-field">Group<select value={targetGroup} required onChange={(event) => setTargetGroup(event.target.value)}><option value="">Choose a group</option>{attachChoices.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label> : <p>This account is already in all your groups. Create another group to use it there.</p>}
          <p className="app-muted sender-dialog-copy">Reuses your saved connection and keeps all existing memberships.</p><Notice error={problem} />
          <div className="app-dialog-actions"><button type="button" className="app-button" disabled={busy} onClick={() => setAttaching(null)}>Cancel</button><button className="app-button is-primary" disabled={busy || !targetGroup}>{busy ? "Adding…" : "Add to group"}</button></div>
        </form>
      </AppDialog>
      <ConfirmDialog open={Boolean(removing)} onClose={() => { if (!busy) setRemoving(null); }} busy={busy} error={problem} onConfirm={() => void remove()}
        title={removing && "group" in removing ? `Delete ${removing.group.name}?` : "Disconnect Gmail from all groups?"}
        description={removing && "group" in removing ? "The Gmail accounts stay saved and available in their other groups. Campaigns using this group will pause until you choose another group." : "This disconnects this Gmail account everywhere. Other Gmail accounts stay connected. You can reconnect it later."} />
    </div>
  );
}

export default function SendersPage() {
  return <Suspense fallback={<PageState loading />}><SendersContent /></Suspense>;
}
