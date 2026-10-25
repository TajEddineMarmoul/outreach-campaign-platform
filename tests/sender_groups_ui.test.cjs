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

const accounts = [
  { id: 11, group_id: 1, group_ids: [1, 2], email: 'first@example.test', display_name: 'First account', daily_cap: 40, sent_today: 5, status: 'connected', gmail_tracking_enabled: true },
  { id: 22, group_id: 1, group_ids: [1], email: 'second@example.test', display_name: 'Second account', daily_cap: 20, sent_today: 2, status: 'connected', gmail_tracking_enabled: true },
  { id: 33, group_id: null, group_ids: [], email: 'saved@example.test', display_name: 'Saved account', daily_cap: 10, sent_today: 0, status: 'connected' },
];
const groups = [
  { id: 1, name: 'Hiring team', senders: accounts.slice(0, 2), connected_sender_count: 2, sender_count: 2 },
  { id: 2, name: 'Founder outreach', senders: accounts.slice(0, 1), connected_sender_count: 1, sender_count: 1 },
];

function fixture() {
  const cells = [];
  let cursor = 0;
  const hooks = { ...React, useState: initial => {
    const index = cursor++;
    if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial;
    return [cells[index], value => { cells[index] = typeof value === 'function' ? value(cells[index]) : value; }];
  }, useEffect() {} };
  const requests = [];
  let refreshed = 0;
  const api = {
    API_URL: '', useApiClient: () => ({ API_URL: '', authFetch: async (url, options) => {
      requests.push({ url, ...options });
      return { ok: true, json: async () => ({ ...groups[1], auth_url: 'https://accounts.google.com/test' }) };
    } }),
    checkResponse: async response => response.json(), errorMessage: error => error.message,
  };
  const swr = url => ({ data: url.endsWith('/api/senders') ? accounts : groups, isLoading: false, mutate: async () => { refreshed++; } });
  swr.mutate = async () => { refreshed++; };
  const realUi = loadTs('components/app-ui.tsx', { '@/components/ui/dialog': {} });
  const ui = { ...realUi,
    ActionMenu: ({ label, children }) => React.createElement('div', { 'aria-label': label }, children),
    MenuAction: ({ children, onClick, disabled }) => React.createElement('button', { onClick, disabled }, children),
    AppDialog: ({ open, title, children }) => open ? React.createElement('section', { role: 'dialog', 'aria-label': title }, children) : null,
    ConfirmDialog: () => null,
  };
  const aliases = { react: hooks, swr, '@/lib/api': api, '@/components/app-ui': ui,
    'next/navigation': { useSearchParams: () => new URLSearchParams() }, '@/lib/analytics': { trackEventOnce() {} } };
  const Table = loadTs('components/senders/SenderAccountsTable.tsx', aliases).default;
  const Add = loadTs('components/senders/AddSenderDialog.tsx', aliases).default;
  const Page = loadTs('app/senders/page.tsx', { ...aliases, '@/components/senders/AddSenderDialog': Add, '@/components/senders/SenderAccountsTable': Table }).default;
  function expand(element) {
    if (element == null || typeof element !== 'object') return element;
    if (Array.isArray(element)) return element.map(expand);
    if (element.type === React.Suspense || element.type === React.Fragment) return expand(element.props.children);
    if (typeof element.type === 'function') return expand(element.type(element.props));
    // Lucide icons use forwardRef. Let React render those without hooks.
    if (typeof element.type !== 'string') return element;
    return React.cloneElement(element, {}, ...React.Children.toArray(expand(element.props.children)));
  }
  const render = (component = Page, props = {}) => { cursor = 0; return expand(React.createElement(component, props)); };
  const all = (tree, match) => {
    const result = [];
    function visit(item) {
      if (!item || typeof item !== 'object') return;
      if (Array.isArray(item)) return item.forEach(visit);
      if (match(item)) result.push(item);
      visit(item.props?.children);
    }
    visit(tree);
    return result;
  };
  const label = node => renderToStaticMarkup(node).replace(/<[^>]+>/g, '');
  const button = (tree, name) => all(tree, node => node.type === 'button' && (node.props['aria-label'] === name || label(node) === name))[0];
  return { render, all, button, label, Page, Add, Table, requests, refreshed: () => refreshed };
}

test('group cards start collapsed, show counts, and expand independently', () => {
  const f = fixture();
  let tree = f.render();
  const toggles = () => f.all(tree, node => node.type === 'button' && String(node.props['aria-controls']).startsWith('sender-group-'));
  assert.equal(toggles().length, 2);
  assert.deepEqual(toggles().map(node => node.props['aria-expanded']), [false, false]);
  assert.match(f.label(toggles()[0]), /Hiring team2 accounts · 2 connected/);
  assert.equal(f.all(tree, node => node.type === 'table').length, 0);
  toggles()[0].props.onClick(); tree = f.render();
  assert.deepEqual(toggles().map(node => node.props['aria-expanded']), [true, false]);
  toggles()[1].props.onClick(); tree = f.render();
  assert.deepEqual(toggles().map(node => node.props['aria-expanded']), [true, true]);
  toggles()[0].props.onClick(); tree = f.render();
  assert.deepEqual(toggles().map(node => node.props['aria-expanded']), [false, true]);
});

