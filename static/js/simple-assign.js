(function () {
  'use strict';
  var form = document.querySelector('[data-assign-form]');
  if (!form) return;
  var send = form.querySelector('[data-send]');
  function sync() {
    var count = form.querySelectorAll('[data-staff]:checked').length;
    if (send) send.textContent = count ? 'Send to ' + count + ' staff' : 'Send to staff';
  }
  form.addEventListener('click', function (event) {
    var radio = event.target.closest('.sv-tile-cats input[type=radio]');
    if (!radio) return;
    var tile = radio.closest('[data-staff-tile]');
    var box = tile.querySelector('[data-staff]');
    // Tapping the chosen category again clears it back to "staff choose".
    if (radio.dataset.wasChecked === '1') {
      tile.querySelector('[data-any]').checked = true;
    } else {
      box.checked = true;
    }
    tile.querySelectorAll('.sv-tile-cats input').forEach(function (r) { r.dataset.wasChecked = r.checked ? '1' : ''; });
    sync();
  });
  form.addEventListener('change', function (event) {
    if (event.target.matches('[data-staff]') && !event.target.checked) {
      var tile = event.target.closest('[data-staff-tile]');
      var any = tile.querySelector('[data-any]');
      if (any) any.checked = true;
      tile.querySelectorAll('.sv-tile-cats input').forEach(function (r) { r.dataset.wasChecked = r.checked ? '1' : ''; });
    }
    sync();
  });
  form.querySelectorAll('.sv-tile-cats input').forEach(function (r) { r.dataset.wasChecked = r.checked ? '1' : ''; });
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
