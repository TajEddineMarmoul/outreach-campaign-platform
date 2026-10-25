import {
  naiveDateTimePayload,
  toZonedDateTimeInput,
  campaignWallTimeInstant,
} from "@/lib/timezones";

export const DAYS = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
] as const;
export type Day = (typeof DAYS)[number];
export interface DayWindow {
  active: boolean;
  cap: string;
  start: string;
  end: string;
}
export interface ScheduleDraft {
  mode: "autopilot" | "schedule" | "send_now";
  timezone: string;
  days: Record<Day, DayWindow>;
  scheduledAt: string;
  startOnDate: boolean;
  delay: string;
  pacing: "fixed_delay" | "spread_evenly";
  dryRun: boolean;
}
export interface SavedSchedule {
  timezone?: string;
  send_settings?: {
    mode?: string;
    delay_minutes?: number;
    pacing_mode?: string;
    dry_run?: boolean;
    draft_scheduled_at?: string;
  };
  autopilot_schedule?: {
    day: string;
    cap: number;
    start: string;
    end: string;
  }[];
}

export function hydrateSchedule(summary: SavedSchedule): ScheduleDraft {
  const settings = summary.send_settings || {};
  const timezone =
    summary.timezone ||
    Intl.DateTimeFormat().resolvedOptions().timeZone ||
    "UTC";
  const saved = summary.autopilot_schedule || [];
  const defaults = DAYS.map((day) => saved.find((entry) => entry.day === day))
    .find((entry) => entry !== undefined);
  return {
    mode:
      settings.mode === "send_now" || settings.mode === "schedule"
        ? settings.mode
        : "autopilot",
    timezone,
    days: Object.fromEntries(
      DAYS.map((day, index) => {
        const entry = saved.find((item) => item.day === day);
        return [
          day,
          {
            active: saved.length ? Boolean(entry) : index < 5,
            cap: String(entry?.cap ?? defaults?.cap ?? 40),
            start: entry?.start || defaults?.start || "09:00",
            end: entry?.end || defaults?.end || "16:30",
          },
        ];
      }),
    ) as ScheduleDraft["days"],
    scheduledAt: settings.draft_scheduled_at
      ? toZonedDateTimeInput(settings.draft_scheduled_at, timezone)
      : "",
    startOnDate: Boolean(settings.draft_scheduled_at),
    delay: String(settings.delay_minutes ?? 5),
    pacing:
      settings.pacing_mode === "fixed_delay"
        ? "fixed_delay"
        : "spread_evenly",
    dryRun: Boolean(settings.dry_run),
  };
}

/** Pick an enabled window for the timezone reference; this is not a delivery estimate. */
export function scheduleWindowReference(
  draft: ScheduleDraft,
  now = Date.now(),
): string {
  if (draft.mode === "schedule" || (draft.mode === "autopilot" && draft.startOnDate))
    return draft.scheduledAt;
  if (draft.mode !== "autopilot") return "";
  const localNow = toZonedDateTimeInput(new Date(now).toISOString(), draft.timezone);
  if (!localNow) return "";
  // UTC is only used for calendar arithmetic, after resolving the campaign's date.
  const calendar = new Date(`${localNow.slice(0, 10)}T00:00:00Z`);
  for (let offset = 0; offset < 8; offset += 1) {
    const day = DAYS[(calendar.getUTCDay() + 6) % 7];
    const window = draft.days[day];
    const wallTime = `${calendar.toISOString().slice(0, 10)}T${window.start}`;
    if (
      window.active &&
      (offset > 0 || localNow.slice(11) <= window.end) &&
      campaignWallTimeInstant(wallTime, draft.timezone)
    ) return wallTime;
    calendar.setUTCDate(calendar.getUTCDate() + 1);
  }
  return "";
}

