/* One fake DOM for every dashboard suite.

   Each JS suite used to hand-roll its own element stub, and every copy
   implemented only the DOM the panel happened to use when that suite was
   written. When the CN/EN switch started calling classList.toggle() at load
   time, seven unrelated suites died before their first assertion - two with
   "LOAD ERROR: classList.toggle is not a function", five with a bare Node
   crash - and the fix each time was to go and patch the same stub in ten
   files.

   This module is the single copy: a small but complete element/document/window,
   plus a permissive fallback that turns an unmodelled DOM call into a no-op and
   records it, so a new panel feature cannot take a suite down again. Suites
   assert on rendered strings, so a permissive stub costs nothing there; the
   recorded calls are available as `dom.unknown` when a suite wants to check
   that the panel stayed within the modelled surface.

   Usage:

     const dom = require('./_dom_stub.js');
     dom.installDom();                       // globals in place
     const el = dom.element('div');          // a standalone element
     const byId = dom.byId('recent');        // the same object every call

   Anything suite-specific (a querySelector that must answer, a fetch payload)
   goes in through installDom({...}) instead of a local copy of the stub.
*/
'use strict';

const unknownCalls = [];

// Set by installDom(); the module-level byId() reads it.
let installed = null;

const NOOP = () => undefined;

function makeClassList() {
  const names = new Set();
  return {
    add(...list) { list.forEach(n => names.add(n)); },
    remove(...list) { list.forEach(n => names.delete(n)); },
    contains(name) { return names.has(name); },
    toggle(name, force) {
      const on = force === undefined ? !names.has(name) : !!force;
      if (on) names.add(name); else names.delete(name);
      return on;
    },
    item(index) { return Array.from(names)[index] || null; },
    replace(oldName, newName) {
      if (!names.delete(oldName)) return false;
      names.add(newName);
      return true;
    },
    toString() { return Array.from(names).join(' '); },
    get length() { return names.size; },
  };
}

function makeStyle() {
  const values = {};
  return new Proxy(values, {
    get(target, key) {
      if (key === 'setProperty') return (name, value) => { target[name] = value; };
      if (key === 'getPropertyValue') return name => target[name] || '';
      if (key === 'removeProperty') return name => { delete target[name]; };
      if (key === 'cssText') return target.cssText || '';
      if (typeof key === 'symbol') return target[key];
      return target[key] === undefined ? '' : target[key];
    },
    set(target, key, value) { target[key] = value; return true; },
  });
}

function makeElement(tag) {
  const name = String(tag || 'div');
  const el = {
    tagName: name.toUpperCase(),
    nodeName: name.toUpperCase(),
    nodeType: 1,
    id: '',
    className: '',
    innerHTML: '',
    outerHTML: '',
    textContent: '',
    innerText: '',
    value: '',
    checked: false,
    disabled: false,
    hidden: false,
    selected: false,
    href: '',
    src: '',
    title: '',
    placeholder: '',
    type: '',
    name: '',
    style: makeStyle(),
    dataset: {},
    classList: makeClassList(),
    childNodes: [],
    children: [],
    parentNode: null,
    firstChild: null,
    lastChild: null,
    nextSibling: null,
    previousSibling: null,
    attributes: {},
    offsetWidth: 0,
    offsetHeight: 0,
    clientWidth: 0,
    clientHeight: 0,
    scrollTop: 0,
    scrollHeight: 0,
    appendChild(child) {
      if (child && typeof child === 'object') {
        el.childNodes.push(child);
        el.children.push(child);
        child.parentNode = el;
        el.firstChild = el.childNodes[0];
        el.lastChild = child;
      }
      return child;
    },
    insertBefore(child) { return el.appendChild(child); },
    replaceChild(child) { return el.appendChild(child); },
    removeChild(child) {
      const at = el.childNodes.indexOf(child);
      if (at >= 0) { el.childNodes.splice(at, 1); el.children.splice(at, 1); }
      return child;
    },
    remove() {},
    contains() { return false; },
    closest() { return null; },
    matches() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementsByTagName() { return []; },
    getElementsByClassName() { return []; },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(el.attributes, key)
        ? el.attributes[key] : null;
    },
    setAttribute(key, value) { el.attributes[key] = String(value); },
    removeAttribute(key) { delete el.attributes[key]; },
    hasAttribute(key) {
      return Object.prototype.hasOwnProperty.call(el.attributes, key);
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    click() {},
    focus() {},
    blur() {},
    submit() {},
    reset() {},
    scrollIntoView() {},
    getBoundingClientRect() {
      return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
    },
    insertAdjacentHTML() {},
    cloneNode() { return makeElement(name); },
  };
  // Anything the panel calls that this stub does not model becomes a no-op
  // instead of a TypeError that takes the whole suite down. The name is kept so
  // a suite can still see what it did not model.
  return new Proxy(el, {
    get(target, key) {
      if (typeof key === 'symbol' || key in target) return target[key];
      unknownCalls.push('element.' + String(key));
      return NOOP;
    },
    set(target, key, value) { target[key] = value; return true; },
  });
}

