"use client";

import { useState } from "react";
import useSWR from "swr";
import { ArrowLeft, ArrowRight, Users, Loader2 } from "lucide-react";
import { missingTemplateVariables, TEMPLATE_VARIABLE_PATTERN } from "@/lib/templateVariables";
import { emailPreviewDocument } from "./emailPreviewDocument";
import { API_URL } from "@/lib/api";

type Recipient = {
  contact_id: number;
  email: string;
  custom_fields: Record<string, unknown>;
};
const escapeHtml = (value: string) =>
  value.replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ]!,
  );

export function renderRecipientTemplate(
  template: string,
  fields: Record<string, unknown>,
  html = false,
): string {
  return template.replace(TEMPLATE_VARIABLE_PATTERN, (_, key: string) => {
    const value = Object.hasOwn(fields, key.trim()) ? fields[key.trim()] : undefined;
    const text =
      value === undefined || value === null || String(value).trim() === ""
        ? `[missing ${key.trim()}]`
        : String(value);
    return html ? escapeHtml(text) : text;
  });
}

export default function RecipientPreview({
  campaignId,
  subject,
  body,
  onAddAudience,
}: {
  campaignId: string;
  subject: string;
  body: string;
  onAddAudience: () => void;
}) {
  const [index, setIndex] = useState(0);
  const { data, error, isLoading } = useSWR<{
    items: Recipient[];
    total: number;
  }>(
    `${API_URL}/api/campaigns/${campaignId}/recipients?page=${index + 1}&page_size=1`,
  );
  const recipient = data?.items[0];
  const fields: Record<string, unknown> = {
    email: recipient?.email || "",
    ...recipient?.custom_fields,
  };
  const name = String(
    fields.full_name || fields.first_name || recipient?.email || "Recipient",
  );
  const company = String(
    fields.company || fields.company_name || recipient?.email || "",
  );
  const initials = name
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  const htmlBody = /<[a-z][\s\S]*>/i.test(body)
    ? body
    : `<p>${escapeHtml(body).replace(/\n/g, "<br>")}</p>`;
  const renderedBody = renderRecipientTemplate(htmlBody, fields, true);
  const missingVariables = [...new Set([
    ...missingTemplateVariables(subject, fields),
    ...missingTemplateVariables(body, fields),
  ])];
  const previewDocument = emailPreviewDocument(renderedBody);
  return (
    <aside className="campaign-preview" aria-label="Recipient preview">
      <h2>Recipient preview</h2>
      {isLoading ? (
        <div className="campaign-empty">
          <Loader2 className="animate-spin" />
          Loading recipient…
        </div>
      ) : error ? (
        <div className="campaign-notice is-error" role="alert">
          Could not load recipients. Refresh the page to try again.
        </div>
      ) : !recipient ? (
        <div className="campaign-empty">
          <Users size={28} />
          <p>
            {index > 0
              ? "This recipient is no longer in the audience."
              : "Add your audience to see a personalized preview."}
          </p>
          <button
            className="campaign-text-button"
            onClick={index > 0 ? () => setIndex(0) : onAddAudience}
          >
            {index > 0 ? "Back to first recipient" : "Choose an audience"}
          </button>
        </div>
      ) : (
        <>
          <div className="campaign-preview-person">
            <span className="campaign-avatar">{initials}</span>
            <div className="campaign-person-details">
              <strong>{name}</strong>
              <span>{company}</span>
            </div>
            <div className="campaign-preview-arrows">
              <button
                className="campaign-icon-button"
                aria-label="Previous recipient"
                disabled={index === 0}
                onClick={() => setIndex(index - 1)}
              >
                <ArrowLeft size={20} />
              </button>
              <button
                className="campaign-icon-button"
                aria-label="Next recipient"
                disabled={index + 1 >= (data?.total || 0)}
                onClick={() => setIndex(index + 1)}
              >
                <ArrowRight size={20} />
              </button>
            </div>
          </div>
          {missingVariables.length > 0 && (
            <div className="campaign-notice is-error" role="alert">
              <strong>Missing personalization data</strong>
              <p>{missingVariables.map((variable) => `{{${variable}}}`).join(", ")} {missingVariables.length === 1 ? "has" : "have"} no value for this recipient.</p>
              <p>This recipient will be skipped. Fill in their data or remove these fields from your message.</p>
            </div>
          )}
          <div className="campaign-preview-email">
            <div className="campaign-preview-subject">
              Subject:{" "}
              {renderRecipientTemplate(subject, fields) ||
                "Your subject will appear here"}
            </div>
            <iframe
              title={`Email preview for ${name}`}
              sandbox=""
              srcDoc={previewDocument}
              className="campaign-preview-frame"
            />
          </div>
          <p className="campaign-preview-caption">
            {index + 1} of {data?.total} recipients · Preview only. Nothing is
            sent.
          </p>
        </>
      )}
    </aside>
  );
}
