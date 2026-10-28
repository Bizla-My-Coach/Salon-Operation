(function () {
  'use strict';
  var root = document.querySelector('[data-live-page]');
  var status = document.querySelector('[data-live-status]');
  if (!root || !status || !root.querySelector('[data-live-region]')) return;
  var interval = Number(root.dataset.liveInterval) === 5000 ? 5000 : 30000;
  var timer, busy = false, stopped = false, submitting = false, failures = 0;
  var dirty = new Set();
  function markDirty(event) {
    var form = event.target.closest('form');
    if (form) dirty.add(form);
  }
  root.addEventListener('input', markDirty);
  root.addEventListener('change', markDirty);
  root.addEventListener('reset', function (event) {
    dirty.delete(event.target);
  });
  document.addEventListener('submit', function (event) {
    if (event.defaultPrevented) return;
    submitting = true;
    queueMicrotask(function () {
      if (event.defaultPrevented) { submitting = false; schedule(); }
    });
  });
  window.addEventListener('pageshow', function () { submitting = false; });
  function schedule() {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(refresh, Math.min(60000, interval * Math.pow(2, failures)));
  }
  function blocked(region) {
    return Array.from(dirty).some(function (form) { return region.contains(form); }) || region.contains(document.activeElement) ||
      region.querySelector('.errorlist, [aria-invalid="true"], details[open] form, .skip-panel:not([hidden])');
  }
  async function refresh() {
    if (busy || stopped || document.hidden || submitting) return;
    busy = true;
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 15000);
    try {
      var response = await fetch(window.location.href, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal
      });
      if (response.redirected || response.status === 401 || response.status === 403 || response.status === 404) {
        stopped = true;
        root.querySelectorAll('[data-live-region]').forEach(function (region) { region.replaceChildren(); });
        status.textContent = 'Access or page changed. Reload to continue.';
        return;
      }
      if (!response.ok) throw new Error('Refresh failed');
      var page = new DOMParser().parseFromString(await response.text(), 'text/html');
      var updated = page.querySelector('[data-live-page]');
      if (!updated || updated.dataset.livePage !== root.dataset.livePage) throw new Error('Unexpected page');
      var fresh = new Map();
      updated.querySelectorAll('[data-live-region]').forEach(function (region) { fresh.set(region.dataset.liveRegion, region); });
      var paused = false, changed = false;
      var scrollX = window.scrollX, scrollY = window.scrollY;
      if (!document.hidden && !submitting) {
        root.querySelectorAll('[data-live-region]').forEach(function (region) {
          var next = fresh.get(region.dataset.liveRegion);
          if (!next || blocked(region)) { paused = true; return; }
          // Restore disclosure state; stable keys keep visit/issue histories associated.
          var open = Array.from(region.querySelectorAll('details')).map(function (item, index) {
            return {key: item.dataset.liveKey || item.id || String(index), open: item.open};
          });
          next.querySelectorAll('details').forEach(function (item, index) {
            var saved = open.find(function (old) { return old.key === (item.dataset.liveKey || item.id || String(index)); });
            if (saved) item.open = saved.open;
          });
          // Scripts are loaded once by the page, never by background responses.
          next.querySelectorAll('script').forEach(function (script) { script.remove(); });
          if (region.innerHTML !== next.innerHTML) {
            region.replaceChildren.apply(region, Array.from(next.childNodes));
            changed = true;
          }
        });
      }
      if (changed) window.scrollTo({left: scrollX, top: scrollY, behavior: 'instant'});
      failures = 0;
      status.textContent = paused ? 'Live · updates to the section you are using are paused until you finish or reset.' :
        'Live · last updated ' + new Date().toLocaleTimeString();
    } catch (error) {
      failures += 1;
      status.textContent = 'Updates interrupted · retrying automatically. Displayed data may be out of date.';
    } finally {
      clearTimeout(timeout);
      busy = false;
      schedule();
    }
  }
  document.addEventListener('visibilitychange', function () {
    clearTimeout(timer);
    if (!document.hidden) return refresh();
  });
  window.addEventListener('online', refresh);
  schedule();
}());
