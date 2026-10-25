const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const web = path.resolve(__dirname, '../outreach_web');
const ts = Module.createRequire(path.join(web, 'package.json'))('typescript');
function loadTs(relative, aliases = {}) {
  const filename = path.join(web, 'src', relative);
  const module = new Module(filename);
  module.filename = filename;
  module.paths = Module._nodeModulePaths(path.dirname(filename));
  const normalRequire = module.require.bind(module);
  module.require = id => aliases[id] || normalRequire(id);
  module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText, filename);
  return module.exports;
}
const schedule = loadTs('components/campaigns/workspace/scheduleDraft.ts', {
  '@/lib/timezones': loadTs('lib/timezones.ts'),
});
const draft = { ...schedule.hydrateSchedule({ timezone: 'UTC', send_settings: { mode: 'autopilot' } }), scheduledAt: '2020-01-01T12:00', startOnDate: false };
test('switching to the next autopilot window ignores a date left over from scheduled sending', () => {
  assert.equal(schedule.scheduleProblem(draft), '');
  assert.equal(schedule.schedulePayload(draft).scheduled_at, null);
  assert.equal(schedule.launchRequest(draft).body.scheduled_at, undefined);
});
test('a date explicitly selected for scheduled sending or autopilot is still validated', () => {
  for (const value of [{ ...draft, mode: 'schedule' }, { ...draft, startOnDate: true }]) {
    assert.match(schedule.scheduleProblem(value), /future/);
  }
});
test('after-launch mode never inherits a hidden start date', () => {
  const value = { ...draft, mode: 'send_now', startOnDate: true };
  assert.equal(schedule.scheduleProblem(value), '');
  const request = schedule.launchRequest(value);
  assert.equal(request.endpoint, 'send-now');
  assert.equal(request.body.scheduled_at, undefined);
});

test('reviewing a paused campaign resumes its saved delivery mode instead of launching again', () => {
  for (const mode of ['send_now', 'schedule', 'autopilot']) {
    const value = { ...draft, mode };
    assert.deepEqual(schedule.launchRequest(value, 'paused'), { endpoint: 'resume', body: {} });
    assert.equal(schedule.launchRequest(value, 'draft').endpoint, {
      send_now: 'send-now', schedule: 'schedule', autopilot: 'autopilot/start',
    }[mode]);
  }
});

test('paused review says resume and keeps launch wording for new drafts', () => {
  const webRequire = Module.createRequire(path.join(web, 'package.json'));
  const React = webRequire('react');
  const { renderToStaticMarkup } = webRequire('react-dom/server');
  const CampaignReview = loadTs('components/campaigns/workspace/CampaignReview.tsx', {
    './scheduleDraft': schedule,
  }).default;
  const props = {
    recipients: 5, subject: 'Hello', senderCount: 1,
    draft: { ...draft, mode: 'send_now' }, configured: true,
    validation: { checked_recipient_count: 5, ready_recipient_count: 5, skipped_recipient_count: 0, overlap_recipient_count: 0, missing_by_variable: [] },
    loading: false, validationError: false, onRetry() {}, messageReady: true,
    scheduleError: '', ready: true, onEdit() {}, onTest() {}, testLoading: false,
    trackingEnabled: false, trackingConfigured: true, trackingLoading: false,
    trackingBusy: false, onTrackingChange() {},
  };
  const paused = renderToStaticMarkup(React.createElement(CampaignReview, { ...props, isPaused: true }));
  assert.match(paused, /Ready to resume/);
  assert.match(paused, /Sending stays paused until you resume/);
  assert.match(paused, /On resume/);
  assert.doesNotMatch(paused, /Ready to launch|until you launch|On launch/);
  const fresh = renderToStaticMarkup(React.createElement(CampaignReview, props));
  assert.match(fresh, /Ready to launch/);
  assert.match(fresh, /Nothing sends until you launch/);
});

