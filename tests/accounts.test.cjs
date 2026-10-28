const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../static/js/accounts.js'), 'utf8');
const initialThemeScript = fs.readFileSync(path.join(__dirname, '../templates/base.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

test('default theme is light regardless of system preference; explicit dark preference is retained', () => {
  for (const saved of [null, 'light', 'invalid', 'dark']) {
    const document = { documentElement: { dataset: {} } };
    vm.runInNewContext(initialThemeScript, {
      document, localStorage: { getItem: () => saved },
      window: { matchMedia: () => { throw new Error('System theme must not determine default'); } },
    });
    assert.equal(document.documentElement.dataset.theme, saved === 'dark' ? 'dark' : 'light');
  }
});

function fixture(theme = 'light', storageThrows = false, options = {}) {
  function element() {
    return { handlers: {}, checked: false, open: false,
      addEventListener(type, handler) { this.handlers[type] = handler; },
      contains(target) { return target === this; },
      querySelector() { return { focus: () => { this.focused = true; } }; },
    };
  }
  const menus = [element(), element()];
  const toggles = [element(), element()];
  const statuses = [{ textContent: '' }, { textContent: '' }];
  const form = { action: '/profile/theme/', elements: { csrfmiddlewaretoken: { value: 'test-csrf' } } };
  const requests = [];
  const document = { documentElement: { dataset: { theme } }, body: { dataset: { themeKey: 'hairship-theme-7' } },
    handlers: {}, querySelectorAll(selector) { return selector === '.account-menu' ? menus : selector === '[data-theme-toggle]' ? toggles : statuses; },
    getElementById() { return options.guest ? null : form; },
    addEventListener(type, handler) { this.handlers[type] = handler; },
  };
  const stored = {};
  const window = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
  if (options.legacy) document.documentElement.dataset.themeNeedsSave = 'true';
  vm.runInNewContext(script, { document, window, localStorage: {
    setItem(key, value) { if (storageThrows) throw new Error('Denied'); stored[key] = value; },
  }, fetch: (url, request) => {
    requests.push({ url, ...request });
    return options.fetch ? options.fetch(request) : Promise.resolve({ ok: true, json: async () => ({ theme: new URLSearchParams(request.body).get('theme') }) });
  } });
  return { document, window, menus, toggles, stored, statuses, requests };
}

test('initial theme synchronizes desktop and mobile switches', () => {
  const f = fixture('dark');
  assert.ok(f.toggles.every(t => t.checked));
});
test('switch changes theme, saves per-account preference and synchronizes menus', async () => {
  const f = fixture();
  f.toggles[1].checked = true;
  await f.toggles[1].handlers.change();
  assert.equal(f.document.documentElement.dataset.theme, 'dark');
  assert.equal(f.stored['hairship-theme-7'], 'dark');
  assert.ok(f.toggles.every(t => t.checked));
  f.toggles[0].checked = false;
  await f.toggles[0].handlers.change();
  assert.equal(f.document.documentElement.dataset.theme, 'light');
  assert.equal(f.stored['hairship-theme-7'], 'light');
  assert.ok(f.toggles.every(t => !t.checked));
  assert.deepEqual(f.requests.map(r => r.body), ['theme=dark', 'theme=light']);
  assert.equal(f.requests[0].headers['X-CSRFToken'], 'test-csrf');
  assert.equal(f.requests[0].credentials, 'same-origin');
  assert.equal(f.requests[0].keepalive, true);
});
test('account theme is saved even when browser storage is unavailable', async () => {
  const f = fixture('light', true);
  f.toggles[0].checked = true;
  await f.toggles[0].handlers.change();
  assert.equal(f.document.documentElement.dataset.theme, 'dark');
  assert.equal(f.requests[0].body, 'theme=dark');
});

test('saved account preference wins over stale browser storage before the page renders', () => {
  for (const theme of ['dark', 'light']) {
    const document = { documentElement: { dataset: {} } };
    vm.runInNewContext(initialThemeScript.replace('{{ user.profile.theme|default:"" }}', theme), {
      document, localStorage: { getItem: () => { throw new Error('Should not read stale browser preferences'); } },
    });
    assert.equal(document.documentElement.dataset.theme, theme);
    assert.equal(document.documentElement.dataset.themeNeedsSave, undefined);
  }
});

test('existing browser choices are imported only when the account has no saved preference', async () => {
  const f = fixture('dark', false, { legacy: true });
  await new Promise(setImmediate);
  assert.equal(f.requests[0].body, 'theme=dark&only_if_unset=1');
  assert.ok(f.toggles.every(t => t.checked && !t.disabled));
});

test('failed account saves restore the previous choice and let the user retry', async () => {
  const f = fixture('light', false, { fetch: async () => ({ ok: false }) });
  f.toggles[0].checked = true;
  await f.toggles[0].handlers.change();
  assert.equal(f.document.documentElement.dataset.theme, 'light');
  assert.equal(f.stored['hairship-theme-7'], 'light');
  assert.ok(f.statuses.every(s => s.textContent.includes('Could not save')));
  assert.ok(f.toggles.every(t => !t.disabled));
});

test('theme switches wait for saving to finish so rapid changes cannot race', async () => {
  let resolveSave;
  const f = fixture('light', false, { fetch: () => new Promise(resolve => { resolveSave = resolve; }) });
  f.toggles[0].checked = true;
  const saved = f.toggles[0].handlers.change();
  assert.ok(f.toggles.every(t => t.disabled));
  f.toggles[1].handlers.change();
  assert.equal(f.requests.length, 1);
  resolveSave({ ok: true, json: async () => ({ theme: 'dark' }) });
  await saved;
  assert.ok(f.toggles.every(t => t.checked && !t.disabled));
});
test('storage events synchronize only the current account across tabs', () => {
  const f = fixture();
  f.window.handlers.storage({ key: 'hairship-theme-8', newValue: 'dark' });
  assert.equal(f.document.documentElement.dataset.theme, 'light');
  f.window.handlers.storage({ key: 'hairship-theme-7', newValue: 'dark' });
  assert.equal(f.document.documentElement.dataset.theme, 'dark');
});
test('opening one menu closes the other, outside click and Escape dismiss', () => {
  const f = fixture();
  f.menus.forEach(m => { m.open = true; });
  f.menus[1].handlers.toggle();
  assert.equal(f.menus[0].open, false);
  f.document.handlers.click({ target: f.menus[1] });
  assert.equal(f.menus[1].open, true);
  f.document.handlers.keydown({ key: 'Escape' });
  assert.equal(f.menus[1].open, false);
  assert.equal(f.menus[1].focused, true);
  f.menus[0].open = true;
  f.document.handlers.click({ target: {} });
  assert.equal(f.menus[0].open, false);
});
