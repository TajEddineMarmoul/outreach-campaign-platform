"use client";

import { useId } from "react";
import type { Editor } from "@tiptap/react";
import RichTextEditor from "@/components/RichTextEditor";

/** The same subject and rich message editor in campaigns and saved templates. */
export default function EmailComposer({ subject, body, onSubjectChange, onBodyChange, validVariables = [], readOnly = false, onBlur, onEditorReady }: {
  subject: string;
  body: string;
  onSubjectChange: (value: string) => void;
  onBodyChange: (value: string) => void;
  validVariables?: string[];
  readOnly?: boolean;
  onBlur?: () => void;
  onEditorReady?: (editor: Editor | null) => void;
}) {
  const subjectId = useId();
  return <>
    <label className="campaign-subject-label" htmlFor={subjectId}>Subject</label>
    <input id={subjectId} className="campaign-subject" placeholder="A clear reason to open your email" value={subject}
      onChange={(event) => onSubjectChange(event.target.value)} onBlur={onBlur} disabled={readOnly} maxLength={998} />
    <div className="campaign-editor-frame">
      <RichTextEditor content={body} onChange={onBodyChange} onBlur={onBlur} placeholder="Write your email here…"
        validVariables={validVariables} onEditorReady={onEditorReady} readOnly={readOnly} minimalToolbar />
    </div>
  </>;
}