test('new autopilot schedules save and launch with evenly spread delivery', () => {
  const value = schedule.hydrateSchedule({ timezone: 'Europe/Warsaw' });
  assert.equal(value.pacing, 'spread_evenly');
  assert.equal(schedule.schedulePayload(value).pacing_mode, 'spread_evenly');
  assert.equal(schedule.launchRequest(value).body.pacing_mode, 'spread_evenly');
});

test('an explicitly saved fixed delay remains intact when reopening a campaign', () => {
  const value = schedule.hydrateSchedule({
    timezone: 'UTC',
    send_settings: { mode: 'autopilot', pacing_mode: 'fixed_delay', delay_minutes: 12 },
  });
  assert.equal(value.pacing, 'fixed_delay');
  assert.equal(schedule.launchRequest(value).body.pacing_mode, 'fixed_delay');
  assert.equal(schedule.launchRequest(value).body.delay_minutes, 12);
});

test('timezone options lead with cities and calculate seasonal and fractional UTC offsets', () => {
  const { timeZoneOption } = loadTs('lib/timezones.ts');
  const summer = new Date('2026-07-01T12:00:00Z');
  const winter = new Date('2026-01-01T12:00:00Z');
  assert.equal(timeZoneOption('Europe/Warsaw', summer).label, 'Warsaw (Europe) · UTC+02:00');
  assert.equal(timeZoneOption('Europe/Warsaw', winter).offset, 'UTC+01:00');
  assert.equal(timeZoneOption('Asia/Kathmandu', summer).offset, 'UTC+05:45');
  assert.equal(timeZoneOption('America/New_York', winter).offset, 'UTC-05:00');
  assert.equal(timeZoneOption('UTC', summer).offset, 'UTC+00:00');
});

test('newly enabled days inherit the saved window and limit instead of a cap of 40', () => {
  const value = schedule.hydrateSchedule({
    timezone: 'Asia/Dubai',
    autopilot_schedule: [
      { day: 'monday', cap: 10, start: '08:30', end: '15:00' },
      { day: 'tuesday', cap: 10, start: '08:30', end: '15:00' },
    ],
  });
  assert.deepEqual(value.days.sunday, { active: false, cap: '10', start: '08:30', end: '15:00' });
  value.days.sunday.active = true;
  assert.deepEqual(schedule.schedulePayload(value).schedule.sunday, { cap: 10, start: '08:30', end: '15:00' });
});

test('the timezone reference skips disabled days rather than showing Sunday for a weekday schedule', () => {
  const value = schedule.hydrateSchedule({ timezone: 'Asia/Dubai' });
  assert.equal(schedule.scheduleWindowReference(value, Date.parse('2026-10-25T12:00:00Z')), '2026-10-26T09:00');
  assert.equal(schedule.scheduleWindowReference(value, Date.parse('2026-10-26T14:00:00Z')), '2026-10-27T09:00');
});

test('the reference follows the campaign weekday when the viewer is on the previous day', () => {
  const value = schedule.hydrateSchedule({
    timezone: 'Asia/Dubai',
    autopilot_schedule: [{ day: 'monday', cap: 10, start: '00:15', end: '02:00' }],
  });
  assert.equal(schedule.scheduleWindowReference(value, Date.parse('2026-10-25T20:30:00Z')), '2026-10-26T00:15');
  const timezones = loadTs('lib/timezones.ts');
  assert.equal(
    timezones.toZonedDateTimeInput(timezones.campaignWallTimeInstant('2026-10-26T00:15', 'Asia/Dubai').toISOString(), 'Africa/Casablanca').slice(0, 10),
    '2026-10-25',
  );
});

test('the reference uses each enabled day’s actual opening time and respects a chosen start date', () => {
  const value = schedule.hydrateSchedule({
    timezone: 'Asia/Dubai',
    autopilot_schedule: [{ day: 'tuesday', cap: 10, start: '11:30', end: '17:00' }],
  });
  assert.equal(schedule.scheduleWindowReference(value, Date.parse('2026-10-25T12:00:00Z')), '2026-10-27T11:30');
  assert.equal(schedule.scheduleWindowReference({ ...value, startOnDate: true, scheduledAt: '2026-11-03T12:00' }), '2026-11-03T12:00');
  for (const day of Object.values(value.days)) day.active = false;
  assert.equal(schedule.scheduleWindowReference(value), '');
});

