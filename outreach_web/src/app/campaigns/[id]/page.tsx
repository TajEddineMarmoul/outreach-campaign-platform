"use client";

import {
  Suspense,
  useState,
  useEffect,
  useRef,
  useMemo,
  useCallback,
} from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR, { mutate } from "swr";
import {
  ArrowLeft,
  ArrowRight,
  Copy,
  Download,
  Loader2,
  MoreVertical,
  Trash2,
  Paperclip,
  CheckCircle,
  CircleX,
  Play,
  Pause,
  Eye,
  Braces,
  X,
  AtSign,
  Plus,
  FileText,
  ChevronDown,
  SlidersHorizontal,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import RichTextEditor from "@/components/RichTextEditor";
import type { Editor } from "@tiptap/react";
import ScheduleDialog from "@/components/campaigns/dialogs/ScheduleDialog";
import AutopilotDailyLimitsDialog from "@/components/campaigns/dialogs/AutopilotDailyLimitsDialog";
import SenderSelectionDialog from "@/components/campaigns/dialogs/SenderSelectionDialog";
import PreviewDialog from "@/components/campaigns/dialogs/PreviewDialog";
import RecipientsImportDialog from "@/components/campaigns/dialogs/RecipientsImportDialog";
import CampaignGuide from "@/components/onboarding/CampaignGuide";
import { useCampaignTips, dismissCampaignTip, hideCampaignTips, type GuideStep } from "@/lib/onboarding";
import AttachmentDialog, {
  attachmentUploadStatusText,
  type AttachmentUpload,
  type CampaignAttachmentSummary,
} from "@/components/campaigns/dialogs/AttachmentDialog";
import LogsSection from "@/components/campaigns/LogsSection";
import ProgressSection from "@/components/campaigns/ProgressSection";
import RecipientsSection from "@/components/campaigns/RecipientsSection";
import { API_URL, responseProblem, useApiClient } from "@/lib/api";
import { trackEvent } from "@/lib/analytics";
import {
  extractTemplateVariables,
  templateVariableName,
} from "@/lib/templateVariables";

import CampaignHeader from "@/components/campaigns/workspace/CampaignHeader";
import CampaignSteps, {
  CAMPAIGN_STEPS,
  type CampaignStep,
} from "@/components/campaigns/workspace/CampaignSteps";
import CampaignOverview, {
  CurrentSchedule,
} from "@/components/campaigns/workspace/CampaignOverview";
import CampaignSchedule from "@/components/campaigns/workspace/CampaignSchedule";
import CampaignReview, {
  type RecipientValidation,
} from "@/components/campaigns/workspace/CampaignReview";
import useScheduleDraft from "@/components/campaigns/workspace/useScheduleDraft";
import {
  launchRequest,
  scheduleProblem,
} from "@/components/campaigns/workspace/scheduleDraft";
import "@/components/campaigns/workspace/campaign-workspace.css";

const EDIT_LOCKED_STATUSES = new Set([
  "sending",
  "scheduled",
  "autopilot",
]);
const REFRESHING_CAMPAIGN_STATUSES = new Set([
  ...EDIT_LOCKED_STATUSES,
  "paused",
]);

interface ComposerDraft {
  subject_template: string;
  body_template: string;
  fallback_body_template: string;
}

type DraftSaveStatus = "idle" | "unsaved" | "saving" | "saved" | "error";

function composerDraftFingerprint(draft: ComposerDraft): string {
  return JSON.stringify(draft);
}

export default function CampaignEditorPage() {
  return (
    <Suspense
      fallback={<div className="campaign-empty">Loading campaign…</div>}
    >
      <CampaignEditor />
    </Suspense>
  );
}

function CampaignEditor() {
  const params = useParams();
  const router = useRouter();
  const campaignId = String(params.id);
  const searchParams = useSearchParams();
  const [notice, setNotice] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [endDialogOpen, setEndDialogOpen] = useState(false);
  const [variablesOpen, setVariablesOpen] = useState(false);
  const [messageToolsOpen, setMessageToolsOpen] = useState(false);
  const [configurationOnly, setConfigurationOnly] = useState(false);
  const { authFetch } = useApiClient();
  const { userId } = useAuth();
  const campaignTips = useCampaignTips(userId);

  // ----------------------------------------------------
  // SWR Hooks for Data Fetching
  // ----------------------------------------------------
  const {
    data: campaign,
    error: campError,
    isLoading: campLoading,
  } = useSWR(campaignId ? `${API_URL}/api/campaigns/${campaignId}` : null, {
    refreshInterval: (latest) =>
      REFRESHING_CAMPAIGN_STATUSES.has(latest?.status) ? 3000 : 0,
  });

  const {
    data: summary,
    error: summaryError,
    mutate: mutateSummary,
  } = useSWR(
    campaignId ? `${API_URL}/api/campaigns/${campaignId}/summary` : null,
  );

  const { data: valSummary, mutate: mutateValSummary } = useSWR(
    campaignId
      ? `${API_URL}/api/campaigns/${campaignId}/validation-summary`
      : null,
  );

  const { data: senderGroups, mutate: mutateSenderGroups } = useSWR(
    `${API_URL}/api/sender-groups`,
  );
  const {
    data: engagementTracking,
    isLoading: engagementTrackingLoading,
    mutate: mutateEngagementTracking,
  } = useSWR<{ enabled: boolean; configured: boolean }>(
    summary?.sender_group_id
      ? `${API_URL}/api/campaigns/${campaignId}/engagement-tracking`
      : null,
  );

  const selectedSenderGroup = useMemo(() => {
    if (!senderGroups || !summary) return null;
    return (
      senderGroups.find(
        (group: { id: number; name: string }) =>
          group.id === summary.sender_group_id,
      ) ||
      senderGroups.find(
        (group: { id: number; name: string }) => group.name === summary.sender,
      ) ||
      null
    );
  }, [senderGroups, summary]);

  const senderEmails: string[] =
    selectedSenderGroup?.senders
      ?.filter((sender: { status: string }) => sender.status === "connected")
      .map((sender: { email: string }) => sender.email) ??
    summary?.sender_emails ??
    [];
  const campaignAttachments: CampaignAttachmentSummary[] = Array.isArray(
    summary?.attachments,
  )
    ? summary.attachments
    : [];
  const editingLocked = Boolean(
    campaign && EDIT_LOCKED_STATUSES.has(campaign.status),
  );
  const isPaused = campaign?.status === "paused";
  const isAutopilotRunning = campaign?.status === "autopilot";
  const hasActiveDeliveryControls = editingLocked || isPaused;
  const schedule = useScheduleDraft(campaignId, summary, editingLocked);
  const launchingRef = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // ----------------------------------------------------
  // UI & Form States
  // ----------------------------------------------------
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [fallback, setFallback] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [draftSaveStatus, setDraftSaveStatus] =
    useState<DraftSaveStatus>("idle");

  const hydratedCampaignIdRef = useRef<string | null>(null);
  const lastSavedDraftFingerprintRef = useRef("");
  const lastQueuedDraftFingerprintRef = useRef("");
  const currentDraftRef = useRef<ComposerDraft>({
    subject_template: "",
    body_template: "",
    fallback_body_template: "",
  });
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const latestSavePromiseRef = useRef<Promise<void>>(Promise.resolve());
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const composerDraft = useMemo<ComposerDraft>(
    () => ({
      subject_template: subject,
      body_template: body,
      fallback_body_template: fallback,
    }),
    [body, fallback, subject],
  );
  const composerDraftKey = useMemo(
    () => composerDraftFingerprint(composerDraft),
    [composerDraft],
  );

  useEffect(() => {
    currentDraftRef.current = composerDraft;
  }, [composerDraft]);

  const activeVariables = useMemo(() => {
    const columns: unknown[] = Array.isArray(valSummary?.all_columns)
      ? valSummary.all_columns
      : [];
    return [
      ...new Set(columns.map((column) => templateVariableName(String(column)))),
    ].sort();
  }, [valSummary]);

  const unknownVariables = useMemo(() => {
    const validVariables = new Set(activeVariables);
    const usedVariables = [
      ...extractTemplateVariables(subject),
      ...extractTemplateVariables(body),
    ];

    return [
      ...new Set(
        usedVariables.filter((variable) => !validVariables.has(variable)),
      ),
    ].sort();
  }, [activeVariables, body, subject]);

  // Hydrate once per campaign. Later SWR refreshes must not replace active edits.
  useEffect(() => {
    if (!campaign) return;
    const campaignKey = String(campaign.id ?? campaignId);
    if (hydratedCampaignIdRef.current === campaignKey) return;

    const serverDraft: ComposerDraft = {
      subject_template: campaign.subject_template || "",
      body_template: campaign.body_template || "",
      fallback_body_template: campaign.fallback_body_template || "",
    };
    const serverFingerprint = composerDraftFingerprint(serverDraft);
    hydratedCampaignIdRef.current = campaignKey;
    lastSavedDraftFingerprintRef.current = serverFingerprint;
    lastQueuedDraftFingerprintRef.current = serverFingerprint;
    currentDraftRef.current = serverDraft;
    setName(campaign.name || "");
    setSubject(serverDraft.subject_template);
    setBody(serverDraft.body_template);
    setFallback(serverDraft.fallback_body_template);
    setDraftSaveStatus("saved");
  }, [campaign, campaignId]);

  const persistComposerDraft = useCallback(
    async (draft: ComposerDraft) => {
      const targetFingerprint = composerDraftFingerprint(draft);
      setDraftSaveStatus("saving");
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/composer`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(draft),
        },
      );
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.detail || "Failed to save draft");
      }

      // Returning through client-side navigation must hydrate the saved draft,
      // not the campaign snapshot cached before the user started writing.
      await mutate(
        `${API_URL}/api/campaigns/${campaignId}`,
        (cached: Record<string, unknown> | undefined) =>
          cached ? { ...cached, ...draft } : cached,
        { revalidate: false },
      );
      lastSavedDraftFingerprintRef.current = targetFingerprint;
      const currentFingerprint = composerDraftFingerprint(
        currentDraftRef.current,
      );
      setDraftSaveStatus(
        currentFingerprint === targetFingerprint &&
          lastQueuedDraftFingerprintRef.current === targetFingerprint
          ? "saved"
          : "unsaved",
      );
    },
    [authFetch, campaignId],
  );

  const queueComposerDraftSave = useCallback(
    (draft: ComposerDraft): Promise<void> => {
      const targetFingerprint = composerDraftFingerprint(draft);
      if (targetFingerprint === lastQueuedDraftFingerprintRef.current) {
        return latestSavePromiseRef.current;
      }

      lastQueuedDraftFingerprintRef.current = targetFingerprint;
      const operation = saveQueueRef.current.then(() =>
        persistComposerDraft(draft),
      );
      latestSavePromiseRef.current = operation;
      saveQueueRef.current = operation.catch(() => undefined);
      operation.catch(() => {
        if (lastQueuedDraftFingerprintRef.current === targetFingerprint) {
          lastQueuedDraftFingerprintRef.current =
            lastSavedDraftFingerprintRef.current;
          setDraftSaveStatus("error");
        }
      });
      return operation;
    },
    [persistComposerDraft],
  );

  const flushComposerDraft = useCallback(async () => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    await queueComposerDraftSave(currentDraftRef.current);
  }, [queueComposerDraftSave]);

  useEffect(() => {
    const campaignKey = campaign ? String(campaign.id ?? campaignId) : null;
    if (
      !campaignKey ||
      hydratedCampaignIdRef.current !== campaignKey ||
      editingLocked
    )
      return;

    const draft = currentDraftRef.current;
    const fingerprint = composerDraftFingerprint(draft);
    if (fingerprint === lastQueuedDraftFingerprintRef.current) return;

    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      void queueComposerDraftSave(draft).catch(() => undefined);
    }, 600);
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, [
    campaign,
    campaignId,
    composerDraftKey,
    editingLocked,
    queueComposerDraftSave,
  ]);

  useEffect(() => {
    const saveWhenHidden = () => {
      if (document.visibilityState === "hidden" && !editingLocked) {
        void flushComposerDraft().catch(() => undefined);
      }
    };
    document.addEventListener("visibilitychange", saveWhenHidden);
    return () =>
      document.removeEventListener("visibilitychange", saveWhenHidden);
  }, [editingLocked, flushComposerDraft]);

  const updateSubjectDraft = useCallback((value: string) => {
    currentDraftRef.current = {
      ...currentDraftRef.current,
      subject_template: value,
    };
    setSubject(value);
    setDraftSaveStatus("unsaved");
  }, []);

  const updateBodyDraft = useCallback((value: string) => {
    currentDraftRef.current = {
      ...currentDraftRef.current,
      body_template: value,
    };
    setBody(value);
    setDraftSaveStatus("unsaved");
  }, []);

  // Modal open states
  const [sendModalOpen, setSendModalOpen] = useState(false);
  const [sendTab, setSendTab] = useState("send-now");
  const [senderModalOpen, setSenderModalOpen] = useState(false);
  const [recipientsModalOpen, setRecipientsModalOpen] = useState(false);
  const [previewModalOpen, setPreviewModalOpen] = useState(false);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [attachmentModalOpen, setAttachmentModalOpen] = useState(false);
  const [autopilotLimitsOpen, setAutopilotLimitsOpen] = useState(false);
  const [attachmentUploads, setAttachmentUploads] = useState<
    AttachmentUpload[]
  >([]);
  const [deletingAttachmentId, setDeletingAttachmentId] = useState<
    number | null
  >(null);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const [trackingBusy, setTrackingBusy] = useState(false);

  const handleAttachmentUploadChange = useCallback(
    (uploads: AttachmentUpload[]) => setAttachmentUploads(uploads),
    [],
  );
  const attachmentUploadInProgress = attachmentUploads.some(
    (upload) => upload.status === "queued" || upload.status === "uploading",
  );

  // ----------------------------------------------------
  // TipTap Editor Ref for Variable Insertion
  // ----------------------------------------------------
  const tiptapEditorRef = useRef<Editor | null>(null);

  const handleEditorReady = useCallback((editor: Editor | null) => {
    tiptapEditorRef.current = editor;
  }, []);

  const insertVariable = (variable: string) => {
    const editor = tiptapEditorRef.current;
    const placeholder = `{{ ${variable} }}`;
    if (!editor || editor.isDestroyed || typeof editor.chain !== "function") {
      updateBodyDraft(
        `${currentDraftRef.current.body_template || ""}${placeholder}`,
      );
      return;
    }
    editor.chain().focus().insertContent(placeholder).run();
  };

  // ----------------------------------------------------
  // Save & Actions Handlers
  // ----------------------------------------------------
  const handleSave = async () => {
    setIsSaving(true);
    try {
      await flushComposerDraft();
      await Promise.all([
        mutate(`${API_URL}/api/campaigns/${campaignId}`),
        mutateSummary(),
        mutateValSummary(),
      ]);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to save draft");
    } finally {
      setIsSaving(false);
    }
  };

  const handleOpenPreview = async () => {
    setIsPreviewLoading(true);
    try {
      await flushComposerDraft();

      await mutate(
        (key) =>
          typeof key === "string" &&
          key.startsWith(`${API_URL}/api/campaigns/${campaignId}/preview?`),
        undefined,
        { revalidate: false },
      );
      setPreviewModalOpen(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      alert("Failed to load preview: " + message);
    } finally {
      setIsPreviewLoading(false);
    }
  };

  const handleRemoveAttachment = async (attachmentId: number) => {
    setDeletingAttachmentId(attachmentId);
    try {
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/attachments/${attachmentId}`,
        { method: "DELETE" },
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(result.detail || "Failed to remove attachment");
      await mutateSummary();
    } catch (error) {
      alert(
        error instanceof Error ? error.message : "Failed to remove attachment",
      );
    } finally {
      setDeletingAttachmentId(null);
    }
  };

  const handleUpdateName = async (newName: string) => {
    const trimmed = newName.trim();
    if (!trimmed) {
      setNotice("Campaign name cannot be empty.");
      return;
    }
    if (trimmed === campaign?.name || editingLocked) return;
    try {
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: trimmed }),
        },
      );
      if (!response.ok)
        throw new Error("Could not save the campaign name. Please try again.");
      setName(trimmed);
      await mutate(`${API_URL}/api/campaigns/${campaignId}`);
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "Could not save campaign name.",
      );
    }
  };
  const handleDeleteCampaign = async () => {
    if (
      !confirm(
        "Delete this campaign and its delivery history? This cannot be undone.",
      )
    )
      return;
    try {
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error("Could not delete the campaign.");
      router.push("/campaigns");
    } catch {
      setNotice("Could not delete the campaign. Please try again.");
    }
  };

  const handleSelectSenderGroup = async (senderGroupId: number) => {
    try {
      const res = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/sender-group`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sender_group_id: senderGroupId }),
        },
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to update sender group");
      }
      mutateSummary();
      mutateSenderGroups();
    } catch (err: unknown) {
      alert(
        err instanceof Error
          ? err.message
          : "Request failed. Please try again.",
      );
    }
  };

  const refreshCampaign = async () => {
    await Promise.all([
      mutate(`${API_URL}/api/campaigns/${campaignId}`),
      mutateSummary(),
      mutateValSummary(),
      mutate(
        (key) =>
          typeof key === "string" &&
          (key.includes(`/api/campaigns/${campaignId}/send-`) ||
            key.includes(`/api/campaigns/${campaignId}/recipients?`)),
      ),
    ]);
  };
  const handleCampaignAction = async (action: string) => {
    setBusyAction(action);
    setNotice("");
    try {
      const res = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/${action}`,
        { method: "POST" },
      );
      const result = await res.json().catch(() => ({}));
      if (!res.ok)
        throw new Error(
          typeof result.detail === "string"
            ? result.detail
            : `Could not ${action} campaign.`,
        );
      if (action === "duplicate") {
        router.push(`/campaigns/${result.id}?step=message`);
        return;
      }
      await refreshCampaign();
      setEndDialogOpen(false);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Could not update campaign.",
      );
    } finally {
      setBusyAction(null);
    }
  };
  const navigateSafely = async (href: string) => {
    if (launchingRef.current) return;
    setIsSaving(true);
    setNotice("");
    try {
      if (!editingLocked) {
        await flushComposerDraft();
        await schedule.flush(activeStep === "schedule");
        const nextName = name.trim();
        if (!nextName)
          throw new Error("Give your campaign a name before leaving.");
        if (nextName !== campaign?.name) {
          const response = await authFetch(
            `${API_URL}/api/campaigns/${campaignId}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: nextName }),
            },
          );
          if (!response.ok)
            throw new Error(
              "Could not save the campaign name. Please try again.",
            );
        }
      }
      router.push(href);
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "Could not save your changes. Please retry.",
      );
    } finally {
      setIsSaving(false);
    }
  };
  const exportReport = async () => {
    setBusyAction("export");
    setNotice("");
    try {
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/logs/export`,
      );
      if (!response.ok)
        throw new Error("Could not export the report. Please try again.");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `campaign_${campaignId}_report.csv`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Export failed.");
    } finally {
      setBusyAction(null);
    }
  };
  const requestedStep = searchParams.get("step");
  const activeStep: CampaignStep = CAMPAIGN_STEPS.includes(
    requestedStep as CampaignStep,
  )
    ? (requestedStep as CampaignStep)
    : summary?.recipients
      ? "message"
      : "audience";
  const isOperational =
    campaign?.status !== "draft" && (editingLocked || !requestedStep);
  const tab = [
    "overview",
    "audience",
    "message",
    "schedule",
    "activity",
  ].includes(searchParams.get("tab") || "")
    ? searchParams.get("tab")!
    : "overview";
  const audienceReady = Number(summary?.recipients || 0) > 0;
  const messageReady = Boolean(
    subject.trim() &&
    body.replace(/<[^>]*>/g, "").trim() &&
    unknownVariables.length === 0,
  );
  const sendersReady = senderEmails.length > 0;
  const scheduleReady = schedule.configured && !schedule.problem;
  const {
    data: recipientValidation,
    error: recipientValidationError,
    isValidating: checkingRecipients,
    mutate: recheckRecipients,
  } = useSWR<RecipientValidation>(
    !isOperational && activeStep === "review"
      ? `${API_URL}/api/campaigns/${campaignId}/recipient-template-validation`
      : null,
    { revalidateOnFocus: true },
  );
  const launchReady = Boolean(
    audienceReady &&
    messageReady &&
    sendersReady &&
    scheduleReady &&
    recipientValidation?.ready_recipient_count &&
    !recipientValidationError &&
    !checkingRecipients &&
    !["unsaved", "saving", "error"].includes(draftSaveStatus) &&
    schedule.status === "saved",
  );
  const stepComplete = [
    audienceReady,
    messageReady,
    sendersReady,
    scheduleReady,
    false,
  ];
  const changeStep = (step: CampaignStep) => {
    void navigateSafely(`/campaigns/${campaignId}?step=${step}`);
  };
  const handleTrackingChange = async (enabled: boolean) => {
    setTrackingBusy(true);
    setNotice("");
    try {
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/engagement-tracking`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        },
      );
      if (!response.ok) {
        throw new Error(
          responseProblem(
            await response.json().catch(() => ({})),
            "Could not update engagement tracking.",
          ),
        );
      }
      await mutateEngagementTracking(await response.json(), {
        revalidate: false,
      });
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "Could not update engagement tracking.",
      );
    } finally {
      setTrackingBusy(false);
    }
  };
  const hideTips = () => {
    if (userId) hideCampaignTips(userId);
  };
  const dismissTip = (step: GuideStep) => {
    if (userId) dismissCampaignTip(userId, step);
  };
  const guideFor = (step: GuideStep) => !isOperational && campaignTips.includes(step) && activeStep === step
    ? <CampaignGuide step={step} onDismiss={() => dismissTip(step)} onHideTips={hideTips} />
    : null;
  const changeTab = (value: string) => {
    void navigateSafely(`/campaigns/${campaignId}?tab=${value}`);
  };
  const stepIndex = CAMPAIGN_STEPS.indexOf(activeStep);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [activeStep, tab]);
  const openSchedule = async (configure: boolean) => {
    setNotice("");
    try {
      await flushComposerDraft();
      await mutateSummary();
      setConfigurationOnly(configure);
      setSendTab(
        summary?.send_settings?.mode === "autopilot"
          ? "autopilot"
          : summary?.send_settings?.mode === "schedule"
            ? "schedule"
            : "send-now",
      );
      setSendModalOpen(true);
    } catch {
      setNotice(
        "Save your message before opening the schedule. Please retry saving.",
      );
    }
  };
  const launchCampaign = async () => {
    if (launchingRef.current || !launchReady || !schedule.draft) return;
    launchingRef.current = true;
    setBusyAction("launch");
    setNotice("");
    try {
      await flushComposerDraft();
      await schedule.flush(true);
      if (!name.trim())
        throw new Error("Give your campaign a name before launching.");
      if (name.trim() !== campaign?.name) {
        const nameResponse = await authFetch(
          `${API_URL}/api/campaigns/${campaignId}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim() }),
          },
        );
        if (!nameResponse.ok)
          throw new Error(
            "Could not save the campaign name. Please retry before launching.",
          );
      }
      const problem = scheduleProblem(schedule.draft);
      if (problem) throw new Error(problem);
      const validationResponse = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/recipient-template-validation`,
      );
      if (!validationResponse.ok)
        throw new Error(
          "Could not verify recipients. Please retry before launching.",
        );
      const validation: RecipientValidation = await validationResponse.json();
      if (!validation.ready_recipient_count)
        throw new Error(
          "No recipients are ready. Review your audience before launching.",
        );
      const request = launchRequest(schedule.draft);
      const response = await authFetch(
        `${API_URL}/api/campaigns/${campaignId}/${request.endpoint}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request.body),
        },
      );
      if (!response.ok)
        throw new Error(
          responseProblem(
            await response.json().catch(() => ({})),
            "The campaign could not launch. Please try again.",
          ),
        );
      if (!schedule.draft.dryRun) {
        trackEvent("campaign_launched", {
          delivery_mode: schedule.draft.mode,
        });
      }
      // An accepted launch must not look failed if the subsequent refresh is unavailable.
      await mutate(
        `${API_URL}/api/campaigns/${campaignId}`,
        {
          ...campaign,
          status:
            schedule.draft.mode === "autopilot"
              ? "autopilot"
              : schedule.draft.mode === "schedule"
                ? "scheduled"
                : "sending",
        },
        { revalidate: false },
      );
      router.push(`/campaigns/${campaignId}?tab=overview`);
      void Promise.allSettled([
        mutate(`${API_URL}/api/campaigns/${campaignId}`),
        mutateSummary(),
      ]);
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "The campaign could not launch.",
      );
    } finally {
      launchingRef.current = false;
      setBusyAction(null);
    }
  };
  const continueStep = async () => {
    if (stepIndex === 3) {
      setIsSaving(true);
      setNotice("");
      try {
        await schedule.flush(true);
        await mutateSummary();
        await navigateSafely(`/campaigns/${campaignId}?step=review`);
      } catch (error) {
        setNotice(
          error instanceof Error
            ? error.message
            : "Could not save your schedule.",
        );
      } finally {
        setIsSaving(false);
      }
      return;
    }
    const issues = [
      "Add at least one recipient to continue.",
      "Add a subject and message, and fix any unknown personalization fields.",
      "Choose a group with at least one connected sender.",
      "Choose and save your sending schedule to continue.",
    ];
    if (stepIndex < 4 && !stepComplete[stepIndex]) {
      setNotice(issues[stepIndex]);
      return;
    }
    if (stepIndex === 4) {
      await launchCampaign();
      return;
    }
    changeStep(CAMPAIGN_STEPS[stepIndex + 1]);
  };
  useEffect(() => {
    const warnBeforeLeaving = (event: BeforeUnloadEvent) => {
      if (
        !editingLocked &&
        (["unsaved", "saving", "error"].includes(draftSaveStatus) ||
          schedule.status !== "saved" ||
          name !== campaign?.name)
      ) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warnBeforeLeaving);
    return () => window.removeEventListener("beforeunload", warnBeforeLeaving);
  }, [draftSaveStatus, editingLocked, name, campaign?.name, schedule.status]);

  if (campLoading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-slate-400 gap-2 h-screen">
        <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
        <span className="text-sm">Loading campaign details...</span>
      </div>
    );
  }

  if (campError || !campaign) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-red-500 font-medium h-screen gap-4">
        <span>Failed to load campaign. Ensure backend is running.</span>
        <Button onClick={() => router.push("/campaigns")} variant="outline">
          Back to campaigns
        </Button>
      </div>
    );
  }

  const saveLabel = isPaused
    ? draftSaveStatus === "error" || schedule.status === "error"
      ? "Changes not saved"
      : draftSaveStatus === "unsaved" || schedule.status === "unsaved"
        ? "Unsaved changes"
        : draftSaveStatus === "saving" || schedule.status === "saving" || isSaving
          ? "Saving changes…"
          : "Changes saved"
    : isOperational
      ? campaign.status === "scheduled"
        ? "Scheduled"
        : ["ended", "stopped"].includes(campaign.status)
          ? "History preserved"
          : "Live campaign"
    : draftSaveStatus === "error" || schedule.status === "error"
      ? "Save failed"
      : draftSaveStatus === "unsaved" || schedule.status === "unsaved"
        ? "Unsaved changes"
        : draftSaveStatus === "saving" ||
            schedule.status === "saving" ||
            isSaving
          ? "Saving…"
          : "Saved just now";
  const messagePanel = (
    <section className="campaign-composer" aria-label="Write your email">
      <div className="campaign-composer-actions">
        <Popover open={messageToolsOpen} onOpenChange={setMessageToolsOpen}>
          <PopoverTrigger className="campaign-button is-quiet" disabled={editingLocked}>
            <Plus size={17} /> Add to message <ChevronDown size={15} />
          </PopoverTrigger>
          <PopoverContent align="start" className="campaign-ui campaign-more-menu">
            <button onClick={() => { setMessageToolsOpen(false); setAttachmentModalOpen(true); }}>
              <Paperclip size={18} /> Attach a file
            </button>
            <button onClick={() => { setMessageToolsOpen(false); setVariablesOpen(true); }}>
              <Braces size={18} /> Personalize for each recipient
            </button>
            <button onClick={() => { setMessageToolsOpen(false); setTemplateModalOpen(true); }}>
              <FileText size={18} /> Use a template
            </button>
          </PopoverContent>
        </Popover>
        <div className="campaign-guide-anchor">
          <button className="campaign-button is-outline" onClick={() => void handleOpenPreview()} disabled={isPreviewLoading}>
            <Eye size={17} /> {isPreviewLoading ? "Opening…" : isOperational ? "Preview message" : "Preview draft"}
          </button>
          {!previewModalOpen && !messageToolsOpen && !variablesOpen && !attachmentModalOpen && !templateModalOpen && guideFor("message")}
        </div>
      </div>
      <label className="campaign-subject-label" htmlFor="campaign-subject">Subject</label>
      <input
        id="campaign-subject"
        className="campaign-subject"
        placeholder="A clear reason to open your email"
        value={subject}
        onChange={(event) => updateSubjectDraft(event.target.value)}
        onBlur={() => void flushComposerDraft().catch(() => undefined)}
        disabled={editingLocked}
      />
      <div className="campaign-editor-frame">
        <RichTextEditor
          content={body}
          onChange={updateBodyDraft}
          onBlur={() => void flushComposerDraft().catch(() => undefined)}
          placeholder="Write your email here…"
          validVariables={activeVariables}
          onEditorReady={handleEditorReady}
          readOnly={editingLocked}
          minimalToolbar
        />
      </div>
      {attachmentUploads.length > 0 && (
        <section
          className="campaign-attachment-uploads"
          aria-label="Attachment upload status"
          aria-live="polite"
        >
          <div className="campaign-attachment-uploads-heading">
            <span>
              {attachmentUploadInProgress
                ? "Uploading attachments"
                : "Attachment upload results"}
            </span>
            {!attachmentUploadInProgress && (
              <button
                type="button"
                className="campaign-attachment-uploads-clear"
                onClick={() => setAttachmentUploads([])}
              >
                Clear status
              </button>
            )}
          </div>
          {attachmentUploads.map((upload) => (
            <div
              key={upload.id}
              className="campaign-attachment-upload"
              data-status={upload.status}
            >
              {upload.status === "uploaded" ? (
                <CheckCircle size={18} aria-hidden="true" />
              ) : upload.status === "error" ? (
                <CircleX size={18} aria-hidden="true" />
              ) : upload.status === "uploading" ? (
                <Loader2 size={18} className="animate-spin" aria-hidden="true" />
              ) : (
                <Paperclip size={18} aria-hidden="true" />
              )}
              <div className="campaign-attachment-upload-details">
                <div className="campaign-attachment-upload-name">{upload.filename}</div>
                <div
                  className="campaign-attachment-upload-state"
                  role={upload.status === "error" ? "alert" : undefined}
                >
                  {attachmentUploadStatusText(upload)}
                </div>
                {upload.status === "uploading" && (
                  <progress
                    value={upload.progress}
                    max="100"
                    aria-label={`${upload.filename} upload progress`}
                  />
                )}
                {upload.status === "error" && upload.error && (
                  <p className="campaign-attachment-upload-error">{upload.error}</p>
                )}
              </div>
              {upload.status === "error" && (
                <button
                  type="button"
                  className="campaign-attachment-upload-retry"
                  onClick={() => setAttachmentModalOpen(true)}
                >
                  Review & retry
                </button>
              )}
            </div>
          ))}
        </section>
      )}
      {campaignAttachments.length > 0 && (
        <div className="campaign-attachments" aria-label="Attached files">
          {campaignAttachments.map((attachment) => (
            <div key={attachment.id} className="campaign-attachment">
              <Paperclip size={14} />
              <span>{attachment.filename}</span>
              <button
                aria-label={`Remove ${attachment.filename}`}
                disabled={editingLocked || deletingAttachmentId === attachment.id}
                onClick={() => handleRemoveAttachment(attachment.id)}
              >
                {deletingAttachmentId === attachment.id ? <Loader2 size={14} className="animate-spin" /> : <X size={14} />}
              </button>
            </div>
          ))}
        </div>
      )}
      {unknownVariables.length > 0 && (
        <div className="campaign-notice is-error" role="alert">
          Unknown fields: {unknownVariables.map((variable) => `{{${variable}}}`).join(", ")}.
          <button onClick={() => setVariablesOpen(true)}>Choose a field from your audience</button>
        </div>
      )}
      <Dialog open={variablesOpen} onOpenChange={setVariablesOpen}>
        <DialogContent className="campaign-ui sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Personalize your message</DialogTitle>
            <DialogDescription>Insert a field such as a first name. It will be filled in for each recipient when the email sends.</DialogDescription>
          </DialogHeader>
          <div className="campaign-variable-list">
            {activeVariables.length ? activeVariables.map((variable) => (
              <button key={variable} className="campaign-text-button" disabled={editingLocked} onClick={() => { insertVariable(variable); setVariablesOpen(false); }}>
                {`{{${variable}}}`}
              </button>
            )) : <p>Add your audience first to use its fields.</p>}
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
  const moreMenu = (
    <Popover>
      <PopoverTrigger
        className="campaign-button"
        disabled={Boolean(busyAction)}
      >
        <MoreVertical size={20} />
        {busyAction === "duplicate"
          ? "Duplicating…"
          : busyAction === "export"
            ? "Exporting…"
            : "More"}
      </PopoverTrigger>
      <PopoverContent align="end" className="campaign-ui campaign-more-menu">
        <button
          disabled={Boolean(busyAction)}
          onClick={() => void handleCampaignAction("duplicate")}
        >
          <Copy size={19} />
          Duplicate campaign
        </button>
        <button
          disabled={Boolean(busyAction)}
          onClick={() => void exportReport()}
        >
          <Download size={19} />
          Export report
        </button>
        {hasActiveDeliveryControls && (
          <>
            <hr />
            <button className="is-red" onClick={() => setEndDialogOpen(true)}>
              <Trash2 size={19} />
              End campaign
            </button>
          </>
        )}
        {!hasActiveDeliveryControls && (
          <>
            <hr />
            <button className="is-red" onClick={handleDeleteCampaign}>
              <Trash2 size={19} />
              Delete campaign
            </button>
          </>
        )}
      </PopoverContent>
    </Popover>
  );

  return (
    <div className="campaign-ui campaign-workspace">
      <CampaignHeader
        name={name}
        status={campaign.status}
        saveLabel={saveLabel}
        readOnly={editingLocked}
        onNameChange={setName}
        onNameSave={handleUpdateName}
        onNavigate={navigateSafely}
      />
      {isOperational ? (
        <nav className="campaign-tabs" aria-label="Campaign sections">
          {["overview", "audience", "message", "schedule", "activity"].map(
            (value) => (
              <button
                key={value}
                aria-current={tab === value ? "page" : undefined}
                onClick={() => changeTab(value)}
              >
                {value.charAt(0).toUpperCase() + value.slice(1)}
              </button>
            ),
          )}
        </nav>
      ) : null}
      <div
        className={`campaign-content ${!isOperational ? `is-builder is-${activeStep}-step` : ""}`}
      >
        {summaryError && (
          <div className="campaign-notice is-error" role="alert">
            Could not load the campaign settings. Your changes are still here.
            <button onClick={() => void mutateSummary()}>Retry loading</button>
          </div>
        )}
        {notice && (
          <div className="campaign-notice is-error" role="alert">
            {notice}
            <button onClick={() => setNotice("")} aria-label="Dismiss message">
              Dismiss
            </button>
          </div>
        )}
        {draftSaveStatus === "error" && (
          <div className="campaign-notice is-error" role="alert">
            Your latest message changes haven’t been saved.
            <button onClick={handleSave} disabled={isSaving}>
              Retry saving
            </button>
          </div>
        )}
        {isOperational ? (
          <>
            <div className="campaign-page-heading">
              <div>
                <h1 ref={headingRef} tabIndex={-1}>
                  {tab === "overview"
                    ? "Campaign overview"
                    : tab === "message"
                      ? "Campaign message"
                      : tab === "audience"
                        ? "Campaign audience"
                        : tab === "schedule"
                          ? "Campaign schedule"
                          : "Campaign activity"}
                </h1>
                <p>
                  {tab === "overview"
                    ? isPaused
                      ? "Sending is paused. You can edit the message, audience, and schedule before resuming."
                      : campaign.status === "ended"
                        ? "Delivery is finished. Every recipient has a final result."
                        : campaign.status === "stopped"
                          ? "Sending is stopped. Your results and activity are saved."
                        : "New emails send according to your campaign schedule."
                    : isPaused
                      ? "Sending is paused. Changes save now and apply when you resume."
                    : isAutopilotRunning
                      ? "Daily limits can be updated while Autopilot runs. Pause to change your message, audience, senders, or schedule."
                    : editingLocked
                      ? "End the campaign before changing its message, audience, or schedule."
                      : "Review and manage this campaign."}
                </p>
              </div>
              <div className="campaign-overview-actions">
                {hasActiveDeliveryControls && (
                  <>
                    <p>
                      {isPaused ? (
                        <>
                          Sending is paused. Changes are safe to make now.
                          <br />
                          Resume when you&apos;re ready to continue.
                        </>
                      ) : (
                        <>
                          Pausing prevents future sends.
                          <br />
                          Emails already sending may finish.
                        </>
                      )}
                    </p>
                    <button
                      className="campaign-button is-outline"
                      disabled={Boolean(busyAction)}
                      onClick={() =>
                        void handleCampaignAction(
                          campaign.status === "paused" ? "resume" : "pause",
                        )
                      }
                    >
                      {busyAction === "pause" || busyAction === "resume" ? (
                        <Loader2 size={19} className="animate-spin" />
                      ) : campaign.status === "paused" ? (
                        <Play size={19} />
                      ) : (
                        <Pause size={19} />
                      )}
                      {campaign.status === "paused"
                        ? "Resume sending"
                        : "Pause sending"}
                    </button>
                  </>
                )}
                {!editingLocked && (
                  <button
                    className="campaign-button is-outline"
                    onClick={() => changeStep("message")}
                  >
                    Edit campaign
                  </button>
                )}
                {moreMenu}
              </div>
            </div>
            {tab === "overview" && (
              <CampaignOverview
                campaignId={campaignId}
                summary={summary}
                onActivity={() => changeTab("activity")}
                onEditDailyLimits={
                  isAutopilotRunning
                    ? () => setAutopilotLimitsOpen(true)
                    : undefined
                }
              />
            )}
            {tab === "message" && messagePanel}
            {tab === "audience" && (
              <RecipientsSection
                campaignId={campaignId}
                onOpenImport={() => setRecipientsModalOpen(true)}
                readOnly={editingLocked}
                onAudienceChange={async () => { await Promise.all([mutateSummary(), mutateValSummary(), recheckRecipients()]); }}
              />
            )}
            {tab === "schedule" && (
              <div className="campaign-step-panel">
                <CurrentSchedule summary={summary} />
                {isAutopilotRunning && (
                  <button
                    className="campaign-button is-primary"
                    onClick={() => setAutopilotLimitsOpen(true)}
                  >
                    <SlidersHorizontal size={17} /> Edit daily limits
                  </button>
                )}
                <button
                  className="campaign-button is-outline"
                  onClick={() => void openSchedule(true)}
                >
                  View schedule settings
                </button>
                <div className="mt-6">
                  <ProgressSection campaignId={campaignId} />
                </div>
              </div>
            )}
            {tab === "activity" && <LogsSection campaignId={campaignId} />}
          </>
        ) : (
          <>
            <div className="campaign-page-heading">
              <div>
                <h1 ref={headingRef} tabIndex={-1}>
                  {activeStep === "message"
                    ? "Write your message"
                    : activeStep === "audience"
                      ? "Who would you like to email?"
                      : activeStep === "senders"
                        ? "Choose your senders"
                        : activeStep === "schedule"
                          ? "Schedule your campaign"
                          : "Review and launch"}
                </h1>
                <p>
                  {activeStep === "message"
                    ? "This is a draft. Nothing sends until you review and launch."
                    : activeStep === "audience"
                      ? "Import your contacts to get started."
                      : activeStep === "senders"
                        ? "Choose the connected email accounts that will send your campaign."
                        : activeStep === "schedule"
                          ? "Choose when your emails should start sending."
                          : "Check the essentials. You can return to any step before launching."}
                </p>
              </div>
            </div>
            {activeStep === "message" && messagePanel}
            {activeStep === "audience" && (
              <RecipientsSection
                campaignId={campaignId}
                onOpenImport={() => setRecipientsModalOpen(true)}
                importGuide={!recipientsModalOpen && guideFor("audience")}
                readOnly={editingLocked}
                onAudienceChange={async () => { await Promise.all([mutateSummary(), mutateValSummary(), recheckRecipients()]); }}
              />
            )}
            {activeStep === "senders" && (
              <div className="campaign-step-panel">
                <section className="campaign-panel">
                  <h2>
                    {summary?.sender_group_name || "No sender group selected"}
                  </h2>
                  <ul className="campaign-sender-list">
                    {senderEmails.map((email) => (
                      <li key={email}>
                        <CheckCircle size={18} className="is-blue" />
                        {email}
                      </li>
                    ))}
                  </ul>
                  <button
                    className="campaign-button is-outline"
                    onClick={() => setSenderModalOpen(true)}
                  >
                    <AtSign size={19} />
                    {sendersReady
                      ? "Change sender group"
                      : "Choose sender group"}
                  </button>
                </section>
                <p>
                  Sending limits stay attached to each account. Only connected
                  senders are used.
                </p>
              </div>
            )}
            {activeStep === "schedule" &&
              (schedule.draft ? (
                <CampaignSchedule
                  draft={schedule.draft}
                  onChange={schedule.update}
                  recipients={Number(summary?.recipients || 0)}
                  error={schedule.error || schedule.problem}
                  onRetry={() =>
                    void schedule.flush(true).catch(() => undefined)
                  }
                />
              ) : (
                <p className="campaign-empty">Loading your schedule…</p>
              ))}
            {activeStep === "review" && (
              <CampaignReview
                recipients={Number(summary?.recipients || 0)}
                subject={subject}
                senderCount={senderEmails.length}
                draft={schedule.draft}
                configured={schedule.configured}
                validation={recipientValidation}
                loading={
                  checkingRecipients ||
                  (!recipientValidation && !recipientValidationError)
                }
                validationError={Boolean(recipientValidationError)}
                onRetry={() => void recheckRecipients()}
                messageReady={messageReady}
                scheduleError={schedule.problem}
                ready={launchReady}
                onEdit={changeStep}
                onTest={() => void handleOpenPreview()}
                testLoading={isPreviewLoading}
                trackingEnabled={engagementTracking?.enabled ?? true}
                trackingConfigured={engagementTracking?.configured ?? false}
                trackingLoading={engagementTrackingLoading}
                trackingBusy={trackingBusy}
                onTrackingChange={(enabled) => void handleTrackingChange(enabled)}
              />
            )}
          </>
        )}
      </div>
      {!isOperational && (
        <footer className="campaign-footer campaign-workflow-footer">
          <div className="campaign-footer-exit">
            {stepIndex > 0 && (
              <button
                className="campaign-icon-button"
                aria-label={`Back to ${CAMPAIGN_STEPS[stepIndex - 1]}`}
                title={`Back to ${CAMPAIGN_STEPS[stepIndex - 1]}`}
                onClick={() => changeStep(CAMPAIGN_STEPS[stepIndex - 1])}
                disabled={isSaving || Boolean(busyAction)}
              ><ArrowLeft size={18} /></button>
            )}
            <button className="campaign-button is-quiet" disabled={isSaving || Boolean(busyAction)} onClick={() => void navigateSafely("/campaigns")}>
              {isSaving ? "Saving…" : "Save & exit"}
            </button>
          </div>
          <CampaignSteps current={activeStep} complete={stepComplete} onChange={changeStep} disabled={isSaving || Boolean(busyAction)} />
          <div className="campaign-footer-next campaign-guide-anchor">
            {guideFor("review")}
            <button
              className="campaign-button is-primary"
              onClick={() => void continueStep()}
              disabled={isSaving || Boolean(busyAction) || (stepIndex === 4 && !launchReady)}
            >
              {stepIndex < 4 ? "Continue" : busyAction === "launch" ? "Launching…" : schedule.draft?.dryRun ? "Launch test run" : "Launch campaign"}
              <ArrowRight size={18} />
            </button>
          </div>
        </footer>
      )}
      <Dialog open={endDialogOpen} onOpenChange={setEndDialogOpen}>
        <DialogContent className="campaign-ui sm:max-w-md">
          <DialogHeader>
            <DialogTitle>End this campaign?</DialogTitle>
          </DialogHeader>
          <p>
            Future sends will be cancelled. Emails already sending may finish.
            Your audience, message, and delivery history are kept.
          </p>
          <DialogFooter>
            <button
              className="campaign-button"
              disabled={Boolean(busyAction)}
              onClick={() => setEndDialogOpen(false)}
            >
              Keep campaign
            </button>
            <button
              className="campaign-button is-danger"
              disabled={Boolean(busyAction)}
              onClick={() => void handleCampaignAction("stop")}
            >
              {busyAction === "stop" ? "Ending…" : "End campaign"}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ----------------------------------------------------
          3. Modals & Dialogs
          ---------------------------------------------------- */}

      {/* A. Send Campaign Modal */}
      <ScheduleDialog
        key={sendTab}
        isOpen={sendModalOpen}
        onClose={() => setSendModalOpen(false)}
        campaignId={campaignId as string}
        defaultTab={sendTab}
        configurationOnly={configurationOnly}
        summary={summary}
        readOnly={editingLocked || (isOperational && !isPaused)}
        mutateAll={() => {
          mutate(`${API_URL}/api/campaigns/${campaignId}`);
          mutateSummary();
        }}
      />

      <AutopilotDailyLimitsDialog
        isOpen={autopilotLimitsOpen}
        onClose={() => setAutopilotLimitsOpen(false)}
        campaignId={campaignId}
        schedule={summary?.autopilot_schedule || []}
        onSaved={async () => {
          await Promise.all([
            mutate(`${API_URL}/api/campaigns/${campaignId}`),
            mutate(`${API_URL}/api/campaigns/${campaignId}/send-progress`),
            mutateSummary(),
          ]);
        }}
      />

      {/* B. Select Sender Modal */}
      <SenderSelectionDialog
        isOpen={senderModalOpen}
        onClose={() => setSenderModalOpen(false)}
        senderGroups={senderGroups || []}
        selectedGroupId={
          summary?.sender_group_id || selectedSenderGroup?.id || null
        }
        onSelect={async (senderGroupId) => {
          await handleSelectSenderGroup(senderGroupId);
          setSenderModalOpen(false);
        }}
      />

      {/* C. Select Recipients Modal */}
      <RecipientsImportDialog
        isOpen={recipientsModalOpen}
        onClose={() => setRecipientsModalOpen(false)}
        campaignId={campaignId as string}
        onImported={async () => {
          await Promise.all([
            mutate((key) => typeof key === "string" && key.startsWith(`${API_URL}/api/campaigns/${campaignId}/recipients?`)),
            mutateSummary(),
            mutateValSummary(),
            recheckRecipients(),
          ]);
        }}
      />

      {/* D. Preview Modal */}
      <PreviewDialog
        isOpen={previewModalOpen}
        onClose={() => setPreviewModalOpen(false)}
        campaignId={campaignId as string}
      />

      {/* E. Attachment Modal */}
      <AttachmentDialog
        isOpen={attachmentModalOpen}
        onClose={() => setAttachmentModalOpen(false)}
        campaignId={campaignId as string}
        mutateSummary={mutateSummary}
        attachments={campaignAttachments}
        deletingAttachmentId={deletingAttachmentId}
        onRemoveAttachment={handleRemoveAttachment}
        onUploadChange={handleAttachmentUploadChange}
      />

      {/* F. Template Modal */}
      <TemplateDialog
        isOpen={templateModalOpen}
        onClose={() => setTemplateModalOpen(false)}
        onSelect={(tplSub, tplBody) => {
          updateSubjectDraft(tplSub);
          updateBodyDraft(tplBody);
          setTemplateModalOpen(false);
        }}
      />
    </div>
  );
}

