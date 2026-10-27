# Create and run a campaign

Create a campaign, then use **Audience → Message → Senders → Schedule → Review**.
You can return to earlier steps before launch. Saving setup changes does not
launch delivery.

## Audience and message

1. Open **Import contacts** and choose paste, CSV, or Google Sheets. Include an
   `email` column in the header row; other columns become personalization fields.
2. Check the import preview before confirming. Google Sheets links must be
   publicly accessible; for a restricted sheet, paste the rows or export CSV.
3. Write the subject and body, or start from a saved template. Insert fields from
   the audience and preview individual messages. Attachments are optional.
4. Check Review for recipients who will be skipped because required values are
   missing. Correct the audience or message before launching if needed.

Clearing a campaign's audience preserves global contacts and the campaign's
message and schedule. Editing a saved template does not rewrite messages already
copied into campaigns.

## Senders

Connect Gmail accounts and choose the campaign's sender group. A batch assigns
at most one eligible recipient to each eligible connected sender. Account limits
and temporary error cooldowns can reduce the number of active senders.

## Schedule

| Option | Behavior after launch |
| --- | --- |
| **After launch** | Start sending, then continue in batches. |
| **At a set time** | Wait for the selected date and time, then continue in batches. |
| **Autopilot** | Use the enabled weekdays, daily limits, and sending windows. |

### Find a timezone

Click **Campaign timezone** and type a city, region, or UTC offset. Searching
`w` puts cities beginning with W first, including Warsaw; `war` narrows the list
to Warsaw. Matches elsewhere in a timezone name are also included.

Each option shows the city, timezone identifier, and **current** UTC offset.
Use the arrow keys and Enter to select, or click a result. Escape cancels a search.
The conversion below the field compares the campaign's time with your local time.

Each campaign keeps its own timezone. A 9 AM Warsaw window stays at 9 AM Warsaw
time as daylight saving changes its UTC offset. The picker shows today's offset;
a future scheduled date may have a different offset. Changing the account's
default timezone does not rewrite an existing campaign's timezone.

### Autopilot limits and pace

New campaign schedules start with Monday–Friday, 9 AM–4:30 PM, a 40-email daily
target, and **Steady pace across the full window**. Saved schedules retain their
configured values, including an explicitly chosen fixed delay.

Under **More settings → Delivery pace**, choose:

- **Steady pace across the full window:** calculate the interval from the whole
  sending window, the daily target, and the batch size. Starting late, resuming,
  or increasing the target never compresses missed sends into the time left.
  For example, 100 emails over 9 AM–5 PM means one email every 4.8 minutes with
  one sender, including when the target changes at 4:45 PM.
- **Wait between batches:** wait the selected number of minutes after a batch
  completes before scheduling the next one.

The daily target is also a maximum: emails already sent count toward it. A
partial day may send fewer. The Today estimate accounts for the remaining time
at the configured pace.

Use **Customize each sending day** for different windows or targets by weekday.
Sender limits still apply. If the day's capacity is exhausted, Autopilot waits
for the next eligible day; immediate or scheduled batch sending can pause.
Worker availability and sender errors can delay delivery, so the displayed
minimum number of sending days is an estimate.

### Edit an existing campaign

Open **Audience**, **Message**, **Senders**, or **Schedule** directly. Existing
campaigns do not return to the launch wizard. **Schedule → Save changes** saves
the settings while preserving whether the campaign is running or paused.

Autopilot's target, sending days, hours, and pace can change while it runs. The
new pace applies to future sends. Lowering a target below today's sent count
waits for the next enabled day. Changing sender groups assigns unclaimed work
through the new group; emails already sending may finish from the old group.

Use **Pause and edit** for the message or audience. Timezone, delivery mode, and test mode also
require a pause. Save the changes, then use **Resume sending** when ready.

For API clients: send `pacing_mode: "spread_evenly"` explicitly when starting
Autopilot. The API's omitted-value default remains `fixed_delay` for compatibility.

## Review, launch, and follow progress

Review the audience, message, connected senders, and schedule, then launch.
**Test mode**, in More settings, simulates delivery without sending messages.
The local development delivery lock also blocks launch requests in this mode.

After launch, closing the browser does not stop the worker. **Pause sending**
stops future work; it cannot recall an email already handed to Gmail.

**Sent** means Gmail accepted the message, rather than confirmed inbox placement.
Response and bounce information depend on Gmail activity synchronization and the
sender's connection status. Simulated sends are excluded from delivery analytics.

### Continue a campaign

A campaign becomes **Completed** once every recipient has a final delivery
result. Adding recipients afterwards does not send to them by itself: the
overview shows how many recipients have no result yet, and **Resume sending**
queues exactly those recipients. Everyone with a result keeps their history and
is never emailed again by that resume.

If a send error stops a batch, **Resume sending** continues with recipients
still waiting, including jobs already queued. Recipients with a final result,
including send errors, are not retried automatically. If senders are temporarily
unavailable, the campaign keeps its stopped state and can be resumed again once
a sender is available.

Open **Menu → Help** to revisit guidance or show dismissed setup tips. Tip
preferences are saved per user in the current browser.
