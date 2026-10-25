"use client";

import { useState } from "react";
import { mutate } from "swr";
import { AppDialog, Notice } from "@/components/app-ui";
import EmailComposer from "@/components/EmailComposer";
import { checkResponse, errorMessage, useApiClient } from "@/lib/api";
import { extractTemplateVariables } from "@/lib/templateVariables";
import "./templates.css";

export default function SaveTemplateDialog({ subject, body, defaultName, onClose, onSaved }: {
  subject: string; body: string; defaultName: string; onClose: () => void; onSaved: () => void;
}) {
  const { API_URL, authFetch } = useApiClient();
  const [title, setTitle] = useState(defaultName);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setProblem("");
    try {
      await checkResponse(await authFetch(`${API_URL}/api/templates`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: title.trim(), subject, body }),
      }));
      await mutate(`${API_URL}/api/templates`);
      onSaved();
    } catch (error) { setProblem(errorMessage(error)); }
    finally { setBusy(false); }
  };
  return <AppDialog open onClose={() => { if (!busy) onClose(); }} title="Save campaign email as a template" description="Reuse this subject, formatting, and placeholders in another campaign.">
    <form className="app-form" onSubmit={save}>
      <label className="app-field">Template name<input autoFocus required maxLength={240} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <details className="template-mapping-preview" open><summary>Preview saved email</summary>
        <div className="campaign-ui campaign-composer"><EmailComposer subject={subject} body={body} onSubjectChange={() => {}} onBodyChange={() => {}} readOnly
          validVariables={[...new Set([...extractTemplateVariables(subject), ...extractTemplateVariables(body)])]} /></div>
      </details>
      <Notice error={problem} /><div className="app-dialog-actions"><button type="button" className="app-button" disabled={busy} onClick={onClose}>Cancel</button><button className="app-button is-primary" disabled={busy || !title.trim() || !subject.trim() || !body.replace(/<[^>]*>/g, "").trim()}>{busy ? "Saving…" : "Save template"}</button></div>
    </form>
  </AppDialog>;
}
