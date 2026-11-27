(function () {
  'use strict';
  var form = document.querySelector('[data-assign-form]');
  if (!form) return;
  var send = form.querySelector('[data-send]');
  function sync() {
    var count = form.querySelectorAll('[data-staff]:checked').length;
    if (send) send.textContent = count ? 'Send to ' + count + ' staff' : 'Send to staff';
  }
  form.addEventListener('change', function (event) {
    var input = event.target;
    var tile = input.closest('[data-staff-tile]');
    if (!tile) return;
    var box = tile.querySelector('[data-staff]');
    if (input.matches('.sv-tile-cats input') && input.checked) {
      box.checked = true;  // a category tap also selects the person
    } else if (input === box && !box.checked) {
      tile.querySelectorAll('.sv-tile-cats input:not(:disabled)').forEach(function (c) { c.checked = false; });
    }
    sync();
  });
  var search = form.querySelector('[data-staff-search]');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      form.querySelectorAll('[data-staff-tile]').forEach(function (tile) {
        tile.hidden = q && tile.dataset.name.indexOf(q) === -1 && !tile.querySelector('[data-staff]').checked;
      });
    });
  }
  sync();
})();
