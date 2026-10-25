const { test } = require('node:test');
const assert = require('node:assert/strict');
const { loadTs, harness, React, renderToStaticMarkup } = require('./ui_test_helpers.cjs');
const variables = loadTs('lib/templateVariables.ts');
const mapping = loadTs('lib/templateMapping.ts', { './templateVariables': variables });
const summary = { total_contacts: 10, all_columns: ['company', 'First Name', 'email'], used_warnings: [], other_warnings: [{ column: 'company', empty_count: 3 }] };
const template = { id: 1, title: 'Good email', subject: 'Hi {{first_name}}', body: '<p><strong>{{first_name}}</strong> at {{company}}</p><p><a href="https://example.test">More</a></p>' };
const fields = names => names.map(name => ({ name, total: 10, missing: 0 }));

test('mapping suggestions normalize case, separators, camel case, and unambiguous initials', () => {
  assert.deepEqual(mapping.suggestVariableMapping(['first_name', 'company-name'], fields(['First Name', 'companyName'])), { first_name: 'First Name', 'company-name': 'companyName' });
  assert.equal(mapping.suggestVariableMapping(['fname'], fields(['first_name'])).fname, 'first_name');
  assert.equal(mapping.suggestVariableMapping(['first_name'], fields(['fname'])).first_name, 'fname');
  assert.equal(mapping.suggestVariableMapping(['customerReference'], fields(['creference'])).customerReference, 'creference');
});

test('suggestions never guess generic name or pick between ambiguous field names', () => {
  for (const source of ['first_name', 'company_name']) assert.equal(mapping.suggestVariableMapping([source], fields(['name']))[source], '');
  assert.equal(mapping.suggestVariableMapping(['fname'], fields(['first_name', 'full_name'])).fname, '');
  assert.equal(mapping.suggestVariableMapping(['firstName'], fields(['First Name', 'first_name'])).firstName, '');
  assert.equal(mapping.suggestVariableMapping(['first_name'], fields(['first_name', 'First Name'])).first_name, 'first_name');
  assert.equal(mapping.suggestVariableMapping(['first_name'], fields(['full_name'])).first_name, '');
});

test('complete fields come first and missing counts remain exact', () => {
  const options = mapping.mappingFields(summary);
  assert.deepEqual(options.map(field => field.name), ['email', 'First Name', 'company']);
  assert.equal(options.at(-1).missing, 3);
  assert.equal(options.at(-1).total, 10);
});

test('remapping is simultaneous, exact, and preserves HTML, links, and ordinary text', () => {
  const source = '<p><b>{{ A }}</b> A {{B}}</p><a href="{{url}}">Read</a>';
  assert.equal(mapping.remapTemplateText(source, { A: 'B', B: 'C', url: 'website' }), '<p><b>{{ B }}</b> A {{ C }}</p><a href="{{ website }}">Read</a>');
  assert.deepEqual(mapping.templateVariables(template), ['first_name', 'company']);
  assert.equal(mapping.remapTemplateText('{{__proto__}}', {}), '{{__proto__}}');
  assert.equal(mapping.remapTemplateText('{{first_name}}', {}), '{{first_name}}');
});

function fixture() {
  const f = harness();
  const requests = [];
  const saved = [];
  const api = { useApiClient: () => ({ API_URL: '', authFetch: async (url, options) => { requests.push({ url, ...options }); return { json: async () => ({ id: 9 }) }; } }),
    checkResponse: async response => response.json(), errorMessage: error => error.message };
  const ui = loadTs('components/app-ui.tsx', { '@/components/ui/dialog': {} });
  ui.AppDialog = ({ open, children, title }) => open ? React.createElement('section', { role: 'dialog', 'aria-label': title }, children) : null;
  ui.ActionMenu = ({ children }) => React.createElement('div', {}, children);
  ui.MenuAction = ({ children, onClick }) => React.createElement('button', { onClick }, children);
  ui.ConfirmDialog = () => null;
  const swr = () => ({ data: [template], isLoading: false }); swr.mutate = async () => {};
  const aliases = { react: f.hooks, swr, '@/lib/api': api, '@/components/app-ui': ui,
    '@/lib/templateVariables': variables, '@/lib/templateMapping': mapping, './templates.css': {}, '@/components/templates/templates.css': {},
    '@/components/EmailComposer': ({ subject, body }) => React.createElement('article', { 'data-preview-subject': subject, 'data-preview-body': body }) };
  const Select = loadTs('components/templates/VariableMappingSelect.tsx', aliases).default;
  const Load = loadTs('components/templates/LoadTemplateDialog.tsx', { ...aliases, './VariableMappingSelect': Select }).default;
  const Save = loadTs('components/templates/SaveTemplateDialog.tsx', aliases).default;
  const Library = loadTs('app/templates/page.tsx', aliases).default;
  const props = { summary, onRetry() {}, onClose() {}, onLoad: (...values) => saved.push(values) };
  return { ...f, Load, Save, Select, Library, props, requests, saved };
}