export function scheduleProblem(
  draft: ScheduleDraft,
  now = Date.now(),
): string {
  try {
    new Intl.DateTimeFormat("en", { timeZone: draft.timezone }).format();
  } catch {
    return "Choose a valid campaign timezone.";
  }
  if (!/^\d+$/.test(draft.delay) || Number(draft.delay) > 1440)
    return "Set the batch delay between 0 and 1,440 minutes.";
  if (draft.mode === "autopilot") {
    const active = Object.values(draft.days).filter((day) => day.active);
    if (!active.length) return "Choose at least one sending day.";
    if (
      active.some(
        (day) =>
          !/^\d+$/.test(day.cap) ||
          Number(day.cap) < 1 ||
          !Number.isSafeInteger(Number(day.cap)),
      )
    )
      return "Set a whole-number daily limit of at least 1 email.";
    if (
      active.some(
        (day) =>
          !/^([01]\d|2[0-3]):[0-5]\d$/.test(day.start) ||
          !/^([01]\d|2[0-3]):[0-5]\d$/.test(day.end) ||
          day.start >= day.end,
      )
    )
      return "The end time must be later than the start time on every sending day.";
  }
  if (
    (draft.mode === "schedule" ||
      (draft.mode === "autopilot" && draft.startOnDate)) &&
    !draft.scheduledAt
  )
    return "Choose a start date and time.";
  if ((draft.mode === "schedule" || (draft.mode === "autopilot" && draft.startOnDate)) && draft.scheduledAt) {
    const instant = campaignWallTimeInstant(draft.scheduledAt, draft.timezone);
    if (!instant)
      return "Choose a valid date and time in the campaign timezone. This time may fall in a daylight-saving gap.";
    if (instant.getTime() <= now)
      return "Choose a start date and time in the future, or use the next available window.";
  }
  return "";
}

export function schedulePayload(draft: ScheduleDraft) {
  return {
    mode: draft.mode,
    timezone: draft.timezone,
    delay_minutes: Number(draft.delay),
    pacing_mode: draft.pacing,
    dry_run: draft.dryRun,
    scheduled_at:
      draft.mode === "schedule" || (draft.mode === "autopilot" && draft.startOnDate)
        ? naiveDateTimePayload(draft.scheduledAt)
        : null,
    ...(draft.mode === "autopilot"
      ? {
          schedule: Object.fromEntries(
            DAYS.filter((day) => draft.days[day].active).map((day) => [
              day,
              {
                cap: Number(draft.days[day].cap),
                start: draft.days[day].start,
                end: draft.days[day].end,
              },
            ]),
          ),
        }
      : {}),
  };
}

export function launchRequest(draft: ScheduleDraft, campaignStatus = "draft") {
  // Paused delivery continues through /resume after the edited settings are saved.
  if (campaignStatus === "paused") return { endpoint: "resume", body: {} };
  const { mode, pacing_mode, schedule, scheduled_at, ...common } =
    schedulePayload(draft);
  if (mode === "autopilot")
    return {
      endpoint: "autopilot/start",
      body: {
        ...common,
        pacing_mode,
        schedule,
        ...(scheduled_at ? { scheduled_at } : {}),
      },
    };
  if (mode === "schedule")
    return { endpoint: "schedule", body: { ...common, scheduled_at } };
  return { endpoint: "send-now", body: common };
}

export function clockLabel(value: string) {
  if (!value) return "—";
  const [hour, minute] = value.split(":").map(Number);
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`;
}

export function scheduleLabel(draft: ScheduleDraft) {
  if (draft.mode === "send_now") return `On launch · ${draft.timezone}`;
  if (draft.mode === "schedule")
    return `${draft.scheduledAt.replace("T", " at ")} · ${draft.timezone}`;
  const days = DAYS.filter((day) => draft.days[day].active);
  const first = draft.days[days[0]];
  const uniform =
    first &&
    days.every(
      (day) =>
        draft.days[day].start === first.start &&
        draft.days[day].end === first.end,
    );
  const label =
    days.length === 5 && days.every((day, index) => day === DAYS[index])
      ? "Mon–Fri"
      : days.map((day) => day[0].toUpperCase() + day.slice(1, 3)).join(", ");
  return `${label || "No sending days"}${uniform ? `, ${clockLabel(first.start)}–${clockLabel(first.end)}` : " · Custom daily windows"} · ${draft.timezone}`;
}
