import { useState } from "react";
import Link from "next/link";
import useSWR, { mutate } from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Plus, CheckCircle, Trash2, FolderPlus, AtSign, Pencil, Check, X } from "lucide-react";
import AddSenderDialog from "@/components/senders/AddSenderDialog";
import { cn } from "@/lib/utils";
import {
  API_URL,
  checkResponse,
  errorMessage,
  useApiClient,
} from "@/lib/api";
import type { Sender, SenderGroup } from "@/types/senders";

function InlineEditSender({
  value,
  onSave,
  placeholder,
  className,
  inputClassName,
}: {
  value: string | number;
  onSave: (v: string) => void;
  placeholder?: string;
  className?: string;
  inputClassName?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(value));

  const commit = () => {
    setEditing(false);
    if (draft !== String(value)) onSave(draft);
  };
  const cancel = () => {
    setDraft(String(value));
    setEditing(false);
  };

  if (editing) {
    return (
      <span className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
        <Input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") cancel();
          }}
          className={cn("h-7 text-xs px-2 w-32", inputClassName)}
        />
        <button onClick={(e) => { e.stopPropagation(); commit(); }} className="text-green-600 hover:text-green-700 cursor-pointer">
          <Check className="w-3.5 h-3.5" />
        </button>
        <button onClick={(e) => { e.stopPropagation(); cancel(); }} className="text-slate-400 hover:text-slate-600 cursor-pointer">
          <X className="w-3.5 h-3.5" />
        </button>
      </span>
    );
  }

  return (
    <button
      onClick={(e) => { e.stopPropagation(); setDraft(String(value)); setEditing(true); }}
      className={cn("group flex items-center gap-1 hover:text-blue-600 transition-colors cursor-pointer", className)}
    >
      <span>{value || <span className="italic text-slate-400">{placeholder}</span>}</span>
      <Pencil className="w-2.5 h-2.5 opacity-0 group-hover:opacity-50 transition-opacity" />
    </button>
  );
}