test('loading shows old placeholders, suggested fields and red missing values before applying', () => {
  const f = fixture();
  let tree = f.render(f.Load, f.props);
  f.all(tree, node => node.type === 'button' && f.text(node).includes('Good email'))[0].props.onClick();
  tree = f.render(f.Load, f.props);
  assert.equal(f.saved.length, 0);
  assert.match(renderToStaticMarkup(tree), /\{\{first_name\}\}/);
  const selects = f.all(tree, node => node.props?.role === 'combobox');
  assert.equal(selects[0].props.value, 'First Name');
  assert.equal(selects[1].props.value, 'company');
  assert.match(renderToStaticMarkup(tree), /has-missing/);
  assert.match(renderToStaticMarkup(tree), /Missing for 3 of 10 recipients/);
  assert.equal(f.button(tree, 'Load into campaign').props.disabled, false);
  f.button(tree, 'Load into campaign').props.onClick();
  assert.equal(f.saved[0][0], 'Hi {{ First Name }}');
  assert.match(f.saved[0][1], /<strong>\{\{ First Name \}\}<\/strong>/);
  assert.match(f.saved[0][1], /href="https:\/\/example.test"/);
  assert.equal(template.subject, 'Hi {{first_name}}');
});

test('unresolved mappings block loading until explicitly selected; unavailable validation stays blocked', () => {
  const f = fixture();
  const props = { ...f.props, summary: { ...summary, all_columns: ['email'] } };
  let tree = f.render(f.Load, props);
  f.all(tree, node => node.type === 'button' && f.text(node).includes('Good email'))[0].props.onClick();
  tree = f.render(f.Load, props);
  assert.equal(f.button(tree, 'Load into campaign').props.disabled, true);
  const unavailable = f.render(f.Load, { ...props, validationError: true });
  assert.equal(f.button(unavailable, 'Load into campaign').props.disabled, true);
});

test('mapping select searches and supports keyboard selection with complete options first', () => {
  const f = fixture();
  let chosen;
  const props = { source: 'old', value: '', fields: mapping.mappingFields(summary), onChange: value => { chosen = value; } };
  let tree = f.render(f.Select, props);
  let input = f.all(tree, node => node.props?.role === 'combobox')[0];
  input.props.onFocus(); tree = f.render(f.Select, props);
  const options = f.all(tree, node => node.props?.role === 'option');
  assert.match(f.text(options[0]), /email/);
  assert.match(options.at(-1).props.className, /has-missing/);
  input = f.all(tree, node => node.props?.role === 'combobox')[0];
  input.props.onChange({ target: { value: 'first' } }); tree = f.render(f.Select, props);
  assert.equal(f.all(tree, node => node.props?.role === 'option').length, 1);
  input = f.all(tree, node => node.props?.role === 'combobox')[0];
  input.props.onKeyDown({ key: 'Enter', preventDefault() {} });
  assert.equal(chosen, 'First Name');
});

test('save from campaign sends the current subject, rich body, and placeholders unchanged', async () => {
  const f = fixture();
  let saved = false;
  const tree = f.render(f.Save, { subject: template.subject, body: template.body, defaultName: 'Good email', onClose() {}, onSaved() { saved = true; } });
  const form = f.all(tree, node => node.type === 'form')[0];
  await form.props.onSubmit({ preventDefault() {} });
  assert.equal(saved, true);
  assert.equal(f.requests[0].url, '/api/templates');
  assert.deepEqual(JSON.parse(f.requests[0].body), { title: 'Good email', subject: template.subject, body: template.body });
});

test('template library opens the same composer with the saved subject and rich body', () => {
  const f = fixture();
  let tree = f.render(f.Library, {});
  f.button(tree, 'Good email').props.onClick();
  tree = f.render(f.Library, {});
  const composer = f.all(tree, node => node.type === 'article')[0];
  assert.equal(composer.props['data-preview-subject'], template.subject);
  assert.equal(composer.props['data-preview-body'], template.body);
  assert.ok(f.button(tree, 'Add to subject'));
  assert.ok(f.button(tree, 'Add to message'));
  assert.ok(f.button(tree, 'Save template'));
  assert.equal(f.all(tree, node => node.type === 'textarea').length, 0);
});
