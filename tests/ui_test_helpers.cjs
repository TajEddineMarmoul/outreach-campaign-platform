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
function harness() {
  const states = new Map();
  let current;
  const hooks = { ...React, useState(initial) {
    const { cells } = current;
    const index = current.cursor++;
    if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial;
    return [cells[index], value => { cells[index] = typeof value === 'function' ? value(cells[index]) : value; }];
  }, useEffect() {}, useCallback: fn => fn, useMemo: fn => fn(),
    useRef: initial => hooks.useState(() => ({ current: initial }))[0],
    useId: () => hooks.useState(() => `fixture-${states.size}-${current.cursor}`)[0] };
  function expand(element, path = 'root') {
    if (element == null || typeof element !== 'object') return element;
    if (Array.isArray(element)) return element.map((item, index) => expand(item, `${path}.${index}`));
    if (element.type === React.Suspense || element.type === React.Fragment) return expand(element.props.children, path);
    if (typeof element.type === 'function') {
      const key = `${path}.${element.type.name}.${element.key || ''}`;
      if (!states.has(key)) states.set(key, []);
      const previous = current;
      current = { cells: states.get(key), cursor: 0 };
      const result = element.type(element.props);
      current = previous;
      return expand(result, `${path}.render`);
    }
    if (typeof element.type !== 'string') return element;
    return React.cloneElement(element, {}, ...React.Children.toArray(expand(element.props.children, `${path}.children`)));
  }
  const all = (tree, match) => {
    const result = [];
    function visit(item) {
      if (!item || typeof item !== 'object') return;
      if (Array.isArray(item)) return item.forEach(visit);
      if (match(item)) result.push(item);
      visit(item.props?.children);
    }
    visit(tree); return result;
  };
  const text = element => renderToStaticMarkup(element).replace(/<[^>]+>/g, '');
  return { hooks, render: (Component, props) => expand(React.createElement(Component, props)), all, text,
    button: (tree, label) => all(tree, node => node.type === 'button' && (node.props['aria-label'] === label || text(node) === label))[0] };
}
module.exports = { loadTs, harness, React, renderToStaticMarkup };