function scheduleEditor(initial) {
  let value = structuredClone(initial);
  let perDay;
  let initialized = false;
  const TimeZonePicker = () => null;
  const CampaignSchedule = loadTs('components/campaigns/workspace/CampaignSchedule.tsx', {
    react: {
      useState(initialState) {
        if (!initialized) { perDay = initialState; initialized = true; }
        return [perDay, next => { perDay = next; }];
      },
    },
    '@/components/ui/timezone-picker': { TimeZonePicker },
    '@/lib/timezones': loadTs('lib/timezones.ts'),
    './scheduleDraft': schedule,
  }).default;
  function tree() {
    return CampaignSchedule({ draft: value, onChange(next) { value = next; }, recipients: 100, error: '', onRetry() {} });
  }
  function nodes(node, result = []) {
    if (Array.isArray(node)) node.forEach(child => nodes(child, result));
    else if (node && typeof node === 'object' && node.props) {
      result.push(node);
      nodes(node.props.children, result);
    }
    return result;
  }
  return {
    get draft() { return value; },
    tree,
    find(predicate) {
      const node = nodes(tree()).find(predicate);
      assert.ok(node, 'Expected schedule control to exist');
      return node.props;
    },
    day(day) { return this.find(node => node.type === 'button' && node.props['aria-label'] === day); },
    customize() { return this.find(node => node.type === 'input' && node.props.type === 'checkbox'); },
  };
}

function savedWeekdays(fridayCap = 10) {
  return schedule.hydrateSchedule({
    timezone: 'Asia/Dubai',
    autopilot_schedule: schedule.DAYS.slice(0, 5).map(day => ({ day, cap: day === 'friday' ? fridayCap : 10, start: '09:00', end: '16:30' })),
  });
}

test('toggling Friday or Sunday changes only that day and does not switch editing layouts', () => {
  const editor = scheduleEditor(savedWeekdays(40));
  assert.equal(editor.customize().checked, true);
  assert.equal(editor.customize().disabled, undefined);
  editor.day('friday').onClick();
  assert.equal(editor.draft.days.friday.active, false);
  assert.equal(editor.customize().checked, true);
  assert.equal(editor.draft.days.friday.cap, '40');
  editor.day('sunday').onClick();
  assert.equal(editor.draft.days.sunday.active, true);
  assert.equal(editor.draft.days.sunday.cap, '10');
  assert.equal(editor.customize().checked, true);
  editor.day('friday').onClick();
  assert.equal(editor.draft.days.friday.cap, '40');
  assert.deepEqual(schedule.DAYS.filter(day => editor.draft.days[day].active), ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'sunday']);
});

test('adding a day to a shared schedule keeps its shared limit and window', () => {
  const editor = scheduleEditor(savedWeekdays());
  editor.day('sunday').onClick();
  assert.equal(editor.customize().checked, false);
  assert.equal(editor.find(node => node.props.id === 'campaign-daily-limit').value, '10');
  assert.equal(editor.draft.days.sunday.cap, '10');
});

test('custom day settings can be explicitly switched back to one shared window and limit', () => {
  const editor = scheduleEditor(savedWeekdays(40));
  editor.customize().onChange({ target: { checked: false } });
  assert.equal(editor.customize().checked, false);
  assert.equal(editor.find(node => node.props.id === 'campaign-daily-limit').value, '10');
  assert.equal(editor.draft.days.friday.cap, '10');
  assert.equal(editor.draft.days.sunday.active, false);
});

test('changing the campaign timezone keeps chosen weekdays and wall-clock windows intact', () => {
  const editor = scheduleEditor(savedWeekdays(40));
  const before = structuredClone(editor.draft.days);
  editor.find(node => node.props.id === 'campaign-timezone').onChange('America/New_York');
  assert.equal(editor.draft.timezone, 'America/New_York');
  assert.deepEqual(editor.draft.days, before);
  assert.deepEqual(Object.keys(schedule.schedulePayload(editor.draft).schedule), ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']);
});
