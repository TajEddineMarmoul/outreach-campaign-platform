const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const web = path.resolve(__dirname, '../outreach_web');
const webRequire = Module.createRequire(path.join(web, 'package.json'));
const ts = webRequire('typescript');
const React = webRequire('react');
const { renderToStaticMarkup } = webRequire('react-dom/server');

function loadTs(relative, aliases = {}) {
  const filename = path.join(web, 'src', relative);
  const mod = new Module(filename);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(path.dirname(filename));
  const normalRequire = mod.require.bind(mod);
  mod.require = id => Object.hasOwn(aliases, id) ? aliases[id] : normalRequire(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText, filename);
  return mod.exports;
}

const meterModule = loadTs('components/campaigns/DailyCapacityMeter.tsx');
const Meter = meterModule.default;
const example = {
  campaign_id: 1, timezone: 'Asia/Dubai', date: '2026-10-25', sent_today: 5,
  estimated_total_today: 10, daily_limit: 40, campaign_limit: 40, sender_limit: 40,
  competing_campaigns: 2, state: 'active',
};
const render = props => renderToStaticMarkup(React.createElement(Meter, props));

test('the meter shows 5 sent, 10 total, and 40 limit with proportional blue and hatched segments', () => {
  const markup = render({ capacity: example });
  assert.match(markup, /5 sent today; estimated total 10; daily limit 40/);
  assert.match(markup, /class="app-daily-capacity-sent" style="width:12.5%"/);
  assert.match(markup, /class="app-daily-capacity-estimate" style="width:25%"/);
  assert.match(markup, /class="app-daily-capacity-unused" style="left:25%;width:75%"/);
  assert.match(markup, />0<\/span>/);
  assert.match(markup, /role="img" aria-label=/);
});

test('unavailable capacity is never displayed as zero sent or zero capacity', () => {
  assert.match(render({ error: true }), /Capacity unavailable/);
  assert.match(render({}), /Checking today/);
  assert.doesNotMatch(render({ error: true }), /sent|limit|width:/);
});

test('empty and lowered limits cannot produce invalid or negative chart widths', () => {
  for (const capacity of [
    { ...example, sent_today: 0, estimated_total_today: 0, daily_limit: 0 },
    { ...example, sent_today: 15, estimated_total_today: 10, daily_limit: 5 },
  ]) {
    assert.doesNotMatch(render({ capacity }), /NaN|Infinity|width:-/);
  }
});

test('paused state and account and campaign limits remain available in the accessible description', () => {
  const markup = render({ capacity: { ...example, state: 'paused', sender_limit: 100 } });
  assert.match(markup, /Sender account limit: 100/);
  assert.match(markup, /Campaign limit: 40/);
  assert.match(markup, /Paused · Dubai/);
});

function campaignListMarkup() {
  const ui = loadTs('components/app-ui.tsx', {
    '@/components/ui/dialog': Object.fromEntries(
      ['Dialog', 'DialogContent', 'DialogTitle', 'DialogDescription'].map(name => [name, () => null]),
    ),
  });
  const capacities = [example, { ...example, campaign_id: 2, timezone: 'Europe/Amsterdam', sent_today: 14, estimated_total_today: 30 },
    { ...example, campaign_id: 3, state: 'paused', sent_today: 8, estimated_total_today: 8 }];
  const Page = loadTs('app/campaigns/page.tsx', {
    'next/link': ({ children, ...props }) => React.createElement('a', props, children),
    'next/navigation': { useRouter: () => ({}), useSearchParams: () => new URLSearchParams() },
    swr: url => ({ data: url.endsWith('/daily-capacity') ? { items: capacities } : [
      { id: 1, name: 'HR applications · Saudi Arabia', status: 'autopilot', recipient_count: 160, sent_count: 70, updated_at: '2026-10-25T10:00:00Z' },
      { id: 2, name: 'Design roles · Netherlands', status: 'sending', recipient_count: 100, sent_count: 32, updated_at: '2026-10-25T10:00:00Z' },
      { id: 3, name: 'Founder outreach · France', status: 'paused', recipient_count: 120, sent_count: 88, updated_at: '2026-10-25T10:00:00Z' },
    ], isLoading: false }),
    '@/lib/api': { useApiClient: () => ({ API_URL: '', authFetch() { throw new Error('No network in render tests'); } }) },
    '@/components/app-ui': ui,
    '@/components/campaigns/DeliveryOutcomeBar': loadTs('components/campaigns/DeliveryOutcomeBar.tsx'),
    '@/components/campaigns/DailyCapacityMeter': meterModule,
  }).default;
  return renderToStaticMarkup(React.createElement(Page));
}

test('the actual Campaigns page retains delivery progress and adds one compact Today column', () => {
  const markup = campaignListMarkup();
  assert.match(markup, />Delivery<\/th><th[^>]*>Today<\/th><th[^>]*>Updated<\/th>/);
  assert.equal((markup.match(/class="app-daily-capacity"/g) || []).length, 3);
  assert.equal((markup.match(/class="app-delivery-progress"/g) || []).length, 3);
});

if (process.argv.includes('--render')) {
  const output = path.resolve(__dirname, '../artifacts/capacity-qa');
  fs.mkdirSync(output, { recursive: true });
  const css = fs.readFileSync(path.join(web, 'src/components/app-ui.css'), 'utf8');
  fs.writeFileSync(path.join(output, 'index.html'), `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>:root{--font-geist-sans:Arial}*{box-sizing:border-box}body{margin:0;background:#fff}button,input,select{font:inherit}a{color:inherit;text-decoration:none}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}${css}</style></head><body class="app-ui app-shell"><header class="app-header"><b style="color:var(--app-blue)">OUTREACH</b><span>Campaigns</span></header>${campaignListMarkup()}</body></html>`);
}
