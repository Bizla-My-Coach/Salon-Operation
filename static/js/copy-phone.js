(function () {
  'use strict';
  // Delegated so buttons keep working after the live dashboard refreshes the list.
  function fallbackCopy(text) {
    var box = document.createElement('textarea');
    box.value = text; box.setAttribute('readonly', ''); box.style.position = 'fixed'; box.style.opacity = '0';
    document.body.appendChild(box); box.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(box);
    return ok;
  }
  function done(button, ok) {
    var label = button.querySelector('[data-copy-label]') || button;
    var original = button.dataset.copyOriginal || label.textContent;
    button.dataset.copyOriginal = original;
    label.textContent = ok ? 'Copied' : 'Copy failed';
    button.classList.toggle('copied', ok);
    clearTimeout(button._copyTimer);
    button._copyTimer = setTimeout(function () { label.textContent = original; button.classList.remove('copied'); }, 1800);
  }
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    event.preventDefault();
    var text = button.dataset.copy;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(button, true); }, function () { done(button, fallbackCopy(text)); });
    } else {
      done(button, fallbackCopy(text));
    }
  });
})();