export default function SenderSelectionDialog({
  isOpen,
  onClose,
  senderGroups: initialGroups,
  selectedGroupId,
  onSelect,
  returnPath,
  beforeConnect,
}: {
  isOpen: boolean;
  onClose: () => void;
  senderGroups?: SenderGroup[];
  selectedGroupId?: number | null;
  onSelect: (senderGroupId: number) => void;
  returnPath?: string;
  beforeConnect?: () => Promise<void>;
}) {
  const { data: groupsData, mutate: mutateGroups } = useSWR<SenderGroup[]>(
    isOpen ? `${API_URL}/api/sender-groups` : null,
    { fallbackData: initialGroups }
  );
  const groups = groupsData || initialGroups || [];
  const { authFetch } = useApiClient();

  const [newGroupName, setNewGroupName] = useState("");
  const [addingGroup, setAddingGroup] = useState(false);
  const [addingSenderGroup, setAddingSenderGroup] = useState<SenderGroup | null>(null);
  const [connectError, setConnectError] = useState("");

  const request = async <T = Record<string, unknown>>(
    path: string,
    options: RequestInit,
    fallback: string,
  ) => {
    return checkResponse<T>(
      await authFetch(`${API_URL}${path}`, options),
      fallback,
    );
  };

  const refreshGroups = async () => {
    await Promise.all([
      mutateGroups(),
      mutate(`${API_URL}/api/sender-groups`),
    ]);
  };

  const handlePatchSender = async (id: number, patch: Partial<Sender>) => {
    if (patch.daily_cap !== undefined && (!Number.isInteger(patch.daily_cap) || patch.daily_cap < 1 || patch.daily_cap > 500)) {
      setConnectError("Enter a daily limit from 1 to 500.");
      return;
    }
    try {
      await request(
        `/api/senders/${id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        },
        "Could not update sender",
      );
      await refreshGroups();
    } catch (error: unknown) {
      setConnectError(errorMessage(error, "Could not reach backend"));
    }
  };

  const handleDelete = async (groupId: number, id: number) => {
    try {
      await request(
        `/api/sender-groups/${groupId}/senders/${id}`,
        { method: "DELETE" },
        "Could not remove sender",
      );
      await refreshGroups();
    } catch (error: unknown) {
      setConnectError(errorMessage(error, "Could not reach backend"));
    }
  };

  const handleRenameGroup = async (groupId: number, newName: string) => {
    const name = newName.trim();
    if (!name) return;
    try {
      await request(
        `/api/sender-groups/${groupId}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        },
        "Could not rename group",
      );
      await refreshGroups();
    } catch (error: unknown) {
      setConnectError(errorMessage(error, "Could not reach backend"));
    }
  };

  const handleCreateGroup = async () => {
    const name = newGroupName.trim();
    if (!name) return;
    setConnectError("");
    try {
      await request(
        "/api/sender-groups",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        },
        "Could not create group",
      );
      await refreshGroups();
      setNewGroupName("");
      setAddingGroup(false);
    } catch (error: unknown) {
      setConnectError(errorMessage(error, "Could not reach backend"));
    }
  };

  if (isOpen && addingSenderGroup) {
    return <AddSenderDialog group={addingSenderGroup} onClose={() => setAddingSenderGroup(null)} onAdded={refreshGroups} returnPath={returnPath} beforeConnect={beforeConnect} />;
  }

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-xl max-h-[85vh] flex flex-col p-6">
        <DialogHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-slate-100 shrink-0">
          <div>
            <DialogTitle className="text-xl font-bold text-slate-900">Choose Campaign Sender Group</DialogTitle>
            <p className="text-xs text-slate-500 mt-1">Choose a group or expand it to see its accounts. Shared accounts use one daily limit.</p>
          </div>
          <Button
            size="sm"
            onClick={() => setAddingGroup(true)}
            className="bg-blue-600 hover:bg-blue-700 text-white gap-1.5 h-8 text-xs font-semibold cursor-pointer shrink-0"
          >
            <FolderPlus className="w-3.5 h-3.5" />
            New Group
          </Button>
        </DialogHeader>

        {connectError && (
          <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2 mt-2 shrink-0">
            {connectError}
          </div>
        )}

        {addingGroup && (
          <div className="flex items-center gap-2 p-3 bg-slate-50 border border-blue-100 rounded-xl mt-3 shrink-0">
            <FolderPlus className="w-4 h-4 text-blue-500 shrink-0" />
            <Input
              autoFocus
              placeholder="New group name"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreateGroup();
                if (e.key === "Escape") { setAddingGroup(false); setNewGroupName(""); }
              }}
              className="h-8 text-xs flex-1 bg-white"
            />
            <Button size="sm" onClick={handleCreateGroup} className="bg-blue-600 hover:bg-blue-700 text-white h-8 text-xs cursor-pointer">
              Create
            </Button>
            <Button size="sm" variant="outline" onClick={() => { setAddingGroup(false); setNewGroupName(""); }} className="h-8 text-xs cursor-pointer">
              Cancel
            </Button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto min-h-0 py-4 space-y-4 pr-1 mt-2">
          {groups.length === 0 && !addingGroup ? (
            <div className="border border-dashed border-slate-200 rounded-xl p-8 text-center space-y-3">
              <div className="w-12 h-12 rounded-full bg-blue-50 flex items-center justify-center text-blue-500 mx-auto">
                <AtSign className="w-6 h-6" />
              </div>
              <div className="text-sm font-semibold text-slate-800">No Groups Found</div>
              <p className="text-xs text-slate-500 max-w-xs mx-auto">
                Create a group first, then connect Gmail senders inside it.
              </p>
              <Button
                size="sm"
                onClick={() => setAddingGroup(true)}
                className="bg-blue-600 hover:bg-blue-700 text-white gap-1.5 h-8 text-xs cursor-pointer"
              >
                <FolderPlus className="w-3.5 h-3.5" />
                Create Group
              </Button>
            </div>
          ) : (
            groups.map((group) => {
              const isSelectedGroup = selectedGroupId === group.id;
              return (
                <div
                  key={group.id}
                  className={`border rounded-xl bg-slate-50/50 overflow-hidden transition-colors ${
                    isSelectedGroup ? "border-blue-300 ring-1 ring-blue-100" : "border-slate-100"
                  }`}
                >
                  <div
                    className="flex items-center justify-between px-4 py-2 bg-white border-b border-slate-100 cursor-pointer hover:bg-blue-50/40"
                  >
                    <div className="flex items-center gap-1.5 text-xs font-bold text-slate-700">
                      {isSelectedGroup && <CheckCircle className="w-4 h-4 text-blue-600 shrink-0" />}
                      <InlineEditSender
                        value={group.name}
                        onSave={(newName) => handleRenameGroup(group.id, newName)}
                        className="text-xs font-bold text-slate-700"
                      />
                      <span className="text-[10px] font-normal text-slate-400">
                        ({group.senders.length} accounts, {group.connected_sender_count} connected)
                      </span>
                    </div>

                    <div className="flex items-center gap-2">
                    <Button size="sm" variant={isSelectedGroup ? "default" : "outline"} onClick={() => onSelect(group.id)} className="h-7 text-[10px] px-2">
                      {isSelectedGroup ? "Selected" : "Use group"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={(e) => {
                        e.stopPropagation();
                        setAddingSenderGroup(group);
                      }}
                      className="h-7 text-[10px] gap-1 px-2 border-slate-200 hover:border-blue-300 hover:text-blue-600 cursor-pointer"
                    >
                      <Plus className="w-3 h-3" /> Add sender
                    </Button>
                    </div>
                  </div>

                  <details className="p-3 space-y-2">
                    <summary className="text-xs text-slate-500 cursor-pointer">Show {group.senders.length} {group.senders.length === 1 ? "account" : "accounts"}</summary>
                    <p className="text-[10px] text-slate-500">Account names and daily limits are shared across all their groups.</p>
                    {group.senders.length === 0 ? (
                      <div className="text-center py-4 text-xs text-slate-400">
                        No senders in this group.{" "}
                        <button
                          onClick={() => setAddingSenderGroup(group)}
                          className="text-blue-500 hover:underline cursor-pointer font-medium"
                        >
                          Add one
                        </button>
                      </div>
                    ) : (
                      group.senders.map((sender) => {
                        const initials = sender.email.slice(0, 2).toUpperCase();
                        return (
                          <div
                            key={sender.id}
                            className="w-full border rounded-xl p-3 text-left flex items-center justify-between transition-all border-slate-200 bg-white"
                          >
                            <div className="flex items-center gap-3 min-w-0">
                              <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-white text-xs font-bold shrink-0">
                                {initials}
                              </div>
                              <div className="min-w-0">
                                <div className="text-xs font-semibold text-slate-800 truncate">{sender.email}</div>
                                <div className="mt-0.5 text-[10px] text-slate-400">
                                  <InlineEditSender
                                    value={sender.display_name}
                                    placeholder="Add display name..."
                                    onSave={(v) => handlePatchSender(sender.id, { display_name: v })}
                                  />
                                </div>
                              </div>
                            </div>

                            <div className="flex items-center gap-3 shrink-0">
                              <div className="flex items-center gap-1 text-[10px] text-slate-500 bg-slate-100 rounded-full px-2 py-0.5 font-medium">
                                <span>Cap:</span>
                                <InlineEditSender
                                  value={sender.daily_cap}
                                  onSave={(v) => handlePatchSender(sender.id, { daily_cap: Number(v) })}
                                  className="font-bold text-slate-700"
                                />
                                <span>/day</span>
                              </div>

                              <button
                                title="Remove from this group"
                                aria-label={`Remove ${sender.email} from ${group.name}`}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (confirm(`Remove ${sender.email} from ${group.name}? Its saved connection and other groups will stay connected.`)) {
                                    handleDelete(group.id, sender.id);
                                  }
                                }}
                                className="p-1 rounded text-slate-400 hover:text-red-500 hover:bg-red-50 transition-colors cursor-pointer shrink-0"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </details>
                </div>
              );
            })
          )}
        </div>

        <DialogFooter className="flex-row justify-between items-center border-t border-slate-100 pt-4 mt-2 shrink-0">
          <Link
            href="/senders"
            className="text-xs text-slate-500 hover:text-blue-600 underline underline-offset-2 transition-colors"
          >
            Manage senders tab
          </Link>
          <Button variant="outline" size="sm" onClick={onClose} className="h-8 text-xs cursor-pointer">
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
