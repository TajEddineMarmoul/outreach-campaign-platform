"use client";

import { useEffect, useRef, useState } from "react";
import { upload } from "@vercel/blob/client";
import { useUser } from "@clerk/nextjs";
import { ArrowLeft, ArrowRight, CheckCircle, Link as LinkIcon, Loader2, Plus, Upload, X } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useApiClient } from "@/lib/api";
import { trackEvent } from "@/lib/analytics";

type Method = "paste" | "csv" | "sheet";
const MAX_CSV_FILE_BYTES = 100 * 1024 * 1024;
const MAX_CSV_BATCH_BYTES = 200 * 1024 * 1024;

interface ImportPreview {
  columns: string[];
  rows: Record<string, string>[];
  total_rows: number;
  email_column: string;
  source_count?: number;
}
interface SheetSource {
  id: string;
  url: string;
  tabName: string;
}
interface UploadedCsv {
  url: string;
  filename: string;
}
interface Props {
  isOpen: boolean;
  onClose: () => void;
  campaignId: string;
  onImported: () => Promise<void>;
}

export default function RecipientsImportDialog(props: Props) {
  return props.isOpen ? <ImportForm {...props} /> : null;
}

function ImportForm({ onClose, campaignId, onImported }: Props) {
  const { API_URL, authFetch } = useApiClient();
  const { user } = useUser();
  const [method, setMethod] = useState<Method>("paste");
  const [raw, setRaw] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [uploadedCsvs, setUploadedCsvs] = useState<UploadedCsv[] | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [sheetSources, setSheetSources] = useState<SheetSource[]>([{ id: "sheet-1", url: "", tabName: "" }]);
  const nextSheetId = useRef(2);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [complete, setComplete] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const activeSheets = sheetSources.filter((source) => source.url.trim());
  const ready = method === "paste" ? Boolean(raw.trim()) : method === "csv" ? files.length > 0 : activeSheets.length > 0;

  const clearUploadedCsvs = (uploads = uploadedCsvs) => {
    if (!uploads?.length) return;
    setUploadedCsvs(null);
    void fetch("/api/campaign-import-cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: uploads.map((item) => item.url) }),
    });
  };

  const chooseFiles = (selected: File[]) => {
    const oversized = selected.find((file) => file.size > MAX_CSV_FILE_BYTES);
    const totalBytes = selected.reduce((total, file) => total + file.size, 0);
    if (oversized) {
      setFiles([]);
      clearUploadedCsvs();
      setError(`“${oversized.name}” is larger than the 100 MB per-file limit.`);
      return;
    }
    if (totalBytes > MAX_CSV_BATCH_BYTES) {
      setFiles([]);
      clearUploadedCsvs();
      setError("The selected CSV files exceed the 200 MB batch limit.");
      return;
    }
    clearUploadedCsvs();
    setFiles(selected);
    setUploadProgress(null);
    setError("");
  };

  const uploadCsvs = async (): Promise<UploadedCsv[]> => {
    if (uploadedCsvs?.length === files.length) return uploadedCsvs;
    if (!user?.id) throw new Error("Your session is still loading. Please try again.");

    const totalBytes = files.reduce((total, file) => total + file.size, 0);
    const uploadedBytes = files.map(() => 0);
    const uploads = await Promise.all(files.map(async (file, index) => {
      const blob = await upload(`campaign-imports/${user.id}/${campaignId}/${safeCsvFilename(file.name, index)}`, file, {
        access: "private",
        contentType: "text/csv",
        handleUploadUrl: "/api/campaign-import-upload",
        multipart: true,
        onUploadProgress: ({ loaded }) => {
          uploadedBytes[index] = loaded;
          const progress = totalBytes ? Math.round((uploadedBytes.reduce((total, value) => total + value, 0) / totalBytes) * 100) : 100;
          setUploadProgress(Math.min(100, progress));
        },
      });
      return { url: blob.url, filename: file.name };
    }));
    setUploadedCsvs(uploads);
    setUploadProgress(null);
    return uploads;
  };

  const submit = async (isPreview: boolean) => {
    if (busy || !ready || (!isPreview && !preview)) return;
    setBusy(true);
    setError("");
    try {
      const isBatch = method === "sheet" && activeSheets.length > 1;
      const suffix = method === "csv" ? "csv/blob" : method === "sheet" ? `google-sheet${isBatch ? "/batch" : ""}` : method;
      const options: RequestInit = { method: "POST" };
      if (method === "csv") {
        const uploads = await uploadCsvs();
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify({ files: uploads });
      } else {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(method === "paste"
          ? { raw }
          : isBatch
            ? { sheets: activeSheets.map((source) => ({ url: source.url.trim(), tab_name: source.tabName, header_row: 1, mapping: {} })) }
            : { url: activeSheets[0].url.trim(), tab_name: activeSheets[0].tabName, header_row: 1, mapping: {} });
      }
      const response = await authFetch(`${API_URL}/api/campaigns/${campaignId}/recipients/${isPreview ? "preview/" : ""}${suffix}`, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The import could not be read. Check your data and try again.");
      if (isPreview) {
        setPreview(data as ImportPreview);
      } else {
        const attached = Number(data.attached || 0);
        setComplete(attached);
        if (attached > 0) {
          trackEvent("contacts_imported", {
            contact_count: attached,
            import_source:
              method === "sheet"
                ? activeSheets.length > 1
                  ? "google_sheets_batch"
                  : "google_sheet"
                : method,
          });
        }
        setUploadedCsvs(null);
        try {
          await onImported();
        } catch {
          setError("Contacts were imported, but the audience could not refresh. Reload the page to see the updated list.");
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not import contacts. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) { clearUploadedCsvs(); onClose(); } }}>
      <DialogContent className="campaign-ui campaign-import-dialog">
        <DialogHeader>
          <DialogTitle>{complete !== null ? "Import complete" : preview ? "Review your contacts" : "Import contacts"}</DialogTitle>
          <DialogDescription className="sr-only">Import contacts from pasted rows, a CSV file, or a Google Sheets link. Review before importing. No email will be sent.</DialogDescription>
        </DialogHeader>
        {complete !== null ? (
          <div className="campaign-import-complete">
            <CheckCircle size={32} />
            <h2>{complete > 0 ? `${complete} ${complete === 1 ? "contact added" : "contacts added"}` : "No new contacts to add"}</h2>
            <p>{complete > 0 ? "Your audience is ready to review. No email has been sent." : "These contacts are already in this campaign or have no email address."}</p>
          </div>
        ) : preview ? (
          <div className="campaign-import-review">
            <p><strong>{preview.total_rows} {preview.total_rows === 1 ? "row" : "rows"} found.</strong> Nothing has been imported yet.</p>
            <div className="campaign-import-table" tabIndex={0} aria-label="Contact import preview">
              <table><thead><tr>{preview.columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index}>{preview.columns.map((column) => <td key={column}>{row[column] || "—"}</td>)}</tr>)}</tbody></table>
            </div>
            <p className="campaign-import-hint">{preview.total_rows > preview.rows.length ? `Showing the first ${preview.rows.length} rows. ` : ""}Extra columns are kept as fields for your message.</p>
            <p className="campaign-import-hint">Duplicate emails and rows without an email are skipped.</p>
          </div>
        ) : (
          <Tabs value={method} onValueChange={(value) => { setMethod(value as Method); setError(""); }}>
            <TabsList className="campaign-import-tabs">
              <TabsTrigger value="paste" disabled={busy}>Paste rows</TabsTrigger>
              <TabsTrigger value="csv" disabled={busy}>Upload CSV</TabsTrigger>
              <TabsTrigger value="sheet" disabled={busy}>Google Sheets</TabsTrigger>
            </TabsList>
            <TabsContent value="paste" className="campaign-import-method">
              <p>Copy rows from your spreadsheet, including the header row.</p>
              <HeaderHint />
              <label className="sr-only" htmlFor="import-pasted-rows">Paste contact rows</label>
              <textarea id="import-pasted-rows" value={raw} onChange={(event) => setRaw(event.target.value)} placeholder={"email,skill,region\nalex@example.com,Design,London\nsam@example.com,Engineering,Paris"} spellCheck={false} disabled={busy} />
              <p className="campaign-import-hint">Extra columns become fields you can use in your message.</p>
            </TabsContent>
            <TabsContent value="csv" className="campaign-import-method">
              <p>Choose one or more CSV files exported from your spreadsheet.</p>
              <HeaderHint />
              <label className="campaign-import-upload"><Upload size={28} /><strong>{files.length ? `${files.length} ${files.length === 1 ? "CSV file" : "CSV files"} selected` : "Choose CSV files"}</strong><span>{files.length ? files.map((file) => file.name).join(" · ") : "CSV files up to 100 MB each"}</span><input type="file" accept=".csv,text/csv" multiple aria-label="Choose CSV files" disabled={busy} onChange={(event) => chooseFiles(Array.from(event.target.files || []))} /></label>
              <p className="campaign-import-hint">Up to 100 MB per CSV and 200 MB per batch. Files upload securely before review.</p>
            </TabsContent>
            <TabsContent value="sheet" className="campaign-import-method">
              <p>Paste one or more public Google Sheets links.</p>
              <HeaderHint />
              <div className="campaign-import-sheets">
                {sheetSources.map((source, index) => <SheetSourceField key={source.id} source={source} index={index} busy={busy} API_URL={API_URL} authFetch={authFetch} canRemove={sheetSources.length > 1} onChange={(updated) => setSheetSources((sources) => sources.map((item) => item.id === updated.id ? updated : item))} onRemove={() => setSheetSources((sources) => sources.filter((item) => item.id !== source.id))} />)}
              </div>
              <button type="button" className="campaign-button is-quiet campaign-import-add-source" disabled={busy} onClick={() => setSheetSources((sources) => [...sources, { id: `sheet-${nextSheetId.current++}`, url: "", tabName: "" }])}><Plus size={16} /> Add another sheet</button>
              <p className="campaign-import-hint">If access is restricted, use Upload CSV or Paste rows.</p>
            </TabsContent>
          </Tabs>
        )}
        {error && <p className="campaign-inline-error" role="alert">{error}</p>}
        <div className="campaign-import-footer">
          <p>{complete !== null ? "No email has been sent." : preview ? "No email will be sent." : "You will review the list before importing."}</p>
          <div>
            {complete !== null ? <button className="campaign-button is-primary" onClick={onClose} disabled={busy}>Done</button> : <>
              <button className="campaign-button is-quiet" disabled={busy} onClick={() => { if (preview) { setPreview(null); setError(""); } else { clearUploadedCsvs(); onClose(); } }}>{preview ? <><ArrowLeft size={16} /> Back</> : "Cancel"}</button>
              <button className="campaign-button is-primary" disabled={busy || !ready} onClick={() => void submit(!preview)}>{busy ? <><Loader2 size={17} className="animate-spin" /> {uploadProgress !== null ? `Uploading ${uploadProgress}%…` : preview ? "Importing…" : "Reading…"}</> : preview ? "Import contacts" : <>Preview import <ArrowRight size={17} /></>}</button>
            </>}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function safeCsvFilename(filename: string, index: number) {
  const normalized = filename.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "contacts.csv";
  const withExtension = normalized.toLowerCase().endsWith(".csv") ? normalized : `${normalized}.csv`;
  return `${index + 1}-${withExtension}`;
}

