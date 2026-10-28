(function () {
  'use strict';
  var menus = Array.from(document.querySelectorAll('.account-menu'));
  var toggles = Array.from(document.querySelectorAll('[data-theme-toggle]'));
  var themeForm = document.getElementById('theme-preference-form');
  var themeStatuses = Array.from(document.querySelectorAll('[data-theme-save-status]'));
  var savingTheme = false;
  function syncTheme(theme) {
    document.documentElement.dataset.theme = theme;
    toggles.forEach(function (toggle) { toggle.checked = theme === 'dark'; });
  }
  syncTheme(document.documentElement.dataset.theme || 'light');
  function cacheTheme(theme) {
    try { localStorage.setItem(document.body.dataset.themeKey, theme); } catch (error) { /* Account preferences do not require browser storage. */ }
  }
  function saveTheme(theme, onlyIfUnset) {
    if (savingTheme) return Promise.resolve();
    var previousTheme = document.documentElement.dataset.theme;
    syncTheme(theme);
    cacheTheme(theme);
    if (!themeForm) return Promise.resolve();
    savingTheme = true;
    toggles.forEach(function (toggle) { toggle.disabled = true; });
    themeStatuses.forEach(function (status) { status.textContent = ''; });
    return fetch(themeForm.action, {
      method: 'POST', credentials: 'same-origin', mode: 'same-origin', redirect: 'error', keepalive: true,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': themeForm.elements.csrfmiddlewaretoken.value },
      body: 'theme=' + encodeURIComponent(theme) + (onlyIfUnset ? '&only_if_unset=1' : ''),
    }).then(function (response) {
      if (!response.ok) throw new Error('Theme save failed');
      return response.json();
    }).then(function (data) {
      if (data.theme !== 'light' && data.theme !== 'dark') throw new Error('Invalid theme');
      syncTheme(data.theme);
      cacheTheme(data.theme);
    }).catch(function () {
      syncTheme(previousTheme);
      cacheTheme(previousTheme);
      themeStatuses.forEach(function (status) { status.textContent = 'Could not save your theme. Please try again.'; });
    }).then(function () {
      savingTheme = false;
      toggles.forEach(function (toggle) { toggle.disabled = false; });
    });
  }
  toggles.forEach(function (toggle) {
    toggle.addEventListener('change', function () {
      var theme = toggle.checked ? 'dark' : 'light';
      return saveTheme(theme, false);
    });
  });
  if (document.documentElement.dataset.themeNeedsSave === 'true' && themeForm) {
    saveTheme(document.documentElement.dataset.theme, true);
  }
  window.addEventListener('storage', function (event) {
    if (event.key === document.body.dataset.themeKey && (event.newValue === 'dark' || event.newValue === 'light')) {
      syncTheme(event.newValue);
    }
  });
  menus.forEach(function (menu) {
    menu.addEventListener('toggle', function () {
      if (menu.open) menus.forEach(function (other) { if (other !== menu) other.open = false; });
    });
  });
  document.addEventListener('click', function (event) {
    menus.forEach(function (menu) { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') menus.forEach(function (menu) {
      if (menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
    });
  });
}());
