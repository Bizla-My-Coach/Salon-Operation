// Person tabs on the simple visit pages: the customer plus family / friends.
(function () {
  'use strict';
  var form = document.querySelector('[data-people]');
  if (!form) return;
  var tabs = form.querySelector('.sv-people-tabs');
  var addButton = form.querySelector('[data-add-person]');
  var template = form.querySelector('[data-panel-template]');
  var next = Number(form.dataset.nextGuest) || 1;
  var serial = 0;
  function changed() { form.dispatchEvent(new CustomEvent('people:change', {bubbles: true})); }
  function show(key) {
    form.querySelectorAll('[data-tab]').forEach(function (tab) {
      var on = tab.dataset.tab === key;
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    form.querySelectorAll('[data-panel]').forEach(function (panel) { panel.hidden = panel.dataset.panel !== key; });
  }
  function tabFor(key) { return form.querySelector('[data-tab="' + key + '"]'); }
  tabs.addEventListener('click', function (event) {
    var tab = event.target.closest('[data-tab]');
    if (tab) show(tab.dataset.tab);
  });
  if (addButton && template) {
    addButton.addEventListener('click', function () {
      do { serial += 1; } while (form.querySelector('[data-panel="n' + serial + '"]'));
      var key = 'n' + serial;
      var label = 'Guest ' + next;
      next += 1;
      var holder = document.createElement('div');
      holder.innerHTML = template.innerHTML.split('__KEY__').join(key);
      var panel = holder.firstElementChild;
      panel.dataset.defaultName = label;
      var nameInput = panel.querySelector('[data-person-name]');
      if (nameInput) nameInput.placeholder = label + ' (or type a name)';
      template.parentNode.insertBefore(panel, template);
      var tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'sv-person-tab';
      tab.setAttribute('role', 'tab');
      tab.dataset.tab = key;
      tab.innerHTML = '<span data-tab-name></span> <em data-tab-count>0</em>';
      tab.querySelector('[data-tab-name]').textContent = label;
      tabs.insertBefore(tab, addButton);
      show(key);
      if (nameInput) nameInput.focus();
      changed();
    });
  }
  form.addEventListener('input', function (event) {
    if (!event.target.matches('[data-person-name]')) return;
    var panel = event.target.closest('[data-panel]');
    var name = tabFor(panel.dataset.panel).querySelector('[data-tab-name]');
    name.textContent = event.target.value.trim() || panel.dataset.defaultName || event.target.placeholder;
  });
  form.addEventListener('click', function (event) {
    var button = event.target.closest('[data-remove-person]');
    if (!button) return;
    var panel = button.closest('[data-panel]');
    var key = panel.dataset.panel;
    if (key.charAt(0) === 'p') {
      var gone = document.createElement('input');
      gone.type = 'hidden'; gone.name = 'remove'; gone.value = key;
      form.appendChild(gone);
    }
    panel.remove();
    tabFor(key).remove();
    var first = form.querySelector('[data-tab]');
    if (first) show(first.dataset.tab);
    changed();
  });
  form.querySelectorAll('[data-panel]').forEach(function (panel) {
    var input = panel.querySelector('[data-person-name]');
    if (input && !panel.dataset.defaultName) panel.dataset.defaultName = input.placeholder;
  });
  var error = form.querySelector('[data-panel] .notice.error, [data-panel] [aria-invalid="true"]');
  if (error) show(error.closest('[data-panel]').dataset.panel);
})();
