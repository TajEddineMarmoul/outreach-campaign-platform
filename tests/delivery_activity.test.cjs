const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const web = path.resolve(__dirname, '../outreach_web');
const ts = Module.createRequire(path.join(web, 'package.json'))('typescript');
const filename = path.join(web, 'src/lib/deliveryActivity.ts');
const mod = new Module(filename);
mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, filename);
const { deliveryActivity } = mod.exports;

test('undelivered takes priority over opens, clicks, and response badges', () => {
  const activity = deliveryActivity({
    status: 'bounced', response_status: 'automated_response', open_count: 9, click_count: 2,
    bounced_at: '2026-10-21T18:23:06Z', responded_at: '2026-10-21T18:24:00Z',
    last_clicked_at: '2026-10-21T18:25:00Z',
  });
  assert.equal(activity.kind, 'bounced');
  assert.equal(activity.label, 'Email was undelivered to');
  assert.equal(activity.at, '2026-10-21T18:23:06Z');
});

test('send errors cannot be presented as opened or clicked', () => {
  for (const status of ['failed', 'error']) {
    assert.equal(deliveryActivity({ status, open_count: 1, click_count: 1 }).kind, 'failed');
  }
});

test('healthy replies and tracking use the matching activity time', () => {
  assert.equal(deliveryActivity({status:'sent', response_status:'replied', open_count:1}).kind, 'replied');
  const activity = deliveryActivity({status:'sent', click_count:1, last_clicked_at:'2026-10-21T18:25:00Z'});
  assert.equal(activity.kind, 'clicked');
  assert.equal(activity.at, '2026-10-21T18:25:00Z');
  assert.match(activity.label, /tracking recorded/);
});
