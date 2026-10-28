(function () {
  'use strict';
  var buttons = Array.from(document.querySelectorAll('[data-install-app]'));
  var statuses = Array.from(document.querySelectorAll('[data-install-status]'));
  var installPrompt = null;
  function installed() {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  }
  function showButtons(show) {
    buttons.forEach(function (button) { button.hidden = !show; });
  }
  function status(message) {
    statuses.forEach(function (item) { item.textContent = message; });
  }
  showButtons(false);
  window.addEventListener('beforeinstallprompt', function (event) {
    if (installed()) return;
    event.preventDefault();
    installPrompt = event;
    showButtons(true);
  });
  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      if (!installPrompt) return;
      var prompt = installPrompt;
      installPrompt = null;
      showButtons(false);
      status('');
      prompt.prompt().then(function () { return prompt.userChoice; }).then(function (choice) {
        if (choice.outcome === 'dismissed') status('You can install later from Chrome’s menu.');
      }).catch(function () { status('Open your browser menu and choose Install app or Add to Home screen.'); });
    });
  });
  window.addEventListener('appinstalled', function () {
    installPrompt = null;
    showButtons(false);
    status('');
  });
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/', updateViaCache: 'none' }).catch(function () {
        // A transient registration failure should not interrupt live service work.
      });
    });
  }
}());