test('group removal uses only the membership endpoint and keeps shared quota visible', async () => {
  const f = fixture();
  let tree = f.render();
  f.all(tree, node => node.props?.['aria-controls'] === 'sender-group-1')[0].props.onClick();
  tree = f.render();
  assert.match(renderToStaticMarkup(tree), /Shared daily limit/);
  assert.match(renderToStaticMarkup(tree), /5 of 40 emails sent today/);
  f.button(tree, 'Remove from this group').props.onClick();
  await new Promise(setImmediate);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].url, '/api/sender-groups/1/senders/11');
  assert.equal(f.requests[0].method, 'DELETE');
  assert.ok(f.refreshed() > 0);
});

test('saved account chooser excludes existing members and attaches selected IDs without OAuth', async () => {
  const f = fixture();
  const props = { group: groups[1], onClose() {}, onAdded: async () => {} };
  let tree = f.render(f.Add, props);
  assert.equal(f.button(tree, 'Use saved account').props['aria-pressed'], true);
  assert.ok(f.button(tree, 'Connect new account'));
  const choices = f.all(tree, node => node.type === 'input' && node.props.type === 'checkbox');
  assert.equal(choices.length, 2);
  assert.doesNotMatch(renderToStaticMarkup(tree), /first@example.test/);
  choices[0].props.onChange({ target: { checked: true } });
  tree = f.render(f.Add, props);
  await f.button(tree, 'Add 1 account').props.onClick();
  await new Promise(setImmediate);
  assert.equal(f.requests[0].url, '/api/sender-groups/2/senders');
  assert.deepEqual(JSON.parse(f.requests[0].body), { sender_ids: [22] });
  assert.equal(f.requests.length, 1);
});

test('new account option starts the existing Google connection flow for this group', async () => {
  const f = fixture();
  const props = { group: groups[1], initialMode: 'new', returnPath: '/campaigns/7?step=senders&sender_setup=1', onClose() {}, onAdded: async () => {} };
  const originalWindow = global.window;
  let location, savedGroup;
  global.window = { sessionStorage: { setItem: (key, value) => { savedGroup = [key, value]; } }, location: { assign: value => { location = value; } } };
  try {
    const tree = f.render(f.Add, props);
    await f.button(tree, 'Continue to Google').props.onClick();
    await new Promise(setImmediate);
    assert.equal(f.requests[0].url, '/api/sender-groups/2/senders/oauth/start');
    assert.equal(JSON.parse(f.requests[0].body).return_path, '/campaigns/7?step=senders&sender_setup=1');
    assert.deepEqual(savedGroup, ['pending_sender_group_id', '2']);
    assert.equal(location, 'https://accounts.google.com/test');
  } finally { global.window = originalWindow; }
});

test('failed campaign save prevents leaving the page for Google', async () => {
  const f = fixture();
  const props = { group: groups[1], initialMode: 'new', beforeConnect: async () => { throw new Error('Save failed'); }, onClose() {}, onAdded: async () => {} };
  let tree = f.render(f.Add, props);
  f.button(tree, 'Continue to Google').props.onClick();
  await new Promise(setImmediate);
  tree = f.render(f.Add, props);
  assert.equal(f.requests.length, 0);
  assert.match(renderToStaticMarkup(tree), /Save failed/);
});

test('saved account library includes accounts with no group and reserves global disconnect for that library', () => {
  const f = fixture();
  let tree = f.render();
  assert.equal(f.button(tree, 'Disconnect from all groups'), undefined);
  f.button(tree, 'Saved accounts (3)').props.onClick();
  tree = f.render();
  assert.match(renderToStaticMarkup(tree), /saved@example.test/);
  assert.ok(f.button(tree, 'Disconnect from all groups'));
});

if (process.argv.includes('--render')) {
  const f = fixture();
  let tree = f.render();
  f.all(tree, node => node.props?.['aria-controls'] === 'sender-group-1')[0].props.onClick();
  tree = f.render();
  // Real menus remain closed in the visual fixture; action behavior is tested above.
  const html = renderToStaticMarkup(tree).replace(/<div aria-label="Actions for [^"]+">[\s\S]*?<\/div>/g, '<button class="app-button is-icon is-quiet" aria-label="Account actions">⋮</button>');
  const output = path.resolve(__dirname, '../artifacts/senders-qa');
  fs.mkdirSync(output, { recursive: true });
  const css = fs.readFileSync(path.join(web, 'src/components/app-ui.css'), 'utf8');
  fs.writeFileSync(path.join(output, 'index.html'), `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>:root{--font-geist-sans:Arial}*{box-sizing:border-box}body{margin:0;background:#fff}button,input,select{font:inherit}a{color:inherit;text-decoration:none}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}${css}</style></head><body class="app-ui app-shell"><header class="app-header"><b style="color:var(--app-blue)">OUTREACH</b><span>☰ &nbsp; Menu</span></header>${html}</body></html>`);
}