// ----------------------------------------------------
// Dialog Components Helpers
// ----------------------------------------------------

// 6. Template Dialog
function TemplateDialog({
  isOpen,
  onClose,
  onSelect,
}: {
  isOpen: boolean;
  onClose: () => void;
  onSelect: (subject: string, body: string) => void;
}) {
  const { data: templates } = useSWR(
    isOpen ? `${API_URL}/api/templates` : null,
  );
  const list = Array.isArray(templates) ? templates : [];

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Select email template</DialogTitle>
        </DialogHeader>
        <div className="py-4 space-y-4">
          {list.length === 0 ? (
            <p className="text-center text-sm text-slate-400 py-8">
              No templates yet. Create one from the Templates page.
            </p>
          ) : (
            list.map(
              (t: {
                id: number;
                title: string;
                subject: string;
                body: string;
              }) => (
                <div
                  key={t.id}
                  className="border border-slate-200 hover:border-blue-400 rounded-lg p-4 cursor-pointer hover:bg-blue-50/10 transition-all space-y-2"
                  onClick={() => onSelect(t.subject, t.body)}
                >
                  <h3 className="font-bold text-slate-800 text-sm">
                    {t.title}
                  </h3>
                  <p className="text-xs text-slate-400 line-clamp-2 leading-relaxed">
                    {t.body}
                  </p>
                </div>
              ),
            )
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
