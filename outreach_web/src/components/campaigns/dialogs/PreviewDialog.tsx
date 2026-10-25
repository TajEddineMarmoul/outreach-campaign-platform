import { useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ChevronLeft, ChevronRight, Loader2, Paperclip, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { API_URL, useApiClient } from "@/lib/api";
import type { CampaignAttachmentSummary } from "@/components/campaigns/dialogs/AttachmentDialog";
import { emailPreviewDocument } from "@/components/campaigns/workspace/emailPreviewDocument";


interface PreviewEntry {
  id: number;
  recipient_email: string;
  subject: string;
  body: string;
  missing_variables: string[];
  can_send: boolean;
  attachment_name: string;
  attachments: CampaignAttachmentSummary[];
}

interface PreviewResponse {
  items: PreviewEntry[];
  total: number;
  offset: number;
  limit: number;
}

export default function PreviewDialog({
  isOpen,
  onClose,
  campaignId,
}: {
  isOpen: boolean;
  onClose: () => void;
  campaignId: string;
}) {
  const [previewIndex, setPreviewIndex] = useState(0);
  const { data: previews, isLoading, isValidating, error } = useSWR<PreviewResponse>(
    isOpen ? `${API_URL}/api/campaigns/${campaignId}/preview?offset=${previewIndex}&limit=1` : null,
  );
  const { authFetch } = useApiClient();
  
  const [testEmail, setTestEmail] = useState("");
  const [testSending, setTestSending] = useState(false);
  const currentPreview = previews?.items?.[0] || null;
  const previewTotal = previews?.total || 0;
  const missingVariables = currentPreview?.missing_variables || [];
  const sendingBlocked = currentPreview?.can_send !== true || missingVariables.length > 0;

  const [testResult, setTestResult] = useState<{ type: "success" | "error"; message: string } | null>(null);

  const handleClose = () => {
    setPreviewIndex(0);
    setTestResult(null);
    onClose();
  };

  const handleSendTest = async () => {
    if (!testEmail.trim() || !currentPreview || sendingBlocked || testSending || isLoading || isValidating || error) return;
    setTestSending(true);
    setTestResult(null);
    try {
      const res = await authFetch(`${API_URL}/api/campaigns/${campaignId}/test-send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recipient_email: testEmail,
          preview_contact_id: currentPreview?.id,
        }),
      });
      if (!res.ok) {
        const errorData = await res.json().catch(() => null);
        throw new Error(errorData?.detail || "Test send failed");
      }
      setTestResult({ type: "success", message: "Test email sent successfully!" });
    } catch (error) {
      setTestResult({
        type: "error",
        message: error instanceof Error ? error.message : "Test send failed",
      });
    } finally {
      setTestSending(false);
    }
  };

  return (
    <Dialog
      open={isOpen}
      disablePointerDismissal
      onOpenChange={(open) => {
        if (!open) handleClose();
      }}
    >
      <DialogContent showCloseButton={false} className="campaign-ui sm:max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <div className="flex items-center justify-between gap-4">
            <DialogTitle>Message preview</DialogTitle>
            <div className="flex items-center gap-2">
              {previewTotal > 0 && (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    className="h-8 w-8"
                    onClick={() => setPreviewIndex((idx) => Math.max(0, idx - 1))}
                    disabled={previewIndex === 0}
                    title="Previous preview"
                    aria-label="Previous preview"
                  >
                    <ChevronLeft className="w-4 h-4" />
                  </Button>
                  <span className="text-xs font-semibold text-slate-500 min-w-16 text-center">
                    {previewIndex + 1} / {previewTotal}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    className="h-8 w-8"
                    onClick={() => setPreviewIndex((idx) => Math.min(previewTotal - 1, idx + 1))}
                    disabled={previewIndex >= previewTotal - 1}
                    title="Next preview"
                    aria-label="Next preview"
                  >
                    <ChevronRight className="w-4 h-4" />
                  </Button>
                </>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={handleClose}
                title="Close preview"
              >
                <X className="w-4 h-4" />
                <span className="sr-only">Close preview</span>
              </Button>
            </div>
          </div>
          <DialogDescription>Preview only. Opening this window does not send an email.</DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 py-4 min-h-[300px]">
          {isLoading ? (
            <div className="p-12 flex items-center justify-center text-sm text-slate-500">
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
              Loading preview...
            </div>
          ) : error ? (
            <p role="alert" className="text-red-700">Could not load the preview. Close this window and try again.</p>
          ) : !currentPreview ? (
            <div className="p-12 text-center text-slate-500">
              <p>Add at least one recipient to preview this campaign.</p>
            </div>
          ) : (
            <>
              {sendingBlocked && (
                <div className="campaign-notice is-error" role="alert">
                  <strong>{missingVariables.length ? "Missing personalization data — sending blocked." : "Personalization could not be verified — sending blocked."}</strong>
                  {missingVariables.length > 0 && (
                    <p>{missingVariables.map((variable) => `{{${variable}}}`).join(", ")} {missingVariables.length === 1 ? "has" : "have"} no value for {currentPreview.recipient_email}.</p>
                  )}
                  <p>Fill in this recipient’s data or remove the missing fields from your message before sending a test. Incomplete recipients will be skipped when the campaign sends.</p>
                </div>
              )}
            <div className="border border-slate-200 rounded-lg bg-white overflow-hidden">
              <div className="px-4 py-3 border-b border-slate-100 text-xs flex items-center justify-between gap-3">
                <span className="text-slate-500 truncate">
                  To: <strong className="text-slate-800">{currentPreview.recipient_email}</strong>
                </span>
                <span className="text-slate-400 shrink-0">Row {previewIndex + 1}</span>
              </div>
              <div className="px-4 py-3 border-b border-slate-100 text-sm font-semibold text-slate-800">
                Subject: {currentPreview.subject || "(empty subject)"}
              </div>
            <div className="px-4 py-4 text-slate-700 leading-relaxed font-sans text-sm border-b border-slate-100">
              {currentPreview.body ? (
                /<[a-z][\s\S]*>/i.test(currentPreview.body) ? (
                  <iframe title="Test email preview" sandbox="" referrerPolicy="no-referrer" className="w-full min-h-[300px] border-0" srcDoc={emailPreviewDocument(currentPreview.body)} />
                ) : (
                  <div className="whitespace-pre-wrap">{currentPreview.body}</div>
                )
              ) : (
                "(empty body)"
              )}
            </div>
            {currentPreview.attachments?.length > 0 && (
              <div className="px-4 py-3 bg-slate-50 space-y-2">
                {currentPreview.attachments.map((attachment) => (
                  <div key={attachment.id} className="flex items-center gap-2 text-xs font-semibold text-slate-600">
                    <Paperclip className="w-3.5 h-3.5 text-blue-500 shrink-0" />
                    <span className="truncate">{attachment.filename}</span>
                  </div>
                ))}
              </div>
            )}
            </div>
            </>
          )}
        </div>

        <div className="border-t border-slate-200 pt-4 space-y-4">
          <details className="campaign-test-options">
            <summary>Send a test email</summary>
            <p className="text-xs text-slate-500 my-3">This sends a real email only to the address you enter. It does not launch the campaign.</p>
          <form onSubmit={(event) => { event.preventDefault(); void handleSendTest(); }} className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
            <div className="flex flex-col w-full sm:max-w-sm gap-2">
              <div className="flex items-center gap-2">
                <Input
                  type="email"
                  aria-label="Test email address"
                  required
                  placeholder="recipient@domain.com"
                  value={testEmail}
                  onChange={(e) => setTestEmail(e.target.value)}
                  className="text-xs"
                />
                <Button
                  className="bg-slate-800 hover:bg-slate-900 text-white text-xs shrink-0"
                  type="submit"
                  disabled={testSending || !testEmail.trim() || !currentPreview || sendingBlocked || isLoading || isValidating || Boolean(error)}
                >
                  {testSending ? "Sending..." : "Send test"}
                </Button>
              </div>
              {testResult && (
                <div role="status" className={cn("text-[11px] font-medium px-1", testResult.type === "success" ? "text-emerald-600" : "text-red-500")}>
                  {testResult.message}
                </div>
              )}
            </div>
          </form>
          </details>
        </div>
      </DialogContent>
    </Dialog>
  );
}
