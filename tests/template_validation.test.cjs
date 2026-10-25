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
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText, filename);
  return mod.exports;
}

const variables = loadTs('lib/templateVariables.ts');
const summary = {
  total_contacts: 3,
  all_columns: ['email', 'first_name', 'company'],
  used_warnings: [],
  other_warnings: [{ column: 'first_name', empty_count: 2 }],
};

test('inserting a known field with missing values warns before the draft is saved', () => {
  assert.deepEqual(variables.templateValueWarnings(['Hello', 'Hi {{first_name}}'], summary), [
    { column: 'first_name', empty_count: 2 },
  ]);
  assert.deepEqual(variables.templateValueWarnings(['Hello', 'Hi there'], summary), []);
});

test('missing headers are distinguished by exact name and repeated fields count once', () => {
  assert.deepEqual(variables.templateValueWarnings(['{{ First Name }}', '{{First Name}}'], summary), [
    { column: 'First Name', empty_count: 3 },
  ]);
});

test('complete fields and built-in email do not produce missing-data warnings', () => {
  assert.deepEqual(variables.templateValueWarnings(['{{company}} {{email}}'], summary), []);
});

test('recipient validation detects absent, null, and whitespace values without rejecting zero or false', () => {
  assert.deepEqual(variables.missingTemplateVariables(
    '{{absent}} {{blank}} {{space}} {{nil}} {{zero}} {{false}} {{space}}',
    { blank: '', space: ' \t\n', nil: null, zero: 0, false: false },
  ), ['absent', 'blank', 'space', 'nil']);
  assert.deepEqual(variables.missingTemplateVariables('{{toString}}', {}), ['toString']);
});

function previewMarkup(entry, state = {}) {
  // Keep a filled test address so button assertions exercise validation, not the empty-address guard.
  const reactWithAddress = {
    ...React,
    useState(initial) { return React.useState(initial === '' ? 'test@example.test' : initial); },
  };
  const tag = name => ({ children }) => React.createElement(name, null, children);
  const PreviewDialog = loadTs('components/campaigns/dialogs/PreviewDialog.tsx', {
    react: reactWithAddress,
    swr: () => ({ data: { items: [entry], total: 1 }, isLoading: false, isValidating: false, ...state }),
    '@/lib/api': { API_URL: '', useApiClient: () => ({ authFetch: () => { throw new Error('No network in render tests'); } }) },
    '@/lib/utils': { cn: (...args) => args.filter(Boolean).join(' ') },
    '@/components/ui/button': { Button: props => React.createElement('button', props) },
    '@/components/ui/input': { Input: props => React.createElement('input', props) },
    '@/components/ui/dialog': Object.fromEntries(
      ['Dialog', 'DialogContent', 'DialogHeader', 'DialogTitle', 'DialogDescription']
        .map(name => [name, tag('div')]),
    ),
    '@/components/campaigns/workspace/emailPreviewDocument': { emailPreviewDocument: body => body },
  }).default;
  return renderToStaticMarkup(React.createElement(PreviewDialog, {
    isOpen: true, onClose() {}, campaignId: '42',
  }));
}

const completePreview = {
  id: 7, recipient_email: 'lead@example.test', subject: 'Hello', body: 'Hi Alex',
  attachments: [], missing_variables: [], can_send: true,
};

test('an incomplete preview visibly names the missing field and disables test sending', () => {
  const markup = previewMarkup({ ...completePreview, body: 'Hi [missing first_name]', missing_variables: ['first_name'], can_send: false });
  assert.match(markup, /role="alert"/);
  assert.match(markup, /sending blocked/);
  assert.match(markup, /\{\{first_name\}\}/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*>Send test<\/button>/);
});

test('test sending stays blocked if preview validation metadata is absent', () => {
  const { can_send, missing_variables, ...oldPreview } = completePreview;
  const markup = previewMarkup(oldPreview);
  assert.match(markup, /could not be verified/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*>Send test<\/button>/);
});

test('a fully populated, verified preview can send a test', () => {
  const markup = previewMarkup(completePreview);
  assert.doesNotMatch(markup, /role="alert"/);
  const button = markup.match(/<button[^>]*>Send test<\/button>/)[0];
  assert.doesNotMatch(button, /disabled/);
});

test('test sending is disabled while the recipient preview is being refreshed', () => {
  const markup = previewMarkup(completePreview, { isValidating: true });
  assert.match(markup, /<button[^>]*disabled=""[^>]*>Send test<\/button>/);
});