function SheetSourceField({ source, index, busy, API_URL, authFetch, canRemove, onChange, onRemove }: {
  source: SheetSource;
  index: number;
  busy: boolean;
  API_URL: string;
  authFetch: (url: string, options?: RequestInit) => Promise<Response>;
  canRemove: boolean;
  onChange: (source: SheetSource) => void;
  onRemove: () => void;
}) {
  const [sheetTabs, setSheetTabs] = useState<{ url: string; tabs: Array<{ title: string; gid?: string | null }> }>({ url: "", tabs: [] });
  const [loadingUrl, setLoadingUrl] = useState("");
  const [tabsError, setTabsError] = useState<{ url: string; message: string }>({ url: "", message: "" });

  useEffect(() => {
    const url = source.url.trim();
    if (!url) return;
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setLoadingUrl(url);
      try {
        const response = await authFetch(`${API_URL}/api/google-sheets/public-tabs?url=${encodeURIComponent(url)}`, { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error("Could not list the sheet tabs. Preview will try the tab in your link.");
        if (!controller.signal.aborted) setSheetTabs({ url, tabs: Array.isArray(data.tabs) ? data.tabs : [] });
      } catch (err) {
        if (!controller.signal.aborted) setTabsError({ url, message: err instanceof Error ? err.message : "Could not load sheet tabs." });
      } finally {
        if (!controller.signal.aborted) setLoadingUrl("");
      }
    }, 500);
    return () => { window.clearTimeout(timeout); controller.abort(); };
  }, [source.url, API_URL, authFetch]);

  const inputId = `import-sheet-link-${source.id}`;
  const currentUrl = source.url.trim();
  const availableTabs = sheetTabs.url === currentUrl ? sheetTabs.tabs : [];
  const currentTabsError = tabsError.url === currentUrl ? tabsError.message : "";
  return <div className="campaign-import-sheet-source">
    <div className="campaign-import-sheet-heading">
      <label className="campaign-import-field" htmlFor={inputId}>Google Sheets link {index + 1}</label>
      {canRemove && <button type="button" className="campaign-import-remove-source" onClick={onRemove} disabled={busy} aria-label={`Remove Google Sheet ${index + 1}`}><X size={15} /> Remove</button>}
    </div>
    <div className="campaign-import-link"><LinkIcon size={18} /><input id={inputId} type="url" value={source.url} placeholder="https://docs.google.com/spreadsheets/d/…" onChange={(event) => onChange({ ...source, url: event.target.value, tabName: "" })} disabled={busy} /></div>
    {loadingUrl === currentUrl && <p className="campaign-import-hint" role="status">Reading sheet tabs…</p>}
    {availableTabs.length > 1 && <label className="campaign-import-field">Sheet tab<select value={source.tabName} onChange={(event) => onChange({ ...source, tabName: event.target.value })} disabled={busy}><option value="">Tab from your link</option>{availableTabs.map((tab) => <option key={`${tab.title}-${tab.gid || ""}`} value={tab.title}>{tab.title}</option>)}</select></label>}
    {currentTabsError && <p className="campaign-import-hint">{currentTabsError}</p>}
  </div>;
}

function HeaderHint() {
  return <div className="campaign-import-header-hint"><p>The first row must include a column named <strong>email</strong>.</p><p>Other columns are optional. Use any names you need.</p></div>;
}
