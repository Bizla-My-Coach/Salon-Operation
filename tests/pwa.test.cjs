const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const installSource = fs.readFileSync(path.join(__dirname, '../static/js/pwa.js'), 'utf8');
const workerSource = fs.readFileSync(path.join(__dirname, '../static/pwa/service-worker.js'), 'utf8');

function installFixture({ secure = true, standalone = false } = {}) {
  const handlers = {}, registrations = [];
  const buttons = [0, 1].map(() => ({ hidden: false, addEventListener(type, fn) { this[type] = fn; } }));
  const statuses = [{ textContent: '' }, { textContent: '' }];
  const navigator = { serviceWorker: { register: async (...args) => registrations.push(args) } };
  const window = { navigator, isSecureContext: secure, matchMedia: () => ({ matches: standalone }), addEventListener: (type, fn) => { handlers[type] = fn; } };
  vm.runInNewContext(installSource, { window, navigator, document: { querySelectorAll: selector => selector === '[data-install-app]' ? buttons : statuses } });
  return { handlers, registrations, buttons, statuses };
}

test('worker registers for the whole app only on secure origins', async () => {
  const f = installFixture();
  assert.ok(f.buttons.every(b => b.hidden));
  await f.handlers.load();
  assert.equal(f.registrations[0][0], '/service-worker.js');
  assert.equal(f.registrations[0][1].scope, '/');
  assert.equal(f.registrations[0][1].updateViaCache, 'none');
  assert.equal(installFixture({ secure: false }).handlers.load, undefined);
});

test('native install availability shows both menu buttons and prompts only once', async () => {
  const f = installFixture();
  let prompted = 0, prevented = false;
  f.handlers.beforeinstallprompt({ preventDefault() { prevented = true; }, prompt: async () => { prompted++; }, userChoice: Promise.resolve({ outcome: 'accepted' }) });
  assert.ok(prevented);
  assert.ok(f.buttons.every(b => !b.hidden));
  f.buttons[0].click();
  f.buttons[1].click();
  await new Promise(setImmediate);
  assert.equal(prompted, 1);
  assert.ok(f.buttons.every(b => b.hidden));
  f.handlers.appinstalled();
  assert.ok(f.statuses.every(s => !s.textContent));
});

test('installed apps do not offer installation again', () => {
  const f = installFixture({ standalone: true });
  f.handlers.beforeinstallprompt({ preventDefault() { throw new Error('Already installed'); } });
  assert.ok(f.buttons.every(b => b.hidden));
});

test('dismissing installation leaves clear instructions for trying later', async () => {
  const f = installFixture();
  f.handlers.beforeinstallprompt({ preventDefault() {}, prompt: async () => {}, userChoice: Promise.resolve({ outcome: 'dismissed' }) });
  f.buttons[0].click();
  await new Promise(setImmediate);
  assert.ok(f.statuses.every(s => s.textContent.includes('Chrome’s menu')));
});

function workerFixture({ failNetwork = false } = {}) {
  const handlers = {}, added = [], deleted = [], requests = [];
  const offline = { offline: true }, network = { online: true };
  const self = { addEventListener: (type, fn) => { handlers[type] = fn; }, skipWaiting: async () => {}, clients: { claim: async () => {} } };
  const caches = {
    open: async () => ({ add: async request => added.push(request) }),
    keys: async () => ['hairship-offline-v0', 'hairship-offline-v1', 'unrelated-app-cache'],
    delete: async key => deleted.push(key), match: async url => { assert.equal(url, '/offline/'); return offline; },
  };
  class Request { constructor(url, options) { this.url = url; Object.assign(this, options); } }
  vm.runInNewContext(workerSource, { self, caches, Request, Response, fetch: async request => { requests.push(request); if (failNetwork) throw new Error('Offline'); return network; } });
  async function lifecycle(type) { let pending; handlers[type]({ waitUntil(promise) { pending = promise; } }); await pending; }
  return { handlers, added, deleted, requests, offline, network, lifecycle };
}

test('worker caches only the anonymous reconnect page and cleans only its own old caches', async () => {
  const f = workerFixture();
  await f.lifecycle('install');
  assert.equal(f.added.length, 1);
  assert.equal(f.added[0].url, '/offline/');
  assert.equal(f.added[0].credentials, 'omit');
  await f.lifecycle('activate');
  assert.deepEqual(f.deleted, ['hairship-offline-v0']);
});

test('live pages always use the network, and disconnected navigation gets the reconnect page', async () => {
  for (const failNetwork of [false, true]) {
    const f = workerFixture({ failNetwork });
    let response;
    f.handlers.fetch({ request: { method: 'GET', mode: 'navigate', url: '/employee/' }, respondWith(promise) { response = promise; } });
    assert.equal(await response, failNetwork ? f.offline : f.network);
    assert.equal(f.requests.length, 1);
    assert.equal(f.added.length, 0);
  }
});

test('worker never intercepts updates or background requests', () => {
  const f = workerFixture();
  for (const request of [{ method: 'POST', mode: 'navigate' }, { method: 'POST', mode: 'cors' }, { method: 'GET', mode: 'cors' }]) {
    f.handlers.fetch({ request, respondWith() { throw new Error('Must reach the server directly'); } });
  }
  assert.equal(f.requests.length, 0);
});
