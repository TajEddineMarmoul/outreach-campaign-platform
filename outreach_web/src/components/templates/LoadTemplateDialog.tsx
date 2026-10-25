"use client";

import { useState } from "react";
import useSWR from "swr";
import { ArrowLeft, ArrowRight, FileText } from "lucide-react";
import { AppDialog, PageState, SearchField } from "@/components/app-ui";
import EmailComposer from "@/components/EmailComposer";
import VariableMappingSelect from "./VariableMappingSelect";
import { useApiClient } from "@/lib/api";
import { mappingFields, remapTemplateText, suggestVariableMapping, templateVariables, type EmailTemplate } from "@/lib/templateMapping";
import { type TemplateValidationSummary } from "@/lib/templateVariables";
import "./templates.css";

function MappingStep({ template, summary, validationError, retry, onBack, onLoad }: {
  template: EmailTemplate; summary?: TemplateValidationSummary; validationError?: boolean; retry: () => void;
  onBack: () => void; onLoad: (subject: string, body: string) => void;
}) {
  const variables = templateVariables(template);
  const fields = mappingFields(summary);
  const [mapping, setMapping] = useState(() => suggestVariableMapping(variables, fields));
  const subject = remapTemplateText(template.subject, mapping);
  const body = remapTemplateText(template.body, mapping);
  const unresolved = variables.filter((variable) => !mapping[variable] || !fields.some((field) => field.name === mapping[variable]));
  const incomplete = fields.filter((field) => field.missing > 0 && Object.values(mapping).includes(field.name));
  const ready = variables.length === 0 || Boolean(summary?.total_contacts && !validationError && unresolved.length === 0);
  return <>
    <button className="app-button is-quiet" onClick={onBack}><ArrowLeft size={16} />Choose another template</button>
    <h3 className="template-mapping-heading">Match template fields to your audience</h3>
    <p className="app-muted template-mapping-copy">Review the suggested matches. These choices update the placeholders in this campaign when you load the template.</p>
    {variables.length > 0 && (!summary || validationError) && <PageState loading={!summary && !validationError} error={validationError ? new Error("Could not check audience fields") : undefined} retry={retry} />}
    {variables.length > 0 && summary?.total_contacts === 0 && <p className="app-notice is-error" role="alert">Add your audience before matching this template’s fields.</p>}
    <div className="template-mapping-rows">
      {variables.map((variable) => <div className="template-mapping-row" key={variable}>
        <code>{`{{${variable}}}`}</code><ArrowRight size={18} aria-hidden="true" />
        <VariableMappingSelect source={variable} value={mapping[variable] || ""} fields={fields}
          onChange={(target) => setMapping({ ...mapping, [variable]: target })} />
      </div>)}
    </div>
    {!variables.length && <p className="app-muted">This template has no personalization fields to map.</p>}
    {unresolved.length > 0 && Boolean(summary?.total_contacts) && <p className="template-field-missing-text" role="status">Choose an audience field for {unresolved.map((value) => `{{${value}}}`).join(", ")}.</p>}
    {incomplete.length > 0 && <div className="app-notice is-error" role="alert">
      Some selected fields have missing values. Recipients missing a required value will be blocked from sending until their data or your message is corrected.
    </div>}
    <details className="template-mapping-preview" open><summary>Preview: {template.title}</summary>
      <div className="campaign-ui campaign-composer"><EmailComposer subject={subject} body={body} onSubjectChange={() => {}} onBodyChange={() => {}} readOnly validVariables={Object.values(mapping).filter(Boolean)} /></div>
    </details>
    <p className="app-muted template-mapping-copy">Loading replaces this campaign’s subject and message. The saved template stays unchanged.</p>
    <div className="app-dialog-actions"><button className="app-button is-primary" disabled={!ready} onClick={() => onLoad(subject, body)}>Load into campaign<ArrowRight size={16} /></button></div>
  </>;
}

export default function LoadTemplateDialog({ onClose, onLoad, summary, validationError, onRetry }: {
  onClose: () => void; onLoad: (subject: string, body: string) => void;
  summary?: TemplateValidationSummary; validationError?: boolean; onRetry: () => void;
}) {
  const { API_URL } = useApiClient();
  const { data: templates = [], error, isLoading, mutate } = useSWR<EmailTemplate[]>(`${API_URL}/api/templates`);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<EmailTemplate | null>(null);
  const filtered = templates.filter((template) => `${template.title} ${template.subject}`.toLowerCase().includes(search.toLowerCase()));
  return <AppDialog open onClose={onClose} title={selected ? `Load ${selected.title}` : "Load an email template"}
    description={selected ? "Match the template’s placeholders to fields in this campaign." : "Choose a saved email, then review its field mapping and preview."}>
    <div className="template-load-dialog">
      {selected ? <MappingStep key={`${selected.id}-${Boolean(summary)}`} template={selected} summary={summary} validationError={validationError}
        retry={onRetry} onBack={() => setSelected(null)} onLoad={onLoad} /> : <>
        <SearchField label="Search saved templates" value={search} onChange={setSearch} />
        <PageState loading={isLoading} error={error} empty={!filtered.length} retry={() => void mutate()}>
          <h3>{templates.length ? "No matching templates" : "No saved templates yet"}</h3>
          <p>{templates.length ? "Try another template name." : "Use Save as template in your campaign, or create one from Templates."}</p>
        </PageState>
        <div className="template-picker-list">{filtered.map((template) => <button key={template.id} className="template-picker-item" onClick={() => setSelected(template)}>
          <FileText size={20} aria-hidden="true" /><span><strong>{template.title}</strong><small>{template.subject}</small></span><ArrowRight size={17} aria-hidden="true" />
        </button>)}</div>
        <div className="app-dialog-actions"><button className="app-button" onClick={onClose}>Cancel</button></div>
      </>}
    </div>
  </AppDialog>;
}
