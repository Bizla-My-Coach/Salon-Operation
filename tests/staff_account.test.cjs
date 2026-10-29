const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('static/js/staff-account.js', 'utf8');

function copyPage(clipboard) {
  const username = {value: 'hs01.alex.smith'};
  const password = {value: 'Example8', focus() { this.focused = true; }, select() { this.selected = true; }};
  const status = {textContent: ''};
  const buttons = ['created-username', 'created-password'].map(id => ({
    dataset: {copyField: id}, addEventListener(event, handler) { this.click = handler; },
  }));
  const fields = {'created-username': username, 'created-password': password};
  vm.runInNewContext(source, {
    navigator: {clipboard},
    document: {
      querySelector: selector => selector === '[data-copy-status]' ? status : null,
      querySelectorAll: () => buttons,
      getElementById: id => fields[id],
    },
  });
  return {buttons, username, password, status};
}

test('both credential buttons copy the corresponding value and confirm success', async () => {
  const copied = [];
  const page = copyPage({writeText: async text => copied.push(text)});
  await page.buttons[0].click();
  assert.equal(page.status.textContent, 'Username copied.');
  await page.buttons[1].click();
  assert.equal(page.status.textContent, 'Password copied.');
  assert.deepEqual(copied, [page.username.value, page.password.value]);
});

test('clipboard failure selects the credentials for manual copying', async () => {
  const page = copyPage(undefined);
  await page.buttons[1].click();
  assert.equal(page.password.focused, true);
  assert.equal(page.password.selected, true);
  assert.match(page.status.textContent, /highlighted text/);
});

test('changing names discards an older username preview response', async () => {
  const fields = Object.fromEntries(['branch', 'first_name', 'last_name'].map(name => [name, {
    value: name === 'branch' ? '1' : '', addEventListener(event, handler) { this.input = handler; },
  }]));
  const username = {value: ''};
  const requests = [];
  let timer;
  vm.runInNewContext(source, {
    URL, window: {location: {origin: 'https://salon.example'}},
    setTimeout: handler => { timer = handler; }, clearTimeout() {},
    fetch: url => new Promise(resolve => requests.push({url, resolve})),
    document: {
      querySelector: () => ({dataset: {previewUrl: '/admin-panel/users/add/'}, elements: fields, querySelector: () => username}),
      querySelectorAll: () => [],
    },
  });
  fields.first_name.value = 'Alex';
  const old = timer();
  fields.first_name.value = 'Sam';
  fields.first_name.input();
  const current = timer();
  requests[1].resolve({ok: true, json: async () => ({username: 'hs01.sam'})});
  await current;
  requests[0].resolve({ok: true, json: async () => ({username: 'hs01.alex'})});
  await old;
  assert.equal(username.value, 'hs01.sam');
  assert.equal(requests[1].url.searchParams.get('first_name'), 'Sam');
});
