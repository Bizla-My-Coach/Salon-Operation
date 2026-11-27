(function () {
  'use strict';
  var form = document.querySelector('[data-assign-form]');
  if (!form) return;
  var none = form.querySelector('[data-none]');
  var send = form.querySelector('[data-send]');
  function sync() {
    var count = 0;
    form.querySelectorAll('[data-staff]').forEach(function (box) {
      var row = form.querySelector('[data-row="' + box.dataset.staff + '"]');
      if (row) row.hidden = !box.checked;
      if (box.checked) count++;
    });
    if (none) none.hidden = count > 0;
    if (send) send.textContent = count ? 'Send to ' + count + ' staff' : 'Send to staff';
  }
  form.addEventListener('change', function (event) { if (event.target.matches('[data-staff]')) sync(); });
  var search = form.querySelector('[data-staff-search]');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      form.querySelectorAll('[data-staff-tile]').forEach(function (tile) {
        tile.hidden = q && tile.dataset.name.indexOf(q) === -1 && !tile.querySelector('input').checked;
      });
    });
  }
  sync();
})();
