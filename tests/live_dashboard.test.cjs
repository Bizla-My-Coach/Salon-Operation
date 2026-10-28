const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(response, interval = 5000) {
  let callback, delay, calls = 0, options, url;
  const listeners = {}, rootListeners = {};
  const form = {};
  const region = {
    dataset: {liveRegion: 'page'}, innerHTML: 'old', focused: false,
    contains() { return this.focused || (this.dirty && arguments[0] === form); },
    querySelector() { return null; }, querySelectorAll: () => [],
    replaceChildren(...nodes) { this.innerHTML = nodes.join(''); }
  };
  const root = {
    dataset: {livePage: '/employee/', liveInterval: String(interval)},
    querySelector: () => region, querySelectorAll: () => [region],
    addEventListener: (event, fn) => { rootListeners[event] = fn; }
  };
  const status = {};
  const document = {
    hidden: false, activeElement: null,
    querySelector: (selector) => selector === '[data-live-status]' ? status : root,
    addEventListener: (event, fn) => { listeners[event] = fn; }
  };
  vm.runInNewContext(fs.readFileSync('static/js/live-dashboard.js', 'utf8'), {
    document, window: {scrollTo() {}, location: {href: 'http://localhost/employee/?branch=2&q=hair&page=3'}, addEventListener() {}},
    setTimeout(fn, ms) { callback = fn; delay = ms; return 1; }, clearTimeout() {},
    AbortController, queueMicrotask, fetch: async (address, settings) => { calls++; url = address; options = settings; return response; },
    DOMParser: class { parseFromString() { return {querySelector: () => ({
      dataset: root.dataset, querySelectorAll: () => [{
        dataset: region.dataset, innerHTML: 'new', childNodes: ['new'], querySelectorAll: () => []
      }]
    })}; } }
  });
  return {region, form, status, document, listeners, rootListeners,
    run: () => callback(), calls: () => calls, delay: () => delay,
    request: () => ({url, options}), input: () => { region.dirty = true; rootListeners.input({target: {closest: () => form}}); }};
}
const success = () => ({ok: true, text: async () => 'html'});
test('refreshes staff assignments without navigation and retains filters', async () => {
  const app = setup(success());
  assert.equal(app.delay(), 5000);
  await app.run();
  assert.equal(app.region.innerHTML, 'new');
  assert.match(app.status.textContent, /last updated/);
  assert.equal(app.request().url, 'http://localhost/employee/?branch=2&q=hair&page=3');
  assert.equal(app.request().options.cache, 'no-store');
  assert.equal(app.request().options.credentials, 'same-origin');
});
test('reports poll every thirty seconds', async () => {
  const app = setup(success(), 30000);
  await app.run();
  assert.equal(app.delay(), 30000);
});
test('preserves focused interactions', async () => {
  const app = setup(success());
  app.region.focused = true;
  await app.run();
  assert.equal(app.region.innerHTML, 'old');
  assert.match(app.status.textContent, /paused/);
});
test('dirty forms stay protected after focus leaves; reset resumes updates', async () => {
  const app = setup(success());
  app.input();
  await app.run();
  assert.equal(app.region.innerHTML, 'old');
  app.rootListeners.reset({target: app.form});
  await app.run();
  assert.equal(app.region.innerHTML, 'new');
});
test('hidden tabs pause and becoming visible refreshes immediately', async () => {
  const app = setup(success());
  app.document.hidden = true;
  await app.run();
  assert.equal(app.calls(), 0);
  app.document.hidden = false;
  await app.listeners.visibilitychange();
  assert.equal(app.region.innerHTML, 'new');
});
test('submission prevents a response replacing active controls', async () => {
  const app = setup(success());
  app.listeners.submit({defaultPrevented: false});
  await app.run();
  assert.equal(app.calls(), 0);
});
test('failed requests retain old data and back off', async () => {
  const app = setup({ok: false, status: 500});
  await app.run();
  assert.equal(app.region.innerHTML, 'old');
  assert.match(app.status.textContent, /out of date/);
  assert.equal(app.delay(), 10000);
});
test('expired session clears sensitive data and stops polling', async () => {
  const app = setup({redirected: true});
  await app.run();
  assert.equal(app.region.innerHTML, '');
  assert.match(app.status.textContent, /Reload/);
  await app.run();
  assert.equal(app.calls(), 1);
});

test('background form submissions do not stop polling', async () => {
  const app = setup(success());
  app.listeners.submit({defaultPrevented: true});
  await app.run();
  assert.equal(app.region.innerHTML, 'new');
});

test('service controls still work for tasks inserted by polling', () => {
  const handlers = {}, panel = {hidden: true, reset() { this.wasReset = true; }};
  vm.runInNewContext(fs.readFileSync('static/js/service-controls.js', 'utf8'), {
    document: {addEventListener: (name, fn) => { handlers[name] = fn; }, getElementById: () => panel}
  });
  handlers.click({target: {dataset: {skipToggle: 'task-new'}}});
  assert.equal(panel.hidden, false);
  handlers.click({target: {dataset: {skipCancel: 'task-new'}}});
  assert.equal(panel.hidden, true);
  assert.equal(panel.wasReset, true);
  const other = {value: 'old reason'};
  const select = {matches: () => true, value: 'OTHER', closest: () => ({querySelector: () => other})};
  handlers.change({target: select});
  assert.equal(other.hidden, false);
  assert.equal(other.required, true);
  select.value = 'Not applicable';
  handlers.change({target: select});
  assert.equal(other.hidden, true);
  assert.equal(other.required, false);
  assert.equal(other.value, '');
});
