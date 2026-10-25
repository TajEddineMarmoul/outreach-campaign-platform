"use client";

import { useState } from "react";
import { TimeZonePicker } from "@/components/ui/timezone-picker";
import {
  CalendarDays,
  Clock3,
  Shield,
  Users,
  ChevronDown,
  Info,
} from "lucide-react";
import {
  campaignWallTimeInstant,
  formatViewerConversion,
  toZonedDateTimeInput,
} from "@/lib/timezones";
import {
  DAYS,
  clockLabel,
  scheduleWindowReference,
  type ScheduleDraft,
  type DayWindow,
} from "./scheduleDraft";

export default function CampaignSchedule({
  draft,
  onChange,
  recipients,
  error,
  onRetry,
}: {
  draft: ScheduleDraft;
  onChange: (draft: ScheduleDraft) => void;
  recipients: number;
  error: string;
  onRetry: () => void;
}) {
  const activeDays = DAYS.filter((day) => draft.days[day].active);
  const first = draft.days[activeDays[0] || "monday"];
  const customized = activeDays.some((day) => {
    const value = draft.days[day];
    return (
      value.cap !== first.cap ||
      value.start !== first.start ||
      value.end !== first.end
    );
  });
  const [perDay, setPerDay] = useState(customized);
  const dateChosen = draft.startOnDate;
  const viewerZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const today = toZonedDateTimeInput(
    new Date().toISOString(),
    draft.timezone,
  ).slice(0, 10);
  const usesStartDate = draft.mode === "schedule" || (draft.mode === "autopilot" && dateChosen);
  const conversionWallTime = scheduleWindowReference(draft);
  const conversion = formatViewerConversion(
    conversionWallTime,
    draft.timezone,
    viewerZone,
  );
  const zoneCity = draft.timezone.split("/").pop()?.replaceAll("_", " ");
  const viewerCity = viewerZone.split("/").pop()?.replaceAll("_", " ");
  const autopilot = draft.mode === "autopilot";
  const patch = (value: Partial<ScheduleDraft>) =>
    onChange({ ...draft, ...value });
  const updateAllDays = (value: Partial<DayWindow>) =>
    patch({
      days: Object.fromEntries(
        DAYS.map((day) => [day, { ...draft.days[day], ...value }]),
      ) as ScheduleDraft["days"],
    });
  const minimumDays =
    first && Number(first.cap) > 0 && !customized
      ? Math.ceil(recipients / Number(first.cap))
      : null;
  const instant = usesStartDate && draft.scheduledAt
    ? campaignWallTimeInstant(draft.scheduledAt, draft.timezone)
    : null;
  const startLabel = instant
    ? `Starts ${new Intl.DateTimeFormat("en", { timeZone: draft.timezone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(instant)} · ${zoneCity}`
    : autopilot
      ? "Starts in the next available window"
      : draft.mode === "send_now"
        ? "Starts when you launch"
        : "Choose a start date and time";

  return (
    <>
      <fieldset className="campaign-delivery-options">
        <legend>When should emails start?</legend>
        <div>
          {([
            { mode: "send_now", label: "After launch" },
            { mode: "schedule", label: "At a set time" },
            { mode: "autopilot", label: "Autopilot" },
          ] as const).map((option) => (
            <label key={option.mode}>
              <input type="radio" name="campaign-delivery-mode" value={option.mode} checked={draft.mode === option.mode} onChange={() => patch({ mode: option.mode })} />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
        <p>
          {autopilot ? "Send gradually on the days and hours you choose." : draft.mode === "schedule" ? "Start at a date and time you choose, then continue until complete." : "Start after you review and launch, then continue until complete."}
        </p>
      </fieldset>
      {draft.dryRun && <div className="campaign-notice" role="status">Test mode is on. This campaign will simulate delivery without sending emails. You can change this in More settings.</div>}
      <div className="campaign-schedule-grid">
        <div className="campaign-schedule-form">
          {draft.mode !== "send_now" && (
          <div className="campaign-field campaign-timezone-field">
            <label htmlFor="campaign-timezone">Campaign timezone</label>
            <TimeZonePicker
              id="campaign-timezone"
              value={draft.timezone}
              onChange={(timezone) => patch({ timezone })}
            />
            {conversionWallTime && conversion && viewerZone !== draft.timezone && (
              <p className="campaign-time-conversion">
                Time reference: {clockLabel(conversionWallTime.slice(11))} in {zoneCity} is{" "}
                {conversion} in {viewerCity}.
              </p>
            )}
          </div>

          )}

          {draft.mode !== "send_now" && (
            <div className={`campaign-field campaign-start-field ${!autopilot ? "is-wide" : ""}`}>
              <label htmlFor="campaign-start-mode">{autopilot ? "Start" : "Start date and time"}</label>
              {autopilot && (
                <select
                  id="campaign-start-mode"
                  value={dateChosen ? "date" : "next"}
                  onChange={(event) => {
                    const chosen = event.target.value === "date";
                    patch({
                      startOnDate: chosen,
                      ...(!chosen ? { scheduledAt: "" } : {}),
                    });
                  }}
                >
                  <option value="next">Next available sending window</option>
                  <option value="date">Choose a date and time</option>
                </select>
              )}
              {(!autopilot || dateChosen) && (
                <input
                  id={!autopilot ? "campaign-start-mode" : undefined}
                  type="datetime-local"
                  aria-label="Campaign start date and time"
                  value={draft.scheduledAt}
                  min={`${today}T00:00`}
                  onInput={(event) =>
                    patch({ scheduledAt: event.currentTarget.value })
                  }
                  required
                />
              )}
              {dateChosen && autopilot && !draft.scheduledAt && (
                <p className="campaign-field-hint">
                  Choose a date, or keep the next available window.
                </p>
              )}
            </div>
          )}

          {autopilot ? (
            <>
              <fieldset className="campaign-field campaign-days-field" aria-describedby="campaign-days-timezone">
                <legend>Sending days</legend>
                <div className="campaign-day-picker">
                  {DAYS.map((day) => (
                    <button
                      type="button"
                      key={day}
                      aria-label={day}
                      aria-pressed={draft.days[day].active}
                      onClick={() =>
                        patch({
                          days: {
                            ...draft.days,
                            [day]: {
                              ...draft.days[day],
                              active: !draft.days[day].active,
                            },
                          },
                        })
                      }
                    >
                      {day[0].toUpperCase() + day.slice(1, 3)}
                    </button>
                  ))}
                </div>
                <p id="campaign-days-timezone" className="campaign-field-hint">
                  All sending days and hours use {zoneCity} ({draft.timezone}).
                </p>
              </fieldset>
              {!perDay && (
                <>
                  <fieldset className="campaign-field campaign-window-field">
                    <legend>Send between</legend>
                    <div className="campaign-time-range">
                      <input
                        type="time"
                        aria-label="Sending window start"
                        value={first.start}
                        onInput={(event) =>
                          updateAllDays({ start: event.currentTarget.value })
                        }
                      />
                      <span>and</span>
                      <input
                        type="time"
                        aria-label="Sending window end"
                        value={first.end}
                        onInput={(event) =>
                          updateAllDays({ end: event.currentTarget.value })
                        }
                      />
                    </div>
                  </fieldset>
                  <div className="campaign-field campaign-limit-field">
                    <label htmlFor="campaign-daily-limit">Daily limit</label>
                    <div className="campaign-input-unit">
                      <input
                        id="campaign-daily-limit"
                        type="number"
                        min="1"
                        step="1"
                        value={first.cap}
                        onInput={(event) =>
                          updateAllDays({ cap: event.currentTarget.value })
                        }
                      />
                      <span>emails per day</span>
                    </div>
                  </div>
                </>
              )}
              {perDay && (
                <div className="campaign-day-settings">
                  <p>Custom limits and windows</p>
                  {activeDays.map((day) => (
                    <div key={day}>
                      <span>{day.slice(0, 3)}</span>
                      <input
                        type="time"
                        aria-label={`${day} start`}
                        value={draft.days[day].start}
                        onInput={(event) =>
                          patch({
                            days: {
                              ...draft.days,
                              [day]: {
                                ...draft.days[day],
                                start: event.currentTarget.value,
                              },
                            },
                          })
                        }
                      />
                      <input
                        type="time"
                        aria-label={`${day} end`}
                        value={draft.days[day].end}
                        onInput={(event) =>
                          patch({
                            days: {
                              ...draft.days,
                              [day]: {
                                ...draft.days[day],
                                end: event.currentTarget.value,
                              },
                            },
                          })
                        }
                      />
                      <input
                        type="number"
                        min="1"
                        step="1"
                        aria-label={`${day} daily limit`}
                        value={draft.days[day].cap}
                        onInput={(event) =>
                          patch({
                            days: {
                              ...draft.days,
                              [day]: {
                                ...draft.days[day],
                                cap: event.currentTarget.value,
                              },
                            },
                          })
                        }
                      />
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : null}

          <details className="campaign-advanced-schedule">
            <summary>
              <ChevronDown size={18} />
              More settings
            </summary>
            <div>
              {autopilot && (
                <>
                  <label className="campaign-check-field">
                    <input
                      type="checkbox"
                      checked={perDay}
                      onChange={(event) => {
                        const enabled = event.target.checked;
                        if (!enabled) {
                          updateAllDays({ cap: first.cap, start: first.start, end: first.end });
                        }
                        setPerDay(enabled);
                      }}
                    />
                    Customize each sending day
                  </label>
                  {perDay && (
                    <p className="campaign-field-hint">
                      Turn this off to use {activeDays[0] || "monday"}’s window and limit for all sending days.
                    </p>
                  )}
                  <div className="campaign-field">
                    <label htmlFor="campaign-pacing">Delivery pace</label>
                    <select
                      id="campaign-pacing"
                      value={draft.pacing}
                      onChange={(event) =>
                        patch({
                          pacing: event.target.value as ScheduleDraft["pacing"],
                        })
                      }
                    >
                      <option value="spread_evenly">
                        Spread evenly through the window
                      </option>
                      <option value="fixed_delay">Wait between batches</option>
                    </select>
                  </div>
                </>
              )}
              {(!autopilot || draft.pacing === "fixed_delay") && (
                <div className="campaign-field">
                  <label htmlFor="campaign-delay">
                    Minutes between batches
                  </label>
                  <input
                    id="campaign-delay"
                    type="number"
                    min="0"
                    max="1440"
                    step="1"
                    value={draft.delay}
                    onInput={(event) =>
                      patch({ delay: event.currentTarget.value })
                    }
                  />
                </div>
              )}
              <label className="campaign-check-field">
                <input
                  type="checkbox"
                  checked={draft.dryRun}
                  onChange={(event) => patch({ dryRun: event.target.checked })}
                />
                Test mode — simulate delivery without sending emails
              </label>
            </div>
          </details>
          {error && (
            <div role="alert" className="campaign-inline-error">
              <Info size={18} />
              <span>{error}</span>
              <button className="campaign-text-button" onClick={onRetry}>
                Retry save
              </button>
            </div>
          )}
        </div>

        <details className="campaign-run-summary">
          <summary><ChevronDown size={16} /> How this will run</summary>
          <ul>
            <li>
              <span className="campaign-summary-icon">
                <Users />
              </span>
              <span>{recipients.toLocaleString()} people</span>
            </li>
            {autopilot && (
              <li>
                <span className="campaign-summary-icon">
                  <CalendarDays />
                </span>
                <span>
                  {minimumDays
                    ? `At least ${minimumDays} sending ${minimumDays === 1 ? "day" : "days"}`
                    : "Uses your daily sending limits"}
                  <small>
                    Sender limits and delivery pace may extend this.
                  </small>
                </span>
              </li>
            )}
            <li>
              <span className="campaign-summary-icon">
                <Clock3 />
              </span>
              <span>
                {startLabel}
                {autopilot && instant && (
                  <small>The first send waits for an enabled window.</small>
                )}
              </span>
            </li>
            <li>
              <span className="campaign-summary-icon">
                <Shield />
              </span>
              <span>
                {autopilot
                  ? "Nothing sends outside this window"
                  : "Nothing sends until you launch"}
              </span>
            </li>
          </ul>
          <p className="campaign-safety-note">
            <Shield />
            <span>
              {draft.dryRun
                ? "Test mode is on. This campaign will simulate sending."
                : "You can pause future sends from the campaign page."}
            </span>
          </p>
        </details>
      </div>
    </>
  );
}