function makeStorage() {
  const store = new Map();
  return {
    getItem: key => (store.has(String(key)) ? store.get(String(key)) : null),
    setItem: (key, value) => { store.set(String(key), String(value)); },
    removeItem: key => { store.delete(String(key)); },
    clear: () => store.clear(),
    key: index => Array.from(store.keys())[index] || null,
    get length() { return store.size; },
  };
}

function installDom(options) {
  const opts = options || {};
  const elements = new Map();
  const byId = id => {
    const key = String(id);
    if (!elements.has(key)) {
      const el = makeElement('div');
      el.id = key;
      elements.set(key, el);
    }
    return elements.get(key);
  };
  const defaultQuery = selector => {
    const sel = String(selector || '');
    // "#name" is the one selector shape every suite uses; anything more complex
    // (a descendant selector, a class) stays null unless the suite says
    // otherwise, exactly like the old hand-written stubs.
    return /^#[\w-]+$/.test(sel) ? byId(sel.slice(1)) : null;
  };
  const documentElement = makeElement('html');
  const document = {
    readyState: 'complete',
    title: '',
    cookie: '',
    documentElement,
    body: makeElement('body'),
    head: makeElement('head'),
    getElementById: byId,
    querySelector: opts.querySelector || defaultQuery,
    querySelectorAll: opts.querySelectorAll || (() => []),
    createElement: tag => makeElement(tag),
    createDocumentFragment: () => makeElement('#fragment'),
    createTextNode: text => ({ nodeType: 3, textContent: String(text), parentNode: null }),
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    getElementsByTagName: () => [],
    getElementsByClassName: () => [],
    execCommand: () => true,
    write() {},
    open() {},
    close() {},
  };
  const location = opts.location || {
    href: 'http://127.0.0.1:8788/', search: '', hash: '', pathname: '/',
    assign() {}, replace() {}, reload() {}, toString() { return location.href; },
  };
  const storage = makeStorage();
  const window = {
    document,
    location,
    localStorage: storage,
    sessionStorage: storage,
    navigator: { userAgent: 'node', language: 'zh-CN', languages: ['zh-CN'] },
    innerWidth: 1280,
    innerHeight: 900,
    devicePixelRatio: 1,
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: () => makeStyle(),
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    alert() {},
    confirm: () => false,
    prompt: () => null,
    scrollTo() {},
    scrollBy() {},
    open: () => null,
    close() {},
    focus() {},
    blur() {},
    print() {},
  };
  window.window = window;
  window.self = window;
  window.globalThis = window;

  class MutationObserver {
    constructor(callback) { this.callback = callback; }
    observe() {}
    disconnect() {}
    takeRecords() { return []; }
  }

  const fetchImpl = opts.fetch || (() => Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve('{}'),
  }));

  // Node 21+ ships some of these as getter-only globals (navigator, and fetch
  // on older releases), so a plain assignment throws under 'use strict'. Define
  // them instead, which works on every Node the suites run on.
  const setGlobal = (name, value) => {
    try {
      global[name] = value;
      if (global[name] === value) return;
    } catch (err) { /* getter-only in this Node: fall through */ }
    Object.defineProperty(global, name, { value, configurable: true, writable: true });
  };

  setGlobal('document', document);
  setGlobal('window', window);
  setGlobal('location', location);
  setGlobal('localStorage', storage);
  setGlobal('sessionStorage', storage);
  setGlobal('navigator', window.navigator);
  setGlobal('MutationObserver', MutationObserver);
  setGlobal('getComputedStyle', window.getComputedStyle);
  setGlobal('requestAnimationFrame', window.requestAnimationFrame);
  setGlobal('cancelAnimationFrame', window.cancelAnimationFrame);
  setGlobal('setTimeout', window.setTimeout);
  setGlobal('clearTimeout', window.clearTimeout);
  setGlobal('setInterval', window.setInterval);
  setGlobal('clearInterval', window.clearInterval);
  setGlobal('alert', window.alert);
  setGlobal('confirm', window.confirm);
  setGlobal('fetch', fetchImpl);
  window.fetch = fetchImpl;

  installed = { document, window, byId, element: makeElement, unknown: unknownCalls };
  return installed;
}

// `installDom()` returns these, and they are also reachable from the module so
// a suite can write `dom.byId(id)` after installing instead of threading the
// return value through.
function byId(id) {
  return installed ? installed.byId(id) : null;
}

module.exports = { installDom, makeElement, byId, unknown: unknownCalls };
