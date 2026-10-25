"use client";

import { useCallback, useRef, useState } from "react";
import useSWR from "swr";
import { ArrowLeft, Braces, FileText, Plus } from "lucide-react";
import type { Editor } from "@tiptap/react";
import EmailComposer from "@/components/EmailComposer";
import { extractTemplateVariables } from "@/lib/templateVariables";
import type { EmailTemplate as Template } from "@/lib/templateMapping";
import "@/components/templates/templates.css";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import {
  ActionMenu,
  ConfirmDialog,
  MenuAction,
  Notice,
  PageHeading,
  PageState,
  Pager,
  SearchField,
  formatDate,
} from "@/components/app-ui";

const emptyTemplate = { title: "", subject: "", body: "" };
const PAGE_SIZE = 6;

export default function TemplatesPage() {
  const { API_URL, authFetch } = useApiClient();
  const {
    data: templates = [],
    error,
    isLoading,
    mutate,
  } = useSWR<Template[]>(`${API_URL}/api/templates`);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Template | "new" | null>(null);
  const [draft, setDraft] = useState(emptyTemplate);
  const [removing, setRemoving] = useState<Template | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [fieldName, setFieldName] = useState("");
  const editorRef = useRef<Editor | null>(null);
  const editorReady = useCallback((editor: Editor | null) => { editorRef.current = editor; }, []);
  const variables = [...new Set([...extractTemplateVariables(draft.subject), ...extractTemplateVariables(draft.body)])];
  const insertField = (target: "subject" | "body", field = fieldName.trim()) => {
    if (!field || /[{}]/.test(field)) return;
    const placeholder = `{{ ${field} }}`;
    if (target === "body" && editorRef.current && !editorRef.current.isDestroyed) {
      editorRef.current.chain().focus().insertContent({ type: "text", text: placeholder }).run();
    } else setDraft({ ...draft, [target]: draft[target] + placeholder });
  };
  const filtered = templates.filter((item) =>
    `${item.title} ${item.subject}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const currentPage = Math.min(
    page,
    Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)),
  );
  const edit = (template: Template | "new") => {
    setEditing(template);
    setDraft(template === "new" ? emptyTemplate : template);
    setActionError("");
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setActionError("");
    try {
      const isNew = editing === "new";
      await checkResponse(
        await authFetch(
          `${API_URL}/api/templates${!isNew && editing ? `/${editing.id}` : ""}`,
          {
            method: isNew ? "POST" : "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: draft.title.trim(),
              subject: draft.subject,
              body: draft.body,
            }),
          },
        ),
      );
      await mutate();
      setEditing(null);
      setMessage("Template saved.");
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
      await checkResponse(
        await authFetch(`${API_URL}/api/templates/${removing.id}`, {
          method: "DELETE",
        }),
      );
      await mutate();
      setRemoving(null);
      setMessage("Template removed. Campaign messages are unchanged.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  if (editing !== null) return (
    <div className="app-page template-editor-page">
      <form className="app-form" onSubmit={save}>
        <PageHeading title={editing === "new" ? "New template" : "Edit template"} description="Write and format your email just as you would in a campaign."
          actions={<><button type="button" className="app-button" disabled={busy} onClick={() => setEditing(null)}><ArrowLeft size={16} />Templates</button>
            <button className="app-button is-primary" disabled={busy || !draft.title.trim() || !draft.subject.trim() || !draft.body.replace(/<[^>]*>/g, "").trim()}>{busy ? "Saving…" : "Save template"}</button></>} />
        <Notice error={actionError} />
        <label className="app-field template-editor-name">Template name<input autoFocus required maxLength={240} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} disabled={busy} /></label>
        <section className="campaign-ui campaign-composer" aria-label="Write template email">
          <div className="template-placeholder-tools">
            <label className="app-field">Personalization field<input value={fieldName} onChange={(event) => setFieldName(event.target.value)} placeholder="e.g. first_name" disabled={busy} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); insertField("body"); } }} /></label>
            <button type="button" className="app-button" disabled={busy || !fieldName.trim() || /[{}]/.test(fieldName)} onClick={() => insertField("subject")}><Braces size={16} />Add to subject</button>
            <button type="button" className="app-button" disabled={busy || !fieldName.trim() || /[{}]/.test(fieldName)} onClick={() => insertField("body")}><Braces size={16} />Add to message</button>
          </div>
          {variables.length > 0 && <div className="template-editor-fields" aria-label="Template fields">{variables.map((field) => <button type="button" key={field} onClick={() => insertField("body", field)} disabled={busy}>{`{{${field}}}`}</button>)}</div>}
          <EmailComposer subject={draft.subject} body={draft.body} onSubjectChange={(subject) => setDraft((current) => ({ ...current, subject }))}
            onBodyChange={(body) => setDraft((current) => ({ ...current, body }))} validVariables={variables} onEditorReady={editorReady} readOnly={busy} />
        </section>
        <p className="app-muted template-mapping-copy">When you load this template into a campaign, you’ll match its placeholders to that campaign’s audience fields.</p>
      </form>
    </div>
  );
  return (
    <div className="app-page">
      <PageHeading
        title="Templates"
        description="Reusable starting points for your emails."
        actions={
          <button className="app-button is-primary" onClick={() => edit("new")}>
            <Plus size={18} /> New template
          </button>
        }
      />
      <Notice message={message} />
      <div className="app-toolbar">
        <SearchField
          label="Search templates"
          value={search}
          onChange={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
      </div>
      <div className="app-table-wrap">
        <PageState
          loading={isLoading}
          error={error}
          empty={!filtered.length}
          retry={() => void mutate()}
        >
          {templates.length ? undefined : (
            <>
              <h2>Save a starting point</h2>
              <p>
                Create a template to reuse your subject and message in a
                campaign.
              </p>
            </>
          )}
        </PageState>
        {!isLoading && !error && filtered.length > 0 && (
          <table className="app-table">
            <thead>
              <tr>
                <th scope="col">Template</th>
                <th scope="col">Subject</th>
                <th scope="col">Updated</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered
                .slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE)
                .map((item) => (
                  <tr key={item.id}>
                    <td>
                      <button
                        className="app-name app-name-button"
                        onClick={() => edit(item)}
                      >
                        <FileText size={19} color="#506184" />
                        {item.title}
                      </button>
                    </td>
                    <td>{item.subject}</td>
                    <td className="app-muted">{formatDate(item.updated_at)}</td>
                    <td>
                      <ActionMenu iconOnly label={`Actions for ${item.title}`}>
                        <MenuAction onClick={() => edit(item)}>
                          Edit template
                        </MenuAction>
                        <MenuAction
                          danger
                          onClick={() => {
                            setRemoving(item);
                            setActionError("");
                          }}
                        >
                          Delete template
                        </MenuAction>
                      </ActionMenu>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </div>
      {filtered.length > 0 && (
        <Pager
          page={currentPage}
          pageSize={PAGE_SIZE}
          total={filtered.length}
          onChange={setPage}
        />
      )}
      <ConfirmDialog
        open={!!removing}
        title="Delete template?"
        description={`Delete “${removing?.title}”? Existing campaign messages will stay unchanged.`}
        busy={busy}
        error={actionError}
        onClose={() => setRemoving(null)}
        onConfirm={() => void remove()}
        label="Delete template"
      />
    </div>
  );
}
