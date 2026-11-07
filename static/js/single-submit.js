(function () {
  'use strict';
  // Forms marked data-single-submit send once: further presses while the
  // server is still answering are ignored, so a slow connection cannot
  // create a second visit or a second copy of the same services.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('data-single-submit')) return;
    if (form.getAttribute('data-submitting') === 'true') { event.preventDefault(); return; }
    form.setAttribute('data-submitting', 'true');
    setTimeout(function () {
      // Another script (for example browser validation helpers) cancelled it.
      if (event.defaultPrevented) { form.removeAttribute('data-submitting'); return; }
      form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (button) {
        button.setAttribute('data-label', button.textContent);
        button.textContent = 'Saving…';
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
      });
    }, 0);
  });
  // Coming back with the browser Back button restores a usable form.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-submitting="true"]').forEach(function (form) {
      form.removeAttribute('data-submitting');
      form.querySelectorAll('button[data-label]').forEach(function (button) {
        button.textContent = button.getAttribute('data-label');
        button.removeAttribute('data-label');
        button.removeAttribute('aria-busy');
        button.disabled = false;
      });
    });
  });
}());
