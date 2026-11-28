(function () {
  'use strict';
  var form = document.querySelector('[data-record-form]');
  if (!form) return;
  var submit = form.querySelector('[data-submit]');
  function sync() {
    var total = 0;
    form.querySelectorAll('[data-panel]').forEach(function (panel) {
      if (panel.closest('template')) return;
      var picked = 0;
      panel.querySelectorAll('[data-section-toggle]').forEach(function (box) {
        var section = panel.querySelector('[data-section="' + box.value + '"]');
        if (section) section.hidden = !box.checked;
        if (box.checked) picked += 1;
      });
      total += picked;
      var count = form.querySelector('[data-tab="' + panel.dataset.panel + '"] [data-tab-count]');
      if (count) count.textContent = picked ? picked : '';
    });
    if (submit) submit.textContent = total ? 'Submit ' + total + ' type' + (total > 1 ? 's' : '') : 'Submit';
  }
  form.addEventListener('change', function (event) {
    if (event.target.matches('[data-section-toggle]')) sync();
  });
  form.addEventListener('people:change', sync);
  sync();
})();
